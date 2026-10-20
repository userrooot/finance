"""Backtest tests: known answers, no lookahead, costs, warm-up, metrics, walk-forward."""
import math

import numpy as np
import pandas as pd
import pytest

from arb.backtest.engine import common_timeline, parse_timeframe_ms, run_backtest
from arb.backtest.metrics import compute_metrics, format_metrics
from arb.backtest.walk_forward import walk_forward
from arb.config import load_settings
from arb.core.types import OrderIntent, Requirements
from arb.strategies.base import Strategy

H = 3_600_000
T0 = 1_760_000_000_000 // (8 * H) * (8 * H)  # aligned to an 8h boundary
SPOT, PERP = ("binance", "BTC/USDT"), ("binance", "BTC/USDT:USDT")


def bars(n, px=100_000.0, t0=T0, step=None):
    prices = np.full(n, px) if step is None else px + step * np.arange(n)
    return pd.DataFrame({"ts": t0 + H * np.arange(n), "open": prices, "high": prices, "low": prices,
                         "close": prices, "volume": 1.0})


def funding_rows(n_bars, rate=0.0001, t0=T0):
    ts = np.arange(t0, t0 + n_bars * H, 8 * H)
    return pd.DataFrame({"ts": ts, "rate": rate})


class Carry(Strategy):
    """Buy spot / sell perp on the first snapshot (or after `start_after`), hold forever."""

    def __init__(self, qty=0.003, start_after=0):
        self.qty, self.start_after, self.seen, self.decisions = qty, start_after, 0, []

    def requirements(self):
        return Requirements()

    def on_snapshot(self, snapshot, portfolio):
        self.seen += 1
        self.decisions.append(snapshot.ts_ms)
        if self.seen <= self.start_after or portfolio.open_groups("carry"):
            return []
        return [OrderIntent("carry1", *SPOT, "spot", "buy", self.qty),
                OrderIntent("carry1", *PERP, "swap", "sell", self.qty)]


@pytest.fixture
def settings():
    s = load_settings()
    s.backtest.spread_bps = 0.0
    s.backtest.slippage_bps = 0.0
    return s


def run(settings, strat, n=48, **kw):
    ohlcv = {SPOT: bars(n), PERP: bars(n)}
    return run_backtest(settings, strat, ohlcv, {PERP: funding_rows(n)}, **kw)


def test_parse_timeframe():
    assert parse_timeframe_ms("1h") == H and parse_timeframe_ms("15m") == 900_000 and parse_timeframe_ms("1d") == 24 * H
    with pytest.raises(ValueError):
        parse_timeframe_ms("xx")


def test_timeline_is_inner_join():
    a, b = bars(10), bars(10, t0=T0 + 3 * H)
    assert list(common_timeline({SPOT: a, PERP: b})) == [T0 + i * H for i in range(3, 10)]


def test_known_answer_carry_funding_and_next_bar_fill(settings):
    res = run(settings, Carry(), n=48)
    # decision at close of bar 0 (T0+H) -> fills at the NEXT bar open = T0+H
    assert {f.ts_ms for f in res.fills} == {T0 + H}
    # funding rows at T0, T0+8h, ... T0+40h; only those after the position opened and <= last close
    # count: T0+8h .. T0+40h = 5 events
    notional, qty = 0.003 * 100_000.0, 0.003
    assert len(res.fills) == 2
    assert res.metrics["funding"] == pytest.approx(5 * notional * 0.0001)
    fees = qty * 100_000 * (0.0010 + 0.0005)
    assert res.metrics["fees"] == pytest.approx(fees)
    assert res.equity["equity"].iloc[-1] == pytest.approx(1100.0 + 5 * notional * 0.0001 - fees)
    assert res.metrics["open_positions"] == 1 and res.metrics["n_trades"] == 0
    assert len(res.equity) == 48 and res.equity["ts"].iloc[0] == T0 + H


