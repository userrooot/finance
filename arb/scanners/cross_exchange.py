"""Cross-exchange spot arbitrage: buy where the ask is low, sell where the bid is high.

Costs are taker fees on both legs, order-book slippage vs top of book, and the withdrawal cost of
moving inventory back (both base and quote) so the pre-funded balances stay balanced.
"""
from __future__ import annotations

import asyncio
import logging

from arb.config import Settings
from arb.connectors.base import MarketData
from arb.costs import CostModel, crossing_capacity, walk_book
from arb.models import Leg, Opportunity, OrderBook

log = logging.getLogger(__name__)

DEPTH = 20
STALE_MS = 2000


async def _spot_symbol_exchanges(source: MarketData, symbol: str) -> list[str]:
    out = []
    for ex in source.exchanges:
        try:
            m = (await source.markets(ex)).get(symbol)
        except Exception as e:  # noqa: BLE001 - one exchange failing must not stop the scan
            log.warning("markets failed on %s: %s", ex, e)
            continue
        if m and m.get("type") == "spot" and m.get("active"):
            out.append(ex)
    return out


async def _fetch_book(source: MarketData, ex: str, symbol: str) -> OrderBook | None:
    try:
        return await source.order_book(ex, symbol, DEPTH)
    except Exception as e:  # noqa: BLE001
        log.warning("order_book failed for %s on %s: %s", symbol, ex, e)
        return None


def _evaluate(symbol: str, base: str, quote: str, buy: OrderBook, sell: OrderBook, settings: Settings,
              costs: CostModel, now_ms: int) -> Opportunity | None:
    cfg = settings.scanners.cross_exchange
    if not buy.asks or not sell.bids:
        return None
    buy_fee = costs.fee_rate(buy.exchange, "spot")
    sell_fee = costs.fee_rate(sell.exchange, "spot")
    qty, _ = crossing_capacity(buy.asks, sell.bids, buy_fee, sell_fee)
    if qty <= 0:
        return None

    ask, bid = buy.asks[0][0], sell.bids[0][0]
    capacity_usd = qty * ask
    trade_qty = min(qty, settings.capital.max_per_trade_usd / ask)
    buy_fill = walk_book(buy.asks, trade_qty)
    sell_fill = walk_book(sell.bids, trade_qty)

    gross = trade_qty * (bid - ask)  # top-of-book edge; depth cost is booked as slippage below
    breakdown = {
        "fees": buy_fill.notional * buy_fee + sell_fill.notional * sell_fee,
        "slippage": CostModel.slippage(buy_fill, ask) + CostModel.slippage(sell_fill, bid),
        "rebalance": costs.withdrawal_fee_usd(base, ask) + costs.withdrawal_fee_usd(quote, 1.0),
    }
    net_before_rebalance = gross - breakdown["fees"] - breakdown["slippage"]
    net = net_before_rebalance - breakdown["rebalance"]
    notional = buy_fill.notional
    net_bps = net / notional * 1e4
    if net_bps < cfg.min_net_bps:
        return None

    notes = [
        "requires pre-funded inventory on both exchanges (no transfer during the trade)",
        "leg risk: one leg may not fill, leaving a directional position",
    ]
    if abs(buy.timestamp_ms - sell.timestamp_ms) > STALE_MS:
        notes.append(f"stale book risk: order book timestamps differ by {abs(buy.timestamp_ms - sell.timestamp_ms)} ms")
    return Opportunity(
        strategy="cross_exchange",
        timestamp_ms=now_ms,
        key=f"{symbol} {buy.exchange}->{sell.exchange}",
        legs=[Leg(buy.exchange, symbol, "spot", "buy", buy_fill.avg_price, trade_qty),
              Leg(sell.exchange, symbol, "spot", "sell", sell_fill.avg_price, trade_qty)],
        notional_usd=notional,
        gross_pnl_usd=gross,
        costs_usd=breakdown,
        net_pnl_usd=net,
        net_after_tax_usd=costs.after_tax(net),
        net_bps=net_bps,
        capacity_usd=capacity_usd,
        holding="instant",
        risk_notes=notes,
        details={"net_before_rebalance_usd": net_before_rebalance, "buy_exchange": buy.exchange,
                 "sell_exchange": sell.exchange, "buy_ask": ask, "sell_bid": bid},
    )


async def scan(source: MarketData, settings: Settings, costs: CostModel, now_ms: int) -> list[Opportunity]:
    if not settings.scanners.cross_exchange.enabled:
        return []
    quote = settings.universe.quote

    async def books_for(base: str) -> tuple[str, str, list[OrderBook]]:
        symbol = f"{base}/{quote}"
        exchanges = await _spot_symbol_exchanges(source, symbol)
        books = await asyncio.gather(*(_fetch_book(source, ex, symbol) for ex in exchanges))
        return base, symbol, [b for b in books if b is not None]

    results = await asyncio.gather(*(books_for(b) for b in settings.universe.base_assets),
                                   return_exceptions=True)
    opps: list[Opportunity] = []
    for res in results:
        if isinstance(res, BaseException):
            log.warning("cross_exchange symbol scan failed: %s", res)
            continue
        base, symbol, books = res
        for buy in books:
            for sell in books:
                if buy.exchange == sell.exchange:
                    continue
                try:
                    opp = _evaluate(symbol, base, quote, buy, sell, settings, costs, now_ms)
                except Exception as e:  # noqa: BLE001
                    log.warning("evaluate failed for %s %s->%s: %s", symbol, buy.exchange, sell.exchange, e)
                    continue
                if opp:
                    opps.append(opp)
    return opps
