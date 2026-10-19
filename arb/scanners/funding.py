"""Funding-rate strategies: spot/perp carry and perp/perp funding spread.

Both are delta-neutral two-leg positions held for `cfg.holding_days` and then closed, so the
cost model charges entry+exit taker fees on both legs (4 fee charges) plus the round-trip
bid/ask spread (walk_book VWAP on both books, assuming prices are unchanged between entry and
exit; real P&L also carries basis/convergence risk).

Capacity: the largest notional in N * {8, 4, 2, 1, 1/2, ..., 1/16} that both fills completely
inside the fetched (20-level) books and keeps net P&L positive after costs.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from arb.config import Settings
from arb.connectors.base import MarketData
from arb.costs import CostModel, walk_book, walk_book_quote
from arb.models import FundingInfo, Leg, MarketType, Opportunity, OrderBook

log = logging.getLogger(__name__)

PERP_LEVERAGE = 2.0  # conservative: liquidation needs a ~50% adverse move on the short perp
BOOK_DEPTH = 20
_CAPACITY_SCALES = (8.0, 4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625)


@dataclass(frozen=True)
class _Venue:
    exchange: str
    symbol: str
    market: MarketType
    book: OrderBook


@dataclass(frozen=True)
class _HedgeCost:
    qty: float
    fees: float
    spread: float
    long_price: float  # entry VWAP of the long leg
    short_price: float  # entry VWAP of the short leg


class _BookCache:
    """Memoises order-book fetches so carry and spread share one request per symbol."""

    def __init__(self, source: MarketData):
        self._source = source
        self._tasks: dict[tuple[str, str], asyncio.Task[OrderBook]] = {}

    def get(self, exchange: str, symbol: str) -> Awaitable[OrderBook]:
        key = (exchange, symbol)
        if key not in self._tasks:
            self._tasks[key] = asyncio.ensure_future(self._source.order_book(exchange, symbol, BOOK_DEPTH))
        return self._tasks[key]


def hedge_cost(costs: CostModel, notional: float, long: _Venue, short: _Venue) -> _HedgeCost | None:
    """Entry+exit fees and round-trip spread for long `notional` of `long`, short same qty of `short`.

    Returns None if either book is too thin to fill the full quantity.
    """
    entry = walk_book_quote(long.book.asks, notional)
    if entry.qty <= 0 or entry.notional < notional * (1 - 1e-9):
        return None
    qty = entry.qty
    long_exit = walk_book(long.book.bids, qty)
    short_entry = walk_book(short.book.bids, qty)
    short_exit = walk_book(short.book.asks, qty)
    if min(long_exit.qty, short_entry.qty, short_exit.qty) < qty * (1 - 1e-9):
        return None
    fees = (
        costs.fee(long.exchange, long.market, entry.notional)
        + costs.fee(long.exchange, long.market, long_exit.notional)
        + costs.fee(short.exchange, short.market, short_entry.notional)
        + costs.fee(short.exchange, short.market, short_exit.notional)
    )
    spread = qty * ((entry.avg_price - long_exit.avg_price) + (short_exit.avg_price - short_entry.avg_price))
    return _HedgeCost(qty, fees, spread, entry.avg_price, short_entry.avg_price)


def _capacity(base_notional: float, net_at: Callable[[float], float | None]) -> float:
    for scale in _CAPACITY_SCALES:
        net = net_at(base_notional * scale)
        if net is not None and net > 0:
            return base_notional * scale
    return 0.0


def _rate(info: FundingInfo) -> float:
    return info.avg_rate_7d if info.avg_rate_7d is not None else info.rate


async def _exchange_funding(
    source: MarketData, exchange: str, settings: Settings
) -> tuple[dict[str, FundingInfo], set[str]]:
    """Return ({base: FundingInfo} for linear perps in the universe, {bases with active spot})."""
    quote = settings.universe.quote
    markets = await source.markets(exchange)
    spots: set[str] = set()
    perps: dict[str, str] = {}
    for base in settings.universe.base_assets:
        spot = markets.get(f"{base}/{quote}")
        if spot and spot.get("type") == "spot" and spot.get("active", True):
            spots.add(base)
        perp = markets.get(f"{base}/{quote}:{quote}")
        if perp and perp.get("type") == "swap" and perp.get("linear") and perp.get("active", True):
            perps[f"{base}/{quote}:{quote}"] = base
    if not perps:
        return {}, spots
    infos = await source.funding(exchange, list(perps))
    return {perps[i.symbol]: i for i in infos if i.symbol in perps}, spots


def _build(
    *,
    strategy: str,
    key: str,
    now_ms: int,
    settings: Settings,
    costs: CostModel,
    notional: float,
    gross: float,
    hc: _HedgeCost,
    legs: list[Leg],
    capital_used: float,
    capacity: float,
    daily_income: float,
    risk_notes: list[str],
    details: dict,
) -> Opportunity | None:
    cfg = settings.scanners.funding
    cost_breakdown = {"fees": hc.fees, "spread": hc.spread}
    net = gross - sum(cost_breakdown.values())
    annualized = net / capital_used * 365 / cfg.holding_days * 100
    if annualized < cfg.min_annualized_net_pct:
        return None
    details = {**details, "breakeven_days": (hc.fees + hc.spread) / daily_income if daily_income > 0 else None,
               "capital_used_usd": capital_used, "leverage": PERP_LEVERAGE}
    return Opportunity(
        strategy=strategy,
        timestamp_ms=now_ms,
        key=key,
        legs=legs,
        notional_usd=notional,
        gross_pnl_usd=gross,
        costs_usd=cost_breakdown,
        net_pnl_usd=net,
        net_after_tax_usd=costs.after_tax(net),
        net_bps=net / notional * 1e4,
        capacity_usd=capacity,
        holding=f"{cfg.holding_days:g}d",
        annualized_pct=annualized,
        risk_notes=risk_notes,
        details=details,
    )


async def _carry_for(
    exchange: str, base: str, info: FundingInfo, books: _BookCache, settings: Settings, costs: CostModel, now_ms: int
) -> Opportunity | None:
    cfg = settings.scanners.funding
    quote = settings.universe.quote
    n = settings.capital.max_per_trade_usd
    rate_val = _rate(info)
    if rate_val <= 0:
        return None  # negative funding would need spot borrowing / shorting
    periods = cfg.holding_days * 24 / info.interval_hours
    gross = n * rate_val * periods
    capital_used = n * (1 + 1 / PERP_LEVERAGE)
    if gross / capital_used * 365 / cfg.holding_days * 100 < cfg.min_annualized_net_pct:
        return None  # costs only make it worse; skip the book fetches
    spot_sym = f"{base}/{quote}"
    spot_book, perp_book = await asyncio.gather(books.get(exchange, spot_sym), books.get(exchange, info.symbol))
    spot = _Venue(exchange, spot_sym, "spot", spot_book)
    perp = _Venue(exchange, info.symbol, "swap", perp_book)

    def net_at(notional: float) -> float | None:
        hc = hedge_cost(costs, notional, spot, perp)
        if hc is None:
            return None
        return notional * rate_val * periods - hc.fees - hc.spread

    hc = hedge_cost(costs, n, spot, perp)
    if hc is None:
        return None
    return _build(
        strategy="funding_carry",
        key=f"{exchange} {base}",
        now_ms=now_ms, settings=settings, costs=costs, notional=n, gross=gross, hc=hc,
        legs=[Leg(exchange, spot_sym, "spot", "buy", hc.long_price, hc.qty),
              Leg(exchange, info.symbol, "swap", "sell", hc.short_price, hc.qty)],
        capital_used=capital_used,
        capacity=_capacity(n, net_at),
        daily_income=n * rate_val * 24 / info.interval_hours,
        risk_notes=[
            "funding can turn negative; the carry then costs money until the position is closed",
            f"short perp liquidation if price spikes; keep leverage <= {PERP_LEVERAGE:g}x and margin topped up",
            "exchange counterparty risk (funds held on one venue)",
            "round-trip spread assumes unchanged prices; entry/exit basis moves add risk",
        ],
        details={"rate": info.rate, "avg_rate_7d": info.avg_rate_7d, "rate_used": rate_val,
                 "interval_hours": info.interval_hours, "periods": periods},
    )


async def _spread_for(
    base: str,
    low: FundingInfo,
    high: FundingInfo,
    books: _BookCache,
    settings: Settings,
    costs: CostModel,
    now_ms: int,
) -> Opportunity | None:
    cfg = settings.scanners.funding
    n = settings.capital.max_per_trade_usd
    h_low = _rate(low) / low.interval_hours
    h_high = _rate(high) / high.interval_hours
    hours = cfg.holding_days * 24
    gross = n * (h_high - h_low) * hours
    if gross <= 0:
        return None
    capital_used = 2 * n / PERP_LEVERAGE
    if gross / capital_used * 365 / cfg.holding_days * 100 < cfg.min_annualized_net_pct:
        return None
    long_book, short_book = await asyncio.gather(
        books.get(low.exchange, low.symbol), books.get(high.exchange, high.symbol)
    )
    long = _Venue(low.exchange, low.symbol, "swap", long_book)
    short = _Venue(high.exchange, high.symbol, "swap", short_book)

    def net_at(notional: float) -> float | None:
        hc = hedge_cost(costs, notional, long, short)
        if hc is None:
            return None
        return notional * (h_high - h_low) * hours - hc.fees - hc.spread

    hc = hedge_cost(costs, n, long, short)
    if hc is None:
        return None
    return _build(
        strategy="funding_spread",
        key=f"{base} long {low.exchange} short {high.exchange}",
        now_ms=now_ms, settings=settings, costs=costs, notional=n, gross=gross, hc=hc,
        legs=[Leg(low.exchange, low.symbol, "swap", "buy", hc.long_price, hc.qty),
              Leg(high.exchange, high.symbol, "swap", "sell", hc.short_price, hc.qty)],
        capital_used=capital_used,
        capacity=_capacity(n, net_at),
        daily_income=n * (h_high - h_low) * 24,
        risk_notes=[
            "funding differential can shrink or flip before the holding period ends",
            f"margin needed on two exchanges (leverage {PERP_LEVERAGE:g}x); either leg can be liquidated on a spike",
            "rebalancing margin between exchanges if one side loses (withdrawal fees and delay)",
            "exchange counterparty risk on both venues",
            "round-trip spread assumes unchanged prices; cross-exchange basis moves add risk",
        ],
        details={
            "hourly_rate_long": h_low, "hourly_rate_short": h_high,
            "rate_long": low.rate, "avg_rate_7d_long": low.avg_rate_7d, "interval_hours_long": low.interval_hours,
            "rate_short": high.rate, "avg_rate_7d_short": high.avg_rate_7d, "interval_hours_short": high.interval_hours,
        },
    )


async def scan(source: MarketData, settings: Settings, costs: CostModel, now_ms: int) -> list[Opportunity]:
    """Scan for funding_carry and funding_spread opportunities, net of all costs."""
    if not settings.scanners.funding.enabled:
        return []
    exchanges = [e for e in source.exchanges if e in settings.exchanges]
    results = await asyncio.gather(
        *(_exchange_funding(source, ex, settings) for ex in exchanges), return_exceptions=True
    )
    funding: dict[str, dict[str, FundingInfo]] = {}
    spots: dict[str, set[str]] = {}
    for ex, res in zip(exchanges, results):
        if isinstance(res, BaseException):
            log.warning("funding data failed on %s: %s", ex, res)
            continue
        funding[ex], spots[ex] = res

    books = _BookCache(source)
    tasks: list[Awaitable[Opportunity | None]] = []
    for ex, by_base in funding.items():
        for base, info in by_base.items():
            if base in spots[ex]:
                tasks.append(_carry_for(ex, base, info, books, settings, costs, now_ms))
    by_base_all: dict[str, list[FundingInfo]] = {}
    for by_base in funding.values():
        for base, info in by_base.items():
            by_base_all.setdefault(base, []).append(info)
    for base, infos in by_base_all.items():
        for low in infos:
            for high in infos:
                if low.exchange != high.exchange and _rate(high) / high.interval_hours > _rate(low) / low.interval_hours:
                    tasks.append(_spread_for(base, low, high, books, settings, costs, now_ms))

    out: list[Opportunity] = []
    for res in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(res, BaseException):
            log.warning("funding scan skipped a candidate: %s", res)
        elif res is not None:
            out.append(res)
    return out
