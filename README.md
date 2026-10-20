# Crypto Arbitrage Opportunity Scanner

A Python tool that scans public market data on Binance, Bybit and OKX for crypto arbitrage
opportunities, prices each one net of fees, slippage, transfer costs and tax, and writes the
results to JSONL files and a daily markdown report. Phase 2 adds a backtester and a paper
trader that share one trading engine.

## 1. What it is

**Phase 1 is a read-only scanner.** It uses public market data only (order books, tickers,
funding rates, OHLCV). It needs no API keys and places no orders.

**Phase 2 is a backtester and paper trader.** Backtests use downloaded history; paper trading
uses live public data with simulated fills. Neither sends orders to an exchange.

**Live trading does not exist yet.** `live_trading: false` must stay false. `python -m arb`
and the paper trader both refuse to run when it is `true`.

The `.env.example` exchange keys are not read by any code. The only environment variables
read are the optional `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Scope: exchanges are `binance`, `bybit` and `okx`. The universe is 12 base assets against USDT
(BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, LINK, TON, SUI, LTC).

## 2. Strategies scanned

| Strategy | What it is | Main catch |
|---|---|---|
| `cross_exchange` | Buy spot on the exchange with the lower ask, sell on the one with the higher bid. | Needs pre-funded inventory on both venues and a rebalancing transfer that costs fees and time. |
| `triangular` | Cycle Q -> A -> B -> Q across spot pairs on one exchange (Binance only in config). | Dominated by HFT firms; edges are small and gone within milliseconds. |
| `funding_carry` | Long spot, short perp (or the reverse) on one exchange to collect funding, held for `holding_days`. | Funding can flip sign before the holding period ends; the spot/perp basis can move. |
| `funding_spread` | Long perp on one exchange, short perp on another, to collect the funding differential. | Margin on two venues; either leg can be liquidated on a spike; transfer risk. |
| `basis` | Cash-and-carry on dated futures: long spot, short a future held to expiry. Contango only. | Mark-to-market margin on the short future before expiry; capital is locked until expiry. |
| `stat_arb` | Cointegrated spot pairs (Engle-Granger), evaluated as linear perps when the spread is at least `entry_z` standard deviations from its mean. | Statistical, not risk-free. The spread can keep diverging, and cointegration can break. |

Each strategy's `holding` field states its assumed holding period (`instant`, `7d`,
`until z-score reverts`, and so on). Each opportunity also carries `risk_notes`.

## 3. Cost model

Every reported number is **net**: fees, slippage, transfer costs and tax are all subtracted.

- **Taker fees** per exchange and market type come from `exchanges.*.fees` in `settings.yaml`.
  Defaults are VIP-0 tiers, for example Binance spot 0.10% and Binance swap 0.05% taker.
- **Slippage** is order-book VWAP against top of book. Books are fetched at depth 20, and fills
  that cannot complete are not counted as full opportunities.
- **Withdrawal and rebalance fees** are per asset in `withdrawal_fees` (cheapest common network,
  approximate). Assets not listed are treated as impossible to move, so cross-exchange
  opportunities in them are not reported.
- **Tax** is `tax.rate_on_gains` (31.2%: 30% plus 4% cess) applied to each opportunity's
  positive net P&L. Losses are not offset against gains, as the config comments note. This is
  an approximation. **Verify the treatment with a chartered accountant before relying on it.**
- `net_after_tax_usd` is the after-tax figure; `net_bps` is the pre-tax net edge in basis points.

## 4. Quick start

Requirements: Python 3.11 or newer (the Docker image uses 3.12).

```bash
pip install -r requirements.txt

# one scan cycle, then exit
python -m arb scan --once

# continuous scanning, one cycle every 60 seconds
python -m arb scan --interval 60

# daily report for a UTC date (default: today)
python -m arb report
python -m arb report --day 2026-10-18
```

`--config` overrides the settings file path. `stat_arb` runs at most once an hour; the other
scanners run every cycle.

### Docker

```bash
docker build -t arb-scanner .

# run the scanner, keeping data and reports on the host
docker run -d --name arb-scanner \
  -v "$PWD/data:/app/data" \
  -v "$PWD/reports:/app/reports" \
  arb-scanner

# generate a report from the stored data
docker run --rm \
  -v "$PWD/data:/app/data" \
  -v "$PWD/reports:/app/reports" \
  arb-scanner python -m arb report
