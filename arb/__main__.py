"""CLI: `python -m arb scan` runs the read-only opportunity scanners; `python -m arb report` builds the daily report."""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import date, datetime, timezone

from arb.config import DEFAULT_PATH, Settings, load_settings
from arb.connectors.ccxt_source import CcxtSource
from arb.connectors.tracking import TrackingSource
from arb.costs import CostModel
from arb.models import Opportunity
from arb.report import write_report
from arb.scanners import basis, cross_exchange, funding, stat_arb, triangular
from arb.storage import OpportunityStore

log = logging.getLogger("arb")

# Fast scanners run every cycle; stat-arb pulls a month of candles, so it runs at most hourly.
FAST_SCANNERS = {
    "cross_exchange": cross_exchange.scan,
    "triangular": triangular.scan,
    "funding": funding.scan,
    "basis": basis.scan,
}
STAT_ARB_EVERY_S = 3600


async def run_cycle(source, settings: Settings, costs: CostModel, include_stat_arb: bool) -> tuple[list[Opportunity], dict[str, int], list[str]]:
    source = TrackingSource(source)
    scanners = dict(FAST_SCANNERS)
    if include_stat_arb:
        scanners["stat_arb"] = stat_arb.scan
    now_ms = int(time.time() * 1000)
    results = await asyncio.gather(*(fn(source, settings, costs, now_ms) for fn in scanners.values()), return_exceptions=True)
    opps: list[Opportunity] = []
    counts: dict[str, int] = {}
    errors: list[str] = []
    for name, res in zip(scanners, results):
        if isinstance(res, BaseException):
            errors.append(f"{name}: {res!r}")
            log.exception("scanner %s failed", name, exc_info=res)
            continue
        opps.extend(res)
        counts[name] = len(res)
    if source.failures:
        log.warning("%d of %d market-data calls failed, e.g. %s", len(source.failures), source.calls, source.failures[0])
    errors.extend(f"data: {f}" for f in source.failures)
    return opps, counts, errors


