"""Chaos (failure-injection) tests: the bot must end in a SAFE state when the exchange or data misbehave.

Safe means: hedged or flat (bot portfolio == what the exchange really filled), no doubled orders,
no crash, and the problem surfaced as an event / alert. Two harnesses:

- Rig: TradingEngine + LiveBroker + ChaosExchange (engine-level, snapshot-driven, fake time).
- LiveTrader tests (make_trader): the whole live loop incl. persisted state, Telegram and reconcile.

Defects this suite found were fixed (they used to be xfail(strict=True, reason="BUG: ...")); the
tests below now guard them as regressions.
"""
import itertools
import json

import ccxt
import pytest

from arb.config import ExecutionCfg, load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import MarketSnapshot, OrderIntent, Quote, Requirements
from arb.costs import CostModel
from arb.execution.live_broker import LiveBroker
from arb.live.runner import LiveTrader
from arb.live.safety import LIVE_CONFIRM_PHRASE
from arb.strategies.base import Strategy
from arb.strategies.funding_carry import FundingCarry
from arb.models import FundingInfo, OrderBook
from tests.fake_ccxt import FakeExchange, perp, spot
from tests.fakes import FakeSource

T0 = 1_760_000_000_000
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"
PX = 100_000.0
QTY = 0.002  # $200 per leg; spot step 0.0001, perp 0.2 contracts (step 0.01)
EX = "okx"
ENV = {"ARB_LIVE_CONFIRM": LIVE_CONFIRM_PHRASE}


# --- chaos exchange ---------------------------------------------------------------------------
class ChaosExchange(FakeExchange):
    """FakeExchange plus injectable faults.

    create_faults: raised BEFORE the order is recorded (the order never reached the exchange).
    fill_ratio / stay_open: per-symbol market-order behaviour (FakeExchange's are global).
    cancel_faults: raised by cancel_order BEFORE it cancels (the order stays live).
    on_cancel: callback run once just before the next cancel_order (a fill racing the cancel).
    fetch_orders_faults: raised by fetch_orders (the client-id lookup used after a network error).
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.create_faults: list[dict] = []
        self.fill_ratio: dict[str, float] = {}
        self.stay_open: set[str] = set()
        self.cancel_faults: list[Exception] = []
        self.on_cancel = None
        self.fetch_orders_faults: list[Exception] = []

    def fail(self, when, exc=ccxt.ExchangeError, times=10**6, msg="injected"):
        self.create_faults.append({"when": when, "exc": exc, "times": times, "msg": msg})

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        for f in self.create_faults:
            if f["times"] > 0 and f["when"](symbol, type_, side, params or {}):
                f["times"] -= 1
                self.calls.append("create_rejected")
                raise f["exc"](f["msg"])
        saved = self.market_order_fill_ratio, self.market_orders_stay_open
        self.market_order_fill_ratio = self.fill_ratio.get(symbol, saved[0])
        self.market_orders_stay_open = symbol in self.stay_open or saved[1]
        try:
            return super().create_order(symbol, type_, side, amount, price, params)
        finally:
            self.market_order_fill_ratio, self.market_orders_stay_open = saved

    def cancel_order(self, oid, symbol=None):
        if self.on_cancel is not None:
            cb, self.on_cancel = self.on_cancel, None
            cb(oid)
        if self.cancel_faults:
            self.calls.append("cancel_rejected")
            raise self.cancel_faults.pop(0)
        return super().cancel_order(oid, symbol)

    def fetch_orders(self, symbol=None, since=None):
        if self.fetch_orders_faults:
            raise self.fetch_orders_faults.pop(0)
        return super().fetch_orders(symbol, since)

    # --- inspection ---
    def net(self, symbol) -> float:
        """Signed base-unit position implied by everything that filled on the exchange."""
        m = self.markets[symbol]
        cs = float(m["contractSize"] or 1.0) if m["contract"] else 1.0
        return sum((o["filled"] if o["side"] == "buy" else -o["filled"]) * cs for o in self.created if o["symbol"] == symbol)

    def open_orders(self) -> list[dict]:
        return [o for o in self.orders.values() if o["status"] == "open"]

    def orders_of(self, symbol=None, type_=None) -> list[dict]:
        return [o for o in self.created if (symbol is None or o["symbol"] == symbol)
                and (type_ is None or o["type"] == type_)]


def make_ex(**kw) -> ChaosExchange:
    return ChaosExchange([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                         {SPOT: PX, PERP: PX}, **kw)


def is_market(symbol):
    return lambda s, t, side, p: s == symbol and t == "market"


# --- engine-level rig -------------------------------------------------------------------------
class Scripted(Strategy):
    """Emits script[n] on the n-th on_snapshot call."""

    def __init__(self, script):
        self.script, self.n = script, 0

    def requirements(self):
        return Requirements()

    def on_snapshot(self, snapshot, portfolio):
        out = self.script.get(self.n, [])
        self.n += 1
        return out


def snap(sec=0, px=PX, spot_age_s=0.0, perp_age_s=0.0) -> MarketSnapshot:
    ts = T0 + int(sec * 1000)
    return MarketSnapshot(ts, {(EX, SPOT): Quote(px - 5, px + 5, ts - int(spot_age_s * 1000)),
                               (EX, PERP): Quote(px - 5, px + 5, ts - int(perp_age_s * 1000))})


def maker_entry(gid="g1", qty=QTY, style="maker_first"):
    return [OrderIntent(gid, EX, SPOT, "spot", "buy", qty, tag="entry", exec_style=style),
            OrderIntent(gid, EX, PERP, "swap", "sell", qty, tag="entry", exec_style=style, maker=style == "maker_first")]


def taker_entry(gid="g1", qty=QTY):
    return [OrderIntent(gid, EX, SPOT, "spot", "buy", qty, tag="entry"),
            OrderIntent(gid, EX, PERP, "swap", "sell", qty, tag="entry")]


def maker_exit(gid="g1", qty=QTY):
    return [OrderIntent(gid, EX, SPOT, "spot", "sell", qty, reduce_only=True, tag="exit", exec_style="maker_first"),
            OrderIntent(gid, EX, PERP, "swap", "buy", qty, reduce_only=True, tag="exit", exec_style="maker_first",
                        maker=True)]


class Rig:
    def __init__(self, strategy, ex=None, **exec_kw):
        exec_kw.setdefault("entry_timeout_s", 3600.0)
        exec_kw.setdefault("exit_timeout_s", 3600.0)
        self.settings = load_settings()
        self.ex = ex or make_ex()
        self.clock = itertools.count(1_000, 5)  # fake time for taker timeouts; advances on every read
        self.broker = LiveBroker({EX: self.ex}, CostModel(self.settings), max_order_usd=300.0,
                                 sleep=lambda s: None, clock=lambda: float(next(self.clock)))
        self.pf = Portfolio(1100.0)
        self.risk = RiskManager(self.settings, 1100.0)
        self.events: list[str] = []
        self.eng = TradingEngine(strategy, self.broker, self.pf, self.risk, on_event=self.events.append,
                                 execution=ExecutionCfg(**exec_kw))

    def step(self, sec=0, **kw):
        return self.eng.step(snap(sec, **kw))

    @property
    def order(self):
        [o] = self.pf.open_orders
        return o

    def place_maker(self, sec=0):
        self.step(sec)
        return self.order.exchange_order_id

    def assert_consistent(self):
        """The bot's books equal what actually filled on the exchange, per instrument."""
        assert self.pf.net_qty(EX, SPOT, "spot") == pytest.approx(self.ex.net(SPOT), abs=1e-9)
        assert self.pf.net_qty(EX, PERP, "swap") == pytest.approx(self.ex.net(PERP), abs=1e-9)

    def assert_hedged(self, qty=None):
        s, p = self.pf.net_qty(EX, SPOT, "spot"), self.pf.net_qty(EX, PERP, "swap")
        assert s + p == pytest.approx(0, abs=1e-9)
        if qty is not None:
            assert s == pytest.approx(qty)

    def all_events(self):
        return self.events + self.broker.events


