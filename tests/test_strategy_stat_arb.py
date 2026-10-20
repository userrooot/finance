"""StatArb driven through the TradingEngine on a synthetic cointegrated pair."""
import json

import numpy as np
import pytest

from arb.config import load_settings
from arb.core.engine import TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import MarketSnapshot, Quote
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.strategies.stat_arb import StatArb

BAR = 3_600_000
T0 = 1_760_000_000_000 // BAR * BAR
N = 100  # lookback bars
BETA = 0.8
Y, X = "ETH/USDT:USDT", "BTC/USDT:USDT"


def make_series(n: int, seed: int = 32) -> tuple[np.ndarray, np.ndarray]:
    """Log prices: x random walk, y = a + BETA*x + AR(1) stationary noise."""
    rng = np.random.default_rng(seed)
    m = 400  # fixed draw length so shorter series are prefixes of longer ones
    lx = np.log(100_000.0) + np.cumsum(rng.normal(0, 0.01, m))
    e = np.zeros(m)
    for i in range(1, m):
        e[i] = 0.7 * e[i - 1] + rng.normal(0, 0.003)
    ly = np.log(3_000.0) + BETA * (lx - np.log(100_000.0)) + e
    return lx[:n], ly[:n]


def snap(i, ly, lx, offset_ms=0):
    ts = T0 + i * BAR + offset_ms
    px_y, px_x = float(np.exp(ly)), float(np.exp(lx))
    return MarketSnapshot(ts, {("binance", Y): Quote(px_y * 0.9999, px_y * 1.0001, ts),
                               ("binance", X): Quote(px_x * 0.9999, px_x * 1.0001, ts)})


def build(**cfg_updates):
    settings = load_settings()
    cfg = settings.strategies.stat_arb.model_copy(update={"lookback_bars": N, **cfg_updates})
    costs = CostModel(settings)
    pf = Portfolio(1000.0)
    strat = StatArb(cfg, costs, bar_ms=BAR)
    eng = TradingEngine(strat, SimBroker(costs), pf, RiskManager(settings, 1000.0), on_event=lambda m: None)
    return eng, strat, pf


def run(eng, ly, lx, start=0, stop=None):
    out = []
    for i in range(start, len(ly) if stop is None else stop):
        out.append(eng.step(snap(i, ly[i], lx[i])))
    return out


def fills_of(results):
    return [f for r in results for f in r.fills]


def diverged(sign: float, shift: float, extra_bars: int = 0):
    """Calm base series of N bars, then `extra_bars+1` bars with y shifted by sign*shift."""
    lx, ly = make_series(N + 1 + extra_bars)
    ly = ly.copy()
    ly[N:] += sign * shift
    return lx, ly


def test_requirements():
    _, strat, _ = build()
    assert strat.requirements().quotes == [("binance", X, "swap"), ("binance", Y, "swap")]
    assert strat.requirements().funding == []


def test_no_trades_without_divergence_or_warmup():
    eng, strat, pf = build()
    lx, ly = make_series(N + 50)
    res = run(eng, ly, lx, stop=N - 1)
    assert not fills_of(res) and strat.signal("ETH", "BTC") is None
    assert not fills_of(run(eng, ly, lx, N - 1, N + 50)) and pf.positions == {}
    assert abs(strat.signal("ETH", "BTC")[0]) < 2.0


def test_enters_short_y_long_x_on_positive_z():
    eng, strat, pf = build(stop_z=1e9)
    lx, ly = diverged(+1, 0.02)
    res = run(eng, ly, lx)
    z, beta = strat.signal("ETH", "BTC")
    assert z >= 2.0 and beta == pytest.approx(BETA, abs=0.15)
    fills = fills_of(res)
    assert {(f.symbol, f.side, f.tag) for f in fills} == {(Y, "sell", "entry"), (X, "buy", "entry")}
    assert pf.open_groups("sa:ETH-BTC:") == [f"sa:ETH-BTC:{T0 + N * BAR}"]
    ny = -pf.net_qty("binance", Y, "swap") * float(np.exp(ly[-1]))
    nx = pf.net_qty("binance", X, "swap") * float(np.exp(lx[-1]))
    assert ny + nx == pytest.approx(300.0, rel=1e-3)
    assert ny / nx == pytest.approx(1 / beta, rel=1e-3)


def test_enters_long_y_short_x_on_negative_z():
    eng, strat, pf = build(stop_z=1e9)
    lx, ly = diverged(-1, 0.02)
    fills = fills_of(run(eng, ly, lx))
    assert strat.signal("ETH", "BTC")[0] <= -2.0
    assert {(f.symbol, f.side) for f in fills} == {(Y, "buy"), (X, "sell")}


