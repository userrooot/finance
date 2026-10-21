"""Maker-first execution: post-only maker leg at the touch, taker hedge on fill."""
import pytest

from arb.config import ExecutionCfg, load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import MarketSnapshot, OrderIntent, Quote, Requirements
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.models import OrderBook
from arb.strategies.base import Strategy

T0 = 1_760_000_000_000
H = 3_600_000
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"


def snap(ts, spot=(99_990.0, 100_010.0), perp=(99_990.0, 100_010.0), perp_range=None, spot_range=None, perp_book=None):
    qs = {("binance", SPOT): Quote(*spot, ts, low=spot_range[0] if spot_range else None,
                                   high=spot_range[1] if spot_range else None),
          ("binance", PERP): Quote(*perp, ts, book=perp_book, low=perp_range[0] if perp_range else None,
                                   high=perp_range[1] if perp_range else None)}
    return MarketSnapshot(ts, qs)


class Scripted(Strategy):
    def __init__(self, script):
        self.script, self.n, self.fills = script, 0, []

    def requirements(self):
        return Requirements()

    def on_snapshot(self, snapshot, portfolio):
        out = self.script.get(self.n, [])
        self.n += 1
        return out

    def on_fills(self, fills):
        self.fills += fills


def entry(qty=0.002, gid="m1"):
    return [OrderIntent(gid, "binance", SPOT, "spot", "buy", qty, exec_style="maker_first"),
            OrderIntent(gid, "binance", PERP, "swap", "sell", qty, exec_style="maker_first", maker=True)]


def exit_(qty=0.002, gid="m1"):
    return [OrderIntent(gid, "binance", SPOT, "spot", "sell", qty, reduce_only=True, exec_style="maker_first"),
            OrderIntent(gid, "binance", PERP, "swap", "buy", qty, reduce_only=True, exec_style="maker_first", maker=True)]


def make(script, through_bps=1.0, **exec_kw):
    exec_kw.setdefault("entry_timeout_s", 10 * 3600)
    exec_kw.setdefault("exit_timeout_s", 10 * 3600)
    s = load_settings()
    pf = Portfolio(1000.0)
    eng = TradingEngine(Scripted(script), SimBroker(CostModel(s), through_bps=through_bps), pf,
                        RiskManager(s, 1000.0), on_event=lambda m: None, execution=ExecutionCfg(**exec_kw))
    return eng, pf


def test_maker_order_rests_at_touch_without_trading():
    eng, pf = make({0: entry()})
    res = eng.step(snap(T0))
    assert not res.fills and pf.positions == {}
    [o] = pf.open_orders
    assert o.side == "sell" and o.price == 100_010.0  # joins the ask: post-only never crosses
    assert pf.pending_groups("m") == ["m1"]


def test_touch_is_not_a_fill_trade_through_is():
    eng, pf = make({0: entry()})
    eng.step(snap(T0))
    assert not eng.step(snap(T0 + H, perp_range=(99_900.0, 100_010.0))).fills  # high == our price
    res = eng.step(snap(T0 + 2 * H, perp_range=(99_900.0, 100_030.0)))  # traded 2 bp through
    maker = [f for f in res.fills if f.maker]
    hedge = [f for f in res.fills if not f.maker]
    assert len(maker) == 1 and maker[0].price == 100_010.0
    assert maker[0].fee == pytest.approx(0.002 * 100_010.0 * 0.0002)  # maker fee
    assert hedge[0].side == "buy" and hedge[0].market == "spot" and hedge[0].qty == pytest.approx(0.002)
    assert hedge[0].price == 100_010.0  # taker at the ask
    assert pf.net_qty("binance", PERP, "swap") == pytest.approx(-0.002)
    assert pf.net_qty("binance", SPOT, "spot") == pytest.approx(0.002)
    assert pf.open_orders == []


def test_known_answer_round_trip_costs():
    """Maker short entry + maker cover exit at flat prices: PnL = spread paid on spot - fees."""
    eng, pf = make({0: entry(), 2: exit_()})
    eng.step(snap(T0))
    eng.step(snap(T0 + H, perp_range=(99_900.0, 100_100.0)))   # entry fills: sell perp @100010, buy spot @100010
    eng.step(snap(T0 + 2 * H))                                  # exit order rests: buy perp @99990
    eng.step(snap(T0 + 3 * H, perp_range=(99_900.0, 100_100.0)))  # exit fills, hedge sells spot @99990
    [t] = pf.closed_trades
    q = 0.002
    fees = q * 100_010 * 0.0002 + q * 100_010 * 0.001 + q * 99_990 * 0.0002 + q * 99_990 * 0.001
    gross = q * (100_010 - 99_990) + q * (99_990 - 100_010)  # perp +20, spot -20 per BTC
    assert t.pnl == pytest.approx(gross - fees)
    assert pf.exec_stats["maker_orders"] == 2 and pf.exec_stats["maker_fills"] == 2