def test_no_lookahead_future_bars_do_not_change_past(settings):
    n, cut = 40, 20
    base = {SPOT: bars(n, step=10.0), PERP: bars(n, step=10.0)}
    alt = {k: v.copy() for k, v in base.items()}
    for v in alt.values():
        v.loc[cut:, ["open", "high", "low", "close"]] *= 1.5
    fund = {PERP: funding_rows(n)}
    r1 = run_backtest(settings, Carry(start_after=5), base, fund)
    r2 = run_backtest(settings, Carry(start_after=5), alt, fund)
    t_cut = T0 + cut * H
    f1 = [(f.ts_ms, f.price, f.qty) for f in r1.fills if f.ts_ms <= t_cut]
    f2 = [(f.ts_ms, f.price, f.qty) for f in r2.fills if f.ts_ms <= t_cut]
    assert f1 == f2 and f1
    e1 = r1.equity[r1.equity.ts <= t_cut].reset_index(drop=True)
    e2 = r2.equity[r2.equity.ts <= t_cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(e1, e2)
    # fill uses the open of the bar after the decision (decision bar index 5 -> fill at bar 6 open)
    assert r1.fills[0].ts_ms == T0 + 6 * H
    assert r1.fills[0].price == pytest.approx(100_000.0 + 10.0 * 6)


def test_spread_and_slippage_applied():
    s = load_settings()
    s.backtest.spread_bps, s.backtest.slippage_bps = 4.0, 2.0
    res = run(s, Carry(), n=10)
    buy = next(f for f in res.fills if f.side == "buy")
    sell = next(f for f in res.fills if f.side == "sell")
    assert buy.price == pytest.approx(100_000 * (1 + 2e-4) * (1 + 2e-4))
    assert sell.price == pytest.approx(100_000 * (1 - 2e-4) * (1 - 2e-4))
    assert buy.price > 100_000 > sell.price


def test_warmup_feeds_strategy_but_no_trading(settings):
    n, warm_bars = 30, 10
    strat = Carry()
    res = run(settings, strat, n=n, warmup_until_ms=T0 + warm_bars * H)
    assert strat.decisions[0] == T0 + H  # warm-up bars were seen by the strategy
    assert len(strat.decisions) == n
    assert all(f.ts_ms > T0 + warm_bars * H for f in res.fills)
    assert res.equity["ts"].iloc[0] == T0 + (warm_bars + 1) * H
    assert len(res.equity) == n - warm_bars


def test_start_end_filter(settings):
    res = run(settings, Carry(), n=40, start_ms=T0 + 10 * H, end_ms=T0 + 19 * H)
    assert len(res.equity) == 10 and res.equity["ts"].iloc[0] == T0 + 11 * H


def test_metrics_hand_built_curve():
    day = 86_400_000
    eq = pd.DataFrame({"ts": [0, day, 2 * day, 3 * day], "equity": [1000.0, 1100.0, 880.0, 990.0]})
    trades = pd.DataFrame({"pnl": [100.0, -50.0, 40.0, -10.0]})
    m = compute_metrics(eq, trades, 1000.0, 0.3, fees=5.0, funding=7.0, traded_notional=4000.0, open_positions=2)
    assert m["max_drawdown_pct"] == pytest.approx(20.0)
    assert m["final_equity"] == 990.0 and m["total_return_pct"] == pytest.approx(-1.0)
    assert m["n_trades"] == 4 and m["win_rate_pct"] == pytest.approx(50.0)
    assert m["profit_factor"] == pytest.approx(140 / 60)
    assert m["avg_trade_pnl"] == pytest.approx(20.0)
    assert m["tax"] == pytest.approx(0.3 * 140)  # losses are not offset
    assert m["net_pnl_after_tax"] == pytest.approx(-10.0 - 42.0)
    assert m["turnover_x"] == pytest.approx(4.0) and m["open_positions"] == 2
    r = np.array([0.1, -0.2, 0.125])
    assert m["sharpe"] == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(365))
    assert m["sortino"] == pytest.approx(r.mean() / math.sqrt((0.2**2) / 3) * math.sqrt(365))
    assert m["days"] == pytest.approx(3.0)
    assert "| max_drawdown_pct | 20.00 |" in format_metrics(m)