# --- 1. taker hedge fails after the maker fill ------------------------------------------------
@pytest.mark.parametrize("exc", [ccxt.ExchangeError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable,
                                 ccxt.InsufficientFunds, ccxt.DDoSProtection])
def test_hedge_exception_is_zero_fill_and_maker_excess_unwound(exc):
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    rig.ex.fail(is_market(SPOT), exc)  # every hedge attempt (incl. network retries) fails
    rig.ex.fill_resting(oid)
    rig.step(30)

    assert rig.ex.orders_of(SPOT) == []                       # the hedge never reached the exchange
    [unwind] = rig.ex.orders_of(PERP, "market")
    assert unwind["side"] == "buy" and unwind["params"]["reduceOnly"] is True
    assert rig.pf.positions == {} and rig.pf.open_orders == []   # flat, not a naked short
    assert rig.ex.net(PERP) == pytest.approx(0)
    rig.assert_consistent()
    assert any("hedge under-filled" in e for e in rig.events)             # engine event
    assert any("failed" in e and SPOT in e for e in rig.broker.events)    # broker anomaly for the alert
    assert not rig.ex.open_orders()


def test_hedge_and_unwind_both_fail_then_next_strategy_step_closes_via_taker():
    s = load_settings()
    cfg = s.strategies.funding_carry.model_copy(
        update={"exchange": EX, "bases": ["BTC"], "avg_window": 3, "execution": "maker", "maker_leg": "perp",
                "notional_usd": QTY * (PX + 5)})
    strat = FundingCarry(cfg, CostModel(s))
    strat.seed_funding_history(EX, PERP, [0.0003] * 3)
    rig = Rig(strat)
    oid = rig.place_maker()
    assert rig.order.qty == pytest.approx(QTY)
    rig.ex.fail(is_market(SPOT))      # hedge fails ...
    rig.ex.fail(is_market(PERP))      # ... and so does the unwind
    rig.ex.fill_resting(oid)
    rig.step(30)

    # unbalanced: naked short perp, but it is visible (events) and the order is not left resting
    assert rig.pf.net_qty(EX, PERP, "swap") == pytest.approx(-QTY) and rig.pf.net_qty(EX, SPOT, "spot") == 0
    rig.assert_consistent()
    assert any("hedge under-filled" in e for e in rig.events) and rig.broker.events
    assert rig.pf.open_orders == []

    rig.ex.create_faults.clear()      # exchange recovers
    rig.step(60)                       # FundingCarry sees an unbalanced group -> taker close
    assert rig.pf.positions == {}
    rig.assert_consistent()
    closers = [o for o in rig.ex.orders_of(PERP, "market")]
    assert len(closers) == 1 and closers[0]["side"] == "buy" and closers[0]["params"]["reduceOnly"] is True


async def test_hedge_and_unwind_both_fail_in_live_trader_alerts(tmp_path):
    """Same double failure through LiveTrader: alerts go out and the next cycle flattens the naked leg."""
    # FundingCarry requires a funding quote for its perp; without one build_snapshot skips every
    # cycle ("missing funding") and no order is ever placed (the old IndexError was this test-setup gap).
    t, ex, notes = make_trader(tmp_path, strategy=carry_strategy(), src=with_funding(make_source()))
    await t.run_once()
    oid = t.portfolio.open_orders[0].exchange_order_id
    ex.fail(is_market(SPOT))
    ex.fail(is_market(PERP))
    ex.fill_resting(oid)
    await t.run_once()
    assert t.portfolio.net_qty(EX, PERP, "swap") == pytest.approx(-QTY)
    assert any("hedge under-filled" in m for m in notes.msgs) or any("failed" in m for m in notes.msgs)
    assert any("failed" in m for m in notes.msgs)
    ex.create_faults.clear()
    await t.run_once()
    assert t.portfolio.positions == {}
    assert ex.net(PERP) == pytest.approx(0) and ex.net(SPOT) == pytest.approx(0)


