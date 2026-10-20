"""Read-only market data interface. Scanners depend only on this protocol."""
from __future__ import annotations

from typing import Protocol

from arb.models import FundingInfo, OrderBook


class MarketData(Protocol):
    exchanges: list[str]

    async def markets(self, exchange: str) -> dict[str, dict]:
        """ccxt-style markets dict keyed by unified symbol.

        Each value has at least: symbol, base, quote, type ('spot'|'swap'|'future'),
        active, linear, settle, expiry (ms or None), contractSize.
        """

    async def order_book(self, exchange: str, symbol: str, depth: int = 20) -> OrderBook: ...

    async def tickers(self, exchange: str, symbols: list[str] | None = None) -> dict[str, dict]:
        """ccxt-style tickers keyed by symbol; each has 'bid', 'ask', 'bidVolume', 'askVolume', 'last'."""

    async def funding(self, exchange: str, symbols: list[str]) -> list[FundingInfo]: ...

    async def funding_history(self, exchange: str, symbol: str, since_ms: int) -> list[tuple[int, float]]:
        """Settled funding rates [(timestamp_ms, rate), ...] since `since_ms`, oldest first."""

    async def ohlcv(self, exchange: str, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        """[[timestamp_ms, open, high, low, close, volume], ...] oldest first."""

    async def close(self) -> None: ...
