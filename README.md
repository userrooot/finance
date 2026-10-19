# Crypto Arbitrage Opportunity Scanner

A Python tool that scans public market data on Binance, Bybit and OKX for crypto arbitrage
opportunities, prices each one net of fees, slippage, transfer costs and tax, and writes the
results to JSONL files and a daily markdown report.

## 1. What it is

**Phase 1 is a read-only scanner.** It uses public market data only (order books, tickers,
funding rates, OHLCV). It needs no API keys and places no orders.

- `live_trading: false` is set in `config/settings.yaml`.
- `python -m arb` refuses to run if `live_trading` is `true`, because no execution engine exists yet.
- The `.env.example` keys are not read by any code in this phase.

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

## 5. Output

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

## 6. Configuration

All settings live in `config/settings.yaml`.

- `live_trading`: must stay `false` in Phase 1.
- `capital.total_usd` (1100) and `capital.max_per_trade_usd` (300): sizing inputs for
  capacity and notional. The starting figure is about INR 1 lakh.
- `risk.max_daily_loss_pct` (2.0) and `risk.max_total_loss_pct` (10.0): loaded from config
  but not enforced in Phase 1, since there is no execution.
- `tax.rate_on_gains`: see section 3.
- `exchanges.<name>.fees`: **update these to your actual VIP tier and any BNB or
  fee-token discounts.** The defaults are VIP-0 and overstate costs for active traders.
- `withdrawal_fees`: approximate per-asset transfer costs in units of the asset. Check current
  network fees.
- `universe.base_assets`: the symbols scanned on every exchange. Perp and future symbols are
  derived from these.
- `scanners.<name>`: `enabled` flags and thresholds, including `min_net_bps` (cross-exchange,
  triangular), `min_annualized_net_pct` (funding, basis), `holding_days` (funding), and
  `timeframe`, `lookback_bars`, `max_coint_pvalue` and `entry_z` (stat_arb).
- `storage.dir`: where `data/` is written, relative to the working directory.

## 7. Tests

```bash
python -m pytest -q
```

## 8. Roadmap

- **Phase 2:** a backtester and paper trading, running on the same code path as the scanner.
- **Phase 3:** implement the two or three best edges that the reports show persisting.
- **Phase 4:** validation and a go-live checklist. Live trading stays off until a strategy
  passes backtest, walk-forward testing and paper trading.

Risk limits (2% daily loss, 10% total loss) are placeholders in config until execution exists.

## 9. Disclaimer

This software is for research and education. Nothing here guarantees a profit. Use only
legal strategies that are permitted in your jurisdiction. Nothing in this repository is
financial, tax or legal advice.
