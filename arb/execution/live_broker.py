"""LiveBroker: real orders through ccxt (sync clients), behind the same Broker protocol as SimBroker.

Safety properties, each covered by tests:
- Every order's notional is capped at `max_order_usd`; larger orders are refused locally.
- Amounts and prices are rounded to the exchange's precision, and orders below the exchange's
  minimum amount or notional are refused locally (reported as a zero fill, never sent).
- Each submission carries a fresh client order id that is reused across retries, and after a
  network error the broker looks the order up by that id before trying again, so a retry can
  never double an order.
- Taker orders are polled until done; anything still open after `order_timeout_s` is cancelled
  and only the filled part is reported.
- A spot buy whose fee is charged in the bought asset reports the net quantity received, so the
  hedge matches what the account actually holds.
- Maker orders are post-only. A post-only reject leaves the order unplaced; it is re-placed on
  the next check. Fills are reported incrementally (partial fills supported), including fills
  that race a cancel.
Swap amounts are converted between base units (the engine's unit) and contracts (ccxt's unit
for contract markets) with the market's contractSize.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

import ccxt

from arb.core.types import Fill, MarketSnapshot, OrderIntent, Quote, RestingOrder
from arb.costs import CostModel

log = logging.getLogger(__name__)

EPS = 1e-12
NETWORK_RETRIES = 3
DONE = {"closed", "canceled", "cancelled", "expired", "rejected"}


class OrderRefused(Exception):
    """Refused locally before reaching the exchange (cap, precision, minimums)."""


class LiveBroker:
    def __init__(self, clients: dict[str, Any], costs: CostModel, max_order_usd: float,
                 order_timeout_s: float = 20.0, poll_s: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.clients = clients
        self.costs = costs
        self.max_order_usd = max_order_usd
        self.order_timeout_s = order_timeout_s
        self.poll_s = poll_s
        self._sleep = sleep
        self._clock = clock
        self.events: list[str] = []  # refusals and anomalies for the runner to log/alert

    # --- helpers -------------------------------------------------------------------------
    def _contract_size(self, exchange: str, symbol: str) -> float:
        m = self.clients[exchange].market(symbol)
        return float(m.get("contractSize") or 1.0) if m.get("contract") else 1.0

    def _amount(self, exchange: str, symbol: str, qty: float, price: float) -> float:
        """Base qty -> exchange amount (contracts for swaps), rounded down and checked."""
        if qty * price > self.max_order_usd * 1.0001:
            raise OrderRefused(f"{symbol} order {qty * price:.2f} USDT exceeds live.max_order_usd {self.max_order_usd}")
        client = self.clients[exchange]
        cs = self._contract_size(exchange, symbol)
        amount = float(client.amount_to_precision(symbol, qty / cs))  # ccxt truncates by default
        limits = client.market(symbol).get("limits") or {}
        min_amount = (limits.get("amount") or {}).get("min")
        min_cost = (limits.get("cost") or {}).get("min")
        if amount <= 0 or (min_amount and amount < float(min_amount)):
            raise OrderRefused(f"{symbol} amount {qty} below exchange minimum {min_amount}")
        if min_cost and amount * cs * price < float(min_cost):
            raise OrderRefused(f"{symbol} notional {amount * cs * price:.2f} below exchange minimum {min_cost}")
        return amount

    def _params(self, market: str, reduce_only: bool, client_id: str, post_only: bool = False) -> dict:
        p: dict = {"clientOrderId": client_id}
        if reduce_only and market == "swap":
            p["reduceOnly"] = True
        if post_only:
            p["postOnly"] = True
        return p

    def _find_by_client_id(self, exchange: str, symbol: str, client_id: str) -> dict | None:
        client = self.clients[exchange]
        since = int((self._clock() - 600) * 1000)
        if client.has.get("fetchOrders"):
            orders = client.fetch_orders(symbol, since=since)
        else:  # e.g. OKX
            orders = client.fetch_open_orders(symbol) + client.fetch_closed_orders(symbol, since=since)
        for o in orders:
            if o.get("clientOrderId") == client_id:
                return o
        return None

    def _create(self, exchange: str, symbol: str, type_: str, side: str, amount: float,
                price: float | None, params: dict) -> dict:
        """create_order with retries that cannot double-submit (looks the order up by client id first)."""
        client = self.clients[exchange]
        cid = params["clientOrderId"]
        for attempt in range(NETWORK_RETRIES):
            try:
                return client.create_order(symbol, type_, side, amount, price, params)
            except ccxt.NetworkError as e:
                log.warning("create_order network error (%s), checking %s before retry", e, cid)
                self._sleep(self.poll_s * (attempt + 1))
                found = self._find_by_client_id(exchange, symbol, cid)
                if found is not None:
                    return found
        raise ccxt.NetworkError(f"create_order failed after {NETWORK_RETRIES} attempts ({cid})")

    def _fee_usdt(self, exchange: str, order: dict, base: str) -> tuple[float, float]:
        """(fee in USDT, fee charged in the base asset) summed over the order's fees."""
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        usdt, in_base = 0.0, 0.0
        avg = float(order.get("average") or order.get("price") or 0.0)
        for f in fees:
            cost, cur = float(f.get("cost") or 0.0), f.get("currency")
            if not cost:
                continue
            if cur in ("USDT", None):
                usdt += cost
            elif cur == base:
                in_base += cost
                usdt += cost * avg
            else:  # e.g. BNB discount: value it at the current price
                usdt += cost * float(self.clients[exchange].fetch_ticker(f"{cur}/USDT")["last"])
        return usdt, in_base

    def _to_fill(self, exchange: str, symbol: str, market: str, side: str, order: dict, filled_amount: float,
                 fee_usdt: float, fee_base: float, group_id: str, requested: float, reduce_only: bool,
                 tag: str, ts_ms: int, maker: bool, fallback_price: float) -> Fill:
        cs = self._contract_size(exchange, symbol)
        qty = filled_amount * cs
        if market == "spot" and side == "buy":
            qty -= fee_base  # the fee came out of what we bought
        price = float(order.get("average") or order.get("price") or fallback_price)
        # cash accounting: Portfolio charges qty*price + fee, which equals the gross cost when the
        # fee was taken in the base asset (qty is net, fee_usdt values that base fee)
        return Fill(group_id, exchange, symbol, market, side, max(qty, 0.0), price, fee_usdt, ts_ms,
                    requested_qty=requested, reduce_only=reduce_only, tag=tag, maker=maker)

    # --- Broker protocol: taker orders ---------------------------------------------------
    def execute(self, intents: list[OrderIntent], snapshot: MarketSnapshot) -> list[Fill]:
        return [self._taker(i, snapshot) for i in intents]

    def _zero(self, i: OrderIntent, ts_ms: int) -> Fill:
        return Fill(i.group_id, i.exchange, i.symbol, i.market, i.side, 0.0, 0.0, 0.0, ts_ms,
                    requested_qty=i.qty, reduce_only=i.reduce_only, tag=i.tag)

    def _taker(self, i: OrderIntent, snapshot: MarketSnapshot) -> Fill:
        q = snapshot.quote(i.exchange, i.symbol)
        ref = q.ask if q and i.side == "buy" else q.bid if q else 0.0
        try:
            amount = self._amount(i.exchange, i.symbol, i.qty, ref)
        except OrderRefused as e:
            self.events.append(f"refused: {e}")
            return self._zero(i, snapshot.ts_ms)
        client = self.clients[i.exchange]
        cid = "arb" + uuid.uuid4().hex[:24]
        try:
            order = self._create(i.exchange, i.symbol, "market", i.side, amount, None,
                                 self._params(i.market, i.reduce_only, cid))
            deadline = self._clock() + self.order_timeout_s
            while order.get("status") not in DONE and self._clock() < deadline:
                self._sleep(self.poll_s)
                order = client.fetch_order(order["id"], i.symbol)
            if order.get("status") not in DONE:
                client.cancel_order(order["id"], i.symbol)
                order = client.fetch_order(order["id"], i.symbol)
                self.events.append(f"taker order {cid} on {i.symbol} timed out; cancelled the rest")
        except ccxt.BaseError as e:
            self.events.append(f"taker order on {i.symbol} failed: {type(e).__name__}: {e}")
            return self._zero(i, snapshot.ts_ms)
        fee_usdt, fee_base = self._fee_usdt(i.exchange, order, i.symbol.split("/")[0])
        return self._to_fill(i.exchange, i.symbol, i.market, i.side, order, float(order.get("filled") or 0.0),
                             fee_usdt, fee_base, i.group_id, i.qty, i.reduce_only, i.tag, snapshot.ts_ms,
                             maker=False, fallback_price=ref)

    # --- Broker protocol: maker orders ---------------------------------------------------
    @staticmethod
    def maker_price(side: str, quote: Quote) -> float:
        return quote.bid if side == "buy" else quote.ask

    def place_resting(self, order: RestingOrder, qty: float | None = None) -> None:
        qty = order.qty if qty is None else qty
        order.exchange_order_id, order.filled_on_exchange = None, 0.0
        client = self.clients[order.exchange]
        try:
            amount = self._amount(order.exchange, order.symbol, qty, order.price)
            price = float(client.price_to_precision(order.symbol, order.price))
            cid = "arb" + uuid.uuid4().hex[:24]
            placed = self._create(order.exchange, order.symbol, "limit", order.side, amount, price,
                                  self._params(order.market, order.reduce_only, cid, post_only=True))
        except OrderRefused as e:
            self.events.append(f"refused: {e}")
            return
        except ccxt.InvalidOrder as e:  # includes post-only "would immediately match"
            log.info("post-only order on %s not placed (%s); will retry", order.symbol, e)
            return
        except ccxt.BaseError as e:
            self.events.append(f"maker order on {order.symbol} failed: {type(e).__name__}: {e}")
            return
        order.exchange_order_id = placed["id"]

    def _new_fill(self, order: RestingOrder, ex_order: dict, ts_ms: int) -> Fill | None:
        """Fill for the part of the exchange order filled since we last looked."""
        cs = self._contract_size(order.exchange, order.symbol)
        filled = float(ex_order.get("filled") or 0.0) * cs
        delta = filled - order.filled_on_exchange
        if delta <= EPS:
            return None
        fee_usdt, fee_base = self._fee_usdt(order.exchange, ex_order, order.symbol.split("/")[0])
        share = delta / filled if filled > 0 else 0.0
        order.filled_on_exchange = filled
        f = self._to_fill(order.exchange, order.symbol, order.market, order.side, ex_order, delta / cs,
                          fee_usdt * share, fee_base * share, order.group_id, delta, order.reduce_only,
                          order.tag, ts_ms, maker=True, fallback_price=order.price)
        return f if f.qty > EPS else None

    def check_resting(self, order: RestingOrder, snapshot: MarketSnapshot) -> Fill | None:
        if order.exchange_order_id is None:
            self.place_resting(order)
            return None
        try:
            ex_order = self.clients[order.exchange].fetch_order(order.exchange_order_id, order.symbol)
        except ccxt.BaseError as e:
            self.events.append(f"fetch_order failed for {order.symbol}: {type(e).__name__}")
            return None
        fill = self._new_fill(order, ex_order, snapshot.ts_ms)
        if ex_order.get("status") in DONE and (fill is None or fill.qty < order.qty - EPS):
            # cancelled/rejected/expired by the exchange (e.g. post-only rejected after placement):
            # re-place the remainder on the next check
            order.exchange_order_id = None
        return fill

    def cancel_resting(self, order: RestingOrder) -> Fill | None:
        if order.exchange_order_id is None:
            return None
        client = self.clients[order.exchange]
        try:
            client.cancel_order(order.exchange_order_id, order.symbol)
        except ccxt.OrderNotFound:
            pass  # already filled or gone; the fetch below tells us which
        except ccxt.BaseError as e:
            self.events.append(f"cancel failed for {order.symbol}: {type(e).__name__}")
        try:
            ex_order = client.fetch_order(order.exchange_order_id, order.symbol)
        except ccxt.BaseError as e:
            self.events.append(f"fetch after cancel failed for {order.symbol}: {type(e).__name__}")
            return None
        order.exchange_order_id = None
        return self._new_fill(order, ex_order, int(self._clock() * 1000))

    def replace_resting(self, order: RestingOrder, new_price: float) -> Fill | None:
        late = self.cancel_resting(order)
        remaining = order.qty - (late.qty if late else 0.0)
        order.price = new_price
        if remaining > EPS:
            self.place_resting(order, qty=remaining)
        return late
