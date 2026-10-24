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
- Unknown outcomes are never guessed. If an order may have reached the exchange (create timed
  out) and the client-id lookup keeps failing, or an accepted order can no longer be read back,
  the result is marked unknown (Fill.unknown / RestingOrder.pending_client_id), not a zero fill;
  the engine records it, pauses entries and freezes the group, and resolve_order /
  check_resting look it up by client id on later cycles and report the real fill.
- A cancel only counts once the exchange confirms the order is no longer open. Otherwise the
  order keeps its exchange id (RestingOrder.cancel_failures > 0), the engine keeps it in the
  book and retries; repeated failures are escalated.
- A maker order's qty is rounded to the tradable amount when it is placed, so the engine's order
  equals the exchange order; an order the exchange reports closed and fully filled is done, and
  a remainder below the exchange minimum is treated as done (logged).
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
CANCEL_ESCALATE_AFTER = 3  # consecutive unconfirmed cancels before the alert says "act now"
LOOKUP_SLACK_MS = 60_000  # client-id lookups for an unresolved order search from its time minus this
CLIENT_ID_PREFIX = "arb"  # every order this bot sends carries a client id starting with this
DONE = {"closed", "canceled", "cancelled", "expired", "rejected"}


class OrderRefused(Exception):
    """Refused locally before reaching the exchange (cap, precision, minimums)."""


class BelowMinimum(OrderRefused):
    """The amount or notional is below the exchange minimum (it can never be placed at this size)."""


class OrderStateUnknown(Exception):
    """The order may be on the exchange but its state could not be read back (lookups kept failing).

    Deliberately not a ccxt.BaseError, so no generic handler can turn it into a zero fill."""

    def __init__(self, exchange: str, symbol: str, client_id: str, cause: BaseException | None):
        super().__init__(f"state of order {client_id} on {exchange} {symbol} is unknown"
                         + (f" ({type(cause).__name__}: {cause})" if cause else ""))
        self.exchange, self.symbol, self.client_id = exchange, symbol, client_id