```

The image's default command is `scan --interval 60`. There is no `.dockerignore`, so the build
context includes everything in the repo, including any local `.env`. Keep secrets out of the
directory before building.

### Where to run it

Run the scanner on a VPS close to the exchanges, such as Tokyo or Singapore, so that data
latency is low and the books you see are reasonably current. Run it for at least 1-2 weeks
before choosing any strategy. Short samples mostly show noise, and the reports depend on
how many scan cycles were observed.

## 5. Phase 2: backtest and paper trading

### One engine for both

Backtests and paper trading both run `TradingEngine` (`arb/core/engine.py`): funding, loss
limits, strategy intents (or flatten-all when killed), risk checks, execution, equity.

- **Backtest** (`arb/backtest/engine.py`): decisions are made on each bar's close and filled at
  the **next bar's open**, with `backtest.spread_bps` and `backtest.slippage_bps` modelled. A
  decision never sees its own fill price.
- **Paper** (`arb/paper/runner.py`): decisions use the live snapshot. Fills are simulated against
  the order-book VWAP (depth `paper.book_depth`, 20), partial when the book is thin, with taker
  fees from the cost model.

### Risk rules that are enforced

Limits come from `capital` and `risk` in `settings.yaml` and are checked in `arb/core/risk.py`:

- **Per-leg cap:** each opening leg's notional must be at most `capital.max_per_trade_usd` (300),
  with 1% rounding slack. Closing orders are exempt.
- **Gross exposure cap:** total position notional must stay within
  `capital.total_usd` x `risk.max_gross_exposure_x` (2.0).
- **No spot shorting:** a spot sell cannot exceed the spot quantity held.
- **Stale quotes rejected:** entries using a quote older than `risk.max_quote_age_s` (15 s) are rejected.
- **Daily loss, 2%:** equity down 2% from the UTC day's start halts new entries until the next
  UTC day. Closing trades are still allowed.
- **Total loss, 10%:** equity down 10% from starting capital triggers the kill switch: every
  position is flattened and new entries are blocked until a human resets the saved risk state.
- **All-or-nothing groups:** a hedge is checked as a whole; if one leg is rejected, all are.

**Leg-failure unwinding:** if one leg of a hedge under-fills, the excess on the other legs is
closed with reduce-only orders so the group stays hedged. The event is logged.

### Strategies

- **`funding_carry`** (`arb/strategies/funding_carry.py`): long spot, short perp on binance for
  `bases` (BTC, ETH, SOL), `notional_usd` 300 per leg. Enters when the average of the last
  `avg_window` (21) realised settlements, net of costs spread over `max_hold_days` (30), is at
  least `entry_annualized_pct` (15%); exits below `exit_annualized_pct` (3%) or after max hold.
- **`stat_arb`** (`arb/strategies/stat_arb.py`): pairs trade on linear perps, trading the z-score
  of log(y) - beta*log(x), with beta re-estimated by OLS over `lookback_bars` (720 hourly bars).
  Enters at `entry_z` (2.0) <= |z| < `stop_z` (4.0); exits at `exit_z` (0.5), at `stop_z`, or
  after `max_hold_bars` (240). `pairs` (`[[ETH, BTC]]`) is chosen by hand from scanner and
  walk-forward results; `notional_usd` is 300 across both legs.

**Tax:** `tax.rate_on_gains` (31.2%) applies to the sum of **positive** closed-trade P&L only;
losses are not offset. This is an estimate. Check it with a CA.

### Workflow, in order

```bash
# 1. download history (OHLCV + funding) into data/history/
python -m arb fetch-history --strategy funding_carry --days 365

# 2. in-sample backtest with fixed parameters; --days N limits it to the last N days
python -m arb backtest --strategy funding_carry [--days N]

# 3. walk-forward: pick parameters on each train window, score the next test window
python -m arb walk-forward --strategy funding_carry --train-days 60 --test-days 30

# 4. paper trade on live public data; --cycles N stops after N cycles (default: forever)
python -m arb paper --strategy funding_carry [--cycles N]

