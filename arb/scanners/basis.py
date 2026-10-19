"""Cash-and-carry on dated futures: long spot + short future held to expiry.

The future converges to spot at expiry, so the entry basis is locked in; the remaining
uncertainty is mark-to-market margin on the short future in the meantime. Only contango
(future bid > spot ask) is traded, backwardation would need spot shorting.

Capacity: the largest notional in N * {8, 4, 2, 1, 1/2, ..., 1/16} that fills completely in the
fetched 20-level books and keeps net P&L positive after costs.
"""
from __future__ import annotations

import asyncio
import logging

from arb.config import Settings
from arb.connectors.base import MarketData
from arb.costs import CostModel, walk_book, walk_book_quote
from arb.models import Leg, Opportunity

log = logging.getLogger(__name__)

PERP_LEVERAGE = 2.0  # margin on the short future; conservative to avoid liquidation
BOOK_DEPTH = 20
MIN_DAYS_TO_EXPIRY = 2.0
DAY_MS = 86_400_000
_CAPACITY_SCALES = (8.0, 4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625)


def _evaluate(costs: CostModel, exchange: str, spot_book, fut_book, notional: float):
    """Return (qty, spot_ask_vwap, fut_bid_vwap, gross, fees, exit_spread) or None if not fully fillable."""
    buy = walk_book_quote(spot_book.asks, notional)
    if buy.qty <= 0 or buy.notional < notional * (1 - 1e-9):
        return None
    qty = buy.qty
    short = walk_book(fut_book.bids, qty)
    exit_fill = walk_book(spot_book.bids, qty)
    if short.qty < qty * (1 - 1e-9) or exit_fill.qty < qty * (1 - 1e-9):
        return None
    gross = qty * (short.avg_price - buy.avg_price)
    fees = (
        costs.fee(exchange, "spot", buy.notional)  # spot buy at entry
        + costs.fee(exchange, "future", short.notional)  # future sell at entry
        + costs.fee(exchange, "spot", exit_fill.notional)  # spot sell at expiry
        + costs.fee(exchange, "future", short.notional)  # settlement fee, assumed taker
    )
    exit_spread = qty * (spot_book.asks[0][0] - spot_book.bids[0][0]) / 2
    return qty, buy.avg_price, short.avg_price, gross, fees, exit_spread


async def _scan_future(
    source: MarketData, settings: Settings, costs: CostModel, now_ms: int,
    exchange: str, base: str, spot_sym: str, fut: dict,
) -> Opportunity | None:
    cfg = settings.scanners.basis
    n = settings.capital.max_per_trade_usd
    days = (fut["expiry"] - now_ms) / DAY_MS
    spot_book, fut_book = await asyncio.gather(
        source.order_book(exchange, spot_sym, BOOK_DEPTH),
        source.order_book(exchange, fut["symbol"], BOOK_DEPTH),
    )
    if not spot_book.asks or not spot_book.bids or not fut_book.bids:
        return None
    if fut_book.bids[0][0] <= spot_book.asks[0][0]:
        return None  # backwardation / no contango

    ev = _evaluate(costs, exchange, spot_book, fut_book, n)
    if ev is None:
        return None
    qty, spot_px, fut_px, gross, fees, exit_spread = ev
    cost_breakdown = {"fees": fees, "exit_spread": exit_spread}
    net = gross - sum(cost_breakdown.values())
    capital_used = n * (1 + 1 / PERP_LEVERAGE)
    annualized = net / capital_used * 365 / days * 100
    if annualized < cfg.min_annualized_net_pct:
        return None

    def net_at(notional: float) -> float | None:
        e = _evaluate(costs, exchange, spot_book, fut_book, notional)
        return None if e is None else e[3] - e[4] - e[5]

    capacity = 0.0
    for scale in _CAPACITY_SCALES:
        v = net_at(n * scale)
        if v is not None and v > 0:
            capacity = n * scale
            break

    return Opportunity(
        strategy="basis",
        timestamp_ms=now_ms,
        key=f"{exchange} {fut['symbol']}",
        legs=[Leg(exchange, spot_sym, "spot", "buy", spot_px, qty),
              Leg(exchange, fut["symbol"], "future", "sell", fut_px, qty)],
        notional_usd=n,
        gross_pnl_usd=gross,
        costs_usd=cost_breakdown,
        net_pnl_usd=net,
        net_after_tax_usd=costs.after_tax(net),
        net_bps=net / n * 1e4,
        capacity_usd=capacity,
        holding=f"{days:.0f}d to expiry",
        annualized_pct=annualized,
        risk_notes=[
            "backwardation trades are skipped (would need spot shorting)",
            f"margin calls on the short future before expiry (leverage {PERP_LEVERAGE:g}x); keep margin topped up",
            "exchange counterparty risk (funds held on one venue)",
            "convergence is locked only if held to expiry; early exit exposes basis risk",
        ],
        details={"days_to_expiry": days, "expiry_ms": fut["expiry"], "basis_pct": (fut_px / spot_px - 1) * 100,
                 "capital_used_usd": capital_used, "leverage": PERP_LEVERAGE},
    )


async def _scan_exchange(
    source: MarketData, settings: Settings, costs: CostModel, now_ms: int, exchange: str
) -> list[Opportunity | BaseException | None]:
    quote = settings.universe.quote
    universe = set(settings.universe.base_assets)
    markets = await source.markets(exchange)
    cutoff = now_ms + MIN_DAYS_TO_EXPIRY * DAY_MS
    tasks = []
    for m in markets.values():
        if m.get("type") != "future" or m.get("expiry") is None or m["expiry"] <= cutoff:
            continue
        if not m.get("active", True) or not m.get("linear") or m.get("base") not in universe or m.get("quote") != quote:
            continue
        spot = markets.get(f"{m['base']}/{quote}")
        if not spot or spot.get("type") != "spot" or not spot.get("active", True):
            continue
        tasks.append(_scan_future(source, settings, costs, now_ms, exchange, m["base"], spot["symbol"], m))
    return list(await asyncio.gather(*tasks, return_exceptions=True))


async def scan(source: MarketData, settings: Settings, costs: CostModel, now_ms: int) -> list[Opportunity]:
    """Scan dated futures for contango cash-and-carry opportunities, net of all costs."""
    if not settings.scanners.basis.enabled:
        return []
    exchanges = [e for e in source.exchanges if e in settings.exchanges]
    per_ex = await asyncio.gather(
        *(_scan_exchange(source, settings, costs, now_ms, ex) for ex in exchanges), return_exceptions=True
    )
    out: list[Opportunity] = []
    for ex, res in zip(exchanges, per_ex):
        if isinstance(res, BaseException):
            log.warning("basis scan failed on %s: %s", ex, res)
            continue
        for item in res:
            if isinstance(item, BaseException):
                log.warning("basis scan skipped a future on %s: %s", ex, item)
            elif item is not None:
                out.append(item)
    return out
