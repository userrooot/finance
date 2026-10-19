"""Triangular arbitrage inside one exchange: Q -> A -> B -> Q over spot markets.

A cheap ticker pre-filter finds candidate cycles; candidates are then simulated against full
order books (VWAP fills, taker fees) at a few notionals, keeping the largest still above threshold.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from arb.config import Settings
from arb.connectors.base import MarketData
from arb.costs import CostModel, walk_book, walk_book_quote
from arb.models import Leg, Opportunity, OrderBook, Side

log = logging.getLogger(__name__)

DEPTH = 20
NOTIONAL_FRACTIONS = [1, 1 / 2, 1 / 4, 1 / 8, 1 / 16]
EPS = 1e-9


@dataclass(frozen=True)
class Cycle:
    exchange: str
    assets: tuple[str, str, str]  # (Q, A, B)
    steps: tuple[tuple[str, Side], ...]  # (symbol, side) per leg, in order


@dataclass
class Sim:
    final_q: float
    fees_q: float
    slippage_q: float
    legs: list[Leg]


def _cycles(exchange: str, markets: dict[str, dict], quote: str) -> list[Cycle]:
    spot = {s for s, m in markets.items() if m.get("type") == "spot" and m.get("active")}
    bases = sorted({s.split("/")[0] for s in spot if s.endswith(f"/{quote}")})
    out = []
    for a in bases:
        for b in bases:
            if a == b:
                continue
            if f"{a}/{b}" in spot:  # A/B exists: sell A for B
                mid: tuple[str, Side] = (f"{a}/{b}", "sell")
            elif f"{b}/{a}" in spot:  # B/A exists: buy B with A
                mid = (f"{b}/{a}", "buy")
            else:
                continue
            out.append(Cycle(exchange, (quote, a, b),
                             ((f"{a}/{quote}", "buy"), mid, (f"{b}/{quote}", "sell"))))
    return out


def _rate(cycle: Cycle, tickers: dict[str, dict], fee: float) -> float | None:
    """Fee-adjusted top-of-book product of conversion rates around the cycle."""
    rate = 1.0
    for symbol, side in cycle.steps:
        t = tickers.get(symbol)
        px = t and (t.get("ask") if side == "buy" else t.get("bid"))
        if not px or px <= 0:
            return None
        rate *= (1 / px if side == "buy" else px) * (1 - fee)
    return rate


def _simulate(cycle: Cycle, books: dict[str, OrderBook], fee: float, notional: float) -> Sim | None:
    quote = cycle.assets[0]
    amt, value_q, fees_q, slip_q = notional, notional, 0.0, 0.0
    legs: list[Leg] = []
    for symbol, side in cycle.steps:
        book = books[symbol]
        levels = book.asks if side == "buy" else book.bids
        if not levels:
            return None
        top = levels[0][0]
        if side == "buy":  # spend `amt` of the market's quote asset
            fill = walk_book_quote(levels, amt)
            if fill.notional < amt * (1 - EPS):
                return None
            slip, got = (fill.avg_price - top) * fill.qty, fill.qty * (1 - fee)
        else:  # sell `amt` of the market's base asset
            fill = walk_book(levels, amt)
            if fill.qty < amt * (1 - EPS):
                return None
            slip, got = (top - fill.avg_price) * fill.qty, fill.notional * (1 - fee)
        market_quote = symbol.split("/")[1]
        if market_quote != quote:  # convert slippage into Q at the top-of-book price of that asset
            q_book = books[f"{market_quote}/{quote}"]
            if not q_book.asks:
                return None
            slip *= q_book.asks[0][0]
        slip_q += slip
        fees_q += value_q * fee
        value_q *= 1 - fee
        legs.append(Leg(book.exchange, symbol, "spot", side, fill.avg_price, fill.qty))
        amt = got
    return Sim(amt, fees_q, slip_q, legs)


async def _fetch(source: MarketData, ex: str, symbol: str) -> OrderBook:
    return await source.order_book(ex, symbol, DEPTH)


async def _evaluate(source: MarketData, cycle: Cycle, settings: Settings, costs: CostModel,
                    now_ms: int) -> Opportunity | None:
    cfg = settings.scanners.triangular
    ex, (quote, a, b) = cycle.exchange, cycle.assets
    try:
        symbols = [s for s, _ in cycle.steps]
        fetched = await asyncio.gather(*(_fetch(source, ex, s) for s in symbols))
        books = dict(zip(symbols, fetched))
        # slippage of a cross-market leg is converted via the quote asset's /Q book
        for s in symbols:
            q = s.split("/")[1]
            if q != quote and f"{q}/{quote}" not in books:
                books[f"{q}/{quote}"] = await _fetch(source, ex, f"{q}/{quote}")
        fee = costs.fee_rate(ex, "spot")
        for frac in NOTIONAL_FRACTIONS:
            notional = settings.capital.max_per_trade_usd * frac
            sim = _simulate(cycle, books, fee, notional)
            if sim is None:
                continue
            net = sim.final_q - notional
            net_bps = net / notional * 1e4
            if net_bps < cfg.min_net_bps:
                continue
            return Opportunity(
                strategy="triangular",
                timestamp_ms=now_ms,
                key=f"{ex} {quote}->{a}->{b}->{quote}",
                legs=sim.legs,
                notional_usd=notional,
                gross_pnl_usd=net + sim.fees_q + sim.slippage_q,
                costs_usd={"fees": sim.fees_q, "slippage": sim.slippage_q},
                net_pnl_usd=net,
                net_after_tax_usd=costs.after_tax(net),
                net_bps=net_bps,
                capacity_usd=notional,
                holding="instant",
                risk_notes=["3-leg execution risk: a failed or partial leg leaves an unhedged intermediate asset",
                            "latency: triangular gaps are normally closed within milliseconds by HFT competition"],
                details={"exchange": ex, "path": [quote, a, b, quote]},
            )
    except Exception as e:  # noqa: BLE001 - one bad cycle must not crash the scan
        log.warning("triangular %s %s->%s->%s failed: %s", ex, quote, a, b, e)
    return None


async def _scan_exchange(source: MarketData, ex: str, settings: Settings, costs: CostModel,
                         now_ms: int) -> list[Opportunity]:
    cfg = settings.scanners.triangular
    try:
        cycles = _cycles(ex, await source.markets(ex), settings.universe.quote)
        if not cycles:
            return []
        tickers = await source.tickers(ex)
    except Exception as e:  # noqa: BLE001
        log.warning("triangular scan failed on %s: %s", ex, e)
        return []
    fee = costs.fee_rate(ex, "spot")
    threshold = 1 + cfg.min_net_bps / 1e4
    candidates = [c for c in cycles if (r := _rate(c, tickers, fee)) is not None and r > threshold]
    results = await asyncio.gather(*(_evaluate(source, c, settings, costs, now_ms) for c in candidates))
    return [o for o in results if o]


async def scan(source: MarketData, settings: Settings, costs: CostModel, now_ms: int) -> list[Opportunity]:
    cfg = settings.scanners.triangular
    if not cfg.enabled:
        return []
    exchanges = [e for e in cfg.exchanges if e in source.exchanges]
    results = await asyncio.gather(*(_scan_exchange(source, e, settings, costs, now_ms) for e in exchanges))
    return [o for r in results for o in r]
