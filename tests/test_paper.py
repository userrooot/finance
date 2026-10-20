"""Paper trader tests: FakeSource, tmp_path, fake clock, no real sleeping."""
import json

import pytest

from arb.config import load_settings
from arb.core.types import OrderIntent, Requirements
from arb.models import FundingInfo
from arb.monitoring.notify import Notifier
from arb.paper.report import paper_report
from arb.paper.runner import PaperTrader
from arb.strategies.base import Strategy
from tests.fakes import FakeSource, perp_market, spot_market

T0 = 1_760_000_000  # seconds
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"
QTY = 0.003


class Dummy(Strategy):
    name = "dummy"

    def __init__(self):
        self.seen = 0
        self.funding_seed = {}
        self.price_seed = {}

    def requirements(self):
        return Requirements(quotes=[("binance", SPOT, "spot"), ("binance", PERP, "swap")],
                            funding=[("binance", PERP)])

    def on_snapshot(self, snapshot, portfolio):
        self.seen += 1
        if portfolio.open_groups("d"):
            return []
        return [OrderIntent("d1", "binance", SPOT, "spot", "buy", QTY),
                OrderIntent("d1", "binance", PERP, "swap", "sell", QTY)]

    def state_dict(self):
        return {"seen": self.seen}

    def load_state(self, state):
        self.seen = state.get("seen", 0)

    def seed_funding_history(self, exchange, symbol, rates):
        self.funding_seed[(exchange, symbol)] = rates

    def seed_prices(self, symbol, closes):
        self.price_seed[symbol] = closes
    bar_ms = 3_600_000
    cfg = type("C", (), {"lookback_bars": 3})()


class Clock:
    def __init__(self):
        self.t = float(T0)

    def __call__(self):
        return self.t


class Notes:
    def __init__(self, fail=False):
        self.msgs, self.fail = [], fail

    def send(self, text):
        if self.fail:
            raise RuntimeError("boom")
        self.msgs.append(text)


def make_source(clock, next_funding_s=None, rate=0.0001, with_perp_book=True):
    src = FakeSource()
    src.add_market("binance", spot_market("BTC"))
    src.add_market("binance", perp_market("BTC"))
    ts = int(clock.t * 1000)
    src.add_book("binance", SPOT, [(99_990.0, 5)], [(100_000.0, 0.001), (100_100.0, 5)], ts)
    if with_perp_book:
        src.add_book("binance", PERP, [(100_010.0, 0.001), (100_000.0, 5)], [(100_020.0, 5)], ts)
    nxt = int((next_funding_s or clock.t + 28_800) * 1000)
    src.add_funding(FundingInfo("binance", PERP, rate, 8.0, 100_000.0, None, nxt))
    return src


def set_books(src, clock):
    ts = int(clock.t * 1000)
    for k, b in list(src._books.items()):
        src.add_book(*k, b.bids, b.asks, ts)


def make(tmp_path, clock=None, src=None, notifier=None, strategy=None, settings=None, **kw):
    clock = clock or Clock()
    src = src or make_source(clock, **kw)
    s = strategy or Dummy()
    t = PaperTrader(settings or load_settings(), s, src, tmp_path, notifier=notifier, clock=clock)
    return t, s, src, clock


async def test_cycle_opens_position_with_vwap_fills(tmp_path):
    t, _, _, _ = make(tmp_path)
    res = await t.run_once()
    spot = next(f for f in res.fills if f.symbol == SPOT)
    perp = next(f for f in res.fills if f.symbol == PERP)
    assert spot.price == pytest.approx((0.001 * 100_000 + 0.002 * 100_100) / 0.003)
    assert perp.price == pytest.approx((0.001 * 100_010 + 0.002 * 100_000) / 0.003)
    assert t.portfolio.net_qty("binance", SPOT, "spot") == pytest.approx(QTY)
    assert len((tmp_path / "fills.jsonl").read_text().splitlines()) == 2
    assert len((tmp_path / "equity.jsonl").read_text().splitlines()) == 1


async def test_failed_book_skips_cycle(tmp_path):
    clock = Clock()
    t, _, _, _ = make(tmp_path, clock=clock, with_perp_book=False)
    assert await t.run_once() is None
    assert not t.portfolio.positions
    ev = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert ev[0]["type"] == "skipped_cycle" and ev[0]["errors"]
    assert not (tmp_path / "fills.jsonl").exists()
    assert "Skipped cycles: 1" in paper_report(tmp_path)


async def test_empty_book_skips_cycle(tmp_path):
    clock = Clock()
    t, _, src, _ = make(tmp_path, clock=clock)
    src.add_book("binance", SPOT, [], [(100_000.0, 1)], int(clock.t * 1000))
    assert await t.run_once() is None


