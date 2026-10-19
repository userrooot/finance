import numpy as np

from arb.config import load_settings
from arb.costs import CostModel
from arb.scanners import stat_arb
from tests.fakes import FakeSource

N = 720


def _rows(prices):
    return [[i * 3_600_000, p, p, p, p, 1.0] for i, p in enumerate(prices)]


def _setup(settings, y_prices, x_prices, n=N):
    settings.universe.base_assets = ["AAA", "BBB"]
    settings.scanners.stat_arb.lookback_bars = N
    src = FakeSource()
    src.add_ohlcv("binance", "AAA/USDT", _rows(y_prices[-n:]))
    src.add_ohlcv("binance", "BBB/USDT", _rows(x_prices[-n:]))
    return src


def _cointegrated(push: float):
    rng = np.random.default_rng(7)
    lx = np.log(100) + np.cumsum(rng.normal(0, 0.01, N))
    noise = np.zeros(N)
    for t in range(1, N):
        noise[t] = 0.8 * noise[t - 1] + rng.normal(0, 0.01)
    noise[-1] = push * noise.std()
    return np.exp(0.5 + 1.2 * lx + noise), np.exp(lx)


async def test_cointegrated_pair_signals():
    s = load_settings()
    y, x = _cointegrated(4.0)
    opps = await stat_arb.scan(_setup(s, y, x), s, CostModel(s), 1_000)
    assert len(opps) == 1
    o = opps[0]
    assert o.strategy == "stat_arb" and o.details["z"] >= 2
    assert abs(o.details["beta"] - 1.2) < 0.15
    assert [l.side for l in o.legs] == ["sell", "buy"]
    assert all(l.market == "swap" and l.symbol.endswith(":USDT") for l in o.legs)
    assert o.details["pairs_tested"] == 1 and o.net_pnl_usd > 0 and o.annualized_pct is None


async def test_negative_z_flips_sides():
    s = load_settings()
    y, x = _cointegrated(-4.0)
    opps = await stat_arb.scan(_setup(s, y, x), s, CostModel(s), 1_000)
    assert len(opps) == 1 and [l.side for l in opps[0].legs] == ["buy", "sell"]


async def test_independent_walks_none():
    s = load_settings()
    rng = np.random.default_rng(1)
    y = np.exp(np.cumsum(rng.normal(0, 0.01, N)) + 4)
    x = np.exp(np.cumsum(rng.normal(0, 0.01, N)) + 4)
    assert await stat_arb.scan(_setup(s, y, x), s, CostModel(s), 1) == []


async def test_insufficient_bars_none():
    s = load_settings()
    y, x = _cointegrated(4.0)
    assert await stat_arb.scan(_setup(s, y, x, n=500), s, CostModel(s), 1) == []


async def test_disabled():
    s = load_settings()
    s.scanners.stat_arb.enabled = False
    y, x = _cointegrated(4.0)
    assert await stat_arb.scan(_setup(s, y, x), s, CostModel(s), 1) == []
