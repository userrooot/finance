# Going live: funding carry checklist

This checklist covers the `funding_carry` strategy on Binance. It moves real money.
Read the whole page before you start. Work through the steps in order.

Nothing here is financial, tax or legal advice. Verify tax with a chartered accountant.

## Read this first: what the bot does and does not do

- **Loss limits use the money you funded.** Set `live.capital_usd` in `config/settings.yaml` to
  the USDT you put on the exchange (spot + futures). Live refuses to start without it. The 2%
  daily halt, the 10% kill switch and the gross exposure cap are measured against it.
- **Reconcile checks coins and perp positions, not USDT cash.** Fees and funding move your USDT;
  compare `/status` equity with the exchange now and then.
- **`live` needs a recent PASS from `validate`.** It refuses to start unless
  `reports/validate_funding_carry.md` says PASS and is under 7 days old. `--skip-gate` overrides
  this; do not use it for real money.
- **`/resume` does not clear a kill.** After you know why the kill switch fired, stop the bot and run
  `python -m arb risk-reset --strategy funding_carry --live --yes-really`. The total-loss limit
  then counts from your current equity.
- **`.env` is loaded automatically** from the folder you run in (variables already set in the
  shell win).
- **The bot does not set leverage or margin mode.** Set them on the exchange yourself
  (cross or isolated margin, low leverage such as 2x).
- **Stopping the bot does not close positions.** Use `/kill` while it runs, or `flatten-all`.

- [ ] I have read the list above.

## 1. Paper trade first, then run the gate

Paper trading uses live public data and simulated fills. It places no orders.

- [ ] Paper trade `funding_carry` for at least 14 days.
  ```
  python -m arb paper --strategy funding_carry
  python -m arb paper-report --strategy funding_carry
  ```
- [ ] Run the gate:
  ```
  python -m arb validate --strategy funding_carry
  ```
  It writes `reports/validate_funding_carry.md` and exits with 1 on FAIL.
- [ ] The gate checks these things. All required checks must pass:
  - at least `live.gate.min_paper_days` (14) days of paper trading
  - no kill-switch event in the paper run
  - after-tax paper PnL at or above `live.gate.min_after_tax_pnl_usd` (0)
  - paper and backtest PnL within `live.gate.max_pnl_gap_pct` (30%) of each other
  - the backtest replay ran
- [ ] The trade count and skipped-cycle checks are for information only.
- [ ] The report ends with `GATE: PASS`. If it says `GATE: FAIL`, stay on paper.

## 2. Check the VPS region

Binance blocks some regions. Check from the VPS itself, not from your laptop.

- [ ] Run these. Each should print `{}` and the HTTP code should be 200, not 451 or 403:
  ```
  curl -s https://api.binance.com/api/v3/ping
  curl -s https://fapi.binance.com/fapi/v1/ping
  curl -s -o /dev/null -w "%{http_code}\n" https://api.binance.com/api/v3/ping
  ```
- [ ] If you use OKX, check its public API from the VPS the same way. Use the ping URL from
  the OKX API docs.

## 3. Create the API keys