async def test_funding_settlement_detected_once(tmp_path):
    clock = Clock()
    nxt = T0 + 1000
    t, _, src, _ = make(tmp_path, clock=clock, next_funding_s=nxt)
    await t.run_once()
    assert t.portfolio.funding_received == 0
    clock.t = T0 + 500
    set_books(src, clock)
    await t.run_once()
    assert t.portfolio.funding_received == 0
    clock.t = T0 + 1001
    set_books(src, clock)
    src._funding = {}
    src.add_funding(FundingInfo("binance", PERP, 0.0002, 8.0, 100_000.0, None, (T0 + 1000 + 28_800) * 1000))
    await t.run_once()
    mark = (100_010.0 + 100_020.0) / 2
    expected = QTY * mark * 0.0001  # remembered rate, short perp is credited
    assert t.portfolio.funding_received == pytest.approx(expected)
    clock.t = T0 + 1100
    set_books(src, clock)
    await t.run_once()
    assert t.portfolio.funding_received == pytest.approx(expected)


async def test_restart_restores_state(tmp_path):
    t, s, src, clock = make(tmp_path)
    await t.run_once()
    eq, cash = t.portfolio.equity(), t.portfolio.cash
    t2, s2, _, _ = make(tmp_path, clock=clock, src=src)
    assert not t2.fresh
    assert t2.portfolio.cash == pytest.approx(cash)
    assert t2.portfolio.positions == t.portfolio.positions
    assert s2.seen == 1
    assert t2.risk.to_dict() == t.risk.to_dict()
    assert t2.tracker == t.tracker
    clock.t += 30
    set_books(src, clock)
    res = await t2.run_once()
    assert not res.fills and s2.seen == 2  # already open -> continues, no duplicate entry
    assert t2.portfolio.equity() == pytest.approx(eq, rel=1e-3)


async def test_state_file_valid_json_each_cycle(tmp_path):
    t, _, src, clock = make(tmp_path)
    for _ in range(3):
        await t.run_once()
        st = json.loads((tmp_path / "state.json").read_text())
        assert {"portfolio", "risk", "strategy", "funding_tracker", "last_ts", "started_ms"} <= st.keys()
        assert not (tmp_path / "state.json.tmp").exists()
        clock.t += 30
        set_books(src, clock)


async def test_kill_switch_notified(tmp_path):
    notes = Notes()
    t, _, src, clock = make(tmp_path, notifier=notes)
    await t.run_once()
    assert any(m.startswith("FILL") for m in notes.msgs)
    t.portfolio.cash -= 200  # >10% loss
    clock.t += 30
    set_books(src, clock)
    await t.run_once()
    assert any("KILL SWITCH" in m for m in notes.msgs)
    assert t.risk.state.killed and not t.portfolio.positions
    assert "KILLED" in paper_report(tmp_path)


async def test_notifier_exception_does_not_break(tmp_path):
    t, _, _, _ = make(tmp_path, notifier=Notes(fail=True))
    res = await t.run_once()
    assert res is not None and res.fills


async def test_run_loop_cycles_and_closes(tmp_path):
    clock = Clock()
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        clock.t += s

    closed = []
    src = make_source(clock)

    async def close():
        closed.append(1)
    src.close = close
    t = PaperTrader(load_settings(), Dummy(), src, tmp_path, clock=clock, sleep=fake_sleep)
    await t.run(cycles=3)
    assert sleeps == [30, 30] and closed == [1]


def test_live_trading_raises(tmp_path):
    s = load_settings()
    s.live_trading = True
    with pytest.raises(RuntimeError):
        PaperTrader(s, Dummy(), FakeSource(), tmp_path)


async def test_report_with_and_without_state(tmp_path):
    assert "No paper trading state" in paper_report(tmp_path / "nope")
    t, _, _, _ = make(tmp_path)
    await t.run_once()
    out = paper_report(tmp_path)
    for needle in ("Equity", "Max drawdown", "Open positions", PERP, "Kill switch: off"):
        assert needle in out


async def test_warm_up_seeds_strategy(tmp_path):
    clock = Clock()
    src = make_source(clock)
    src.add_funding_history("binance", PERP, [(T0 * 1000 - 2000, 0.0002), (T0 * 1000 - 1000, 0.0003),
                                              (T0 * 1000 - 20 * 86_400_000, 0.9)])
    src.add_ohlcv("binance", SPOT, [[1, 1, 1, 1, 10.0, 1], [2, 1, 1, 1, 11.0, 1], [3, 1, 1, 1, 12.0, 1]])
    src.add_ohlcv("binance", PERP, [[1, 1, 1, 1, 10.5, 1], [2, 1, 1, 1, 11.5, 1], [3, 1, 1, 1, 12.5, 1]])
    t, s, _, _ = make(tmp_path, clock=clock, src=src)
    await t.warm_up()
    assert s.funding_seed[("binance", PERP)] == [0.0002, 0.0003]
    assert s.price_seed[SPOT] == [10.0, 11.0, 12.0]
    await t.run_once()
    t2, s2, _, _ = make(tmp_path, clock=clock, src=src)
    await t2.warm_up()  # state exists -> no seeding
    assert not s2.funding_seed


def test_notifier_unconfigured_logs(monkeypatch, caplog):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with caplog.at_level("INFO"):
        assert Notifier().send("hello") is False
    assert "hello" in caplog.text


def test_notifier_swallows_errors(monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert Notifier("SECRETTOKEN", "1").send("x") is False
    assert "SECRETTOKEN" not in caplog.text
