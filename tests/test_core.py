"""Core engine tests: accounting, risk limits, kill switch, leg-failure hedging, delayed execution."""
import pytest

from arb.config import load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import FundingEvent, MarketSnapshot, OrderIntent, Quote, Requirements
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.models import OrderBook
from arb.strategies.base import Strategy

DAY = 86_400_000
T0 = 1_760_000_000_000


def snap(ts, px, spread=0.0, perp_px=None, events=(), books=None):
    perp_px = px if perp_px is None else perp_px
    quotes = {("binance", "BTC/USDT"): Quote(px - spread / 2, px + spread / 2, ts),
              ("binance", "BTC/USDT:USDT"): Quote(perp_px - spread / 2, perp_px + spread / 2, ts)}
    for key, book in (books or {}).items():
        q = quotes[key]
        quotes[key] = Quote(q.bid, q.ask, ts, book=book)
    return MarketSnapshot(ts, quotes, {}, list(events))


class Scripted(Strategy):
    """Emits pre-set intents at given step numbers."""

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


def carry_open(qty=0.003, gid="g1"):
    return [OrderIntent(gid, "binance", "BTC/USDT", "spot", "buy", qty),
            OrderIntent(gid, "binance", "BTC/USDT:USDT", "swap", "sell", qty)]


def carry_close(qty=0.003, gid="g1"):
    return [OrderIntent(gid, "binance", "BTC/USDT", "spot", "sell", qty, reduce_only=True),
            OrderIntent(gid, "binance", "BTC/USDT:USDT", "swap", "buy", qty, reduce_only=True)]


def make_engine(script, delay=False, settings=None):
    settings = settings or load_settings()
    pf = Portfolio(1000.0)
    eng = TradingEngine(Scripted(script), SimBroker(CostModel(settings)), pf,
                        RiskManager(settings, 1000.0), delay_execution=delay, on_event=lambda m: None)
    return eng, pf


def test_known_answer_carry_flat_prices():
    """Flat price, 3 funding payments of 0.01% -> PnL = funding - 4 taker fees exactly."""
    px, qty = 100_000.0, 0.003
    ev = lambda i: [FundingEvent(T0 + i, "binance", "BTC/USDT:USDT", 0.0001)]
    eng, pf = make_engine({0: carry_open(qty), 4: carry_close(qty)})
    for i in range(5):
        eng.step(snap(T0 + i, px, events=ev(i) if 1 <= i <= 3 else ()))
    notional = qty * px
    fees = 2 * notional * 0.0010 + 2 * notional * 0.0005
    funding = 3 * notional * 0.0001
    assert pf.positions == {}
    assert len(pf.closed_trades) == 1
    t = pf.closed_trades[0]
    assert t.funding == pytest.approx(funding)
    assert t.fees == pytest.approx(fees)
    assert t.pnl == pytest.approx(funding - fees)
    assert pf.equity() == pytest.approx(1000.0 + funding - fees)


def test_per_trade_cap_rejects_whole_group():
    eng, pf = make_engine({0: carry_open(qty=0.01)})  # $1000 per leg > $300 cap
    res = eng.step(snap(T0, 100_000.0))
    assert not res.fills and len(res.rejected) == 2
    assert "max_per_trade_usd" in res.rejected[0][1]


def test_spot_short_rejected():
    eng, _ = make_engine({0: [OrderIntent("g", "binance", "BTC/USDT", "spot", "sell", 0.001)]})
    res = eng.step(snap(T0, 100_000.0))
    assert "spot short" in res.rejected[0][1]


def test_stale_quote_rejected():
    eng, _ = make_engine({0: carry_open()})
    s = snap(T0, 100_000.0)
    s.ts_ms += 60_000  # quotes are a minute old
    assert "stale" in eng.step(s).rejected[0][1]


def test_daily_loss_halts_entries_until_next_day():
    eng, pf = make_engine({1: carry_open(gid="a"), 2: carry_open(gid="b")})
    eng.step(snap(T0, 100_000.0))
    pf.cash -= 25.0  # 2.5% loss
    res = eng.step(snap(T0 + 1, 100_000.0))
    assert any("DAILY HALT" in e for e in res.events)
    assert "daily loss" in res.rejected[0][1]
    res = eng.step(snap(T0 + DAY, 100_000.0))  # new UTC day
    assert res.fills


def test_kill_switch_flattens_and_blocks():
    eng, pf = make_engine({0: carry_open(), 2: carry_open(gid="g2")})
    eng.step(snap(T0, 100_000.0))
    assert pf.positions
    pf.cash -= 150.0  # 15% loss
    res = eng.step(snap(T0 + 1, 100_000.0))
    assert any("KILL SWITCH" in e for e in res.events)
    assert pf.positions == {}
    res = eng.step(snap(T0 + 2, 100_000.0))  # strategy is not consulted while killed
    assert not res.fills and pf.positions == {}
    assert eng.strategy.n == 1
    _, rejected = eng.risk.check(carry_open(gid="g3"), pf, snap(T0 + 2, 100_000.0))
    assert "entries halted" in rejected[0][1]


def test_leg_failure_unwinds_excess():
    thin = OrderBook("binance", "BTC/USDT:USDT", bids=[(100_000.0, 0.001)], asks=[(100_001.0, 1.0)], timestamp_ms=T0)
    eng, pf = make_engine({0: carry_open(qty=0.003)})
    res = eng.step(snap(T0, 100_000.0, books={("binance", "BTC/USDT:USDT"): thin}))
    assert any(f.tag == "leg_unwind" for f in res.fills)
    assert pf.net_qty("binance", "BTC/USDT", "spot") == pytest.approx(0.001)
    assert pf.net_qty("binance", "BTC/USDT:USDT", "swap") == pytest.approx(-0.001)


def test_delayed_execution_fills_at_next_snapshot():
    eng, pf = make_engine({0: [OrderIntent("g", "binance", "BTC/USDT", "spot", "buy", 0.001)]}, delay=True)
    res = eng.step(snap(T0, 100_000.0))
    assert not res.fills and eng.pending
    res = eng.execute_pending(snap(T0 + 1, 101_000.0))
    assert res.fills[0].price == pytest.approx(101_000.0)


def test_reduce_only_cannot_flip():
    eng, _ = make_engine({0: carry_open(qty=0.001), 1: carry_close(qty=0.002)})
    eng.step(snap(T0, 100_000.0))
    res = eng.step(snap(T0 + 1, 100_000.0))
    assert "would not reduce" in res.rejected[0][1]


def test_portfolio_and_risk_roundtrip():
    eng, pf = make_engine({0: carry_open()})
    eng.step(snap(T0, 100_000.0))
    pf2 = Portfolio.from_dict(pf.to_dict())
    assert pf2.to_dict() == pf.to_dict()
    rm = RiskManager(load_settings(), 1000.0, eng.risk.to_dict())
    assert rm.to_dict() == eng.risk.to_dict()
