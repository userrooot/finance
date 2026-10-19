"""Core data types shared by connectors, scanners and storage."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

Side = Literal["buy", "sell"]
MarketType = Literal["spot", "swap", "future"]

# (price, quantity in base units), best level first
Level = tuple[float, float]


@dataclass(frozen=True)
class OrderBook:
    exchange: str
    symbol: str  # ccxt unified symbol, e.g. "BTC/USDT" or "BTC/USDT:USDT"
    bids: list[Level]  # descending price
    asks: list[Level]  # ascending price
    timestamp_ms: int

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None


@dataclass(frozen=True)
class FundingInfo:
    exchange: str
    symbol: str  # perp symbol, e.g. "BTC/USDT:USDT"
    rate: float  # current/next funding rate per interval, as a fraction
    interval_hours: float  # usually 8 (some contracts 4 or 1)
    mark_price: float
    index_price: float | None
    next_funding_ms: int | None
    avg_rate_7d: float | None = None  # mean per-interval rate over the last 7 days, if known


@dataclass(frozen=True)
class Leg:
    exchange: str
    symbol: str
    market: MarketType
    side: Side
    price: float  # expected average fill price (VWAP over book depth)
    qty: float  # base units


@dataclass
class Opportunity:
    strategy: str  # cross_exchange / triangular / funding_carry / funding_spread / basis / stat_arb
    timestamp_ms: int
    key: str  # stable id for counting recurrence, e.g. "BTC/USDT binance->okx"
    legs: list[Leg]
    notional_usd: float
    gross_pnl_usd: float
    costs_usd: dict[str, float]  # breakdown: fees, slippage, withdrawal, ...
    net_pnl_usd: float  # after costs, before tax
    net_after_tax_usd: float
    net_bps: float  # net_pnl_usd / notional_usd in basis points
    capacity_usd: float  # max notional at which the edge stays positive, given book depth
    holding: str  # "instant", "7d", "until z-score reverts", ...
    annualized_pct: float | None = None
    risk_notes: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        rec = asdict(self)
        rec["legs"] = [asdict(l) for l in self.legs]
        return rec
