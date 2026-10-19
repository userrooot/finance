"""Statistical arbitrage scanner: cointegrated spot pairs traded as linear perps.

Pairs are selected with the Engle-Granger test on log prices. A pair is reported when the
current spread is at least `entry_z` standard deviations from its lookback mean and the
mean-reversion half-life is plausible. Executed on perps (shorting spot needs borrowing).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math

import numpy as np
import pandas as pd
from statsmodels.tsa.stattools import coint

from arb.config import Settings
from arb.connectors.base import MarketData
from arb.costs import CostModel
from arb.models import Leg, Opportunity

log = logging.getLogger(__name__)

MIN_BAR_FRACTION = 0.9


async def _closes(source: MarketData, exchange: str, base: str, quote: str, timeframe: str,
                  limit: int) -> tuple[str, pd.Series | None]:
    try:
        rows = await source.ohlcv(exchange, f"{base}/{quote}", timeframe, limit)
        if not rows:
            return base, None
        arr = np.asarray(rows, dtype=float)
        return base, pd.Series(arr[:, 4], index=arr[:, 0].astype("int64")).groupby(level=0).last()
    except Exception as exc:  # noqa: BLE001 - per-symbol isolation
        log.warning("stat_arb: ohlcv failed for %s/%s on %s: %s", base, quote, exchange, exc)
        return base, None


def _half_life(spread: np.ndarray) -> float | None:
    """Half-life in bars from AR(1) on differences: ds_t = a + b * s_{t-1}; hl = -ln2 / b."""
    lag, diff = spread[:-1], np.diff(spread)
    lag_c = lag - lag.mean()
    denom = float(lag_c @ lag_c)
    if denom <= 0:
        return None
    b = float(lag_c @ (diff - diff.mean())) / denom
    if b >= 0:
        return None
    return -math.log(2) / b


def _analyse(base_y: str, base_x: str, ly: np.ndarray, lx: np.ndarray, cfg, lookback: int
             ) -> dict | None:
    pvalue = float(coint(ly, lx, trend="c", autolag="aic")[1])
    if not pvalue <= cfg.max_coint_pvalue:
        return None
    xm, ym = lx.mean(), ly.mean()
    xc = lx - xm
    var = float(xc @ xc)
    if var <= 0:
        return None
    beta = float(xc @ (ly - ym)) / var
    alpha = float(ym - beta * xm)
    spread = ly - beta * lx - alpha
    mean, std = float(spread.mean()), float(spread.std(ddof=1))
    if std <= 0:
        return None
    hl = _half_life(spread)
    if hl is None or not 1.0 <= hl <= lookback / 4:
        return None
    return {"pvalue": pvalue, "beta": beta, "alpha": alpha, "mean": mean, "std": std,
            "s_last": float(spread[-1]), "z": (float(spread[-1]) - mean) / std, "half_life": hl}


async def scan(source: MarketData, settings: Settings, costs: CostModel, now_ms: int) -> list[Opportunity]:
    cfg = settings.scanners.stat_arb
    if not cfg.enabled:
        return []
    quote = settings.universe.quote
    results = await asyncio.gather(*(
        _closes(source, cfg.exchange, b, quote, cfg.timeframe, cfg.lookback_bars)
        for b in settings.universe.base_assets))
    series = {b: s for b, s in results if s is not None}
    if len(series) < 2:
        return []
    frame = pd.DataFrame(series).dropna()
    frame = frame[(frame > 0).all(axis=1)]
    n_bars = len(frame)
    if n_bars < MIN_BAR_FRACTION * cfg.lookback_bars:
        log.info("stat_arb: only %d aligned bars (< %.0f%% of %d)", n_bars, 100 * MIN_BAR_FRACTION,
                 cfg.lookback_bars)
        return []
    logs = np.log(frame.to_numpy(dtype=float))
    col = {b: i for i, b in enumerate(frame.columns)}
    last = frame.iloc[-1]

    pairs = list(itertools.combinations(frame.columns, 2))
    hits: list[tuple[str, str, dict]] = []
    for by, bx in pairs:
        try:
            res = _analyse(by, bx, logs[:, col[by]], logs[:, col[bx]], cfg, cfg.lookback_bars)
        except Exception as exc:  # noqa: BLE001
            log.warning("stat_arb: pair %s/%s failed: %s", by, bx, exc)
            continue
        if res is not None:
            hits.append((by, bx, res))

    k = len(pairs)
    expected_fp = k * cfg.max_coint_pvalue
    notional = settings.capital.max_per_trade_usd
    out: list[Opportunity] = []
    for by, bx, r in hits:
        if abs(r["z"]) < cfg.entry_z:
            continue
        beta = r["beta"]
        w_y, w_x = 1.0, abs(beta)
        n_y = notional * w_y / (w_y + w_x)
        n_x = notional - n_y
        px_y, px_x = float(last[by]), float(last[bx])
        # entry + exit on both legs = 4 perp taker fees
        fees = 2 * (costs.fee(cfg.exchange, "swap", n_y) + costs.fee(cfg.exchange, "swap", n_x))
        # Approximation: reversion of the log-spread to its mean earns ~ n_y * |s_last - mean|
        # (log-return of the y-leg-equivalent spread); ignores drift in the mean and partial reversion.
        gross = n_y * abs(r["s_last"] - r["mean"])
        net = gross - fees
        if net <= 0:
            continue
        short_spread = r["z"] > 0  # spread = ly - beta*lx too high: sell y, buy x (if beta > 0)
        side_y = "sell" if short_spread else "buy"
        opp_side = "buy" if side_y == "sell" else "sell"
        side_x = opp_side if beta >= 0 else side_y
        sym = lambda b: f"{b}/{quote}:{quote}"  # noqa: E731
        legs = [Leg(cfg.exchange, sym(by), "swap", side_y, px_y, n_y / px_y),
                Leg(cfg.exchange, sym(bx), "swap", side_x, px_x, n_x / px_x)]
        out.append(Opportunity(
            strategy="stat_arb", timestamp_ms=now_ms, key=f"{by}/{bx} {cfg.exchange}",
            legs=legs, notional_usd=notional, gross_pnl_usd=gross, costs_usd={"fees": fees},
            net_pnl_usd=net, net_after_tax_usd=costs.after_tax(net), net_bps=net / notional * 1e4,
            capacity_usd=notional, holding=f"~{r['half_life']:.0f} {cfg.timeframe} bars",
            annualized_pct=None,
            risk_notes=[
                "Statistical, not risk-free: the spread may keep diverging before (or instead of) reverting.",
                "Cointegration can break; the relationship is estimated in-sample over the lookback window.",
                f"Multiple testing: {k} pairs tested at p<={cfg.max_coint_pvalue} -> ~{expected_fp:.1f} "
                "false-positive cointegrated pairs expected by chance.",
                "Requires shorting linear perps (funding and liquidation risk apply); gross is an approximation.",
            ],
            details={"pvalue": r["pvalue"], "beta": beta, "alpha": r["alpha"], "z": r["z"],
                     "half_life": r["half_life"], "n_bars": n_bars, "pairs_tested": k},
        ))
    return out
