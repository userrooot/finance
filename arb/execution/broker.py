"""Order execution. SimBroker fills taker orders against the snapshot, used by backtest and paper.

Fill model:
- With a full order book on the quote (paper): VWAP over the book, partial fill when it is thin.
- Without one (backtest bars): top of book (bid/ask already include the modelled spread),
  limited by bid_size/ask_size.
Plus `slippage_bps` adverse to the order, and taker fees from the CostModel.

Resting post-only orders (maker legs) are priced at the touch and fill only when the market
trades `through_bps` beyond their price, a conservative stand-in for queue position: touching
the price is not enough. The traded range is the quote's low/high when given (backtest bars),
else the opposite side of the book crossing through (paper). Maker fills pay the maker fee
and no spread or slippage.
"""
from __future__ import annotations

from typing import Protocol

from arb.core.types import Fill, MarketSnapshot, OrderIntent, Quote, RestingOrder
from arb.costs import CostModel, walk_book


class Broker(Protocol):
    def execute(self, intents: list[OrderIntent], snapshot: MarketSnapshot) -> list[Fill]: ...

    def maker_price(self, side: str, quote: Quote) -> float: ...

    def check_resting(self, order: RestingOrder, snapshot: MarketSnapshot) -> Fill | None: ...

    def place_resting(self, order: RestingOrder) -> None: ...

    def replace_resting(self, order: RestingOrder, new_price: float) -> Fill | None:
        """Move a resting order to new_price; returns any fill that happened before the cancel."""

    def cancel_resting(self, order: RestingOrder) -> Fill | None:
        """Cancel a resting order; returns any fill that raced the cancel. A live broker leaves
        order.cancel_failures > 0 when the cancel is not confirmed (the engine then keeps the order)."""

    # Live only (the engine calls it only for Fill.unknown records, which SimBroker never returns):
    # def resolve_order(self, u: dict, snapshot: MarketSnapshot) -> Fill | None


class SimBroker:
    def __init__(self, costs: CostModel, slippage_bps: float = 0.0, through_bps: float = 1.0):
        self.costs = costs
        self.slippage = slippage_bps / 1e4
        self.through = through_bps / 1e4

    # Simulated resting orders live only in the portfolio, so placing, moving and cancelling
    # them needs no exchange call.
    def place_resting(self, order: RestingOrder) -> None:
        pass

    def replace_resting(self, order: RestingOrder, new_price: float) -> Fill | None:
        return None

    def cancel_resting(self, order: RestingOrder) -> Fill | None:
        return None

    @staticmethod
    def maker_price(side: str, quote: Quote) -> float:
        """Post-only price at the touch: join the bid to buy, the ask to sell (never crosses)."""
        return quote.bid if side == "buy" else quote.ask

    def check_resting(self, order: RestingOrder, snapshot: MarketSnapshot) -> Fill | None:
        q = snapshot.quote(order.exchange, order.symbol)
        if q is None or order.qty <= 0:
            return None
        if order.side == "buy":
            low = q.low if q.low is not None else q.ask
            filled = low <= order.price * (1 - self.through)
        else:
            high = q.high if q.high is not None else q.bid
            filled = high >= order.price * (1 + self.through)
        if not filled:
            return None
        fee = self.costs.fee(order.exchange, order.market, order.qty * order.price, taker=False)
        return Fill(order.group_id, order.exchange, order.symbol, order.market, order.side, order.qty,
                    order.price, fee, snapshot.ts_ms, requested_qty=order.qty,
                    reduce_only=order.reduce_only, tag=order.tag, maker=True)

    def execute(self, intents: list[OrderIntent], snapshot: MarketSnapshot) -> list[Fill]:
        return [self._fill(i, snapshot) for i in intents]

    def _fill(self, i: OrderIntent, snapshot: MarketSnapshot) -> Fill:
        q = snapshot.quote(i.exchange, i.symbol)
        qty, price = 0.0, 0.0
        if q is not None:
            buy = i.side == "buy"
            if q.book is not None:
                levels = q.book.asks if buy else q.book.bids
            else:
                levels = [(q.ask, q.ask_size)] if buy else [(q.bid, q.bid_size)]
            f = walk_book(levels, i.qty)
            qty = f.qty
            price = f.avg_price * (1 + self.slippage if buy else 1 - self.slippage)
        fee = self.costs.fee(i.exchange, i.market, qty * price) if qty > 0 else 0.0
        return Fill(i.group_id, i.exchange, i.symbol, i.market, i.side, qty, price, fee, snapshot.ts_ms,
                    requested_qty=i.qty, reduce_only=i.reduce_only, tag=i.tag)