def test_metrics_edge_cases():
    empty = compute_metrics(pd.DataFrame(columns=["ts", "equity"]), pd.DataFrame(), 1000.0, 0.3, 0, 0, 0, 0)
    assert empty["final_equity"] == 1000.0 and empty["sharpe"] == 0 and empty["n_trades"] == 0
    assert empty["max_drawdown_pct"] == 0 and empty["tax"] == 0
    wins = compute_metrics(empty and pd.DataFrame({"ts": [0], "equity": [1000.0]}),
                           pd.DataFrame({"pnl": [5.0, 3.0]}), 1000.0, 0.3, 0, 0, 0, 0)
    assert wins["profit_factor"] == math.inf and wins["win_rate_pct"] == 100.0
    format_metrics(wins)


# --- walk-forward -----------------------------------------------------------------------------
class ParamCarry(Carry):
    """Carry whose `side` param decides the direction; profit only for the right one."""

    def __init__(self, direction):
        super().__init__()
        self.direction = direction

    def on_snapshot(self, snapshot, portfolio):
        out = super().on_snapshot(snapshot, portfolio)
        if self.direction == "skip":
            return []
        return out


def test_walk_forward_windows_and_oos_only_in_test(settings):
    n, train, test = 100, 30, 20
    ohlcv = {SPOT: bars(n), PERP: bars(n)}
    fund = {PERP: funding_rows(n, rate=0.0005)}
    res = walk_forward(settings, lambda p: ParamCarry(p["dir"]), [{"dir": "skip"}, {"dir": "carry"}],
                       ohlcv, fund, train_bars=train, test_bars=test, objective="total_return_pct")
    # windows start at 0, 20, 40 -> test ends at 50, 70, 90 (<=100); a 4th would end at 110
    assert len(res.windows) == 3
    assert all(w["is_objective"] != 0 or w["params"] == {"dir": "skip"} for w in res.windows)
    ts = common_timeline(ohlcv)
    test_ts = set()
    for k in range(3):
        a = k * test + train
        test_ts |= {int(t) + H for t in ts[a:a + test]}
    assert set(res.oos_equity["ts"]) == test_ts
    assert len(res.oos_equity) == 3 * test
    assert res.oos_equity["equity"].iloc[0] == pytest.approx(1100.0, rel=0.05)
    assert "Walk-forward" in res.summary and "degradation" in res.summary
    assert res.oos_metrics["start"] == res.windows[0]["test_start"][:10] + res.oos_metrics["start"][10:]


def test_walk_forward_oos_trades_not_in_train(settings):
    n, train, test = 60, 20, 10
    ohlcv = {SPOT: bars(n), PERP: bars(n)}

    class Cycler(Strategy):
        """Opens and closes a carry position every 4 snapshots."""

        def __init__(self, p):
            self.k = 0

        def requirements(self):
            return Requirements()

        def on_snapshot(self, snapshot, portfolio):
            self.k += 1
            g = f"carry{self.k // 4}"
            if self.k % 4 == 1:
                return [OrderIntent(g, *SPOT, "spot", "buy", 0.003), OrderIntent(g, *PERP, "swap", "sell", 0.003)]
            if self.k % 4 == 3:
                gid = f"carry{(self.k - 2) // 4}"
                return [OrderIntent(gid, *SPOT, "spot", "sell", 0.003, reduce_only=True),
                        OrderIntent(gid, *PERP, "swap", "buy", 0.003, reduce_only=True)]
            return []

    res = walk_forward(settings, Cycler, [{}], ohlcv, None, train_bars=train, test_bars=test)
    assert len(res.windows) == 4
    assert len(res.oos_trades) > 0
    ts = common_timeline(ohlcv)
    first_test_open = int(ts[train])
    assert res.oos_trades["open_ms"].min() >= first_test_open
