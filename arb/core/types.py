"""Types shared by the trading engine in backtest, paper and (later) live mode."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

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
    low: float | None = None  # traded range since the previous snapshot (backtest bars)
    high: float | None = None

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
    # exec_style "maker_first": the leg with maker=True rests as a post-only limit at the touch;
    # the group's other leg is sent as a taker hedge for exactly the qty the maker leg fills.
    exec_style: Literal["taker", "maker_first"] = "taker"
    maker: bool = False


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
    maker: bool = False  # filled as a resting post-only order (maker fee, no spread)
    # live only: the order reached (or may have reached) the exchange but its outcome could not be
    # read back. qty is 0 but NOT a confirmed zero fill: the engine records it as unresolved
    # (client_order_id), pauses entries and never unwinds/hedges against it until it is resolved.
    unknown: bool = False
    client_order_id: str | None = None

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "buy" else -self.qty


@dataclass
class RestingOrder:
    """A post-only limit order (the maker leg of a maker_first group) waiting for a fill."""

    order_id: str
    group_id: str
    exchange: str
    symbol: str
    market: MarketType
    side: Side
    qty: float  # remaining base units
    price: float
    created_ms: int  # first placement; timeouts count from here
    placed_ms: int  # last (re)placement
    reduce_only: bool
    tag: str
    hedge: dict  # the other leg: exchange, symbol, market, side, reduce_only
    exchange_order_id: str | None = None  # live only: the exchange's id for the current placement
    filled_on_exchange: float = 0.0  # live only: cumulative filled qty seen on the exchange order
    # live only: client id of a placement whose outcome is unknown (create + lookups all failed);
    # the broker resolves it by client id before it will place, re-place or forget the order
    pending_client_id: str | None = None
    # live only: consecutive cancels that did not confirm the order is gone; while > 0 the order
    # may still be live on the exchange, so the engine keeps it and retries the cancel
    cancel_failures: int = 0
    done: bool = False  # live only: the exchange reports the order fully filled (or the rest is dust)


@dataclass(frozen=True)
class Requirements:
    """What market data a strategy needs each step."""

    quotes: list[tuple[str, str, MarketType]] = field(default_factory=list)  # (exchange, symbol, market)
    funding: list[Key] = field(default_factory=list)  # perp symbols
