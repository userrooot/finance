"""FundingCarry driven through the TradingEngine."""
import json

import pytest

from arb.config import load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import FundingEvent, FundingQuote, MarketSnapshot, Quote
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.strategies.funding_carry import FundingCarry

H8 = 8 * 3_600_000
T0 = 1_760_000_000_000
PERP = "BTC/USDT:USDT"
HIGH, LOW = 0.0003, 0.00001  # per 8h: ~33% / ~1% annualised


def build(**cfg_updates):
    settings = load_settings()
    cfg = settings.strategies.funding_carry.model_copy(update={"bases": ["BTC"], "avg_window": 3, **cfg_updates})
    costs = CostModel(settings)
    pf = Portfolio(1000.0)
    strat = FundingCarry(cfg, costs)
    eng = TradingEngine(strat, SimBroker(costs), pf, RiskManager(settings, 1000.0), on_event=lambda m: None)
    return eng, strat, pf, settings


def snap(i, rate=None, px=100_000.0, spread=10.0, quote_rate=None):
    ts = T0 + i * H8
    quotes = {("binance", "BTC/USDT"): Quote(px - spread / 2, px + spread / 2, ts),
              ("binance", PERP): Quote(px - spread / 2, px + spread / 2, ts)}
    events = [] if rate is None else [FundingEvent(ts, "binance", PERP, rate)]
    funding = {} if quote_rate is None else {("binance", PERP): FundingQuote(quote_rate, 8.0)}
    return MarketSnapshot(ts, quotes, funding, events)


def feed(eng, rates, start=0):
    fills = []
    for k, r in enumerate(rates):
        fills += eng.step(snap(start + k, r)).fills
    return fills


def test_requirements():
    _, strat, _, _ = build()
    req = strat.requirements()
    assert req.quotes == [("binance", "BTC/USDT", "spot"), ("binance", PERP, "swap")]
    assert req.funding == [("binance", PERP)]


def test_enters_only_after_history_full():
    eng, _, pf, _ = build()
    assert feed(eng, [HIGH, HIGH]) == []
    assert pf.positions == {}
    fills = feed(eng, [HIGH], start=2)
    assert {(f.symbol, f.side, f.tag) for f in fills} == {("BTC/USDT", "buy", "entry"), (PERP, "sell", "entry")}
    gid = pf.open_groups("fc:binance:BTC:")
    assert gid == [f"fc:binance:BTC:{T0 + 2 * H8}"]
    spot, perp = pf.net_qty("binance", "BTC/USDT", "spot"), pf.net_qty("binance", PERP, "swap")
    assert spot == pytest.approx(300 / 100_005) and perp == pytest.approx(-spot)


def test_no_entry_with_low_funding():
    eng, _, pf, _ = build()
    assert feed(eng, [LOW] * 8) == []
    assert pf.positions == {}


def test_no_reentry_while_open():
    eng, _, pf, _ = build()
    feed(eng, [HIGH] * 3)
    assert feed(eng, [HIGH] * 4, start=3) == []
    assert len(pf.open_groups()) == 1


def test_current_funding_quote_alone_never_triggers_entry():
    eng, strat, pf, _ = build()
    for i in range(10):
        assert not eng.step(snap(i, rate=None, quote_rate=0.01)).fills
    assert not strat.history and pf.positions == {}


def test_other_symbols_and_exchanges_events_ignored():
    eng, strat, _, _ = build()
    s = snap(0, HIGH)
    s.funding_events += [FundingEvent(T0, "binance", "ETH/USDT:USDT", HIGH), FundingEvent(T0, "bybit", PERP, HIGH)]
    eng.step(s)
    assert list(strat.history[("binance", PERP)]) == [HIGH]
    assert ("bybit", PERP) not in strat.history


def test_exits_when_funding_drops():
    eng, _, pf, _ = build()
    feed(eng, [HIGH] * 3)
    # trailing mean over 3 events: (H, H, L) still high, (H, L, L) ~ 12% > 3%, (L, L, L) ~ 1% < 3%
    assert feed(eng, [LOW, LOW], start=3) == []
    fills = feed(eng, [LOW], start=5)
    assert pf.positions == {}
    assert fills and all(f.reduce_only and f.tag == "exit" for f in fills)
    assert len(pf.closed_trades) == 1