# --- 2. partial taker fill on one leg of a taker entry ----------------------------------------
@pytest.mark.parametrize("short_leg, other, other_side", [(PERP, SPOT, "sell"), (SPOT, PERP, "buy")])
def test_partial_taker_leg_unwinds_the_other_leg_to_equal(short_leg, other, other_side):
    rig = Rig(Scripted({0: taker_entry()}))
    rig.ex.fill_ratio[short_leg] = 0.5
    rig.step(0)

    assert rig.pf.open_orders == [] and rig.ex.open_orders() == []
    rig.assert_hedged(QTY / 2)
    rig.assert_consistent()
    unwinds = [o for o in rig.ex.orders_of(other, "market") if o["params"].get("clientOrderId")][1:]
    assert len(unwinds) == 1 and unwinds[0]["side"] == other_side
    assert any("leg failure" in e for e in rig.events)


def test_taker_entry_where_one_leg_fills_nothing_ends_flat():
    rig = Rig(Scripted({0: taker_entry()}))
    rig.ex.fail(is_market(PERP), ccxt.ExchangeError)
    rig.step(0)
    assert rig.pf.positions == {}
    rig.assert_consistent()
    assert any("leg failure" in e for e in rig.events)
    assert any("failed" in e for e in rig.broker.events)


# --- 3. network error after the order was accepted --------------------------------------------
def test_network_error_after_accepted_hedge_is_not_doubled():
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    rig.ex.fill_resting(oid)
    rig.ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")  # next create = the spot hedge
    rig.step(30)

    assert len(rig.ex.orders_of(SPOT)) == 1                  # exactly one hedge on the exchange
    rig.assert_hedged(QTY)
    rig.assert_consistent()
    assert rig.pf.open_orders == []


def test_network_error_on_maker_placement_is_not_doubled():
    rig = Rig(Scripted({0: maker_entry()}))
    rig.ex.fail_next_create = ccxt.NetworkError("accepted, response lost")
    rig.step(0)
    assert len(rig.ex.orders_of(PERP, "limit")) == 1
    assert rig.order.exchange_order_id == rig.ex.orders_of(PERP, "limit")[0]["id"]
    rig.step(30)  # still resting, not re-placed
    assert len(rig.ex.orders_of(PERP, "limit")) == 1


def test_accepted_hedge_with_failing_lookup_must_not_be_treated_as_zero_fill():
    """Order accepted, response lost AND the client-id lookup also errors."""
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    rig.ex.fill_resting(oid)
    rig.ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")
    rig.ex.fetch_orders_faults.append(ccxt.NetworkError("lookup down"))
    rig.step(30)
    # Whatever the bot decided, its books must match the exchange (no unknown naked leg).
    rig.assert_consistent()
    # the lookup is retried: the accepted hedge is found and booked, nothing is unwound
    rig.assert_hedged(QTY)
    assert len(rig.ex.orders_of(SPOT)) == 1 and rig.ex.orders_of(PERP, "market") == []


def test_hedge_lookup_down_for_good_is_unknown_not_zero_then_resolved():
    """Create times out and every client-id lookup fails: the hedge is UNKNOWN, not a zero fill."""
    rig = Rig(Scripted({0: maker_entry(), 1: taker_entry("g2"), 2: taker_entry("g3")}))
    oid = rig.place_maker()
    rig.ex.fill_resting(oid)
    rig.ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")
    rig.ex.fetch_orders_faults += [ccxt.NetworkError("lookup down")] * 3   # all retries of the lookup
    res = rig.step(30)

    assert rig.ex.orders_of(PERP, "market") == []            # maker leg NOT unwound against a guess
    assert rig.pf.net_qty(EX, PERP, "swap") == pytest.approx(-QTY)
    [u] = rig.pf.unresolved_orders
    assert u["symbol"] == SPOT and u["side"] == "buy" and u["group_id"] == "g1" and u["client_order_id"].startswith("arb")
    assert rig.risk.state.paused and "unknown" in rig.risk.state.pause_reason
    assert any("ORDER STATE UNKNOWN" in e for e in rig.events)
    assert any("UNKNOWN order state" in e for e in rig.broker.events)
    assert [f for f in res.fills if f.unknown]
    assert [i.group_id for i, r in res.rejected] == ["g2", "g2"]   # entries paused

    rig.step(60)                                                 # lookup works again -> resolved & booked
    assert rig.pf.unresolved_orders == []
    rig.assert_hedged(QTY)
    rig.assert_consistent()
    assert any("RESOLVED" in e for e in rig.events)
    assert rig.risk.state.paused                                 # a human still has to /resume
    assert len(rig.ex.orders_of(SPOT)) == 1                      # never re-sent


