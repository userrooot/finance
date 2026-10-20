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


def main() -> None:
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
    for p in (p_hist, p_bt, p_wf, p_paper, p_prep):
        p.add_argument("--strategy", choices=tc.STRATEGIES, required=True)
    p_hist.add_argument("--days", type=int, default=365)
    p_bt.add_argument("--days", type=int, help="only the last N days")
    p_wf.add_argument("--train-days", type=int, default=60)
    p_wf.add_argument("--test-days", type=int, default=30)
    p_paper.add_argument("--cycles", type=int, help="stop after N cycles (default: run forever)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    if settings.live_trading:
        raise SystemExit("live_trading=true but no live execution engine exists yet. Set it to false.")

    if args.cmd == "scan":
        asyncio.run(scan_loop(settings, args.once, args.interval))
    elif args.cmd == "fetch-history":
        asyncio.run(tc.fetch_history(settings, args.strategy, args.days))
    elif args.cmd == "backtest":
        tc.backtest(settings, args.strategy, args.days)
    elif args.cmd == "walk-forward":
        tc.walk_forward_cmd(settings, args.strategy, args.train_days, args.test_days)
    elif args.cmd == "paper":
        asyncio.run(tc.paper(settings, args.strategy, args.cycles))
    elif args.cmd == "paper-report":
        tc.paper_report_cmd(settings, args.strategy)
    else:
        day = date.fromisoformat(args.day) if args.day else datetime.now(timezone.utc).date()
        path = write_report(OpportunityStore(settings.storage.dir), day)
        print(path.read_text())
        print(f"\nSaved to {path}")


if __name__ == "__main__":
    main()