def test_exits_at_max_hold_days():
    rich = 0.001
    eng, _, pf, _ = build(max_hold_days=2.0)
    feed(eng, [rich] * 3)  # entered at step 2
    assert feed(eng, [rich] * 5, start=3) == []  # up to 40h held
    fills = feed(eng, [rich], start=8)  # 48h held
    assert fills and all(f.tag == "exit" and f.reduce_only for f in fills)
    assert pf.positions == {}


def test_closes_unbalanced_group():
    eng, _, pf, _ = build()
    feed(eng, [HIGH] * 3)
    spot_pos = next(p for p in pf.positions.values() if p.market == "spot")
    spot_pos.qty *= 0.9  # simulated leg failure
    fills = feed(eng, [HIGH], start=3)
    assert len(fills) == 2 and pf.positions == {}


def test_closes_group_with_missing_leg():
    eng, _, pf, _ = build()
    feed(eng, [HIGH] * 3)
    del pf.positions[next(k for k, p in pf.positions.items() if p.market == "swap")]
    fills = feed(eng, [HIGH], start=3)
    assert [f.symbol for f in fills] == ["BTC/USDT"] and pf.positions == {}


def test_restart_resumes_without_reentry():
    eng, strat, pf, settings = build()
    feed(eng, [HIGH] * 3)
    state = json.loads(json.dumps(strat.state_dict()))
    pf2 = Portfolio.from_dict(json.loads(json.dumps(pf.to_dict())))
    costs = CostModel(settings)
    strat2 = FundingCarry(strat.cfg, costs)
    strat2.load_state(state)
    eng2 = TradingEngine(strat2, SimBroker(costs), pf2, RiskManager(settings, 1000.0), on_event=lambda m: None)
    assert feed(eng2, [HIGH] * 3, start=3) == []
    assert len(pf2.open_groups()) == 1
    # and it still exits correctly with the restored history
    assert feed(eng2, [LOW] * 3, start=6) and pf2.positions == {}


def test_restart_without_state_does_not_enter_until_warm():
    _, strat, pf, settings = build()
    costs = CostModel(settings)
    fresh = FundingCarry(strat.cfg, costs)
    eng = TradingEngine(fresh, SimBroker(costs), pf, RiskManager(settings, 1000.0), on_event=lambda m: None)
    assert feed(eng, [HIGH, HIGH]) == []


def test_seed_funding_history_warms_up():
    eng, strat, pf, _ = build()
    strat.seed_funding_history("binance", PERP, [LOW, HIGH, HIGH, HIGH])
    assert list(strat.history[("binance", PERP)]) == [HIGH, HIGH, HIGH]
    assert eng.step(snap(0)).fills and pf.positions


def test_interval_from_funding_quote_scales_annualisation():
    _, strat, _, _ = build()
    strat.seed_funding_history("binance", PERP, [0.0001] * 3)
    s = snap(0)
    assert strat.gross_ann_pct(s, "BTC") == pytest.approx(0.0001 * 3 * 365 * 100)
    s.funding[("binance", PERP)] = FundingQuote(0.0, 1.0)
    assert strat.gross_ann_pct(s, "BTC") == pytest.approx(0.0001 * 24 * 365 * 100)


def test_costs_are_charged_against_entry():
    eng, strat, pf, _ = build(max_hold_days=1.0)  # amortising over 1 day: costs exceed 33% gross
    strat.seed_funding_history("binance", PERP, [HIGH] * 3)
    s = snap(0)
    gross = strat.gross_ann_pct(s, "BTC")
    cost_frac = 2 * 0.0010 + 2 * 0.0005 + 2 * 10 / 100_000
    assert strat.net_ann_pct(gross, s, "BTC") == pytest.approx(gross - cost_frac * 365 * 100)
    assert not eng.step(s).fills and pf.positions == {}


def test_state_is_json_serialisable():
    eng, strat, _, _ = build()
    feed(eng, [HIGH] * 2)
    assert json.loads(json.dumps(strat.state_dict()))["funding_history"] == {f"binance|{PERP}": [HIGH, HIGH]}