async def test_unknown_hedge_in_live_trader_pauses_alerts_and_survives_restart(tmp_path):
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")))
    await t.run_once()
    [o] = t.portfolio.open_orders
    ex.fill_resting(o.exchange_order_id)
    ex.fail_next_create = ccxt.RequestTimeout("accepted, response lost")
    ex.fetch_orders_faults += [ccxt.NetworkError("lookup down")] * 3
    await t.run_once()
    assert t.risk.state.paused and t.portfolio.unresolved_orders
    assert any("UNKNOWN" in m for m in notes.msgs)
    assert ex.orders_of(PERP, "market") == []                 # no unwind on a guess

    t2, _, notes2 = make_trader(tmp_path, ex=ex)              # crash/restart: the unknown order is not forgotten
    assert [u["client_order_id"] for u in t2.portfolio.unresolved_orders] == \
        [u["client_order_id"] for u in t.portfolio.unresolved_orders]
    assert t2.risk.state.paused
    await t2.run_once()                                       # resolved by client id and booked
    assert t2.portfolio.unresolved_orders == []
    assert t2.portfolio.net_qty(EX, SPOT, "spot") == pytest.approx(QTY) == ex.net(SPOT)
    assert t2.portfolio.net_qty(EX, PERP, "swap") == pytest.approx(-QTY) == ex.net(PERP)
    assert len(ex.orders_of(SPOT)) == 1 and any("RESOLVED" in m for m in notes2.msgs)


def test_unknown_order_that_never_reached_exchange_resolves_as_zero_and_group_is_closed():
    rig = Rig(carry_strategy())
    oid = rig.place_maker()
    rig.ex.fill_resting(oid)
    rig.ex.fail(is_market(SPOT), ccxt.RequestTimeout, times=1)   # never reached the exchange ...
    rig.ex.fetch_orders_faults += [ccxt.NetworkError("lookup down")] * 3   # ... but we cannot tell
    rig.step(30)
    assert rig.pf.unresolved_orders and rig.ex.orders_of(SPOT) == []
    assert rig.pf.net_qty(EX, PERP, "swap") == pytest.approx(-QTY)   # group frozen, not closed on a guess
    assert rig.ex.orders_of(PERP, "market") == []
    rig.step(60)       # lookup confirms there is no such order -> zero fill; the unbalanced group is closed
    assert rig.pf.unresolved_orders == [] and rig.pf.positions == {}
    rig.assert_consistent()



# --- 4. taker order stuck open ----------------------------------------------------------------
@pytest.mark.parametrize("ratio", [0.0, 0.5])
def test_stuck_taker_hedge_is_cancelled_and_partial_reported(ratio):
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    rig.ex.stay_open.add(SPOT)
    rig.ex.fill_ratio[SPOT] = ratio
    rig.ex.fill_resting(oid)
    rig.step(30)

    [hedge] = rig.ex.orders_of(SPOT)
    assert hedge["status"] == "canceled" and hedge["filled"] == pytest.approx(QTY * ratio)
    assert any("timed out" in e for e in rig.broker.events)
    assert any("hedge under-filled" in e for e in rig.events)
    assert rig.ex.open_orders() == []                         # nothing left live
    rig.assert_hedged(QTY * ratio)                            # perp excess unwound to match
    rig.assert_consistent()


def test_stuck_taker_unwind_partial_leaves_group_that_strategy_closes():
    """The unwind order is itself stuck at 50%: group is unbalanced (visible), next carry step flattens it."""
    s = load_settings()
    cfg = s.strategies.funding_carry.model_copy(
        update={"exchange": EX, "bases": ["BTC"], "avg_window": 3, "execution": "maker", "maker_leg": "perp",
                "notional_usd": QTY * (PX + 5)})
    strat = FundingCarry(cfg, CostModel(s))
    strat.seed_funding_history(EX, PERP, [0.0003] * 3)
    rig = Rig(strat)
    oid = rig.place_maker()
    rig.ex.fail(is_market(SPOT))                  # hedge: zero fill
    rig.ex.stay_open.add(PERP)
    rig.ex.fill_ratio[PERP] = 0.5                 # unwind: only half
    rig.ex.fill_resting(oid)
    rig.step(30)
    # The unwind leaves -QTY/2. In the SAME step FundingCarry then sees the group unbalanced (no
    # spot) and sends its taker close for that QTY/2, which is also stuck at 50% -> -QTY/4. That is
    # the intended order of the engine step (resting work, then strategy), not a bug: the books still
    # equal the exchange and nothing is left open.
    assert rig.pf.net_qty(EX, PERP, "swap") == pytest.approx(-QTY / 4)
    closes = [o for o in rig.ex.orders_of(PERP, "market") if o["side"] == "buy"]
    assert [o["amount"] for o in closes] == pytest.approx([0.2, 0.1])   # unwind, then the strategy's close
    rig.assert_consistent()
    assert rig.ex.open_orders() == []
    rig.ex.create_faults.clear()
    rig.ex.stay_open.clear()
    rig.ex.fill_ratio.clear()
    rig.step(60)
    assert rig.pf.positions == {}
    rig.assert_consistent()


# --- 5. post-only rejected repeatedly ---------------------------------------------------------
def test_post_only_always_crossing_never_places_and_times_out_cleanly():
    rig = Rig(Scripted({0: maker_entry()}), entry_timeout_s=600)
    rig.ex.prices[PERP] = PX + 50  # market moved above our sell price: every post-only would match
    for sec in range(0, 600, 30):
        rig.step(sec)
        assert rig.ex.created == []
        assert len(rig.pf.open_orders) == 1 and rig.order.exchange_order_id is None
    rig.step(600)
    assert rig.pf.open_orders == [] and rig.pf.positions == {}
    assert rig.ex.created == [] and rig.pf.exec_stats["entry_cancels"] == 1
    rig.step(630)  # strategy is silent now; nothing sneaks out
    assert rig.ex.created == []


# --- live trader harness ----------------------------------------------------------------------
class OkxChaos(ChaosExchange):
    perm = "read_only,trade"

    def private_get_account_config(self):
        return {"data": [{"perm": self.perm}]}


class Hold(Strategy):
    def __init__(self, intents=None):
        self.intents = intents or []

    def requirements(self):
        return Requirements(quotes=[(EX, SPOT, "spot"), (EX, PERP, "swap")], funding=[])

    def on_snapshot(self, snapshot, portfolio):
        out, self.intents = self.intents, []
        return out


