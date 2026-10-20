"""Strategy interface. A strategy only turns market snapshots into order intents.

Strategies must derive "what is open" from the Portfolio (group ids) rather than private
flags, so a paper/live process can restart and resume from persisted portfolio state.
Any rolling history a strategy keeps must use only data up to the current snapshot.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from arb.core.portfolio import Portfolio
from arb.core.types import Fill, MarketSnapshot, OrderIntent, Requirements


class Strategy(ABC):
    name: str = "strategy"

    @abstractmethod
    def requirements(self) -> Requirements: ...

    @abstractmethod
    def on_snapshot(self, snapshot: MarketSnapshot, portfolio: Portfolio) -> list[OrderIntent]: ...

    def on_fills(self, fills: list[Fill]) -> None:
        pass

    def state_dict(self) -> dict:
        return {}

    def load_state(self, state: dict) -> None:
        pass
