"""FundingCarry maker execution mode, driven through TradingEngine + SimBroker."""
import pytest

from arb.config import ExecutionCfg, load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import FundingEvent, MarketSnapshot, Quote
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.strategies.funding_carry import FundingCarry

H8 = 8 * 3_600_000
T0 = 1_760_000_000_000
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"
HIGH, LOW = 0.0003, 0.00001
PX, SPREAD = 100_000.0, 10.0
BID, ASK = PX - SPREAD / 2, PX + SPREAD / 2


def build(execution="maker", maker_leg="perp", **exec_kw):
    exec_kw.setdefault("entry_timeout_s", 1e7)
    exec_kw.setdefault("exit_timeout_s", 1e7)
    settings = load_settings()
    cfg = settings.strategies.funding_carry.model_copy(
        update={"bases": ["BTC"], "avg_window": 3, "execution": execution, "maker_leg": maker_leg})
    costs = CostModel(settings)
    pf = Portfolio(1000.0)
    strat = FundingCarry(cfg, costs)
    eng = TradingEngine(strat, SimBroker(costs), pf, RiskManager(settings, 1000.0),
                        on_event=lambda m: None, execution=ExecutionCfg(**exec_kw))
    return eng, strat, pf


def snap(i, rate=None, perp_range=None, spot_range=None):
    ts = T0 + i * H8
    quotes = {("binance", SPOT): Quote(BID, ASK, ts, low=spot_range[0] if spot_range else None,
                                       high=spot_range[1] if spot_range else None),
              ("binance", PERP): Quote(BID, ASK, ts, low=perp_range[0] if perp_range else None,
                                       high=perp_range[1] if perp_range else None)}
    events = [] if rate is None else [FundingEvent(ts, "binance", PERP, rate)]
    return MarketSnapshot(ts, quotes, {}, events)


THROUGH = (99_900.0, 100_100.0)


def enter(eng):
    for i in range(3):
        res = eng.step(snap(i, HIGH))
    return res


def test_maker_entry_rests_then_fills_with_maker_fee():
    eng, _, pf = build()
    res = enter(eng)
    assert not res.fills and pf.positions == {}
    [o] = pf.open_orders
    assert o.symbol == PERP and o.side == "sell" and o.price == ASK
    res = eng.step(snap(3, HIGH, perp_range=THROUGH))
    maker = [f for f in res.fills if f.maker]
    hedge = [f for f in res.fills if not f.maker]
    assert len(maker) == 1 and maker[0].symbol == PERP
    assert maker[0].fee == pytest.approx(maker[0].qty * maker[0].price * 0.0002)
    assert hedge[0].symbol == SPOT and hedge[0].side == "buy"
    q = 300 / ASK
    assert pf.net_qty("binance", SPOT, "spot") == pytest.approx(q)
    assert pf.net_qty("binance", PERP, "swap") == pytest.approx(-q)
    assert pf.open_orders == []


def test_no_second_entry_while_order_rests():
    eng, _, pf = build()
    enter(eng)
    for i in range(3, 8):
        assert not eng.step(snap(i, HIGH)).fills
    assert len(pf.open_orders) == 1 and pf.exec_stats["maker_orders"] == 1
    assert len(pf.pending_groups("fc:binance:BTC:")) == 1


def test_maker_leg_spot_rests_spot_buy_and_perp_hedges():
    eng, _, pf = build(maker_leg="spot")
    enter(eng)
    [o] = pf.open_orders
    assert o.symbol == SPOT and o.side == "buy" and o.price == BID
    res = eng.step(snap(3, HIGH, spot_range=THROUGH))
    assert [f.symbol for f in res.fills if f.maker] == [SPOT]
    hedge = [f for f in res.fills if not f.maker]
    assert hedge[0].symbol == PERP and hedge[0].side == "sell"
    assert pf.net_qty("binance", SPOT, "spot") == pytest.approx(-pf.net_qty("binance", PERP, "swap"))