def test_exits_on_reversion():
    eng, strat, pf = build(stop_z=1e9)
    lx, ly = diverged(+1, 0.02)
    run(eng, ly, lx)
    assert pf.positions
    lx2, ly2 = make_series(N + 10)
    # revert: y goes back to the model line for the next bars
    res = []
    for k in range(1, 6):
        res.append(eng.step(snap(N + k, ly2[N], lx2[N])))
    fills = fills_of(res)
    assert fills and all(f.tag == "exit" and f.reduce_only for f in fills)
    assert pf.positions == {} and len(pf.closed_trades) == 1


def test_no_reentry_while_open():
    eng, strat, pf = build(stop_z=1e9, exit_z=-1e9)  # never reverts
    lx, ly = diverged(+1, 0.02, extra_bars=5)
    run(eng, ly, lx)
    assert len(pf.open_groups()) == 1


def test_stop_loss():
    eng, strat, pf = build(stop_z=1e9, exit_z=0.0)
    lx, ly = diverged(+1, 0.02)
    run(eng, ly, lx)
    assert pf.positions
    z = strat.signal("ETH", "BTC")[0]
    strat.cfg = strat.cfg.model_copy(update={"stop_z": z * 1.05})
    lx3, ly3 = diverged(+1, 0.06, extra_bars=1)  # y runs further away
    res = eng.step(snap(N + 1, ly3[N + 1], lx3[N + 1]))
    assert abs(strat.signal("ETH", "BTC")[0]) >= strat.cfg.stop_z
    assert res.fills and all(f.tag == "stop" for f in res.fills) and pf.positions == {}


def test_no_entry_beyond_stop_z():
    eng, strat, pf = build(stop_z=3.0)
    lx, ly = diverged(+1, 0.08)
    fills = fills_of(run(eng, ly, lx))
    assert abs(strat.signal("ETH", "BTC")[0]) >= 3.0
    assert fills == [] and pf.positions == {}


def test_time_stop():
    eng, strat, pf = build(stop_z=1e9, exit_z=-1e9, max_hold_bars=4)
    lx, ly = diverged(+1, 0.02, extra_bars=4)
    res = run(eng, ly, lx)
    assert [r.fills for r in res[:-1]][N + 1:] == [[]] * 3  # held 1..3 bars
    assert res[-1].fills and all(f.tag == "time_stop" for f in res[-1].fills)
    assert pf.positions == {}


def test_per_bar_sampling():
    eng, strat, _ = build()
    lx, ly = make_series(3)
    for i in range(3):
        for off in (0, 30_000, 60_000, 1_800_000, BAR - 1):
            eng.step(snap(i, ly[i], lx[i], offset_ms=off))
        assert len(strat.prices[Y]) == i + 1 and len(strat.prices[X]) == i + 1
    # first snapshot of each bar is the sample
    assert strat.prices[Y][1] == pytest.approx(np.exp(ly[1]))
    strat.seed_prices(Y, list(range(1, 500)))
    assert len(strat.prices[Y]) == N and strat.prices[Y][-1] == 499


def test_state_roundtrip_and_restart_resumes():
    eng, strat, pf = build(stop_z=1e9, exit_z=-1e9)
    lx, ly = diverged(+1, 0.02, extra_bars=2)
    run(eng, ly, lx)
    state = json.loads(json.dumps(strat.state_dict()))
    strat2 = StatArb(strat.cfg, strat.costs, bar_ms=BAR)
    strat2.load_state(state)
    assert strat2.state_dict() == strat.state_dict()
    pf2 = Portfolio.from_dict(json.loads(json.dumps(pf.to_dict())))
    eng2 = TradingEngine(strat2, SimBroker(strat.costs), pf2, RiskManager(load_settings(), 1000.0),
                         on_event=lambda m: None)
    lx2, ly2 = diverged(+1, 0.02, extra_bars=6)
    res = [eng2.step(snap(i, ly2[i], lx2[i])) for i in range(N + 3, N + 7)]
    assert not fills_of(res) and len(pf2.open_groups()) == 1


def test_no_lookahead():
    """Decisions up to bar k are identical whatever the future bars contain."""
    lx, ly = diverged(+1, 0.02, extra_bars=10)
    lx_b, ly_b = lx.copy(), ly.copy()
    rng = np.random.default_rng(1)
    k = N + 3
    lx_b[k + 1:] += rng.normal(0, 0.2, len(lx) - k - 1)
    ly_b[k + 1:] -= 0.5
    runs = []
    for a, b in ((lx, ly), (lx_b, ly_b)):
        eng, _, _ = build(stop_z=1e9, exit_z=0.0)
        res = run(eng, b, a, stop=k + 1)
        runs.append([[(f.group_id, f.symbol, f.side, f.qty, f.price, f.tag) for f in r.fills] for r in res])
    assert runs[0] == runs[1] and any(runs[0])
