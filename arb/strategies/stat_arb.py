"""Pairs trading on linear perps: trade the z-score of the log-price spread y - beta*x.

Per bar the hedge ratio is re-estimated by OLS over the rolling window (ly = alpha + beta*lx), so
the spread and its z-score always use the newest bar and nothing later (no lookahead). Pair
selection (cointegration) is done outside, by the scanner / walk-forward. What is open is derived
from the Portfolio (group ids `sa:{y}-{x}:{ts}`); the only strategy state is the price history.
"""
from __future__ import annotations

from collections import deque

import numpy as np

from arb.config import StatArbStrategyCfg
from arb.core.portfolio import Portfolio
from arb.core.types import MarketSnapshot, OrderIntent, Requirements
from arb.costs import CostModel
from arb.models import Side
from arb.strategies.base import Strategy

MIN_STD = 1e-12


class StatArb(Strategy):
    name = "stat_arb"

    def __init__(self, cfg: StatArbStrategyCfg, costs: CostModel, bar_ms: int = 3_600_000):
        self.cfg = cfg
        self.costs = costs
        self.bar_ms = bar_ms
        self.prices: dict[str, deque[float]] = {}
        self.last_bar: dict[str, int] = {}

    @staticmethod
    def perp_symbol(base: str) -> str:
        return f"{base}/USDT:USDT"

    def seed_prices(self, symbol: str, closes: list[float]) -> None:
        """Warm up with past bar closes (oldest first); the newest lookback_bars are kept."""
        self._hist(symbol).extend(closes)

    def _hist(self, symbol: str) -> deque[float]:
        h = self.prices.get(symbol)
        if h is None:
            h = self.prices[symbol] = deque(maxlen=self.cfg.lookback_bars)
        return h

    def requirements(self) -> Requirements:
        syms = sorted({self.perp_symbol(b) for pair in self.cfg.pairs for b in pair})
        return Requirements(quotes=[(self.cfg.exchange, s, "swap") for s in syms])

    # --- sampling ------------------------------------------------------------------------
    def _sample(self, snapshot: MarketSnapshot) -> None:
        bar = snapshot.ts_ms // self.bar_ms
        for sym in {self.perp_symbol(b) for pair in self.cfg.pairs for b in pair}:
            q = snapshot.quote(self.cfg.exchange, sym)
            if q is None or self.last_bar.get(sym) == bar:
                continue
            self._hist(sym).append(q.mid)
            self.last_bar[sym] = bar

    def signal(self, y: str, x: str) -> tuple[float, float] | None:
        """(z, beta) from the current windows, or None if not warmed up / degenerate."""
        hy, hx = self.prices.get(self.perp_symbol(y)), self.prices.get(self.perp_symbol(x))
        n = self.cfg.lookback_bars
        if hy is None or hx is None or len(hy) < n or len(hx) < n:
            return None
        ly, lx = np.log(np.asarray(hy)), np.log(np.asarray(hx))
        var = float(np.var(lx))
        if var <= 0:
            return None
        beta = float(np.cov(lx, ly, bias=True)[0, 1] / var)
        alpha = float(ly.mean() - beta * lx.mean())
        spread = ly - beta * lx - alpha
        std = float(spread.std(ddof=1))
        if std <= MIN_STD:
            return None
        return float((spread[-1] - spread.mean()) / std), beta

    # --- decisions -----------------------------------------------------------------------
    def on_snapshot(self, snapshot: MarketSnapshot, portfolio: Portfolio) -> list[OrderIntent]:
        self._sample(snapshot)
        intents: list[OrderIntent] = []
        for y, x in self.cfg.pairs:
            sig = self.signal(y, x)
            if sig is not None:
                intents += self._on_pair(y, x, sig, snapshot, portfolio)
        return intents

    def _on_pair(self, y: str, x: str, sig: tuple[float, float], snapshot: MarketSnapshot,
                 portfolio: Portfolio) -> list[OrderIntent]:
        cfg, ex = self.cfg, self.cfg.exchange
        z, beta = sig
        prefix = f"sa:{y}-{x}:"
        out: list[OrderIntent] = []
        for gid in portfolio.open_groups(prefix):
            positions = portfolio.group_positions(gid)
            tag = self._exit_tag(z, positions, y, snapshot.ts_ms)
            if tag:
                out += [OrderIntent(gid, p.exchange, p.symbol, p.market, "sell" if p.qty > 0 else "buy",
                                    abs(p.qty), reduce_only=True, tag=tag) for p in positions]
        if out or portfolio.open_groups(prefix):
            return out

        if not cfg.entry_z <= abs(z) < cfg.stop_z:
            return []
        qy, qx = snapshot.quote(ex, self.perp_symbol(y)), snapshot.quote(ex, self.perp_symbol(x))
        if qy is None or qx is None or qy.mid <= 0 or qx.mid <= 0:
            return []
        n_y = cfg.notional_usd / (1 + abs(beta))
        n_x = cfg.notional_usd - n_y
        y_side: Side = "sell" if z > 0 else "buy"
        x_long = (z > 0) == (beta > 0)  # short y hedges with long x when beta > 0
        x_side: Side = "buy" if x_long else "sell"
        gid = f"{prefix}{snapshot.ts_ms}"
        return [OrderIntent(gid, ex, self.perp_symbol(y), "swap", y_side, n_y / qy.mid, tag="entry"),
                OrderIntent(gid, ex, self.perp_symbol(x), "swap", x_side, n_x / qx.mid, tag="entry")]

    def _exit_tag(self, z: float, positions: list, y: str, ts_ms: int) -> str | None:
        cfg = self.cfg
        if abs(z) >= cfg.stop_z:
            return "stop"
        y_qty = sum(p.qty for p in positions if p.symbol == self.perp_symbol(y))
        if y_qty < 0 and z <= cfg.exit_z or y_qty > 0 and z >= -cfg.exit_z or y_qty == 0:
            return "exit"
        if (ts_ms - min(p.opened_ms for p in positions)) / self.bar_ms >= cfg.max_hold_bars:
            return "time_stop"
        return None

    # --- persistence ---------------------------------------------------------------------
    def state_dict(self) -> dict:
        return {"prices": {s: list(h) for s, h in self.prices.items()}, "last_bar": dict(self.last_bar)}

    def load_state(self, state: dict) -> None:
        self.prices = {}
        for sym, vals in state.get("prices", {}).items():
            self._hist(sym).extend(vals)
        self.last_bar = {s: int(b) for s, b in state.get("last_bar", {}).items()}
