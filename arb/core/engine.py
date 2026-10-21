"""TradingEngine: the single decision/execution loop shared by backtest, paper and live.

Per step: apply funding settlements -> evaluate loss limits -> strategy intents (or flatten-all
when the kill switch is on) -> risk checks -> execute -> account fills -> record equity.

With delay_execution=True (backtests) approved intents are queued and executed on the next
call to execute_pending(), e.g. at the next bar's open, so decisions never see their own fill price.

Maker-first groups (OrderIntent.exec_style == "maker_first"): the maker leg rests as a post-only
limit at the touch and the other leg is sent as a taker hedge for exactly what the maker leg
fills, in the same step, so the position is never left unhedged. Each step the engine first
checks resting orders for fills (against the market since they were placed), then re-pegs ones
the touch has moved away from, cancels entries that time out (or when entries are halted),
and turns timed-out exits into taker orders so exits always complete. The kill switch cancels
every resting order and flattens with taker orders.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from arb.core.portfolio import EPS, Portfolio
from arb.core.risk import RiskManager
from arb.config import ExecutionCfg
from arb.core.types import Fill, MarketSnapshot, OrderIntent, RestingOrder
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
                 delay_execution: bool = False, on_event: Callable[[str], None] | None = None,
                 execution: ExecutionCfg | None = None):
        self.strategy = strategy
        self.execution = execution or ExecutionCfg()
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

        if self.risk.state.killed and self.portfolio.open_orders:
            for o in list(self.portfolio.open_orders):
                res.fills += self._cancel(o, snapshot, "kill_cancels")
            res.events.append("kill switch: cancelled all resting orders")
        res.fills += self._work_resting(snapshot)

        if self.risk.state.killed:
            intents = self._flatten_all()
        else:
            busy = {o.group_id for o in self.portfolio.open_orders}
            intents = [i for i in self.strategy.on_snapshot(snapshot, self.portfolio) if i.group_id not in busy]
        approved, rejected = self.risk.check(intents, self.portfolio, snapshot)
        res.rejected += rejected
        maker_groups = set()
        for gid in {i.group_id for i in approved if i.exec_style == "maker_first"}:
            legs = [i for i in approved if i.group_id == gid]
            if len(legs) == 2 and sum(l.maker for l in legs) == 1:
                maker_groups.add(gid)
            else:  # not a maker/hedge pair: execute it as taker rather than leave it stuck
                self.on_event(f"maker_first group {gid} is not one maker leg + one hedge leg; using taker")
        for gid in maker_groups:
            self._place_maker([i for i in approved if i.group_id == gid], snapshot)
        approved = [i for i in approved if i.group_id not in maker_groups]
        if self.delay_execution:
            self.pending = approved
        else:
            res.fills += self._execute(approved, snapshot)
        res.equity = self.portfolio.record_equity(snapshot)
        return res

    # --- maker-first execution -----------------------------------------------------------
    def _place_maker(self, legs: list[OrderIntent], snapshot: MarketSnapshot) -> None:
        m = next(l for l in legs if l.maker)
        h = next(l for l in legs if not l.maker)
        q = snapshot.quote(m.exchange, m.symbol)
        price = self.broker.maker_price(m.side, q)
        order = RestingOrder(
            f"{m.group_id}:{snapshot.ts_ms}", m.group_id, m.exchange, m.symbol, m.market, m.side, m.qty, price,
            snapshot.ts_ms, snapshot.ts_ms, m.reduce_only, m.tag,
            {"exchange": h.exchange, "symbol": h.symbol, "market": h.market, "side": h.side,
             "reduce_only": h.reduce_only})
        # A live broker sends the order here; a post-only reject leaves it unplaced and the broker
        # retries on the next check. Recording it first means a crash never loses track of it.
        self.portfolio.open_orders.append(order)
        self.broker.place_resting(order)
        self.portfolio.count("maker_orders")

    def _work_resting(self, snapshot: MarketSnapshot) -> list[Fill]:
        pf, cfg = self.portfolio, self.execution
        fills: list[Fill] = []
        for o in list(pf.open_orders):
            f = self.broker.check_resting(o, snapshot)
            if f is not None:
                fills += self._apply_maker_fill(o, f, snapshot)
            if o.qty <= EPS:
                pf.open_orders.remove(o)

        for o in list(pf.open_orders):
            age_s = (snapshot.ts_ms - o.created_ms) / 1000
            q = snapshot.quote(o.exchange, o.symbol)
            if not o.reduce_only and (not self.risk.entries_allowed or age_s >= cfg.entry_timeout_s):
                fills += self._cancel(o, snapshot, "entry_cancels")
            elif o.reduce_only and age_s >= cfg.exit_timeout_s:
                fills += self._cancel(o, snapshot, "exit_taker_fallbacks")
                if o.qty > EPS:
                    h = o.hedge
                    fills += self._execute([
                        OrderIntent(o.group_id, o.exchange, o.symbol, o.market, o.side, o.qty, reduce_only=True, tag=o.tag),
                        OrderIntent(o.group_id, h["exchange"], h["symbol"], h["market"], h["side"], o.qty,
                                    reduce_only=True, tag=o.tag)], snapshot)
            elif q is not None:
                touch = self.broker.maker_price(o.side, q)
                if abs(touch / o.price - 1) * 1e4 > cfg.requote_bps:
                    late = self.broker.replace_resting(o, touch)  # live: cancel + re-post
                    if late is not None:
                        fills += self._apply_maker_fill(o, late, snapshot)
                    o.price, o.placed_ms = touch, snapshot.ts_ms
                    pf.count("requotes")
                    if o.qty <= EPS:
                        pf.open_orders.remove(o)
        if fills:
            self.strategy.on_fills(fills)
        return fills

    def _apply_maker_fill(self, o: RestingOrder, f: Fill, snapshot: MarketSnapshot) -> list[Fill]:
        pf = self.portfolio
        pf.apply_fill(f)
        pf.count("maker_fills")
        pf.count("maker_fill_wait_s", (snapshot.ts_ms - o.created_ms) / 1000)
        o.qty -= f.qty
        return [f] + self._hedge_maker_fill(o, f, snapshot)

    def _cancel(self, o: RestingOrder, snapshot: MarketSnapshot, stat: str) -> list[Fill]:
        """Cancel a resting order. A fill that raced the cancel is still applied and hedged."""
        late = self.broker.cancel_resting(o)
        fills = self._apply_maker_fill(o, late, snapshot) if late is not None else []
        if o in self.portfolio.open_orders:
            self.portfolio.open_orders.remove(o)
        self.portfolio.count(stat)
        return fills

    def _hedge_maker_fill(self, o: RestingOrder, maker_fill: Fill, snapshot: MarketSnapshot) -> list[Fill]:
        """Taker-hedge exactly the maker fill; if the hedge under-fills, unwind the excess maker qty."""
        h = o.hedge
        hedge = self.broker.execute([OrderIntent(o.group_id, h["exchange"], h["symbol"], h["market"], h["side"],
                                                 maker_fill.qty, reduce_only=h["reduce_only"], tag=o.tag)], snapshot)
        for f in hedge:
            self.portfolio.apply_fill(f)
        short = maker_fill.qty - sum(f.qty for f in hedge)
        if short <= EPS:
            return hedge
        self.on_event(f"hedge under-filled in {o.group_id} by {short:.8f}; unwinding the maker leg excess")
        unwind = self.broker.execute([OrderIntent(o.group_id, o.exchange, o.symbol, o.market,
                                                  "sell" if o.side == "buy" else "buy", short,
                                                  reduce_only=True, tag="leg_unwind")], snapshot)
        for f in unwind:
            self.portfolio.apply_fill(f)
        return hedge + unwind

    def close_group(self, group_id: str, snapshot: MarketSnapshot, tag: str) -> list[Fill]:
        """Cancel the group's resting orders and close its positions now with taker orders."""
        fills: list[Fill] = []
        for o in [o for o in self.portfolio.open_orders if o.group_id == group_id]:
            fills += self._cancel(o, snapshot, "forced_cancels")
        intents = [OrderIntent(p.group_id, p.exchange, p.symbol, p.market, "sell" if p.qty > 0 else "buy",
                               abs(p.qty), reduce_only=True, tag=tag)
                   for p in self.portfolio.group_positions(group_id)]
        return fills + self._execute(intents, snapshot)

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
