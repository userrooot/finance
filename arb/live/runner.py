"""LiveTrader: the paper trading loop with real orders (LiveBroker) and the live safety rails.

Before it will start: the triple live gate passes and every exchange's API key is verified
trade-only. Then each cycle:
- Telegram commands are handled (/status, /pause, /resume, /kill).
- Every live.reconcile_every_s the exchange accounts are compared with the bot's portfolio; a
  mismatch pauses new entries and alerts. Nothing is traded to "fix" it.
- Short perps too close to liquidation have their group closed with taker orders.
- The engine step runs in a worker thread (ccxt calls block), exactly as in paper trading.
State lives in its own directory (data/live/<strategy>), never shared with paper.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from arb.config import Settings
from arb.core.engine import StepResult
from arb.core.types import MarketSnapshot
from arb.costs import CostModel
from arb.execution.live_broker import LiveBroker
from arb.live.safety import check_key_permissions, check_live_gate, liquidation_risks, reconcile
from arb.monitoring.commands import TelegramCommands
from arb.paper.runner import PaperTrader
from arb.strategies.base import Strategy

log = logging.getLogger(__name__)


class LiveTrader(PaperTrader):
    LIVE = True

    def __init__(self, settings: Settings, strategy: Strategy, source, clients: Mapping[str, Any],
                 state_dir: str | Path, notifier=None, commands: TelegramCommands | None = None,
                 env: Mapping[str, str] = os.environ, clock: Callable[[], float] = time.time,
                 sleep=asyncio.sleep, broker: LiveBroker | None = None):
        check_live_gate(settings, env)
        exchanges = sorted({ex for ex, _, _ in strategy.requirements().quotes})
        missing = [ex for ex in exchanges if ex not in clients]
        if missing:
            raise RuntimeError(f"no authenticated client for {missing}")
        for ex in exchanges:
            check_key_permissions(ex, clients[ex])
        self.clients = {ex: clients[ex] for ex in exchanges}
        self.broker = broker or LiveBroker(self.clients, CostModel(settings), settings.live.max_order_usd,
                                           settings.live.order_timeout_s)
        # Live starting equity, exposure caps and loss limits use the money actually funded.
        settings = settings.model_copy(update={
            "capital": settings.capital.model_copy(update={"total_usd": settings.live.capital_usd})})
        super().__init__(settings, strategy, source, state_dir, notifier=notifier, clock=clock, sleep=sleep,
                         broker=self.broker)
        self.commands = commands if commands is not None else TelegramCommands()
        self._last_reconcile = clock()
        self.safety_check()  # startup reconciliation

    # --- safety --------------------------------------------------------------------------
    def _alert(self, msg: str) -> None:
        log.warning(msg)
        self._append("events.jsonl", {"ts": int(self.clock() * 1000), "type": "live", "msg": msg})
        if self.notifier is not None:
            try:
                self.notifier.send(msg)
            except Exception as e:
                log.warning("notifier failed: %s", type(e).__name__)

    def safety_check(self) -> list[str]:
        issues = reconcile(self.clients, self.portfolio, self.settings.live.reconcile_tolerance_pct)
        if issues:
            self.risk.pause(f"reconcile mismatch: {issues[0]}")
            self._alert("RECONCILE MISMATCH, new entries paused:\n" + "\n".join(issues))
            self._persist()
        return issues

    def status_text(self) -> str:
        s = self.risk.state
        state = "KILLED" if s.killed else f"PAUSED ({s.pause_reason})" if s.paused else "running"
        return (f"live bot {state}\nequity {self.portfolio.equity():,.2f} USDT\n"
                f"open groups {len(self.portfolio.open_groups())}, resting orders {len(self.portfolio.open_orders)}\n"
                f"closed trades {len(self.portfolio.closed_trades)}, fees {self.portfolio.fees_paid:.2f}, "
                f"funding {self.portfolio.funding_received:+.2f}")

    def handle_commands(self) -> None:
        for cmd in self.commands.poll():
            if cmd == "/pause":
                self.risk.pause("telegram /pause")
                self._alert("paused by /pause: no new entries; open positions are still managed")
            elif cmd == "/resume":
                self.risk.resume()
                self._alert("resumed by /resume")
            elif cmd == "/kill":
                self.risk.kill("telegram /kill")
                self._alert("KILL by /kill: cancelling all orders and closing all positions")
            elif cmd == "/status":
                self._alert(self.status_text())
        self._persist()

    def _close_liquidation_risks(self, snap: MarketSnapshot) -> None:
        for ex, sym, dist in liquidation_risks(self.clients, self.settings.live.min_liq_distance_pct):
            groups = {p.group_id for p in self.portfolio.positions.values() if p.exchange == ex and p.symbol == sym}
            self._alert(f"{ex} {sym}: liquidation {dist:.1f}% from mark; closing {len(groups)} group(s)")
            for gid in groups:
                self.engine.close_group(gid, snap, tag="liquidation_guard")

    def _drain_broker_events(self) -> None:
        events, self.broker.events = self.broker.events, []
        for e in events:
            self._alert(f"order: {e}")

    # --- loop ----------------------------------------------------------------------------
    async def _step(self, snap: MarketSnapshot) -> StepResult:
        await asyncio.to_thread(self.handle_commands)
        if self.clock() - self._last_reconcile >= self.settings.live.reconcile_every_s:
            self._last_reconcile = self.clock()
            await asyncio.to_thread(self.safety_check)
        await asyncio.to_thread(self._close_liquidation_risks, snap)
        res = await asyncio.to_thread(self.engine.step, snap)
        self._drain_broker_events()
        return res