def test_requote_follows_touch():
    eng, pf = make({0: entry()}, requote_bps=2)
    eng.step(snap(T0))
    eng.step(snap(T0 + H, perp=(100_190.0, 100_210.0), perp_range=(100_000.0, 100_015.0)))
    [o] = pf.open_orders
    assert o.price == 100_210.0 and pf.exec_stats["requotes"] == 1


def test_entry_timeout_cancels_without_trading():
    eng, pf = make({0: entry()}, entry_timeout_s=3600)
    eng.step(snap(T0))
    res = eng.step(snap(T0 + H))
    assert pf.open_orders == [] and not res.fills and pf.exec_stats["entry_cancels"] == 1


def test_exit_timeout_falls_back_to_taker():
    eng, pf = make({0: entry(), 2: exit_()}, exit_timeout_s=3600)
    eng.step(snap(T0))
    eng.step(snap(T0 + H, perp_range=(99_900.0, 100_100.0)))
    eng.step(snap(T0 + 2 * H))
    res = eng.step(snap(T0 + 3 * H))  # exit never traded through -> taker both legs
    assert pf.positions == {} and pf.exec_stats["exit_taker_fallbacks"] == 1
    assert {f.market for f in res.fills} == {"spot", "swap"} and not any(f.maker for f in res.fills)


def test_strategy_repeats_are_ignored_while_order_rests():
    eng, pf = make({0: entry(), 1: entry()})
    eng.step(snap(T0))
    eng.step(snap(T0 + 60_000))
    assert len(pf.open_orders) == 1 and pf.exec_stats["maker_orders"] == 1


def test_hedge_underfill_unwinds_maker_excess():
    eng, pf = make({0: [OrderIntent("m1", "binance", SPOT, "spot", "sell", 0.002, exec_style="maker_first", maker=True),
                         OrderIntent("m1", "binance", PERP, "swap", "buy", 0.002, exec_style="maker_first")]})
    # spot maker sell would be a spot short; the risk manager must refuse it as a whole group
    res = eng.step(snap(T0))
    assert res.rejected and pf.open_orders == []

    thin = OrderBook("binance", SPOT, bids=[(99_990.0, 1.0)], asks=[(100_010.0, 0.0005)], timestamp_ms=T0)
    eng, pf = make({0: entry()})
    eng.step(snap(T0))
    qs = snap(T0 + H, perp_range=(99_900.0, 100_100.0))
    q = qs.quotes[("binance", SPOT)]
    qs.quotes[("binance", SPOT)] = Quote(q.bid, q.ask, q.ts_ms, book=thin)
    eng.step(qs)
    assert pf.net_qty("binance", SPOT, "spot") == pytest.approx(0.0005)
    assert pf.net_qty("binance", PERP, "swap") == pytest.approx(-0.0005)


def test_kill_switch_cancels_resting_and_flattens_taker():
    eng, pf = make({0: entry(), 2: entry(gid="m2")})
    eng.step(snap(T0))
    eng.step(snap(T0 + H, perp_range=(99_900.0, 100_100.0)))
    assert pf.positions
    pf.cash -= 150.0
    res = eng.step(snap(T0 + 2 * H))
    assert pf.positions == {} and pf.open_orders == []
    assert not any(f.maker for f in res.fills)


def test_daily_halt_cancels_entry_orders():
    eng, pf = make({1: entry()})
    eng.step(snap(T0))
    eng.step(snap(T0 + 1))
    assert pf.open_orders
    pf.cash -= 25.0
    eng.step(snap(T0 + 2))
    assert pf.open_orders == [] and pf.exec_stats["entry_cancels"] == 1


def test_resting_orders_count_toward_exposure():
    eng, pf = make({0: entry(qty=0.0029, gid="a"), 1: entry(qty=0.0029, gid="b"), 2: entry(qty=0.0029, gid="c"),
                    3: entry(qty=0.0029, gid="d")})
    for i in range(4):
        res = eng.step(snap(T0 + i))
    # 3 resting groups x 2 legs x ~$290 = ~$1740; a 4th would exceed 2 x $1100
    assert len(pf.open_orders) == 3 and "gross exposure" in res.rejected[0][1]


def test_open_orders_persist():
    eng, pf = make({0: entry()})
    eng.step(snap(T0))
    pf2 = Portfolio.from_dict(pf.to_dict())
    assert pf2.open_orders == pf.open_orders and pf2.exec_stats == pf.exec_stats


def test_malformed_maker_group_falls_back_to_taker():
    eng, pf = make({0: [OrderIntent("x", "binance", SPOT, "spot", "buy", 0.001, exec_style="maker_first")]})
    res = eng.step(snap(T0))
    assert res.fills and not res.fills[0].maker and pf.open_orders == []