def test_net_ann_pct_maker_vs_taker_exact_difference():
    s = snap(0)
    _, taker, _ = build("taker")
    _, mperp, _ = build("maker", "perp")
    _, mspot, _ = build("maker", "spot")
    gross = 40.0
    k = 365 / taker.cfg.max_hold_days * 100
    spread = SPREAD / PX
    base = taker.net_ann_pct(gross, s, "BTC")
    assert base == pytest.approx(gross - (2 * 0.0010 + 2 * 0.0005 + 2 * spread) * k)
    # perp maker: swap fee 0.0005 -> 0.0002 on both sides, no perp spread
    assert mperp.net_ann_pct(gross, s, "BTC") == pytest.approx(base + (2 * 0.0003 + spread) * k)
    assert mperp.net_ann_pct(gross, s, "BTC") > base
    # spot maker: spot maker fee == taker fee in config, only the spot spread disappears
    assert mspot.net_ann_pct(gross, s, "BTC") == pytest.approx(base + spread * k)


def test_maker_exit_rests_at_bid_and_completes():
    eng, _, pf = build()
    enter(eng)
    eng.step(snap(3, HIGH, perp_range=THROUGH))
    eng.step(snap(4, LOW))
    eng.step(snap(5, LOW))
    assert pf.open_orders == []
    res = eng.step(snap(6, LOW))  # (L, L, L) -> exit
    assert not res.fills
    [o] = pf.open_orders
    assert o.symbol == PERP and o.side == "buy" and o.price == BID
    res = eng.step(snap(7, perp_range=THROUGH))
    assert pf.positions == {} and pf.open_orders == []
    assert any(f.maker and f.tag == "exit" for f in res.fills)
    assert len(pf.closed_trades) == 1


def test_maker_exit_timeout_falls_back_to_taker():
    eng, _, pf = build(exit_timeout_s=3600)
    enter(eng)
    eng.step(snap(3, HIGH, perp_range=THROUGH))
    for i in (4, 5, 6):
        eng.step(snap(i, LOW))
    assert len(pf.open_orders) == 1
    res = eng.step(snap(7))  # never traded through, order older than timeout
    assert pf.positions == {} and pf.exec_stats["exit_taker_fallbacks"] == 1
    assert res.fills and not any(f.maker for f in res.fills)


def test_taker_mode_intents_unchanged():
    _, strat, pf = build("taker")
    strat.seed_funding_history("binance", PERP, [HIGH] * 3)
    intents = strat.on_snapshot(snap(0), pf)
    assert [i.symbol for i in intents] == [SPOT, PERP]
    assert all(i.exec_style == "taker" and i.maker is False for i in intents)


def test_maker_intents_flags():
    for leg, maker_sym in (("perp", PERP), ("spot", SPOT)):
        _, strat, pf = build("maker", leg)
        strat.seed_funding_history("binance", PERP, [HIGH] * 3)
        intents = strat.on_snapshot(snap(0), pf)
        assert all(i.exec_style == "maker_first" for i in intents)
        assert {i.symbol for i in intents if i.maker} == {maker_sym}


def test_exit_reason_empty_positions():
    _, strat, _ = build()
    assert strat._exit_reason([], 50.0, T0) is None


def test_unbalanced_group_closes_with_taker_in_maker_mode():
    from arb.config import load_settings
    from arb.core.portfolio import Portfolio
    from arb.core.types import Fill, MarketSnapshot, Quote
    from arb.costs import CostModel
    from arb.strategies.funding_carry import FundingCarry

    s = load_settings()
    cfg = s.strategies.funding_carry.model_copy(update={"execution": "maker", "bases": ["BTC"]})
    strat = FundingCarry(cfg, CostModel(s))
    pf = Portfolio(1000.0)
    pf.apply_fill(Fill("fc:binance:BTC:1", "binance", "BTC/USDT:USDT", "swap", "sell", 0.001, 100_000.0, 0.0, 1, 0.001))
    snap = MarketSnapshot(2, {("binance", "BTC/USDT"): Quote(99_990.0, 100_010.0, 2),
                              ("binance", "BTC/USDT:USDT"): Quote(99_990.0, 100_010.0, 2)})
    [i] = strat.on_snapshot(snap, pf)
    assert i.exec_style == "taker" and not i.maker and i.reduce_only and i.side == "buy"
