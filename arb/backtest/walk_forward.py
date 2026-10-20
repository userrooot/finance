"""Rolling walk-forward optimisation: pick params in-sample, judge only out-of-sample.

Keep the param grid small: every extra combination is another chance to fit noise in-sample.
Report OOS results only; the IS numbers are shown just to quantify degradation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from arb.backtest.engine import BacktestResult, common_timeline, parse_timeframe_ms, run_backtest
from arb.backtest.metrics import compute_metrics, format_metrics
from arb.config import Settings
from arb.core.types import Key
from arb.strategies.base import Strategy


@dataclass
class WalkForwardResult:
    windows: list[dict]
    oos_equity: pd.DataFrame
    oos_metrics: dict
    summary: str = ""
    oos_trades: pd.DataFrame = field(default_factory=pd.DataFrame)


def _date(ts_ms: int) -> str:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")


def walk_forward(settings: Settings, build_strategy: Callable[[dict], Strategy], param_grid: list[dict],
                 ohlcv: dict[Key, pd.DataFrame], funding: dict[Key, pd.DataFrame] | None = None,
                 *, train_bars: int, test_bars: int, objective: str = "sharpe") -> WalkForwardResult:
    if not param_grid:
        raise ValueError("param_grid is empty")
    ts = common_timeline(ohlcv)
    bar_ms = parse_timeframe_ms(settings.backtest.timeframe)
    initial = float(settings.capital.total_usd)
    windows: list[dict] = []
    curves: list[pd.DataFrame] = []
    trades: list[pd.DataFrame] = []
    capital, fees, fund, notional, open_pos = initial, 0.0, 0.0, 0.0, 0

    i = 0
    while train_bars > 0 and test_bars > 0 and i + train_bars + test_bars <= len(ts):
        tr0, tr1 = int(ts[i]), int(ts[i + train_bars - 1])
        te0, te1 = int(ts[i + train_bars]), int(ts[i + train_bars + test_bars - 1])
        best: tuple[float, dict] | None = None
        for params in param_grid:
            m = run_backtest(settings, build_strategy(params), ohlcv, funding, start_ms=tr0, end_ms=tr1).metrics
            if best is None or m[objective] > best[0]:
                best = (m[objective], params)
        is_obj, params = best
        res: BacktestResult = run_backtest(settings, build_strategy(params), ohlcv, funding, start_ms=tr0,
                                           end_ms=te1, warmup_until_ms=tr1 + bar_ms)
        windows.append({"train_start": _date(tr0), "train_end": _date(tr1 + bar_ms),
                        "test_start": _date(te0), "test_end": _date(te1 + bar_ms),
                        "params": params, "is_objective": is_obj, "oos_objective": res.metrics[objective],
                        "oos_return_pct": res.metrics["total_return_pct"]})
        eq = res.equity.copy()
        eq["equity"] = capital * eq["equity"] / initial  # chain: each window starts at the previous end
        capital = float(eq["equity"].iloc[-1]) if len(eq) else capital
        curves.append(eq)
        trades.append(res.trades)
        fees += res.metrics["fees"]
        fund += res.metrics["funding"]
        notional += res.metrics["turnover_x"] * initial
        open_pos += res.metrics["open_positions"]
        i += test_bars

    oos_eq = pd.concat(curves, ignore_index=True) if curves else pd.DataFrame(columns=["ts", "equity"])
    oos_tr = pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()
    m = compute_metrics(oos_eq, oos_tr, initial, settings.tax.rate_on_gains, fees, fund, notional, open_pos)
    return WalkForwardResult(windows, oos_eq, m, _summary(windows, m, objective), oos_tr)


def _summary(windows: list[dict], m: dict, objective: str) -> str:
    if not windows:
        return "Walk-forward: not enough bars for a single train+test window."
    n = len(windows)
    is_avg = sum(w["is_objective"] for w in windows) / n
    oos_avg = sum(w["oos_objective"] for w in windows) / n
    lines = [f"## Walk-forward ({n} windows, objective: {objective})", "",
             "| train | test | params | IS | OOS | OOS return % |", "|---|---|---|---|---|---|"]
    lines += [f"| {w['train_start']} -> {w['train_end']} | {w['test_start']} -> {w['test_end']} | {w['params']} "
              f"| {w['is_objective']:.2f} | {w['oos_objective']:.2f} | {w['oos_return_pct']:.2f} |" for w in windows]
    lines += ["", f"Mean IS {objective}: {is_avg:.2f}, mean OOS {objective}: {oos_avg:.2f}, "
              f"degradation: {is_avg - oos_avg:.2f}", "", "### Stitched out-of-sample result", "", format_metrics(m)]
    return "\n".join(lines)