def carry_strategy():
    s = load_settings()
    cfg = s.strategies.funding_carry.model_copy(
        update={"exchange": EX, "bases": ["BTC"], "avg_window": 3, "execution": "maker", "maker_leg": "perp",
                "notional_usd": QTY * (PX + 5)})
    strat = FundingCarry(cfg, CostModel(s))
    strat.seed_funding_history(EX, PERP, [0.0003] * 3)
    return strat


class Cmds:
    def __init__(self, *cmds):
        self.queue = list(cmds)
        self.enabled = True

    def poll(self):
        out, self.queue = self.queue, []
        return out


class Notes:
    def __init__(self):
        self.msgs = []

    def send(self, m):
        self.msgs.append(m)
        return True


class FlakySource(FakeSource):
    """FakeSource whose books can be made to raise or come back empty."""

    def __init__(self):
        super().__init__()
        self.raise_for: set[str] = set()
        self.empty_for: set[str] = set()

    async def order_book(self, exchange, symbol, depth=20):
        if symbol in self.raise_for:
            raise ccxt.RequestTimeout("market data timeout")
        if symbol in self.empty_for:
            return OrderBook(exchange, symbol, [], [], 0)
        return await super().order_book(exchange, symbol, depth)


NOW_S = 1000.0


def live_settings(**kw):
    s = load_settings().model_copy(update={"live_trading": True})
    s.live = s.live.model_copy(update={"max_order_usd": 300.0, "capital_usd": 1100.0, **kw})
    return s


def with_funding(src, rate=0.0003, next_s=NOW_S + 3600):
    src.add_funding(FundingInfo(EX, PERP, rate, 8.0, PX, PX, int(next_s * 1000)))
    return src


def make_source(ts_s=NOW_S, perp_ts_s=None, src=None):
    src = src or FlakySource()
    src.add_book(EX, SPOT, [(PX - 5, 1.0)], [(PX + 5, 1.0)], ts=int(ts_s * 1000))
    src.add_book(EX, PERP, [(PX - 5, 1.0)], [(PX + 5, 1.0)], ts=int((perp_ts_s or ts_s) * 1000))
    return src


def make_ex_live() -> OkxChaos:
    return OkxChaos([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
                    {SPOT: PX, PERP: PX})


def make_trader(tmp_path, strategy=None, ex=None, cmds=None, src=None, clock=None, **live):
    ex = ex or make_ex_live()
    notes = Notes()
    clock = clock or (lambda: NOW_S)
    t = LiveTrader(live_settings(**live), strategy or Hold(), src or make_source(), {EX: ex}, tmp_path,
                   notifier=notes, commands=cmds or Cmds(), env=ENV, clock=clock)
    t.broker._sleep = lambda s: None
    return t, ex, notes


def events_log(t):
    p = t.dir / "events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def entry_legs(gid="g", qty=QTY, style="taker"):
    return maker_entry(gid, qty, style) if style == "maker_first" else taker_entry(gid, qty)


# --- 6. market data failure -------------------------------------------------------------------
@pytest.mark.parametrize("mode, symbols", [("raise_for", {PERP}), ("raise_for", {SPOT, PERP}),
                                           ("empty_for", {SPOT}), ("empty_for", {PERP})])
async def test_market_data_failure_skips_cycle_and_sends_no_orders(tmp_path, mode, symbols):
    src = make_source()
    legs = entry_legs()
    t, ex, notes = make_trader(tmp_path, strategy=Hold(legs), src=src)
    getattr(src, mode).update(symbols)

    res = await t.run_once()
    assert res is None and ex.created == [] and t.portfolio.positions == {}
    assert any(e["type"] == "skipped_cycle" and e["errors"] for e in events_log(t))
    assert t.strategy.intents == legs                  # strategy was never even consulted

    src.raise_for.clear(); src.empty_for.clear()       # data recovers: trading resumes normally
    res = await t.run_once()
    assert res is not None and len(ex.created) == 2
    assert t.portfolio.net_qty(EX, PERP, "swap") == pytest.approx(-QTY)


async def test_market_data_failure_does_not_stop_run_loop(tmp_path):
    src = make_source()
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs()), src=src)
    src.raise_for.add(PERP)

    async def nosleep(_):
        pass
    t._sleep = nosleep
    await t.run(cycles=3)
    assert ex.created == []


# --- 7. stale quotes --------------------------------------------------------------------------
@pytest.mark.parametrize("which", ["spot", "perp", "both"])
async def test_stale_quote_rejects_new_entries(tmp_path, which):
    old = NOW_S - 60  # max_quote_age_s is 15
    src = make_source(ts_s=old if which == "both" else NOW_S,
                      perp_ts_s=old if which in ("perp", "both") else None)
    if which == "spot":
        src.add_book(EX, SPOT, [(PX - 5, 1.0)], [(PX + 5, 1.0)], ts=int(old * 1000))
    assert t_age(old) > load_settings().risk.max_quote_age_s
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs()), src=src)
    res = await t.run_once()
    assert ex.created == [] and t.portfolio.positions == {}
    assert res.rejected and all("stale quote" in r for _, r in res.rejected)
    assert any(e["type"] == "rejected" and "stale" in e["reason"] for e in events_log(t))


def t_age(ts_s):
    return NOW_S - ts_s


async def test_stale_quote_rejects_maker_entry_too(tmp_path):
    t, ex, _ = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")), src=make_source(NOW_S - 60))
    res = await t.run_once()
    assert ex.created == [] and t.portfolio.open_orders == [] and res.rejected


