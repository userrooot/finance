"""Pre-trade risk checks and loss limits.

- Daily loss limit: equity down max_daily_loss_pct from the UTC day's start -> no new entries
  until the next UTC day (closing trades are still allowed).
- Total loss limit: equity down max_total_loss_pct from the start -> kill switch: the engine
  flattens everything and no new entries are allowed until the state is reset by a human.
- Every opening group is checked as a whole: one bad leg rejects all legs, so we never open
  half of a hedge.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from arb.config import Settings
from arb.core.portfolio import EPS, Portfolio
from arb.core.types import MarketSnapshot, OrderIntent

NOTIONAL_TOLERANCE = 1.01  # rounding slack on the per-trade cap


@dataclass
class RiskState:
    initial_equity: float
    day: str | None = None
    day_start_equity: float = 0.0
    daily_halt_day: str | None = None
    killed: bool = False
    kill_reason: str = ""


class RiskManager:
    def __init__(self, settings: Settings, initial_equity: float, state: dict | None = None):
        self.settings = settings
        self.state = RiskState(**state) if state else RiskState(initial_equity=initial_equity)

    @property
    def entries_allowed(self) -> bool:
        return not self.state.killed and self.state.daily_halt_day != self.state.day

    def update(self, ts_ms: int, equity: float) -> list[str]:
        """Roll the trading day and evaluate loss limits. Returns human-readable events."""
        s, r = self.state, self.settings.risk
        events = []
        day = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date().isoformat()
        if day != s.day:
            s.day, s.day_start_equity = day, equity
        if not s.killed and equity <= s.initial_equity * (1 - r.max_total_loss_pct / 100):
            s.killed = True
            s.kill_reason = f"total loss limit: equity {equity:.2f} <= {(1 - r.max_total_loss_pct / 100):.0%} of {s.initial_equity:.2f}"
            events.append(f"KILL SWITCH: {s.kill_reason}")
        if s.daily_halt_day != day and equity <= s.day_start_equity * (1 - r.max_daily_loss_pct / 100):
            s.daily_halt_day = day
            events.append(f"DAILY HALT {day}: equity {equity:.2f} down {r.max_daily_loss_pct}% from {s.day_start_equity:.2f}")
        return events

    def check(self, intents: list[OrderIntent], portfolio: Portfolio, snapshot: MarketSnapshot
              ) -> tuple[list[OrderIntent], list[tuple[OrderIntent, str]]]:
        groups: dict[str, list[OrderIntent]] = defaultdict(list)
        for i in intents:
            groups[i.group_id].append(i)
        approved: list[OrderIntent] = []
        rejected: list[tuple[OrderIntent, str]] = []
        exposure = portfolio.gross_exposure(snapshot)
        for legs in groups.values():
            reason = self._check_group(legs, portfolio, snapshot, exposure)
            if reason:
                rejected.extend((l, reason) for l in legs)
                continue
            approved.extend(legs)
            exposure += sum(self._notional(l, snapshot) for l in legs if not l.reduce_only)
        return approved, rejected

    @staticmethod
    def _notional(intent: OrderIntent, snapshot: MarketSnapshot) -> float:
        q = snapshot.quote(intent.exchange, intent.symbol)
        return intent.qty * q.mid if q else float("inf")

    def _check_group(self, legs: list[OrderIntent], portfolio: Portfolio, snapshot: MarketSnapshot,
                     exposure: float) -> str | None:
        cfg = self.settings
        for l in legs:
            if l.qty <= 0:
                return f"non-positive qty on {l.symbol}"
            q = snapshot.quote(l.exchange, l.symbol)
            if q is None:
                return f"no quote for {l.exchange} {l.symbol}"
            if l.reduce_only:
                held = sum(p.qty for p in portfolio.group_positions(l.group_id)
                           if p.exchange == l.exchange and p.symbol == l.symbol and p.market == l.market)
                signed = l.qty if l.side == "buy" else -l.qty
                if held * signed >= 0 or abs(signed) > abs(held) + EPS:
                    return f"reduce_only order on {l.symbol} would not reduce the position"
        if all(l.reduce_only for l in legs):
            return None  # closing is always allowed, even when halted or killed

        if not self.entries_allowed:
            return "entries halted: " + (self.state.kill_reason if self.state.killed else "daily loss limit")
        max_age_ms = cfg.risk.max_quote_age_s * 1000
        for l in legs:
            q = snapshot.quote(l.exchange, l.symbol)
            if snapshot.ts_ms - q.ts_ms > max_age_ms:
                return f"stale quote for {l.exchange} {l.symbol}"
            if not l.reduce_only and l.qty * q.mid > cfg.capital.max_per_trade_usd * NOTIONAL_TOLERANCE:
                return f"{l.symbol} leg notional {l.qty * q.mid:.2f} > max_per_trade_usd"
            if l.market == "spot" and l.side == "sell" and not l.reduce_only:
                if portfolio.net_qty(l.exchange, l.symbol, "spot") - l.qty < -EPS:
                    return f"spot short on {l.symbol} not allowed (no borrowing)"
        new = sum(self._notional(l, snapshot) for l in legs if not l.reduce_only)
        limit = cfg.capital.total_usd * cfg.risk.max_gross_exposure_x
        if exposure + new > limit:
            return f"gross exposure {exposure + new:.2f} > limit {limit:.2f}"
        return None

    def to_dict(self) -> dict:
        return asdict(self.state)
