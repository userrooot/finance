"""CLI commands for Phase 2: history download, backtest, walk-forward and paper trading."""
from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

from arb.backtest.data import HistoryStore
from arb.backtest.engine import parse_timeframe_ms, run_backtest
from arb.backtest.metrics import format_metrics
from arb.backtest.walk_forward import walk_forward
from arb.config import Settings
from arb.connectors.ccxt_source import CcxtSource
from arb.costs import CostModel
from arb.monitoring.notify import Notifier
from arb.paper.report import paper_report
from arb.paper.runner import PaperTrader
from arb.strategies.base import Strategy
from arb.strategies.funding_carry import FundingCarry
from arb.strategies.stat_arb import StatArb

STRATEGIES = ("funding_carry", "stat_arb")

# Deliberately small grids: every extra combination raises the chance of an overfit "winner".
PARAM_GRIDS: dict[str, list[dict]] = {
    "funding_carry": [{"entry_annualized_pct": e, "exit_annualized_pct": x} for e in (10, 15, 25) for x in (0, 3)],
    "stat_arb": [{"entry_z": e, "exit_z": x} for e in (1.5, 2.0, 2.5) for x in (0.0, 0.5)],
}


def build_strategy(name: str, settings: Settings, params: dict | None = None) -> Strategy:
    costs = CostModel(settings)
    if name == "funding_carry":
        return FundingCarry(settings.strategies.funding_carry.model_copy(update=params or {}), costs)
    if name == "stat_arb":
        bar_ms = parse_timeframe_ms(settings.backtest.timeframe)
        return StatArb(settings.strategies.stat_arb.model_copy(update=params or {}), costs, bar_ms=bar_ms)
    raise SystemExit(f"unknown strategy {name!r}; choose from {STRATEGIES}")


async def fetch_history(settings: Settings, name: str, days: int, source: str = "ccxt") -> None:
    req = build_strategy(name, settings).requirements()
    store = HistoryStore(Path(settings.storage.dir) / "history")
    if source == "binance-vision":
        from arb.backtest.binance_vision import import_history

        counts = await asyncio.to_thread(import_history, store, list(req.quotes), list(req.funding),
                                         math.ceil(days / 30), settings.backtest.timeframe)
        for key, n in counts.items():
            print(f"{key}: {n} rows")
        return
    until = int(time.time() * 1000)
    since = until - days * 86_400_000
    for ex, sym, _ in req.quotes:
        df = await store.download_ohlcv(ex, sym, settings.backtest.timeframe, since, until)
        print(f"{ex} {sym} {settings.backtest.timeframe}: {len(df)} bars")
    for ex, sym in req.funding:
        df = await store.download_funding(ex, sym, since, until)
        print(f"{ex} {sym} funding: {len(df)} rows")


def _load(settings: Settings, name: str):
    req = build_strategy(name, settings).requirements()
    store = HistoryStore(Path(settings.storage.dir) / "history")
    ohlcv = {(ex, sym): store.load_ohlcv(ex, sym, settings.backtest.timeframe) for ex, sym, _ in req.quotes}
    funding = {(ex, sym): store.load_funding(ex, sym) for ex, sym in req.funding}
    return ohlcv, funding


def _save(text: str, filename: str) -> Path:
    path = Path("reports") / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def backtest(settings: Settings, name: str, days: int | None) -> None:
    ohlcv, funding = _load(settings, name)
    start = None
    if days:
        last = max(int(df["ts"].max()) for df in ohlcv.values())
        start = last - days * 86_400_000
    res = run_backtest(settings, build_strategy(name, settings), ohlcv, funding, start_ms=start)
    text = (f"# Backtest: {name}\n\n{format_metrics(res.metrics)}\n\n"
            f"Rejected orders: {len(res.rejections)} | Risk events: {len(res.events)}\n\n"
            "In-sample result with fixed parameters. Use walk-forward for an honest out-of-sample estimate.\n")
    print(text)
    path = _save(text, f"backtest_{name}.md")
    res.equity.to_csv(path.with_suffix(".equity.csv"), index=False)
    print(f"Saved to {path}")


def walk_forward_cmd(settings: Settings, name: str, train_days: int, test_days: int) -> None:
    ohlcv, funding = _load(settings, name)
    bars_per_day = 86_400_000 // parse_timeframe_ms(settings.backtest.timeframe)
    res = walk_forward(settings, lambda p: build_strategy(name, settings, p), PARAM_GRIDS[name], ohlcv, funding,
                       train_bars=train_days * bars_per_day, test_bars=test_days * bars_per_day)
    text = f"# Walk-forward: {name}\n\n{res.summary}\n\n## Out-of-sample metrics\n\n{format_metrics(res.oos_metrics)}\n"
    print(text)
    path = _save(text, f"walk_forward_{name}.md")
    if not res.oos_equity.empty:
        res.oos_equity.to_csv(path.with_suffix(".equity.csv"), index=False)
    print(f"Saved to {path}")


async def paper(settings: Settings, name: str, cycles: int | None) -> None:
    strategy = build_strategy(name, settings)
    exchanges = sorted({ex for ex, _, _ in strategy.requirements().quotes})
    trader = PaperTrader(settings, strategy, CcxtSource(exchanges), Path(settings.paper.state_dir) / name,
                         notifier=Notifier())
    await trader.warm_up()
    await trader.run(cycles)


def paper_report_cmd(settings: Settings, name: str) -> None:
    print(paper_report(Path(settings.paper.state_dir) / name, tax_rate=settings.tax.rate_on_gains))