# --- 8. crash between maker fill and hedge ----------------------------------------------------
async def test_crash_after_maker_fill_restart_pauses_alerts_and_hedges_known_order(tmp_path):
    """State saved while the maker order rested; it fills on the exchange; the process dies before hedging."""
    t1, ex, _ = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")))
    await t1.run_once()
    [o] = t1.portfolio.open_orders
    ex.fill_resting(o.exchange_order_id)                      # filled while the bot was down ...
    ex.positions = [{"symbol": PERP, "contracts": QTY / 0.01, "side": "short"}]   # ... no hedge was sent
    assert len(ex.created) == 1

    t2, _, notes = make_trader(tmp_path, ex=ex)               # restart from the persisted state.json
    assert t2.risk.state.paused and "reconcile mismatch" in t2.risk.state.pause_reason
    assert any("RECONCILE MISMATCH" in m for m in notes.msgs)
    assert len(ex.created) == 1                               # startup itself sends nothing

    await t2.run_once()                                       # the tracked order is found filled -> hedged
    spot_orders = ex.orders_of(SPOT)
    assert len(spot_orders) == 1 and spot_orders[0]["side"] == "buy" and spot_orders[0]["type"] == "market"
    assert t2.portfolio.net_qty(EX, SPOT, "spot") == pytest.approx(QTY)
    assert t2.portfolio.net_qty(EX, PERP, "swap") == pytest.approx(-QTY)
    assert t2.risk.state.paused                               # still needs a human /resume
    ex.balances = {"BTC": QTY}
    assert t2.safety_check() == []                            # books now agree with the exchange


async def test_crash_with_unknown_naked_short_pauses_and_sends_nothing(tmp_path):
    """State predates the order entirely: the bot knows nothing about a short perp on the exchange."""
    ex = make_ex_live()
    ex.positions = [{"symbol": PERP, "contracts": QTY / 0.01, "side": "short"}]
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")), ex=ex)
    assert t.risk.state.paused and any("RECONCILE MISMATCH" in m for m in notes.msgs)
    res = await t.run_once()
    assert ex.created == [] and t.portfolio.open_orders == []
    assert res.rejected and all("paused" in r for _, r in res.rejected)
    # paused state survives another restart
    t2, _, _ = make_trader(tmp_path, ex=ex)
    assert t2.risk.state.paused


async def test_orphan_resting_order_that_fills_is_caught_by_periodic_reconcile(tmp_path):
    """Crash after the order was sent but before state was persisted: the order is unknown to the bot."""
    now = [NOW_S]
    ex = make_ex_live()
    t, ex, notes = make_trader(tmp_path, ex=ex, clock=lambda: now[0])
    ex.create_order(PERP, "limit", "sell", QTY / 0.01, PX + 5, {"postOnly": True, "clientOrderId": "orphan"})
    oid = ex.created[0]["id"]
    assert not t.risk.state.paused                            # nothing to see yet (positions are flat)
    ex.fill_resting(oid)
    ex.positions = [{"symbol": PERP, "contracts": QTY / 0.01, "side": "short"}]
    now[0] += t.settings.live.reconcile_every_s + 1
    await t.run_once()
    assert t.risk.state.paused and any("RECONCILE MISMATCH" in m for m in notes.msgs)
    assert len(ex.created) == 1                               # no automatic trading to "fix" it


async def test_startup_flags_unknown_open_order_on_exchange(tmp_path):
    ex = make_ex_live()
    ex.create_order(PERP, "limit", "sell", QTY / 0.01, PX + 5, {"postOnly": True, "clientOrderId": "orphan"})
    t, ex, notes = make_trader(tmp_path, ex=ex)
    assert t.risk.state.paused and "reconcile mismatch" in t.risk.state.pause_reason
    assert any("RECONCILE MISMATCH" in m and "not tracked" in m for m in notes.msgs)
    assert len(ex.open_orders()) == 1                         # not ours (no "arb" prefix): left alone


async def test_startup_cancels_untracked_open_order_with_bot_prefix(tmp_path):
    ex = make_ex_live()
    ex.create_order(PERP, "limit", "sell", QTY / 0.01, PX + 5, {"postOnly": True, "clientOrderId": "arb" + "0" * 24})
    t, ex, notes = make_trader(tmp_path, ex=ex)
    assert t.risk.state.paused and ex.open_orders() == []
    assert any("CANCELLED" in m for m in notes.msgs)
    assert len(ex.created) == 1                               # nothing traded


async def test_resting_order_is_persisted_before_the_cycle_ends(tmp_path):
    """A crash later in the same cycle must not lose the order that was just placed."""
    t, ex, _ = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")))

    def crash(_snap):
        raise RuntimeError("process died")
    t.portfolio.record_equity = crash                        # dies after placement, before end-of-cycle persist
    with pytest.raises(RuntimeError):
        await t.run_once()
    [placed] = ex.open_orders()
    t2, _, notes = make_trader(tmp_path, ex=ex)              # restart from state.json
    assert [o.exchange_order_id for o in t2.portfolio.open_orders] == [placed["id"]]
    assert not t2.risk.state.paused                          # known order: no mismatch


# --- 9. Telegram /kill while a maker entry rests partially filled -----------------------------
async def test_kill_cancels_partially_filled_resting_entry_and_flattens(tmp_path):
    cmds = Cmds()
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")), cmds=cmds)
    await t.run_once()
    [o] = t.portfolio.open_orders
    oid = o.exchange_order_id
    assert oid and ex.orders[oid]["status"] == "open"
    ex.fill_resting(oid, 0.1)                                 # 0.1 of 0.2 contracts filled, bot has not looked yet
    cmds.queue = ["/kill"]
    await t.run_once()

    assert ex.orders[oid]["status"] == "canceled" and ex.open_orders() == []
    assert t.portfolio.open_orders == [] and t.portfolio.positions == {}
    assert t.risk.state.killed and "telegram /kill" in t.risk.state.kill_reason
    assert ex.net(PERP) == pytest.approx(0) and ex.net(SPOT) == pytest.approx(0)
    assert any("KILL" in m for m in notes.msgs)
    n = len(ex.created)
    await t.run_once()                                        # stays killed: nothing more is sent
    assert len(ex.created) == n and ex.open_orders() == []


