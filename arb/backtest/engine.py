"""Bar-based backtest driving the shared TradingEngine.

Per bar: decisions are made on the bar's close snapshot and executed at the NEXT bar's open
(delay_execution), with a modelled half-spread and slippage, so a decision never sees its own fill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

from arb.backtest.metrics import compute_metrics
from arb.config import Settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import Fill, FundingEvent, FundingQuote, Key, MarketSnapshot, OrderIntent, Quote
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.strategies.base import Strategy

_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
TRADE_COLS = ["group_id", "tag", "open_ms", "close_ms", "pnl", "fees", "funding"]


def parse_timeframe_ms(tf: str) -> int:
    m = re.fullmatch(r"(\d+)([mhdw])", tf.strip())
    if not m:
        raise ValueError(f"unsupported timeframe {tf!r}")
    return int(m[1]) * _UNIT_MS[m[2]]


@dataclass
class BacktestResult:
    portfolio: Portfolio
    equity: pd.DataFrame
    trades: pd.DataFrame
    fills: list[Fill]
    rejections: list[tuple[OrderIntent, str]]
    events: list[str]
    metrics: dict
    bar_ms: int = 0


def common_timeline(ohlcv: dict[Key, pd.DataFrame]) -> np.ndarray:
    """Sorted bar open timestamps present in every series (inner join)."""
    ts: np.ndarray | None = None
    for df in ohlcv.values():
        cur = df["ts"].to_numpy(np.int64)
        ts = cur if ts is None else np.intersect1d(ts, cur)
    return np.unique(ts) if ts is not None else np.array([], dtype=np.int64)


def _funding_interval_hours(ts: np.ndarray) -> float:
    return float(np.median(np.diff(ts))) / 3_600_000 if len(ts) >= 2 else 8.0


def run_backtest(settings: Settings, strategy: Strategy, ohlcv: dict[Key, pd.DataFrame],
                 funding: dict[Key, pd.DataFrame] | None = None, start_ms: int | None = None,
                 end_ms: int | None = None, warmup_until_ms: int | None = None,
                 initial_cash: float | None = None) -> BacktestResult:
    """Run `strategy` over bars; start_ms/end_ms are inclusive bounds on bar open times.

    Bars whose close <= warmup_until_ms only feed strategy.on_snapshot (no trading, no equity).
    """
    cfg = settings.backtest
    bar_ms = parse_timeframe_ms(cfg.timeframe)
    initial = float(initial_cash if initial_cash is not None else settings.capital.total_usd)
    half = cfg.spread_bps / 2 / 1e4

    ts_all = common_timeline(ohlcv)
    keep = np.ones(len(ts_all), dtype=bool)
    if start_ms is not None:
        keep &= ts_all >= start_ms
    if end_ms is not None:
        keep &= ts_all <= end_ms
    idx = {k: df.set_index("ts") for k, df in ohlcv.items()}
    opens = {k: d["open"].reindex(ts_all).to_numpy(float) for k, d in idx.items()}
    closes = {k: d["close"].reindex(ts_all).to_numpy(float) for k, d in idx.items()}

    fund = {k: (df["ts"].to_numpy(np.int64), df["rate"].to_numpy(float))
            for k, df in (funding or {}).items() if df is not None and len(df)}
    fund_hours = {k: _funding_interval_hours(v[0]) for k, v in fund.items()}

    portfolio = Portfolio(initial)
    events: list[str] = []
    engine = TradingEngine(strategy, SimBroker(CostModel(settings), cfg.slippage_bps), portfolio,
                           RiskManager(settings, initial), delay_execution=True, on_event=events.append)
    scratch = Portfolio(initial)  # warm-up: strategy sees an empty portfolio
    fills: list[Fill] = []
    rejections: list[tuple[OrderIntent, str]] = []

    def quotes(px: dict[Key, np.ndarray], i: int, ts: int) -> dict[Key, Quote]:
        return {k: Quote(v[i] * (1 - half), v[i] * (1 + half), ts) for k, v in px.items()}

    prev_close: int | None = None
    for i in np.flatnonzero(keep):
        ts = int(ts_all[i])
        close_ts = ts + bar_ms
        warm = warmup_until_ms is not None and close_ts <= warmup_until_ms
        if not warm:
            res = engine.execute_pending(MarketSnapshot(ts, quotes(opens, i, ts)))
            fills += res.fills
            rejections += res.rejected
        lo = ts if prev_close is None else prev_close
        snap = MarketSnapshot(close_ts, quotes(closes, i, close_ts))
        for k, (fts, rates) in fund.items():
            j = int(np.searchsorted(fts, close_ts, side="right"))
            if j > 0:
                snap.funding[k] = FundingQuote(float(rates[j - 1]), fund_hours[k])
            a = int(np.searchsorted(fts, lo, side="right"))
            snap.funding_events += [FundingEvent(int(fts[n]), k[0], k[1], float(rates[n])) for n in range(a, j)]
        prev_close = close_ts
        if warm:
            strategy.on_snapshot(snap, scratch)
            continue
        res = engine.step(snap)
        fills += res.fills
        rejections += res.rejected

    equity = pd.DataFrame(portfolio.equity_curve, columns=["ts", "equity"])
    trades = pd.DataFrame([vars(t) for t in portfolio.closed_trades], columns=TRADE_COLS)
    metrics = compute_metrics(equity, trades, initial, settings.tax.rate_on_gains, portfolio.fees_paid,
                              portfolio.funding_received, portfolio.traded_notional, len(portfolio.open_groups()))
    return BacktestResult(portfolio, equity, trades, fills, rejections, events, metrics, bar_ms)
