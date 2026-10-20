"""Types shared by the trading engine in backtest, paper and (later) live mode."""
from __future__ import annotations

from dataclasses import dataclass, field

from arb.models import MarketType, OrderBook, Side

Key = tuple[str, str]  # (exchange, symbol)


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ts_ms: int
    bid_size: float = float("inf")  # base units available at the top level
    ask_size: float = float("inf")
    book: OrderBook | None = None  # full depth when available (paper/live)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class FundingQuote:
    rate: float  # current/next rate per interval, fraction
    interval_hours: float
    next_funding_ms: int | None = None


@dataclass(frozen=True)
class FundingEvent:
    """A funding settlement that happened since the previous snapshot."""

    ts_ms: int
    exchange: str
    symbol: str
    rate: float


@dataclass
class MarketSnapshot:
    ts_ms: int
    quotes: dict[Key, Quote] = field(default_factory=dict)
    funding: dict[Key, FundingQuote] = field(default_factory=dict)
    funding_events: list[FundingEvent] = field(default_factory=list)

    def quote(self, exchange: str, symbol: str) -> Quote | None:
        return self.quotes.get((exchange, symbol))


@dataclass(frozen=True)
class OrderIntent:
    group_id: str  # all legs of one hedged position share a group id
    exchange: str
    symbol: str
    market: MarketType
    side: Side
    qty: float  # base units, > 0
    reduce_only: bool = False
    tag: str = ""


@dataclass(frozen=True)
class Fill:
    group_id: str
    exchange: str
    symbol: str
    market: MarketType
    side: Side
    qty: float  # base units actually filled (may be < requested)
    price: float  # average fill price
    fee: float  # quote currency
    ts_ms: int
    requested_qty: float
    reduce_only: bool = False
    tag: str = ""

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass(frozen=True)
class Requirements:
    """What market data a strategy needs each step."""

    quotes: list[tuple[str, str, MarketType]] = field(default_factory=list)  # (exchange, symbol, market)
    funding: list[Key] = field(default_factory=list)  # perp symbols