async def test_kill_while_entry_resting_unfilled(tmp_path):
    cmds = Cmds()
    t, ex, _ = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")), cmds=cmds)
    await t.run_once()
    cmds.queue = ["/kill"]
    await t.run_once()
    assert ex.open_orders() == [] and t.portfolio.open_orders == [] and t.risk.state.killed
    assert len(ex.created) == 1                               # only the original post-only order


async def test_kill_with_cancel_failure_must_not_forget_live_order(tmp_path):
    cmds = Cmds()
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")), cmds=cmds)
    await t.run_once()
    ex.cancel_faults.append(ccxt.RequestTimeout("cancel timed out"))
    cmds.queue = ["/kill"]
    await t.run_once()
    # either the bot still tracks the order (so the next cycle retries the cancel) or it is really gone
    assert t.portfolio.open_orders or ex.open_orders() == []
    assert ex.open_orders() == [] and t.portfolio.open_orders == []   # retried within the kill cycle


async def test_kill_keeps_retrying_failed_cancels_every_cycle_and_still_flattens(tmp_path):
    cmds = Cmds()
    t, ex, notes = make_trader(tmp_path, strategy=Hold(entry_legs(style="maker_first")), cmds=cmds)
    await t.run_once()
    [o] = t.portfolio.open_orders
    oid = o.exchange_order_id
    ex.fill_resting(oid, 0.1)                                 # half filled -> a position to flatten
    ex.cancel_faults += [ccxt.ExchangeNotAvailable("maintenance")] * 4
    cmds.queue = ["/kill"]
    await t.run_once()
    assert ex.orders[oid]["status"] == "open"
    assert [x.exchange_order_id for x in t.portfolio.open_orders] == [oid]   # still tracked
    assert t.portfolio.positions == {} and ex.net(PERP) == pytest.approx(0) and ex.net(SPOT) == pytest.approx(0)
    await t.run_once()                                        # attempts 3 and 4: escalated
    assert any("ESCALATE" in m for m in notes.msgs)
    assert t.portfolio.open_orders and ex.orders[oid]["status"] == "open"
    await t.run_once()                                        # exchange recovers -> cancelled, forgotten
    assert ex.orders[oid]["status"] == "canceled" and t.portfolio.open_orders == []
    assert t.portfolio.positions == {}
    assert ex.net(PERP) == pytest.approx(0) and ex.net(SPOT) == pytest.approx(0)



def test_entry_timeout_with_cancel_failure_must_not_forget_live_order():
    rig = Rig(Scripted({0: maker_entry()}), entry_timeout_s=600)
    oid = rig.place_maker()
    rig.ex.cancel_faults.append(ccxt.RequestTimeout("cancel timed out"))
    rig.step(600)
    assert rig.pf.open_orders or rig.ex.open_orders() == []
    assert any("cancel failed" in e for e in rig.broker.events)
    assert rig.order.exchange_order_id == oid and rig.ex.orders[oid]["status"] == "open"
    rig.ex.on_cancel = lambda o: rig.ex.fill_resting(o, 0.1)   # partial fill races the retried cancel
    rig.step(630)                                              # retried: cancelled, late fill hedged
    assert rig.pf.open_orders == [] and rig.ex.open_orders() == []
    rig.assert_hedged(0.001)
    rig.assert_consistent()
    assert rig.pf.exec_stats["entry_cancels"] == 1


def test_requote_with_failed_cancel_never_posts_a_second_order():
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    rig.ex.cancel_faults.append(ccxt.RequestTimeout("cancel timed out"))
    rig.step(30, px=PX + 500)                                  # touch moved: requote, but the cancel fails
    assert len(rig.ex.orders_of(PERP, "limit")) == 1 and rig.order.exchange_order_id == oid
    assert rig.order.price == PX + 5                           # still the live order's price
    rig.step(60, px=PX + 500)                                  # retried: cancelled and re-posted once
    assert len(rig.ex.open_orders()) == 1 and len(rig.ex.orders_of(PERP, "limit")) == 2
    assert rig.order.price == PX + 505


def test_exit_timeout_with_failed_cancel_waits_before_taker_fallback():
    rig, exit_id = open_position_rig(exit_timeout_s=300)
    rig.ex.cancel_faults.append(ccxt.RequestTimeout("cancel timed out"))
    rig.step(400)                                              # (script step 3 also rests a g2 entry)
    [ex_order] = [o for o in rig.pf.open_orders if o.group_id == "g1"]
    assert rig.ex.orders[exit_id]["status"] == "open" and ex_order.exchange_order_id == exit_id
    assert rig.ex.orders_of(SPOT, "market")[1:] == []          # no taker close while the exit may still fill
    rig.step(430)
    assert rig.ex.orders[exit_id]["status"] == "canceled"
    assert rig.pf.group_positions("g1") == [] and [o.group_id for o in rig.pf.open_orders] == ["g2"]
    rig.assert_consistent()



