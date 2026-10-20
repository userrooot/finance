"""Order execution. SimBroker fills taker orders against the snapshot, used by backtest and paper.

Fill model:
- With a full order book on the quote (paper): VWAP over the book, partial fill when it is thin.
- Without one (backtest bars): top of book (bid/ask already include the modelled spread),
  limited by bid_size/ask_size.
Plus `slippage_bps` adverse to the order, and taker fees from the CostModel.
"""
from __future__ import annotations

from typing import Protocol

from arb.core.types import Fill, MarketSnapshot, OrderIntent
from arb.costs import CostModel, walk_book


class Broker(Protocol):
    def execute(self, intents: list[OrderIntent], snapshot: MarketSnapshot) -> list[Fill]: ...


class SimBroker:
    def __init__(self, costs: CostModel, slippage_bps: float = 0.0):
        self.costs = costs
        self.slippage = slippage_bps / 1e4

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
