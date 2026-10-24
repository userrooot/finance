"""LiveBroker against an in-memory ccxt stand-in: caps, precision, idempotency, fees, maker fills."""
import ccxt
import pytest

from arb.config import ExecutionCfg, load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import MarketSnapshot, OrderIntent, Quote, Requirements, RestingOrder
from arb.costs import CostModel
from arb.execution.live_broker import LiveBroker
from arb.strategies.base import Strategy
from tests.fake_ccxt import FakeExchange, perp, spot

T0 = 1_760_000_000_000
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"


def make(max_usd=300.0, **kw):
    ex = FakeExchange([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                      {SPOT: 100_000.0, PERP: 100_000.0}, **kw)
    s = load_settings()
    return ex, LiveBroker({"okx": ex}, CostModel(s), max_order_usd=max_usd, sleep=lambda s: None)


def snap(ts=T0, px=100_000.0):
    return MarketSnapshot(ts, {("okx", SPOT): Quote(px - 5, px + 5, ts), ("okx", PERP): Quote(px - 5, px + 5, ts)})


def intent(sym=SPOT, market="spot", side="buy", qty=0.002, **kw):
    return OrderIntent("g1", "okx", sym, market, side, qty, **kw)


def test_taker_fill_reports_real_price_qty_fee():
    ex, b = make()
    [f] = b.execute([intent()], snap())
    assert f.qty == pytest.approx(0.002) and f.price == 100_000.0
    assert f.fee == pytest.approx(0.002 * 100_000 * 0.0005) and not f.maker
    assert ex.created[0]["type"] == "market"


def test_max_order_usd_refuses_locally():
    ex, b = make(max_usd=50)
    [f] = b.execute([intent(qty=0.002)], snap())  # $200 > $50
    assert f.qty == 0 and ex.created == [] and "max_order_usd" in b.events[0]


def test_below_exchange_minimum_refused_locally():
    ex, b = make()
    [f] = b.execute([intent(qty=0.00001)], snap())  # below min amount 0.0001
    assert f.qty == 0 and ex.created == [] and "minimum" in b.events[0]


def test_swap_amount_in_contracts_and_reduce_only():
    ex, b = make()
    [f] = b.execute([intent(PERP, "swap", "sell", 0.002, reduce_only=True)], snap())
    o = ex.created[0]
    assert o["amount"] == pytest.approx(0.2)  # 0.002 BTC / 0.01 BTC per contract
    assert o["params"]["reduceOnly"] is True
    assert f.qty == pytest.approx(0.002)


def test_precision_rounds_down():
    ex, b = make()
    [f] = b.execute([intent(PERP, "swap", "sell", 0.002599)], snap())  # 0.2599 contracts -> 0.25
    assert ex.created[0]["amount"] == pytest.approx(0.25) and f.qty == pytest.approx(0.0025)
    [f] = b.execute([intent(SPOT, "spot", "buy", 0.00259)], snap())  # step 0.0001 -> 0.0025
    assert ex.created[1]["amount"] == pytest.approx(0.0025) and f.qty == pytest.approx(0.0025)


def test_network_error_after_submit_is_not_doubled():
    ex, b = make()
    ex.fail_next_create = ccxt.NetworkError("timeout after send")
    [f] = b.execute([intent()], snap())
    assert len(ex.created) == 1 and f.qty == pytest.approx(0.002)


def test_network_error_lookup_without_fetch_orders():
    ex, b = make(has_fetch_orders=False)
    ex.fail_next_create = ccxt.RequestTimeout("timeout")
    b.execute([intent()], snap())
    assert len(ex.created) == 1


def test_spot_buy_fee_in_base_reduces_qty():
    ex, b = make(fee_in_base_on_spot_buy=True)
    [f] = b.execute([intent(qty=0.002)], snap())
    assert f.qty == pytest.approx(0.002 * (1 - 0.0005))
    # Portfolio cash: qty*price + fee == gross cost paid
    assert f.qty * f.price + f.fee == pytest.approx(0.002 * 100_000)


def test_taker_timeout_cancels_and_reports_partial():
    ex, b = make()
    ex.market_orders_stay_open = True
    ex.market_order_fill_ratio = 0.5
    clock = iter(range(0, 1000, 5))
    b._clock = lambda: next(clock)
    [f] = b.execute([intent()], snap())
    assert f.qty == pytest.approx(0.001) and "timed out" in b.events[0]
    assert ex.created[0]["status"] == "canceled"


def resting(side="sell", price=100_005.0, qty=0.002):
    return RestingOrder("o1", "g1", "okx", PERP, "swap", side, qty, price, T0, T0, False, "entry",
                        {"exchange": "okx", "symbol": SPOT, "market": "spot", "side": "buy", "reduce_only": False})


def test_post_only_reject_then_replaced_next_check():
    ex, b = make()
    o = resting(price=99_000.0)  # sell below market -> would match -> rejected
    b.place_resting(o)
    assert o.exchange_order_id is None and ex.created == []
    o.price = 100_005.0
    assert b.check_resting(o, snap()) is None
    assert o.exchange_order_id is not None and ex.created[0]["params"]["postOnly"] is True


def test_partial_maker_fills_are_incremental():
    ex, b = make()
    o = resting()
    b.place_resting(o)
    ex.fill_resting(o.exchange_order_id, 0.1)  # 0.1 contracts = 0.001 BTC
    f1 = b.check_resting(o, snap())
    assert f1.qty == pytest.approx(0.001) and f1.maker
    assert b.check_resting(o, snap()) is None  # nothing new
    ex.fill_resting(o.exchange_order_id)
    f2 = b.check_resting(o, snap())
    assert f2.qty == pytest.approx(0.001)
    assert f1.fee + f2.fee == pytest.approx(0.002 * 100_005 * 0.0002)


def test_cancel_reports_fill_that_raced_it():
    ex, b = make()
    o = resting()
    b.place_resting(o)
    ex.fill_resting(o.exchange_order_id, 0.05)
    late = b.cancel_resting(o)
    assert late.qty == pytest.approx(0.0005) and o.exchange_order_id is None


def test_replace_cancels_and_reposts_remaining():
    ex, b = make()
    o = resting()
    b.place_resting(o)
    first = o.exchange_order_id
    ex.fill_resting(first, 0.1)
    late = b.replace_resting(o, 100_105.0)
    assert late.qty == pytest.approx(0.001)
    assert ex.orders[first]["status"] == "canceled"
    new = ex.orders[o.exchange_order_id]
    assert new["price"] == pytest.approx(100_105.0) and new["amount"] == pytest.approx(0.1)


class Once(Strategy):
    def __init__(self, intents):
        self.intents = intents

    def requirements(self):
        return Requirements()

    def on_snapshot(self, snapshot, portfolio):
        out, self.intents = self.intents, []
        return out


def test_engine_maker_first_end_to_end_with_live_broker():
    ex, b = make()
    s = load_settings()
    pf = Portfolio(1000.0)
    legs = [OrderIntent("g1", "okx", SPOT, "spot", "buy", 0.002, exec_style="maker_first"),
            OrderIntent("g1", "okx", PERP, "swap", "sell", 0.002, exec_style="maker_first", maker=True)]
    eng = TradingEngine(Once(legs), b, pf, RiskManager(s, 1000.0), on_event=lambda m: None,
                        execution=ExecutionCfg(entry_timeout_s=1e9))
    eng.step(snap())
    [o] = pf.open_orders
    assert ex.orders[o.exchange_order_id]["params"]["postOnly"]
    ex.fill_resting(o.exchange_order_id)
    eng.step(snap(T0 + 30_000))
    assert pf.open_orders == []
    assert pf.net_qty("okx", PERP, "swap") == pytest.approx(-0.002)
    assert pf.net_qty("okx", SPOT, "spot") == pytest.approx(0.002)
    assert [o["type"] for o in ex.created] == ["limit", "market"]


# --- unknown order outcomes -------------------------------------------------------------------
class LookupDown(FakeExchange):
    """fetch_orders fails `lookup_failures` more times (the client-id lookup after a network error)."""
    lookup_failures = 0

    def fetch_orders(self, symbol=None, since=None):
        if self.lookup_failures:
            self.lookup_failures -= 1
            raise ccxt.NetworkError("lookup down")
        return super().fetch_orders(symbol, since)


def make_lookup_down(failures):
    ex = LookupDown([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                    {SPOT: 100_000.0, PERP: 100_000.0})
    ex.lookup_failures = failures
    return ex, LiveBroker({"okx": ex}, CostModel(load_settings()), max_order_usd=300.0, sleep=lambda s: None)


def test_failing_lookup_is_retried_and_finds_the_accepted_order():
    ex, b = make_lookup_down(failures=2)  # fewer than the retries
    ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")
    [f] = b.execute([intent()], snap())
    assert f.qty == pytest.approx(0.002) and not f.unknown and len(ex.created) == 1


def test_lookup_down_for_good_reports_unknown_not_zero_then_resolves():
    ex, b = make_lookup_down(failures=3)
    ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")
    [f] = b.execute([intent()], snap())
    assert f.unknown and f.qty == 0 and f.client_order_id == ex.created[0]["clientOrderId"]
    assert any("UNKNOWN order state" in e for e in b.events)
    u = {"client_order_id": f.client_order_id, "group_id": "g1", "exchange": "okx", "symbol": SPOT,
         "market": "spot", "side": "buy", "qty": 0.002, "reduce_only": False, "tag": "", "ts_ms": T0, "attempts": 0}
    ex.lookup_failures = 3
    assert b.resolve_order(u, snap()) is None and u["attempts"] == 1   # still unknown: keep it
    r = b.resolve_order(u, snap())
    assert r.qty == pytest.approx(0.002) and not r.unknown and len(ex.created) == 1


def test_resolve_order_confirms_never_placed_as_zero():
    ex, b = make()
    u = {"client_order_id": "arbmissing", "group_id": "g1", "exchange": "okx", "symbol": SPOT, "market": "spot",
         "side": "buy", "qty": 0.002, "reduce_only": False, "tag": "", "ts_ms": T0}
    r = b.resolve_order(u, snap())
    assert r is not None and r.qty == 0 and not r.unknown


def test_fetch_failure_after_accepted_taker_is_unknown():
    class FetchDown(FakeExchange):
        def fetch_order(self, oid, symbol=None):
            raise ccxt.ExchangeNotAvailable("down")
    ex = FetchDown([spot("BTC")], {SPOT: 100_000.0})
    ex.market_orders_stay_open = True
    b = LiveBroker({"okx": ex}, CostModel(load_settings()), max_order_usd=300.0, sleep=lambda s: None)
    [f] = b.execute([intent()], snap())
    assert f.unknown and f.qty == 0


def test_maker_placement_unknown_is_resolved_before_any_replacement():
    ex, b = make_lookup_down(failures=3)
    ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")
    o = resting()
    b.place_resting(o)
    assert o.exchange_order_id is None and o.pending_client_id and len(ex.created) == 1
    ex.lookup_failures = 3
    assert b.check_resting(o, snap()) is None and len(ex.created) == 1   # still unknown: not re-placed
    ex.lookup_failures = 3
    b.cancel_resting(o)
    assert o.cancel_failures == 1                                          # cannot confirm a cancel either
    assert b.check_resting(o, snap()) is None                              # lookup works: adopted
    assert o.pending_client_id is None and o.exchange_order_id == ex.created[0]["id"] and len(ex.created) == 1


# --- cancel failures --------------------------------------------------------------------------
class CancelDown(FakeExchange):
    cancel_failures = 0

    def cancel_order(self, oid, symbol=None):
        if self.cancel_failures:
            self.cancel_failures -= 1
            raise ccxt.RequestTimeout("cancel timed out")
        return super().cancel_order(oid, symbol)


def test_failed_cancel_keeps_the_order_and_escalates():
    ex = CancelDown([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                    {SPOT: 100_000.0, PERP: 100_000.0})
    b = LiveBroker({"okx": ex}, CostModel(load_settings()), max_order_usd=300.0, sleep=lambda s: None)
    o = resting()
    b.place_resting(o)
    oid = o.exchange_order_id
    ex.cancel_failures = 3
    for n in (1, 2, 3):
        assert b.cancel_resting(o) is None
        assert o.exchange_order_id == oid and o.cancel_failures == n and ex.orders[oid]["status"] == "open"
    assert "ESCALATE" in b.events[-1] and "cancel failed" in b.events[-1]
    assert b.cancel_resting(o) is None
    assert o.exchange_order_id is None and o.cancel_failures == 0      # 4th attempt went through
    assert ex.orders[oid]["status"] == "canceled"


def test_replace_with_failed_cancel_does_not_post_a_second_order():
    ex = CancelDown([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                    {SPOT: 100_000.0, PERP: 100_000.0})
    b = LiveBroker({"okx": ex}, CostModel(load_settings()), max_order_usd=300.0, sleep=lambda s: None)
    o = resting()
    b.place_resting(o)
    ex.cancel_failures = 1
    b.replace_resting(o, 100_105.0)
    assert len(ex.created) == 1 and o.price == 100_005.0 and o.cancel_failures == 1


def test_engine_keeps_order_whose_cancel_failed():
    ex = CancelDown([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                    {SPOT: 100_000.0, PERP: 100_000.0})
    b = LiveBroker({"okx": ex}, CostModel(load_settings()), max_order_usd=300.0, sleep=lambda s: None)
    s = load_settings()
    pf = Portfolio(1000.0)
    legs = [OrderIntent("g1", "okx", SPOT, "spot", "buy", 0.002, exec_style="maker_first"),
            OrderIntent("g1", "okx", PERP, "swap", "sell", 0.002, exec_style="maker_first", maker=True)]
    eng = TradingEngine(Once(legs), b, pf, RiskManager(s, 1000.0), on_event=lambda m: None,
                        execution=ExecutionCfg(entry_timeout_s=60))
    eng.step(snap())
    ex.cancel_failures = 1
    eng.step(snap(T0 + 60_000))                     # entry timeout, cancel fails
    assert len(pf.open_orders) == 1 and "entry_cancels" not in pf.exec_stats
    eng.step(snap(T0 + 90_000))                     # retried
    assert pf.open_orders == [] and pf.exec_stats["entry_cancels"] == 1
    assert all(o["status"] != "open" for o in ex.orders.values())


# --- dust ---------------------------------------------------------------------------------------
def test_maker_qty_rounded_to_tradable_amount_at_placement():
    ex, b = make()
    o = resting(qty=0.0019999)                      # 0.19999 contracts -> 0.19
    b.place_resting(o)
    assert ex.created[0]["amount"] == pytest.approx(0.19) and o.qty == pytest.approx(0.0019)
    ex.fill_resting(o.exchange_order_id)
    f = b.check_resting(o, snap())
    assert f.qty == pytest.approx(0.0019) and o.done


def test_fully_filled_closed_order_is_done_even_if_net_qty_differs():
    """Spot maker buy with the fee taken in BTC: the fill is net of the fee, the order is still done."""
    ex, b = make(fee_in_base_on_spot_buy=True)
    o = RestingOrder("o1", "g1", "okx", SPOT, "spot", "buy", 0.002, 99_995.0, T0, T0, False, "entry",
                     {"exchange": "okx", "symbol": PERP, "market": "swap", "side": "sell", "reduce_only": False})
    b.place_resting(o)
    ex.fill_resting(o.exchange_order_id)
    f = b.check_resting(o, snap())
    assert f.qty < 0.002 and o.done                 # remainder (the fee) is not left resting


def test_remainder_below_exchange_minimum_is_done():
    ex, b = make()
    o = resting(qty=0.00005)                        # 0.005 contracts < min 0.01
    b.place_resting(o)
    assert o.done and ex.created == [] and "treated as done" in b.events[-1]