- [ ] Create a trade-only key. Turn off withdrawals.
- [ ] Restrict the key to the VPS IP address.
- [ ] Turn on spot trading and USDⓈ-M futures for the key. The bot does not check these.
- [ ] Put the keys in `/opt/arb/.env` (or `.env` in the repo for a manual run). Use the names from
  `.env.example`:
  - Binance: `BINANCE_API_KEY`, `BINANCE_API_SECRET`
  - OKX: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSWORD`
- [ ] Restrict the file: `sudo chmod 600 /opt/arb/.env`.
- [ ] Never commit `.env`. It is in `.gitignore`. Check with `git status` before every push.

What the bot checks when it starts:

- Binance: the bot refuses the key if withdrawals are enabled. It also refuses if it cannot
  check the key at all.
- OKX: the bot refuses the key if it has withdraw permission, or if it has no trade permission.

## 4. Fund the wallets

The bot does not move money between wallets. You do that.

- [ ] Put USDT in the **spot** wallet for the spot legs.
- [ ] Put USDT in the **USDⓈ-M futures** wallet for perp margin.
- [ ] The $50 stage is 3 bases x 2 legs x $50 = $300 of position notional
  (BTC, ETH and SOL). Add a buffer on top for perp margin and fees.
- [ ] Set leverage and margin mode on the exchange yourself.

## 5. Stage 1: $50 per order

- [ ] Stop the paper service first. `live_trading: true` makes the paper trader refuse to start,
  and the live and paper traders must not share keys.
- [ ] Change `config/settings.yaml`:
  ```yaml
  live_trading: true
  live:
    capital_usd: 400        # the USDT you actually funded (spot + futures)
    max_order_usd: 50
  strategies:
    funding_carry:
      notional_usd: 50
  ```
- [ ] Add this line to `.env`:
  ```
  ARB_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY
  ```
- [ ] Start it by hand from the repo folder (it reads `.env` itself):
  ```
  python -m arb live --strategy funding_carry
  ```
  Or use the systemd unit in `deploy/README.md` (`arb-live.service`).
- [ ] Check the first lines of the log. Look for `RECONCILE MISMATCH`. If you see it, the bot
  has paused new entries. Find out why before you resume.
- [ ] If the gate refuses to start, the error names the failed check. The `arb-live.service`
  unit restarts every 10 seconds, so run `sudo systemctl stop arb-live` if the gate keeps
  refusing.

## 6. First-day drills

Telegram commands only work when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. The bot
only accepts commands from that chat.

- [ ] `/status`: the bot replies with its state, equity, open groups and resting orders.
- [ ] `/pause`: no new entries. Open positions are still managed. Then `/resume`.
- [ ] `/kill` drill, on the $50 stage only:
  - Send `/kill`. The bot cancels all orders and closes all positions.
  - Check the exchange. There should be no open perp positions and no open orders for the
    bot's symbols. The bot's spot coins should be sold.
  - The kill stays in force after `/resume`. Plan how you will restart before you run this drill.
- [ ] Manual emergency exit. Know this command before you need it:
  ```
  python -m arb flatten-all --exchange binance --strategy funding_carry --yes-really
  ```
  - It sends reduce-only market orders. It closes every perp position on that exchange, whatever
    opened it.
  - It sells only the spot the bot's own state says it holds. Other coins are left alone.
  - It needs the exchange keys in your environment. It does not need `ARB_LIVE_CONFIRM`.
  - It does not stop the live service. Stop `arb-live` first, as the command output says.
  - Check the exchange afterwards.

## 7. What the bot does on its own

These are the automatic rules in the code. Check the numbers in `config/settings.yaml`.

- [ ] **Reconcile every 5 minutes** (`live.reconcile_every_s: 300`), and once at startup.
  It compares the bot's spot coins and perp positions with the exchange, with a 2% tolerance.
  On a mismatch it pauses new entries and sends an alert. It never trades to fix a mismatch.
- [ ] **Liquidation guard at 25%** (`live.min_liq_distance_pct: 25`). If a perp position's
  liquidation price is within 25% of the mark price, the bot closes the group on that perp.
- [ ] **Daily halt at 2%** (`risk.max_daily_loss_pct`). New entries stop until the next UTC day.
  Closing trades are still allowed. Measured against `live.capital_usd`.
- [ ] **Kill switch at 10%** (`risk.max_total_loss_pct`). The bot closes everything and blocks
  new entries until a person runs `risk-reset` (see the list at the top).
- [ ] **Order cap** (`live.max_order_usd`). Any order above the cap is refused before it reaches
  the exchange. The refusal shows up as a Telegram alert.
- [ ] **Exit fallback.** Unfilled maker exits switch to taker orders after 2 hours
  (`execution.exit_timeout_s: 7200`). Unfilled maker entries are cancelled after 6 hours
  (`execution.entry_timeout_s: 21600`).
- [ ] **Taker timeout.** Taker orders that are not filled after 20 seconds
  (`live.order_timeout_s`) are cancelled. Only the filled part counts.

## Alerts you may see and what to do

- **UNKNOWN / "order state unknown":** the bot sent an order but could not read the result back
  (network trouble). It does not guess. Entries are paused, the order's group is frozen, and the bot
  looks the order up by its client id every cycle. When it finds it, you get **RESOLVED** and the
  real fill is booked. `/resume` does not override this pause while an order is still unresolved.
  If it stays unresolved, check the order on the exchange and, if needed, close the position by hand
  or run `flatten-all`.
- **ESCALATE ... cancel manually:** a cancel failed three times. The order is still live on the
  exchange and the bot keeps it in its books. Cancel it on the exchange website or app.
- **Open order "not tracked":** the exchange has a resting order the bot does not know about
  (for example after a crash). The bot pauses entries. If its client id starts with `arb` it is
  cancelled for you; otherwise it is left alone and you decide. Do not run two live bots on the
  same account and symbols: they would treat each other's orders as strays.

## 8. Stage 2: $300 per leg

Only move to stage 2 after at least 2 clean weeks at $50. Clean means:

- [ ] No reconcile mismatches.
- [ ] No kill-switch event.
- [ ] Results broadly in line with the paper run.
- [ ] Settings changed:
  ```yaml
  live:
    max_order_usd: 300
  strategies:
    funding_carry:
      notional_usd: 300
  ```

Note: the $300 is per leg. Three bases with two legs each is about $1,800 of notional, plus margin.

## 9. Records and tax (India)

- [ ] Export the records for each year:
  ```
  python -m arb tax-export --strategy funding_carry --year 2026 --live
  ```
  It writes `reports/tax_funding_carry_2026_fills.csv`, `..._trades.csv` and `..._README.txt`.
  Dates are IST. Amounts are USDT.
- [ ] Back up `data/live/` and `reports/` on a schedule.
- [ ] The export covers only what the bot tracked. Trades you make by hand are not in it.
- [ ] The tax rate in the bot is 31.2% (30% plus 4% cess) on gains. Losses are not set off
  against gains (Sec 115BBH). 1% TDS (Sec 194S) may apply on Indian exchanges.
- [ ] Use exchanges registered with FIU India.
- [ ] Have a chartered accountant check the numbers and the treatment before you file.

## 10. When to stop

Stop the bot if any of these happen:

- [ ] The kill switch fired.
- [ ] Reconcile mismatches keep coming back.
- [ ] Funding has been negative for a long time.
- [ ] The exchange has problems: API errors, locked accounts or withdrawal issues.

How to stop:

1. `sudo systemctl stop arb-live`, or stop the process by hand. This does not close positions.
2. Check the exchange.
3. If you need to exit, run `flatten-all` (see section 6).
4. Find out what went wrong before you restart.