# --- 10. exchange cancels our resting order ---------------------------------------------------
@pytest.mark.parametrize("filled_contracts", [0.1, 0.0])
def test_exchange_cancelled_resting_order_partial_hedged_and_remainder_replaced(filled_contracts):
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    if filled_contracts:
        rig.ex.fill_resting(oid, filled_contracts)
    rig.ex.orders[oid]["status"] = "canceled"                 # e.g. exchange self-trade prevention / maintenance
    rig.step(30)

    done = filled_contracts * 0.01
    rig.assert_hedged(done)                                   # partial fill hedged immediately, no naked leg
    rig.assert_consistent()
    rig.step(60)                                              # next check re-places the remainder
    limits = rig.ex.orders_of(PERP, "limit")
    assert len(limits) == 2
    new = rig.ex.orders[rig.order.exchange_order_id]
    assert new["status"] == "open" and new["params"]["postOnly"] is True
    assert new["amount"] * 0.01 == pytest.approx(QTY - done)
    assert len(rig.ex.open_orders()) == 1                     # never two live orders for one group

    rig.ex.fill_resting(new["id"])                            # remainder fills -> fully hedged
    rig.step(90)
    rig.assert_hedged(QTY)
    rig.assert_consistent()
    assert rig.pf.open_orders == []


# --- 11. late fill racing a cancel on entry timeout -------------------------------------------
@pytest.mark.parametrize("late_contracts", [None, 0.1])
def test_late_fill_racing_entry_timeout_cancel_is_hedged(late_contracts):
    rig = Rig(Scripted({0: maker_entry()}), entry_timeout_s=600)
    oid = rig.place_maker()
    rig.ex.on_cancel = lambda o: rig.ex.fill_resting(o, late_contracts)   # fills just before the cancel lands
    rig.step(600)

    late = (late_contracts or 0.2) * 0.01
    assert rig.pf.open_orders == [] and rig.ex.open_orders() == []
    rig.assert_hedged(late)                                   # the late fill got its hedge
    rig.assert_consistent()
    assert rig.pf.exec_stats["entry_cancels"] == 1
    assert len(rig.ex.orders_of(SPOT)) == 1


def test_late_fill_racing_kill_cancel_is_hedged_then_flattened():
    rig = Rig(Scripted({0: maker_entry()}))
    oid = rig.place_maker()
    rig.risk.kill("test kill")
    rig.ex.on_cancel = lambda o: rig.ex.fill_resting(o)
    rig.step(30)
    assert rig.pf.open_orders == [] and rig.pf.positions == {}
    rig.assert_consistent()
    assert rig.ex.open_orders() == []


# --- 12. daily loss halt while a maker exit rests ---------------------------------------------
def open_position_rig(**exec_kw):
    """Hedged position g1 on the exchange, then a maker exit resting; returns (rig, exit_order_id)."""
    rig = Rig(Scripted({0: maker_entry(), 2: maker_exit(), 3: maker_entry("g2")}), **exec_kw)
    oid = rig.place_maker(0)
    rig.ex.fill_resting(oid)
    rig.step(30)
    rig.assert_hedged(QTY)
    rig.step(60)                                              # script step 2: exit rests
    return rig, rig.order.exchange_order_id


def test_daily_halt_does_not_block_resting_exit_but_blocks_entries():
    rig, exit_id = open_position_rig()
    assert rig.order.reduce_only and rig.ex.orders[exit_id]["status"] == "open"
    rig.pf.cash -= 40.0                                       # > 2% daily loss on 1100
    res = rig.step(90)                                        # script step 3: a new entry g2 is proposed
    assert rig.risk.state.daily_halt_day is not None and not rig.risk.entries_allowed
    assert any("DAILY HALT" in e for e in rig.events)
    assert [i.group_id for i, r in res.rejected if "daily loss" in r] == ["g2", "g2"]
    assert rig.ex.orders[exit_id]["status"] == "open" and rig.order.group_id == "g1"   # exit still working
    assert len(rig.ex.orders_of(PERP, "limit")) == 2          # entry g2 never reached the exchange

    rig.ex.fill_resting(exit_id)
    rig.step(120)
    assert rig.pf.positions == {} and rig.pf.open_orders == []
    rig.assert_consistent()
    assert len(rig.pf.closed_trades) == 1


def test_daily_halt_exit_timeout_still_falls_back_to_taker():
    rig, exit_id = open_position_rig(exit_timeout_s=300)
    rig.pf.cash -= 40.0
    rig.step(90)
    assert rig.ex.orders[exit_id]["status"] == "open"
    rig.step(400)                                             # exit order is now > exit_timeout_s old
    assert rig.pf.positions == {} and rig.pf.open_orders == []
    assert rig.ex.orders[exit_id]["status"] == "canceled" and rig.ex.open_orders() == []
    rig.assert_consistent()


def test_total_loss_kill_while_exit_rests_flattens_with_taker():
    rig, exit_id = open_position_rig()
    rig.pf.cash -= 200.0                                      # > 10% total loss
    rig.step(90)
    assert rig.risk.state.killed
    assert rig.pf.positions == {} and rig.pf.open_orders == []
    assert rig.ex.open_orders() == []
    rig.assert_consistent()


# --- extra: rounding dust ---------------------------------------------------------------------
def test_filled_maker_order_leaves_no_dust_resting_order_blocking_the_group():
    """qty from a USD notional is not a multiple of the exchange step: the filled order must not linger."""
    s = load_settings()
    cfg = s.strategies.funding_carry.model_copy(
        update={"exchange": EX, "bases": ["BTC"], "avg_window": 3, "execution": "maker", "maker_leg": "perp",
                "notional_usd": 200.0})   # 200 / 100005 = 0.0019999 BTC -> 0.19 contracts after truncation
    strat = FundingCarry(cfg, CostModel(s))
    strat.seed_funding_history(EX, PERP, [0.0003] * 3)
    rig = Rig(strat)
    oid = rig.place_maker()
    rig.ex.fill_resting(oid)                                  # the whole exchange order fills
    rig.step(30)
    rig.assert_hedged()
    assert rig.pf.open_orders == []                           # nothing should still be resting