# 5. print a summary of the paper run to stdout
python -m arb paper-report --strategy funding_carry
```

`--strategy` is `funding_carry` or `stat_arb`. Walk-forward searches a small grid per strategy:
6 combinations for `funding_carry` and 6 for `stat_arb`.

### Outputs

- `data/history/<exchange>/<SYMBOL>_<timeframe>.parquet` and `<SYMBOL>_funding.parquet`: the
  history cache written by `fetch-history`.
- `reports/backtest_<strategy>.md` and `reports/backtest_<strategy>.equity.csv`: from `backtest`.
  The report is in-sample.
- `reports/walk_forward_<strategy>.md` (plus an out-of-sample equity CSV when there is one): from
  `walk-forward`.
- `data/paper/<strategy>/`: `state.json` (portfolio, risk, strategy and funding state),
  `fills.jsonl`, `equity.jsonl` and `events.jsonl` (risk events, rejections, skipped cycles).

**Paper resumes after a restart** from `state.json`, so positions and risk state carry over.
**Telegram alerts are optional**: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Without
them, notifications go to the log.

## 6. How to judge results

- **Trust walk-forward out-of-sample numbers over a single backtest.** A backtest uses
  parameters chosen with hindsight; walk-forward scores only the test windows.
- **Short windows make annualised Sharpe noisy.** A few weeks of data can produce large
  Sharpe values by chance.
- **Paper trade for at least 2-4 weeks and compare with the backtest** before any live money.
  Large gaps mean the model is wrong somewhere.
- **Cross-exchange, triangular and basis are not backtestable** (no historical order books,
  no expired futures). They remain scanner-only.

## 7. Output (scanner)

- `data/opportunities/YYYY-MM-DD.jsonl`: one record per opportunity that passed the cost
  filters (strategy, key, legs, notional, costs breakdown, net and after-tax P&L, capacity,
  holding, risk notes).
- `data/scans/YYYY-MM-DD.jsonl`: one record per scan cycle (per-scanner counts, errors, duration).
- `reports/YYYY-MM-DD.md`: the daily report, written by `python -m arb report`. It summarises
  each strategy and lists the top keys.

**Read the report's error warning.** If any scanner or market-data call failed, the report shows
a warning with the error count and a sample. Missing data is not the same as no edge. A report
with no opportunities and no errors is a valid "no edge found" result. With errors, the result
only covers what was fetched.

## 8. Configuration

All settings live in `config/settings.yaml`.

- `live_trading`: must stay `false`. No live execution engine exists.
- `capital.total_usd` (1100) and `capital.max_per_trade_usd` (300): sizing inputs for
  capacity and notional, and the per-leg cap in Phase 2. The starting figure is about INR 1 lakh.
- `risk.max_daily_loss_pct` (2.0) and `risk.max_total_loss_pct` (10.0): enforced by the paper
  and backtest engines (see section 5). `risk.max_gross_exposure_x` (2.0) and `risk.max_quote_age_s`
  (15 s) set the exposure cap and the stale-price limit.
- `tax.rate_on_gains`: see section 3.
- `exchanges.<name>.fees`: **update these to your actual VIP tier and any BNB or
  fee-token discounts.** The defaults are VIP-0 and overstate costs for active traders.
- `withdrawal_fees`: approximate per-asset transfer costs in units of the asset. Check current
  network fees.
- `universe.base_assets`: the symbols scanned on every exchange.
- `scanners.<name>`: `enabled` flags and thresholds, including `min_net_bps` (cross-exchange,
  triangular), `min_annualized_net_pct` (funding, basis), `holding_days` (funding), and
  `timeframe`, `lookback_bars`, `max_coint_pvalue` and `entry_z` (stat_arb).
- `strategies.*`, `backtest` (`timeframe`, `spread_bps`, `slippage_bps`) and `paper`
  (`interval_s`, `state_dir`, `book_depth`): the Phase 2 parameters described in section 5.
- `storage.dir`: where `data/` and history are written, relative to the working directory.

## 9. Tests

```bash
python -m pytest -q
```

## 10. Roadmap

- **Phase 1 (done):** read-only scanner over six strategies.
- **Phase 2 (done):** backtester and paper trader on one shared engine, with the risk rules
  in section 5.
- **Phase 3:** pick the two or three best edges, using real scanner reports, walk-forward
  results and paper trading results.
- **Phase 4:** validation, chaos tests, live execution behind the `live_trading` flag, and a
  go-live checklist. Live trading stays off
  until a strategy passes backtest, walk-forward testing and paper trading.

## 11. Disclaimer

This software is for research and education. Nothing here guarantees a profit. Use only
legal strategies that are permitted in your jurisdiction. Nothing in this repository is
financial, tax or legal advice.
