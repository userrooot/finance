"""A small in-memory stand-in for a sync ccxt exchange, for LiveBroker / LiveTrader tests."""
from __future__ import annotations

import itertools
import math

import ccxt


def spot(base: str, min_amount=0.0001, min_cost=5.0, step=0.0001, tick=0.01) -> dict:
    return {"symbol": f"{base}/USDT", "base": base, "quote": "USDT", "type": "spot", "contract": False,
            "contractSize": None, "limits": {"amount": {"min": min_amount}, "cost": {"min": min_cost}},
            "precision": {"amount": step, "price": tick}}


def perp(base: str, contract_size=1.0, min_amount=0.001, min_cost=5.0, step=0.001, tick=0.1) -> dict:
    return {"symbol": f"{base}/USDT:USDT", "base": base, "quote": "USDT", "type": "swap", "contract": True,
            "contractSize": contract_size, "limits": {"amount": {"min": min_amount}, "cost": {"min": min_cost}},
            "precision": {"amount": step, "price": tick}}


class FakeExchange:
    """Market orders fill at `prices[symbol]`; limit orders rest until `fill_resting` is called."""

    def __init__(self, markets: list[dict], prices: dict[str, float], taker_fee=0.0005, maker_fee=0.0002,
                 fee_in_base_on_spot_buy=False, has_fetch_orders=True):
        self.markets = {m["symbol"]: m for m in markets}
        self.prices = dict(prices)
        self.taker_fee, self.maker_fee = taker_fee, maker_fee
        self.fee_in_base_on_spot_buy = fee_in_base_on_spot_buy
        self.has = {"fetchOrders": has_fetch_orders}
        self.orders: dict[str, dict] = {}
        self.created: list[dict] = []  # every order that reached the "exchange"
        self.balances: dict[str, float] = {}
        self.positions: list[dict] = []
        self.fail_next_create: Exception | None = None  # raised AFTER the order is recorded
        self.market_order_fill_ratio = 1.0
        self.market_orders_stay_open = False
        self._ids = itertools.count(1)
        self.calls: list[str] = []

    # --- precision / markets ---
    def market(self, symbol):
        return self.markets[symbol]

    def amount_to_precision(self, symbol, amount):
        step = self.markets[symbol]["precision"]["amount"]
        return str(math.floor(amount / step + 1e-9) * step)

    def price_to_precision(self, symbol, price):
        tick = self.markets[symbol]["precision"]["price"]
        return str(round(round(price / tick) * tick, 10))

    # --- orders ---
    def _fee(self, symbol, side, filled, price, maker):
        rate = self.maker_fee if maker else self.taker_fee
        m = self.markets[symbol]
        if m["type"] == "spot" and side == "buy" and self.fee_in_base_on_spot_buy:
            return {"cost": filled * rate, "currency": m["base"]}
        return {"cost": filled * (m["contractSize"] or 1.0) * price * rate, "currency": "USDT"}

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append("create")
        params = params or {}
        m = self.markets[symbol]
        oid = str(next(self._ids))
        o = {"id": oid, "clientOrderId": params.get("clientOrderId"), "symbol": symbol, "type": type_, "side": side,
             "amount": amount, "price": price, "filled": 0.0, "average": None, "status": "open",
             "params": params, "fee": None, "timestamp": 0}
        if type_ == "limit" and params.get("postOnly"):
            mkt = self.prices[symbol]
            if (side == "buy" and price >= mkt) or (side == "sell" and price <= mkt):
                raise ccxt.OrderImmediatelyFillable("post-only order would match")
        if type_ == "market":
            px = self.prices[symbol]
            filled = amount * self.market_order_fill_ratio
            o.update(filled=filled, average=px, fee=self._fee(symbol, side, filled, px, maker=False))
            o["status"] = "open" if self.market_orders_stay_open else "closed"
        self.orders[oid] = o
        self.created.append(o)
        if self.fail_next_create is not None:
            e, self.fail_next_create = self.fail_next_create, None
            raise e
        return dict(o)

    def fill_resting(self, oid, amount=None):
        o = self.orders[oid]
        amount = o["amount"] - o["filled"] if amount is None else amount
        o["filled"] += amount
        o["average"] = o["price"]
        o["fee"] = self._fee(o["symbol"], o["side"], o["filled"], o["price"], maker=True)
        if o["filled"] >= o["amount"] - 1e-12:
            o["status"] = "closed"

    def fetch_order(self, oid, symbol=None):
        self.calls.append("fetch")
        if oid not in self.orders:
            raise ccxt.OrderNotFound(oid)
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol=None):
        self.calls.append("cancel")
        o = self.orders.get(oid)
        if o is None or o["status"] != "open":
            raise ccxt.OrderNotFound(oid)
        o["status"] = "canceled"
        return dict(o)

    def fetch_orders(self, symbol=None, since=None):
        return [dict(o) for o in self.orders.values() if symbol is None or o["symbol"] == symbol]

    def fetch_open_orders(self, symbol=None):
        return [o for o in self.fetch_orders(symbol) if o["status"] == "open"]

    def fetch_closed_orders(self, symbol=None, since=None):
        return [o for o in self.fetch_orders(symbol) if o["status"] != "open"]

    def fetch_ticker(self, symbol):
        return {"symbol": symbol, "last": self.prices[symbol]}

    # --- account ---
    def fetch_balance(self, params=None):
        return {"total": dict(self.balances), "free": dict(self.balances)}

    def fetch_positions(self, symbols=None, params=None):
        return [p for p in self.positions if symbols is None or p["symbol"] in symbols]
