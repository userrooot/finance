"""Cost model: exchange fees, order-book slippage, withdrawals and tax.

Every scanner must express its edge through these helpers so that no opportunity
is ever reported gross of costs.
"""
from __future__ import annotations

from dataclasses import dataclass

from arb.config import Settings
from arb.models import Level, MarketType


@dataclass(frozen=True)
class Fill:
    avg_price: float
    qty: float  # base units actually fillable
    notional: float  # quote units

    @property
    def complete(self) -> bool:
        return self.qty > 0


def walk_book(levels: list[Level], qty: float) -> Fill:
    """Fill `qty` base units against `levels` (asks for a buy, bids for a sell).

    Returns a partial fill if the book is too thin.
    """
    remaining, notional = qty, 0.0
    for price, size in levels:
        take = min(size, remaining)
        notional += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    filled = qty - max(remaining, 0.0)
    return Fill(notional / filled if filled > 0 else 0.0, filled, notional)


def walk_book_quote(levels: list[Level], quote_amount: float) -> Fill:
    """Spend/receive `quote_amount` against `levels`; returns base qty filled."""
    remaining, qty = quote_amount, 0.0
    for price, size in levels:
        level_notional = price * size
        if level_notional >= remaining:
            qty += remaining / price
            remaining = 0.0
            break
        qty += size
        remaining -= level_notional
    spent = quote_amount - remaining
    return Fill(spent / qty if qty > 0 else 0.0, qty, spent)


def crossing_capacity(asks: list[Level], bids: list[Level], buy_fee: float, sell_fee: float) -> tuple[float, float]:
    """Max base qty buyable on `asks` and sellable on `bids` with every unit profitable after fees.

    Returns (qty, gross_profit_after_fees_quote).
    """
    i = j = 0
    ask_left = asks[0][1] if asks else 0.0
    bid_left = bids[0][1] if bids else 0.0
    qty = profit = 0.0
    while i < len(asks) and j < len(bids):
        buy_px, sell_px = asks[i][0], bids[j][0]
        unit_edge = sell_px * (1 - sell_fee) - buy_px * (1 + buy_fee)
        if unit_edge <= 0:
            break
        take = min(ask_left, bid_left)
        qty += take
        profit += take * unit_edge
        ask_left -= take
        bid_left -= take
        if ask_left <= 1e-12:
            i += 1
            ask_left = asks[i][1] if i < len(asks) else 0.0
        if bid_left <= 1e-12:
            j += 1
            bid_left = bids[j][1] if j < len(bids) else 0.0
    return qty, profit


class CostModel:
    def __init__(self, settings: Settings):
        self.settings = settings

    def fee_rate(self, exchange: str, market: MarketType, taker: bool = True) -> float:
        rate = self.settings.exchanges[exchange].fees[market]
        return rate.taker if taker else rate.maker

    def fee(self, exchange: str, market: MarketType, notional: float, taker: bool = True) -> float:
        return abs(notional) * self.fee_rate(exchange, market, taker)

    def withdrawal_fee_usd(self, asset: str, price_usd: float) -> float:
        """Cost of moving `asset` once between exchanges. Unknown assets are treated as unmovable (inf)."""
        units = self.settings.withdrawal_fees.get(asset)
        return float("inf") if units is None else units * price_usd

    def tax(self, net_pnl: float) -> float:
        return max(net_pnl, 0.0) * self.settings.tax.rate_on_gains

    def after_tax(self, net_pnl: float) -> float:
        return net_pnl - self.tax(net_pnl)

    @staticmethod
    def slippage(fill: Fill, top_price: float) -> float:
        """Quote-currency cost of filling at VWAP instead of top of book (always >= 0)."""
        return abs(fill.avg_price - top_price) * fill.qty