def new_client_id() -> str:
    return CLIENT_ID_PREFIX + uuid.uuid4().hex[:24]


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
            raise BelowMinimum(f"{symbol} amount {qty} below exchange minimum {min_amount}")
        if min_cost and amount * cs * price < float(min_cost):
            raise BelowMinimum(f"{symbol} notional {amount * cs * price:.2f} below exchange minimum {min_cost}")
        return amount

    def _params(self, market: str, reduce_only: bool, client_id: str, post_only: bool = False) -> dict:
        p: dict = {"clientOrderId": client_id}
        if reduce_only and market == "swap":
            p["reduceOnly"] = True
        if post_only:
            p["postOnly"] = True
        return p

    def _find_by_client_id(self, exchange: str, symbol: str, client_id: str,
                           since_ms: int | None = None) -> dict | None:
        client = self.clients[exchange]
        since = int((self._clock() - 600) * 1000) if since_ms is None else int(since_ms)
        if client.has.get("fetchOrders"):
            orders = client.fetch_orders(symbol, since=since)
        else:  # e.g. OKX
            orders = client.fetch_open_orders(symbol) + client.fetch_closed_orders(symbol, since=since)
        for o in orders:
            if o.get("clientOrderId") == client_id:
                return o
        return None

    def _lookup(self, exchange: str, symbol: str, client_id: str, since_ms: int | None = None) -> dict | None:
        """The order with this client id, or None when the exchange confirms there is none.

        Retries a failing lookup; raises OrderStateUnknown when it never succeeds."""
        last: BaseException | None = None
        for attempt in range(NETWORK_RETRIES):
            try:
                return self._find_by_client_id(exchange, symbol, client_id, since_ms)
            except ccxt.BaseError as e:
                last = e
                log.warning("lookup of %s on %s failed (%s), attempt %d", client_id, symbol, e, attempt + 1)
                self._sleep(self.poll_s * (attempt + 1))
        raise OrderStateUnknown(exchange, symbol, client_id, last)

    def _create(self, exchange: str, symbol: str, type_: str, side: str, amount: float,
                price: float | None, params: dict) -> dict:
        """create_order with retries that cannot double-submit (looks the order up by client id first).

        Raises ccxt errors only when the order is known NOT to be on the exchange, and
        OrderStateUnknown when that cannot be established."""
        client = self.clients[exchange]
        cid = params["clientOrderId"]
        for attempt in range(NETWORK_RETRIES):
            try:
                return client.create_order(symbol, type_, side, amount, price, params)
            except ccxt.NetworkError as e:
                log.warning("create_order network error (%s), checking %s before retry", e, cid)
                self._sleep(self.poll_s * (attempt + 1))
                found = self._lookup(exchange, symbol, cid)  # OrderStateUnknown propagates
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

    def _unknown(self, i: OrderIntent, cid: str, ts_ms: int, why: str) -> Fill:
        self.events.append(f"UNKNOWN order state: {i.side} {i.qty:.8f} {i.symbol} on {i.exchange} (group "
                           f"{i.group_id}, client id {cid}): {why}. Not treated as a zero fill; it is "
                           f"resolved by client id on the next cycles")
        return Fill(i.group_id, i.exchange, i.symbol, i.market, i.side, 0.0, 0.0, 0.0, ts_ms,
                    requested_qty=i.qty, reduce_only=i.reduce_only, tag=i.tag, unknown=True, client_order_id=cid)

    def _finish_taker(self, client: Any, order: dict, symbol: str, cid: str) -> dict:
        """Poll a taker order until done; cancel whatever is still open at the timeout."""
        deadline = self._clock() + self.order_timeout_s
        while order.get("status") not in DONE and self._clock() < deadline:
            self._sleep(self.poll_s)
            order = client.fetch_order(order["id"], symbol)
        if order.get("status") not in DONE:
            try:
                client.cancel_order(order["id"], symbol)
            except ccxt.OrderNotFound:
                pass  # finished meanwhile; the fetch says how
            order = client.fetch_order(order["id"], symbol)
            self.events.append(f"taker order {cid} on {symbol} timed out; cancelled the rest")
        return order

    def _taker(self, i: OrderIntent, snapshot: MarketSnapshot) -> Fill:
        q = snapshot.quote(i.exchange, i.symbol)
        ref = q.ask if q and i.side == "buy" else q.bid if q else 0.0
        try:
            amount = self._amount(i.exchange, i.symbol, i.qty, ref)
        except OrderRefused as e:
            self.events.append(f"refused: {e}")
            return self._zero(i, snapshot.ts_ms)
        client = self.clients[i.exchange]
        cid = new_client_id()
        try:
            order = self._create(i.exchange, i.symbol, "market", i.side, amount, None,
                                 self._params(i.market, i.reduce_only, cid))
        except OrderStateUnknown as e:
            return self._unknown(i, cid, snapshot.ts_ms, str(e))
        except ccxt.BaseError as e:  # rejected, or confirmed absent after network errors
            self.events.append(f"taker order on {i.symbol} failed: {type(e).__name__}: {e}")
            return self._zero(i, snapshot.ts_ms)
        try:
            order = self._finish_taker(client, order, i.symbol, cid)
        except ccxt.BaseError as e:  # accepted, then lost track of it: its fill is unknown, not zero
            return self._unknown(i, cid, snapshot.ts_ms, f"accepted as {order.get('id')} but could not be "
                                                         f"read back or cancelled: {type(e).__name__}: {e}")
        if order.get("status") not in DONE:
            return self._unknown(i, cid, snapshot.ts_ms, f"order {order.get('id')} still open after cancel")
        fee_usdt, fee_base = self._fee_usdt(i.exchange, order, i.symbol.split("/")[0])
        return self._to_fill(i.exchange, i.symbol, i.market, i.side, order, float(order.get("filled") or 0.0),
                             fee_usdt, fee_base, i.group_id, i.qty, i.reduce_only, i.tag, snapshot.ts_ms,
                             maker=False, fallback_price=ref)

    def resolve_order(self, u: dict, snapshot: MarketSnapshot) -> Fill | None:
        """Resolve an unknown taker order (a Portfolio.unresolved_orders record) by its client id.

        Returns its real fill (qty 0 when the exchange confirms it never got the order), or None
        while its state is still unknown (u["attempts"] counts the failures)."""
        ex, sym, cid = u["exchange"], u["symbol"], u["client_order_id"]
        client = self.clients[ex]
        try:
            order = self._lookup(ex, sym, cid, since_ms=int(u["ts_ms"]) - LOOKUP_SLACK_MS)
            if order is not None and order.get("status") not in DONE:
                order = self._finish_taker(client, order, sym, cid)
            if order is not None and order.get("status") not in DONE:
                raise OrderStateUnknown(ex, sym, cid, None)
        except (OrderStateUnknown, ccxt.BaseError) as e:
            u["attempts"] = int(u.get("attempts", 0)) + 1
            self.events.append(f"order {cid} ({u['side']} {u['qty']:.8f} {sym}, group {u['group_id']}) still "
                               f"UNRESOLVED after {u['attempts']} check(s): {type(e).__name__}: {e}")
            return None
        q = snapshot.quote(ex, sym)
        ref = (q.ask if u["side"] == "buy" else q.bid) if q else 0.0
        if order is None:
            return Fill(u["group_id"], ex, sym, u["market"], u["side"], 0.0, 0.0, 0.0, snapshot.ts_ms,
                        requested_qty=u["qty"], reduce_only=u["reduce_only"], tag=u["tag"])
        fee_usdt, fee_base = self._fee_usdt(ex, order, sym.split("/")[0])
        return self._to_fill(ex, sym, u["market"], u["side"], order, float(order.get("filled") or 0.0),
                             fee_usdt, fee_base, u["group_id"], u["qty"], u["reduce_only"], u["tag"],
                             snapshot.ts_ms, maker=False, fallback_price=ref)

    # --- Broker protocol: maker orders ---------------------------------------------------
    @staticmethod
    def maker_price(side: str, quote: Quote) -> float:
        return quote.bid if side == "buy" else quote.ask

    def place_resting(self, order: RestingOrder, qty: float | None = None) -> None:
        whole = qty is None  # placing the order's whole remaining qty (not a replace's remainder)
        qty = order.qty if qty is None else qty
        order.exchange_order_id, order.filled_on_exchange = None, 0.0
        client = self.clients[order.exchange]
        cid = new_client_id()
        try:
            amount = self._amount(order.exchange, order.symbol, qty, order.price)
            if whole:
                # The exchange order is the truncated amount: make the engine's order equal it, so
                # a full fill leaves no dust remainder (and the hedge still matches each fill).
                tradable = amount * self._contract_size(order.exchange, order.symbol)
                if tradable < order.qty - EPS:
                    log.info("%s maker qty %.10f rounded to tradable %.10f", order.symbol, order.qty, tradable)
                    order.qty = tradable
            price = float(client.price_to_precision(order.symbol, order.price))
            placed = self._create(order.exchange, order.symbol, "limit", order.side, amount, price,
                                  self._params(order.market, order.reduce_only, cid, post_only=True))
        except BelowMinimum as e:
            # can never be placed at this size: the order is done (any remainder is dust)
            order.done = True
            self.events.append(f"refused: {e}; remaining {qty:.10f} of {order.symbol} order {order.order_id} "
                               f"treated as done")
            return
        except OrderRefused as e:
            self.events.append(f"refused: {e}")
            return
        except OrderStateUnknown as e:
            order.pending_client_id = cid
            self.events.append(f"UNKNOWN order state: maker {order.side} {qty:.8f} {order.symbol} (group "
                               f"{order.group_id}, client id {cid}): {e}. Not re-placed until resolved")
            return
        except ccxt.InvalidOrder as e:  # includes post-only "would immediately match"
            log.info("post-only order on %s not placed (%s); will retry", order.symbol, e)
            return
        except ccxt.BaseError as e:
            self.events.append(f"maker order on {order.symbol} failed: {type(e).__name__}: {e}")
            return
        order.exchange_order_id = placed["id"]

    def _resolve_pending(self, order: RestingOrder) -> bool:
        """Look up a placement whose outcome was unknown. True once its state is known."""
        cid = order.pending_client_id
        try:
            found = self._lookup(order.exchange, order.symbol, cid, since_ms=order.placed_ms - LOOKUP_SLACK_MS)
        except OrderStateUnknown as e:
            self.events.append(f"maker order {cid} on {order.symbol} (group {order.group_id}) still UNRESOLVED: {e}")
            return False
        order.pending_client_id = None
        if found is None:
            self.events.append(f"maker order {cid} on {order.symbol} resolved: never reached the exchange")
        else:
            order.exchange_order_id, order.filled_on_exchange = found["id"], 0.0
            self.events.append(f"maker order {cid} on {order.symbol} resolved: exchange order {found['id']}")
        return True

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

    @staticmethod
    def _fully_filled(ex_order: dict) -> bool:
        amount, filled = float(ex_order.get("amount") or 0.0), float(ex_order.get("filled") or 0.0)
        return ex_order.get("status") == "closed" and amount > 0 and filled >= amount * (1 - 1e-9)

    def _settle_done(self, order: RestingOrder, ex_order: dict) -> None:
        """The exchange order is no longer open: forget its id; mark the order done if it fully filled."""
        order.exchange_order_id, order.cancel_failures = None, 0
        if self._fully_filled(ex_order):
            order.done = True

    def check_resting(self, order: RestingOrder, snapshot: MarketSnapshot) -> Fill | None:
        if order.pending_client_id is not None:
            if not self._resolve_pending(order) or order.exchange_order_id is None:
                return None  # still unknown, or never placed: (re)place on the next check
        if order.exchange_order_id is None:
            self.place_resting(order)
            return None
        try:
            ex_order = self.clients[order.exchange].fetch_order(order.exchange_order_id, order.symbol)
        except ccxt.BaseError as e:
            self.events.append(f"fetch_order failed for {order.symbol}: {type(e).__name__}")
            return None
        fill = self._new_fill(order, ex_order, snapshot.ts_ms)
        if ex_order.get("status") in DONE:
            # fully filled -> done; cancelled/rejected/expired by the exchange (e.g. post-only
            # rejected after placement) -> the remainder is re-placed on the next check
            self._settle_done(order, ex_order)
        return fill

    def _cancel_failed(self, order: RestingOrder, why: str) -> None:
        order.cancel_failures += 1
        n = order.cancel_failures
        ref = order.exchange_order_id or f"client id {order.pending_client_id}"
        msg = (f"cancel failed for {order.symbol} order {ref} (group {order.group_id}): {why}; it may still be "
               f"open on the exchange, so it stays tracked and the cancel is retried next cycle (attempt {n})")
        if n >= CANCEL_ESCALATE_AFTER:
            msg = f"ESCALATE: {msg}. Cancel it manually on the exchange if this keeps failing"
        self.events.append(msg)

    def cancel_resting(self, order: RestingOrder) -> Fill | None:
        """Cancel; returns any fill that raced it. order.cancel_failures > 0 afterwards means the
        cancel is NOT confirmed (the order may still be live) and must be retried."""
        if order.pending_client_id is not None and not self._resolve_pending(order):
            self._cancel_failed(order, "its placement is still unresolved")
            return None
        if order.exchange_order_id is None:
            order.cancel_failures = 0
            return None
        client = self.clients[order.exchange]
        err: BaseException | None = None
        try:
            client.cancel_order(order.exchange_order_id, order.symbol)
        except ccxt.OrderNotFound:
            pass  # already filled or gone; the fetch below tells us which
        except ccxt.BaseError as e:
            err = e
        try:
            ex_order = client.fetch_order(order.exchange_order_id, order.symbol)
        except ccxt.BaseError as e:
            self._cancel_failed(order, f"{type(err).__name__ + ', ' if err else ''}"
                                       f"fetch after cancel failed: {type(e).__name__}")
            return None
        fill = self._new_fill(order, ex_order, int(self._clock() * 1000))
        if ex_order.get("status") in DONE:
            self._settle_done(order, ex_order)
        else:
            self._cancel_failed(order, f"{type(err).__name__}: {err}" if err else "still open after cancel")
        return fill

    def replace_resting(self, order: RestingOrder, new_price: float) -> Fill | None:
        late = self.cancel_resting(order)
        if order.cancel_failures or order.exchange_order_id is not None:
            return late  # the old order may still be live: never post a second one
        remaining = order.qty - (late.qty if late else 0.0)
        order.price = new_price
        if remaining > EPS and not order.done:
            self.place_resting(order, qty=remaining)
        return late
