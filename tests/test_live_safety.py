"""Live safety rails: triple gate, key permissions, reconciliation, liquidation guard, Telegram commands."""
import pytest

from arb.config import ExecutionCfg, load_settings
from arb.core.portfolio import Portfolio
from arb.core.types import Fill, OrderIntent, Requirements
from arb.costs import CostModel
from arb.execution.live_broker import LiveBroker
from arb.live.flatten import flatten_all
from arb.live.runner import LiveTrader
from arb.live.safety import (LIVE_CONFIRM_PHRASE, LiveSafetyError, check_key_permissions, check_live_gate,
                             liquidation_risks, reconcile)
from arb.monitoring.commands import TelegramCommands
from arb.paper.runner import PaperTrader
from arb.strategies.base import Strategy
from tests.fake_ccxt import FakeExchange, perp, spot
from tests.fakes import FakeSource

SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"
ENV = {"ARB_LIVE_CONFIRM": LIVE_CONFIRM_PHRASE}


def live_settings(**kw):
    s = load_settings().model_copy(update={"live_trading": True})
    s.live = s.live.model_copy(update={"max_order_usd": 300.0, **kw})
    return s


class OkxKey(FakeExchange):
    perm = "read_only,trade"

    def private_get_account_config(self):
        return {"data": [{"perm": self.perm}]}


class BinanceKey(FakeExchange):
    withdraw = False

    def sapi_get_account_apirestrictions(self):
        return {"enableWithdrawals": self.withdraw, "enableSpotAndMarginTrading": True}


def exchange(cls=OkxKey):
    return cls([spot("BTC"), perp("BTC", contract_size=0.01, min_amount=0.01, step=0.01)],
               {SPOT: 100_000.0, PERP: 100_000.0})


# --- gate & keys ----------------------------------------------------------------------------
@pytest.mark.parametrize("settings, env, missing", [
    (load_settings(), ENV, "live_trading is false"),
    (live_settings(), {}, "ARB_LIVE_CONFIRM"),
    (live_settings(), {"ARB_LIVE_CONFIRM": "yes"}, "ARB_LIVE_CONFIRM"),
    (live_settings(max_order_usd=0), ENV, "max_order_usd"),
])
def test_live_gate_needs_all_three(settings, env, missing):
    with pytest.raises(LiveSafetyError, match=missing):
        check_live_gate(settings, env)


def test_live_gate_passes_with_all_three():
    check_live_gate(live_settings(), ENV)


def test_withdraw_keys_refused():
    ex = exchange()
    ex.perm = "read_only,withdraw,trade"
    with pytest.raises(LiveSafetyError, match="withdraw"):
        check_key_permissions("okx", ex)
    b = exchange(BinanceKey)
    b.withdraw = True
    with pytest.raises(LiveSafetyError, match="withdrawals"):
        check_key_permissions("binance", b)
    check_key_permissions("okx", exchange())
    check_key_permissions("binance", exchange(BinanceKey))


def test_unverifiable_exchange_refused():
    with pytest.raises(LiveSafetyError, match="cannot verify"):
        check_key_permissions("bybit", exchange())


def test_paper_still_refuses_live_settings(tmp_path):
    with pytest.raises(RuntimeError):
        PaperTrader(live_settings(), Hold(), FakeSource(), tmp_path)


# --- reconciliation -------------------------------------------------------------------------
def hedged_portfolio(qty=0.002):
    pf = Portfolio(1000.0)
    pf.apply_fill(Fill("g1", "okx", SPOT, "spot", "buy", qty, 100_000.0, 0.0, 1, qty))
    pf.apply_fill(Fill("g1", "okx", PERP, "swap", "sell", qty, 100_000.0, 0.0, 1, qty))
    return pf


def test_reconcile_clean_and_mismatch():
    ex = exchange()
    ex.balances = {"BTC": 0.0025, "USDT": 500.0}  # user's extra BTC is fine
    ex.positions = [{"symbol": PERP, "contracts": 0.2, "side": "short"}]
    assert reconcile({"okx": ex}, hedged_portfolio(), 2.0) == []
    ex.positions = []  # the short perp is missing on the exchange: unhedged spot
    issues = reconcile({"okx": ex}, hedged_portfolio(), 2.0)
    assert len(issues) == 1 and "perp" in issues[0]
    ex.positions = [{"symbol": PERP, "contracts": 0.2, "side": "short"}]
    ex.balances = {"BTC": 0.001}
    assert "spot BTC" in reconcile({"okx": ex}, hedged_portfolio(), 2.0)[0]


def test_reconcile_flags_unknown_exchange_position():
    ex = exchange()
    ex.positions = [{"symbol": PERP, "contracts": 0.5, "side": "short"}]
    assert reconcile({"okx": ex}, Portfolio(1000.0), 2.0)


def test_liquidation_risks():
    ex = exchange()
    ex.positions = [{"symbol": PERP, "contracts": 0.2, "side": "short", "markPrice": 100_000, "liquidationPrice": 115_000}]
    assert liquidation_risks({"okx": ex}, 25) == [("okx", PERP, pytest.approx(15.0))]
    assert liquidation_risks({"okx": ex}, 10) == []


