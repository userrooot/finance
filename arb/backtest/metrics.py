"""Performance metrics for a backtest equity curve and trade ledger."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

DAY_MS = 86_400_000
ANNUALISATION = 365  # crypto trades every day


def _iso(ts_ms: int) -> str:
    return pd.Timestamp(ts_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M")


def daily_returns(equity: pd.DataFrame) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    s = pd.Series(equity["equity"].to_numpy(float), index=pd.to_datetime(equity["ts"], unit="ms", utc=True))
    return s.resample("1D").last().dropna().pct_change().dropna()


def _sharpe_sortino(r: pd.Series) -> tuple[float, float]:
    if len(r) < 2:
        return 0.0, 0.0
    ann = math.sqrt(ANNUALISATION)
    std = float(r.std(ddof=1))
    sharpe = float(r.mean()) / std * ann if std > 1e-15 else 0.0
    downside = math.sqrt(float((np.minimum(r.to_numpy(), 0.0) ** 2).mean()))
    sortino = float(r.mean()) / downside * ann if downside > 1e-15 else 0.0
    return sharpe, sortino


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame, initial: float, tax_rate: float, fees: float,
                    funding: float, traded_notional: float, open_positions: int) -> dict:
    final = float(equity["equity"].iloc[-1]) if len(equity) else initial
    start_ms = int(equity["ts"].iloc[0]) if len(equity) else 0
    end_ms = int(equity["ts"].iloc[-1]) if len(equity) else 0
    days = (end_ms - start_ms) / DAY_MS
    total_ret = final / initial - 1 if initial else 0.0
    cagr = (final / initial) ** (ANNUALISATION / days) - 1 if days > 0 and initial > 0 and final > 0 else 0.0
    if len(equity):
        eq = np.concatenate([[initial], equity["equity"].to_numpy(float)])
        max_dd = float(((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq)).max())
    else:
        max_dd = 0.0
    sharpe, sortino = _sharpe_sortino(daily_returns(equity))

    pnl = trades["pnl"].to_numpy(float) if len(trades) else np.array([])
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    if losses.sum() < 0:
        pf = float(wins.sum() / -losses.sum())
    else:
        pf = math.inf if wins.sum() > 0 else 0.0
    tax = tax_rate * float(wins.sum())
    return {
        "start": _iso(start_ms) if len(equity) else "-", "end": _iso(end_ms) if len(equity) else "-",
        "days": days, "final_equity": final,
        "total_return_pct": total_ret * 100, "cagr_pct": cagr * 100,
        "sharpe": sharpe, "sortino": sortino, "max_drawdown_pct": max_dd * 100,
        "n_trades": int(len(pnl)), "win_rate_pct": float(len(wins) / len(pnl) * 100) if len(pnl) else 0.0,
        "profit_factor": pf, "avg_trade_pnl": float(pnl.mean()) if len(pnl) else 0.0,
        "fees": fees, "funding": funding,
        "turnover_x": traded_notional / initial if initial else 0.0,
        "tax": tax, "net_pnl_after_tax": final - initial - tax,
        "open_positions": open_positions,
    }


def format_metrics(m: dict) -> str:
    def fmt(v: object) -> str:
        if isinstance(v, float):
            return "inf" if math.isinf(v) else f"{v:,.2f}"
        return str(v)

    rows = ["| metric | value |", "|---|---|"]
    rows += [f"| {k} | {fmt(v)} |" for k, v in m.items()]
    return "\n".join(rows)
