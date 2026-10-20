"""TradingEngine: the single decision/execution loop shared by backtest, paper and live.

Per step: apply funding settlements -> evaluate loss limits -> strategy intents (or flatten-all
when the kill switch is on) -> risk checks -> execute -> account fills -> record equity.

With delay_execution=True (backtests) approved intents are queued and executed on the next
call to execute_pending(), e.g. at the next bar's open, so decisions never see their own fill price.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from arb.core.portfolio import EPS, Portfolio
from arb.core.risk import RiskManager
from arb.core.types import Fill, MarketSnapshot, OrderIntent
from arb.execution.broker import Broker
from arb.strategies.base import Strategy

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    ts_ms: int
    equity: float
    fills: list[Fill] = field(default_factory=list)
    rejected: list[tuple[OrderIntent, str]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


class TradingEngine:
    def __init__(self, strategy: Strategy, broker: Broker, portfolio: Portfolio, risk: RiskManager,
                 delay_execution: bool = False, on_event: Callable[[str], None] | None = None):
        self.strategy = strategy
        self.broker = broker
        self.portfolio = portfolio
        self.risk = risk
        self.delay_execution = delay_execution
        self.on_event = on_event or (lambda msg: log.warning(msg))
        self.pending: list[OrderIntent] = []

    def execute_pending(self, snapshot: MarketSnapshot) -> StepResult:
        """Execute queued intents at this snapshot's prices, re-checking risk against current state."""
        intents, self.pending = self.pending, []
        approved, rejected = self.risk.check(intents, self.portfolio, snapshot)
        fills = self._execute(approved, snapshot)
        return StepResult(snapshot.ts_ms, self.portfolio.equity(snapshot), fills, rejected)

    def step(self, snapshot: MarketSnapshot) -> StepResult:
        res = self.execute_pending(snapshot) if self.pending else StepResult(snapshot.ts_ms, 0.0)
        self.portfolio.apply_funding(snapshot.funding_events, snapshot)
        res.events += self.risk.update(snapshot.ts_ms, self.portfolio.equity(snapshot))
        for e in res.events:
            self.on_event(e)

        intents = self._flatten_all() if self.risk.state.killed else self.strategy.on_snapshot(snapshot, self.portfolio)
        approved, rejected = self.risk.check(intents, self.portfolio, snapshot)
        res.rejected += rejected
        if self.delay_execution:
            self.pending = approved
        else:
            res.fills += self._execute(approved, snapshot)
        res.equity = self.portfolio.record_equity(snapshot)
        return res

    def _flatten_all(self) -> list[OrderIntent]:
        return [OrderIntent(p.group_id, p.exchange, p.symbol, p.market, "sell" if p.qty > 0 else "buy",
                            abs(p.qty), reduce_only=True, tag="kill_switch")
                for p in self.portfolio.positions.values()]

    def _execute(self, intents: list[OrderIntent], snapshot: MarketSnapshot) -> list[Fill]:
        groups: dict[str, list[OrderIntent]] = defaultdict(list)
        for i in intents:
            groups[i.group_id].append(i)
        all_fills: list[Fill] = []
        for legs in groups.values():
            fills = self.broker.execute(legs, snapshot)
            for f in fills:
                self.portfolio.apply_fill(f)
            all_fills += fills
            if len(legs) > 1 and not all(l.reduce_only for l in legs):
                all_fills += self._hedge_leg_failure(legs, fills, snapshot)
        if all_fills:
            self.strategy.on_fills(all_fills)
        return all_fills

    def _hedge_leg_failure(self, legs: list[OrderIntent], fills: list[Fill], snapshot: MarketSnapshot) -> list[Fill]:
        """If one leg of a hedge under-fills, unwind the excess on the others so the group stays hedged."""
        ratio = min(f.qty / f.requested_qty for f in fills)
        unwinds = []
        for f in fills:
            excess = f.qty - ratio * f.requested_qty
            if excess > EPS:
                unwinds.append(OrderIntent(f.group_id, f.exchange, f.symbol, f.market,
                                           "sell" if f.side == "buy" else "buy", excess,
                                           reduce_only=True, tag="leg_unwind"))
        if not unwinds:
            return []
        self.on_event(f"leg failure in {fills[0].group_id}: filled ratio {ratio:.2%}, unwinding {len(unwinds)} leg(s)")
        out = self.broker.execute(unwinds, snapshot)
        for f in out:
            self.portfolio.apply_fill(f)
        return out