# --- Telegram -------------------------------------------------------------------------------
def test_commands_only_from_configured_chat_and_once():
    payload = {"result": [
        {"update_id": 5, "message": {"chat": {"id": 42}, "text": "/pause"}},
        {"update_id": 6, "message": {"chat": {"id": 99}, "text": "/kill"}},
        {"update_id": 7, "message": {"chat": {"id": 42}, "text": "/status@mybot extra"}},
        {"update_id": 8, "message": {"chat": {"id": 42}, "text": "hello"}}]}
    seen = []
    tc = TelegramCommands("T", "42", fetch=lambda url: (seen.append(url), payload)[1])
    assert tc.poll() == ["/pause", "/status"]
    assert tc.offset == 9
    assert TelegramCommands(None, None).poll() == []


# --- LiveTrader -----------------------------------------------------------------------------
class Hold(Strategy):
    def __init__(self, intents=None):
        self.intents = intents or []

    def requirements(self):
        return Requirements(quotes=[("okx", SPOT, "spot"), ("okx", PERP, "swap")], funding=[])

    def on_snapshot(self, snapshot, portfolio):
        out, self.intents = self.intents, []
        return out


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


def source(ts_s=1000.0):
    src = FakeSource()
    for sym in (SPOT, PERP):
        src.add_book("okx", sym, [(99_995.0, 1.0)], [(100_005.0, 1.0)], ts=int(ts_s * 1000))
    return src


def make_trader(tmp_path, strategy=None, ex=None, cmds=None, env=ENV, **live):
    ex = ex or exchange()
    notes = Notes()
    t = LiveTrader(live_settings(**live), strategy or Hold(), source(), {"okx": ex}, tmp_path, notifier=notes,
                   commands=cmds or Cmds(), env=env, clock=lambda: 1000.0)
    t.broker._sleep = lambda s: None
    return t, ex, notes


def test_live_trader_refuses_without_gate(tmp_path):
    with pytest.raises(LiveSafetyError):
        make_trader(tmp_path, env={})


def test_live_trader_refuses_withdraw_key(tmp_path):
    ex = exchange()
    ex.perm = "trade,withdraw"
    with pytest.raises(LiveSafetyError):
        make_trader(tmp_path, ex=ex)


async def test_startup_mismatch_pauses_entries(tmp_path):
    ex = exchange()
    ex.positions = [{"symbol": PERP, "contracts": 1.0, "side": "short"}]  # unknown to the bot
    legs = [OrderIntent("g", "okx", SPOT, "spot", "buy", 0.001), OrderIntent("g", "okx", PERP, "swap", "sell", 0.001)]
    t, ex, notes = make_trader(tmp_path, strategy=Hold(legs), ex=ex)
    assert t.risk.state.paused and "RECONCILE MISMATCH" in notes.msgs[0]
    res = await t.run_once()
    assert not res.fills and "paused" in res.rejected[0][1]
    assert [o for o in ex.created] == []


async def test_live_trade_flows_through_live_broker(tmp_path):
    legs = [OrderIntent("g", "okx", SPOT, "spot", "buy", 0.001), OrderIntent("g", "okx", PERP, "swap", "sell", 0.001)]
    t, ex, notes = make_trader(tmp_path, strategy=Hold(legs))
    res = await t.run_once()
    assert len(ex.created) == 2 and {f.market for f in res.fills} == {"spot", "swap"}
    assert t.portfolio.net_qty("okx", PERP, "swap") == pytest.approx(-0.001)


async def test_kill_command_flattens(tmp_path):
    legs = [OrderIntent("g", "okx", SPOT, "spot", "buy", 0.001), OrderIntent("g", "okx", PERP, "swap", "sell", 0.001)]
    cmds = Cmds()
    t, ex, notes = make_trader(tmp_path, strategy=Hold(legs), cmds=cmds)
    await t.run_once()
    cmds.queue = ["/kill"]
    await t.run_once()
    assert t.risk.state.killed and t.portfolio.positions == {}
    assert any("KILL" in m for m in notes.msgs)


async def test_pause_resume_status(tmp_path):
    cmds = Cmds("/pause", "/status")
    t, ex, notes = make_trader(tmp_path, cmds=cmds)
    await t.run_once()
    assert t.risk.state.paused and any("PAUSED" in m for m in notes.msgs)
    cmds.queue = ["/resume"]
    await t.run_once()
    assert not t.risk.state.paused


async def test_liquidation_guard_closes_group(tmp_path):
    legs = [OrderIntent("g", "okx", SPOT, "spot", "buy", 0.001), OrderIntent("g", "okx", PERP, "swap", "sell", 0.001)]
    t, ex, notes = make_trader(tmp_path, strategy=Hold(legs))
    await t.run_once()
    ex.positions = [{"symbol": PERP, "contracts": 0.1, "side": "short", "markPrice": 100_000, "liquidationPrice": 104_000}]
    await t.run_once()
    assert t.portfolio.positions == {} and any("liquidation" in m for m in notes.msgs)


def test_flatten_all_closes_perps_and_bot_spot_in_capped_chunks():
    ex = exchange()
    ex.positions = [{"symbol": PERP, "contracts": 0.5, "side": "short"}]  # 0.005 BTC = $500
    pf = hedged_portfolio(0.002)
    b = LiveBroker({"okx": ex}, CostModel(load_settings()), max_order_usd=300.0, sleep=lambda s: None)
    log = flatten_all("okx", ex, b, pf)
    perp_orders = [o for o in ex.created if o["symbol"] == PERP]
    assert len(perp_orders) == 2 and all(o["side"] == "buy" and o["params"]["reduceOnly"] for o in perp_orders)
    assert sum(o["amount"] for o in perp_orders) == pytest.approx(0.5)
    assert [o["side"] for o in ex.created if o["symbol"] == SPOT] == ["sell"]
    assert len(log) == 2