async def scan_loop(settings: Settings, once: bool, interval_s: float) -> None:
    source = CcxtSource(list(settings.exchanges))
    costs = CostModel(settings)
    store = OpportunityStore(settings.storage.dir)
    last_stat_arb = 0.0
    try:
        while True:
            started = time.time()
            include_stat_arb = started - last_stat_arb >= STAT_ARB_EVERY_S
            opps, counts, errors = await run_cycle(source, settings, costs, include_stat_arb)
            if include_stat_arb:
                last_stat_arb = started
            store.append(opps)
            store.log_scan(int(started * 1000), counts, errors, time.time() - started)
            for o in sorted(opps, key=lambda o: o.net_bps, reverse=True)[:10]:
                log.info("%-15s %-40s net %7.1f bps  after-tax $%.2f  cap $%.0f", o.strategy, o.key, o.net_bps, o.net_after_tax_usd, o.capacity_usd)
            log.info("cycle done in %.1fs: %s errors=%d", time.time() - started, counts, len(errors))
            if once:
                return
            await asyncio.sleep(max(0.0, interval_s - (time.time() - started)))
    finally:
        await source.close()


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env into the environment (existing variables win)."""
    import os

    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="arb")
    parser.add_argument("--config", default=str(DEFAULT_PATH))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="run read-only opportunity scanners")
    p_scan.add_argument("--once", action="store_true", help="single cycle then exit")
    p_scan.add_argument("--interval", type=float, default=60.0, help="seconds between cycles")
    p_rep = sub.add_parser("report", help="write the daily opportunity report")
    p_rep.add_argument("--day", help="YYYY-MM-DD (UTC), default today")

    from arb import trading_cli as tc

    p_hist = sub.add_parser("fetch-history", help="download OHLCV + funding history for a strategy")
    p_bt = sub.add_parser("backtest", help="backtest a strategy on downloaded history")
    p_wf = sub.add_parser("walk-forward", help="walk-forward (out-of-sample) evaluation")
    p_paper = sub.add_parser("paper", help="paper trade a strategy on live data (no real orders)")
    p_prep = sub.add_parser("paper-report", help="summarise a paper trading run")
    p_val = sub.add_parser("validate", help="paper-vs-backtest gate before going live (exit 1 on FAIL)")
    for p in (p_hist, p_bt, p_wf, p_paper, p_prep, p_val):
        p.add_argument("--strategy", choices=tc.STRATEGIES, required=True)
    p_val.add_argument("--source", choices=("ccxt", "binance-vision"), default="binance-vision",
                       help="history importer used to cover the paper period")
    p_hist.add_argument("--days", type=int, default=365)
    p_hist.add_argument("--source", choices=("ccxt", "binance-vision"), default="ccxt",
                        help="binance-vision = public archive at data.binance.vision (binance only)")
    p_bt.add_argument("--days", type=int, help="only the last N days")
    p_wf.add_argument("--train-days", type=int, default=60)
    p_wf.add_argument("--test-days", type=int, default=30)
    p_tax = sub.add_parser("tax-export", help="export fills and closed trades for a calendar year (CSV, IST dates)")
    p_tax.add_argument("--strategy", choices=tc.STRATEGIES, required=True)
    p_tax.add_argument("--year", type=int, required=True)
    p_tax.add_argument("--state-dir", help="override the state directory")
    p_tax.add_argument("--live", action="store_true", help="use data/live/<strategy>")
    p_paper.add_argument("--cycles", type=int, help="stop after N cycles (default: run forever)")
    p_live = sub.add_parser("live", help="REAL MONEY: live trading behind the safety gate (see docs/GO_LIVE.md)")
    p_live.add_argument("--strategy", choices=tc.STRATEGIES, required=True)
    p_live.add_argument("--cycles", type=int, help="stop after N cycles (default: run forever)")
    p_live.add_argument("--skip-gate", action="store_true", help="start without a recent PASS from `validate`")
    p_reset = sub.add_parser("risk-reset", help="clear a kill switch/pause in saved state (after human review)")
    p_reset.add_argument("--strategy", choices=tc.STRATEGIES, required=True)
    p_reset.add_argument("--live", action="store_true", help="the live state (data/live/<strategy>)")
    p_reset.add_argument("--yes-really", action="store_true")
    p_flat = sub.add_parser("flatten-all", help="REAL MONEY: close all perp positions and the bot's spot on an exchange")
    p_flat.add_argument("--exchange", required=True)
    p_flat.add_argument("--strategy", choices=tc.STRATEGIES, required=True, help="whose live state lists the bot's spot")
    p_flat.add_argument("--yes-really", action="store_true", help="required: confirms you want market orders now")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    if args.cmd == "tax-export":
        from pathlib import Path

        from arb.tax_export import export_tax

        sd = Path(args.state_dir) if args.state_dir else (
            Path("data/live") / args.strategy if args.live else Path(settings.paper.state_dir) / args.strategy)
        out = export_tax(sd, args.year, Path("reports") / f"tax_{args.strategy}_{args.year}")
        print(f"Saved to {out}_fills.csv, {out}_trades.csv, {out}_README.txt")
        return
    if settings.live_trading and args.cmd == "paper":
        raise SystemExit("live_trading=true: paper trading is refused so the two are never confused. "
                         "Use `python -m arb live`, or set live_trading: false.")

    if args.cmd == "live":
        asyncio.run(tc.live(settings, args.strategy, args.cycles, args.skip_gate))
    elif args.cmd == "risk-reset":
        tc.risk_reset_cmd(args.strategy, args.live, args.yes_really, settings.paper.state_dir)
    elif args.cmd == "flatten-all":
        tc.flatten_all_cmd(settings, args.exchange, args.strategy, args.yes_really)
    elif args.cmd == "scan":
        asyncio.run(scan_loop(settings, args.once, args.interval))
    elif args.cmd == "fetch-history":
        asyncio.run(tc.fetch_history(settings, args.strategy, args.days, args.source))
    elif args.cmd == "backtest":
        tc.backtest(settings, args.strategy, args.days)
    elif args.cmd == "walk-forward":
        tc.walk_forward_cmd(settings, args.strategy, args.train_days, args.test_days)
    elif args.cmd == "paper":
        asyncio.run(tc.paper(settings, args.strategy, args.cycles))
    elif args.cmd == "paper-report":
        tc.paper_report_cmd(settings, args.strategy)
    elif args.cmd == "validate":
        raise SystemExit(tc.validate_cmd(settings, args.strategy, args.source))
    else:
        day = date.fromisoformat(args.day) if args.day else datetime.now(timezone.utc).date()
        path = write_report(OpportunityStore(settings.storage.dir), day)
        print(path.read_text())
        print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
