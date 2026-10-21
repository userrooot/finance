"""Paper-vs-backtest validation: synthetic paper state dir + synthetic history, no network."""
import json
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from arb import trading_cli
from arb.backtest.data import HistoryStore
from arb.backtest.engine import run_backtest
from arb.config import load_settings
from arb.validate import FAIL_LINE, PASS_LINE, validate

H = 3_600_000
DAY = 24 * H
T0 = 1_760_000_000_000 // (8 * H) * (8 * H)
WARM_DAYS, PAPER_DAYS = 12, 16
START = T0 + WARM_DAYS * DAY
END = T0 + (WARM_DAYS + PAPER_DAYS) * DAY
SPOT, PERP = ("binance", "BTC/USDT"), ("binance", "BTC/USDT:USDT")


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s.backtest.spread_bps = 0.0
    s.backtest.slippage_bps = 0.0
    s.strategies.funding_carry.bases = ["BTC"]
    s.strategies.funding_carry.max_hold_days = 4.0
    s.strategies.funding_carry.avg_window = 6
    s.storage.dir = str(tmp_path / "data")
    s.paper.state_dir = str(tmp_path / "paper")
    return s


def _history(s):
    n = (WARM_DAYS + PAPER_DAYS) * 24 + 24
    ts = T0 + H * np.arange(n)
    px = 100_000 + 50 * np.sin(np.arange(n) / 10)
    bars = pd.DataFrame({"ts": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1.0})
    f_ts = np.arange(T0, T0 + n * H, 8 * H)
    funding = pd.DataFrame({"ts": f_ts, "rate": 0.0006})
    store = HistoryStore(f"{s.storage.dir}/history")
    store.merge_save(store.ohlcv_path(*SPOT, "1h"), bars)
    store.merge_save(store.ohlcv_path(*PERP, "1h"), bars)
    store.merge_save(store.funding_path(*PERP), funding)
    return store


def _replay(s, store):
    strat = trading_cli.build_strategy("funding_carry", s)
    ohlcv = {k: store.load_ohlcv(*k, "1h") for k in (SPOT, PERP)}
    return run_backtest(s, strat, ohlcv, {PERP: store.load_funding(*PERP)}, start_ms=T0, end_ms=END,
                        warmup_until_ms=START)


def _write_paper(s, res, start=START, last=END, events=(), skipped=0, cash_shift=0.0, drop_trades=0):
    d = trading_cli.Path(s.paper.state_dir) / "funding_carry"
    d.mkdir(parents=True)
    pf = res.portfolio.to_dict()
    pf["cash"] += cash_shift
    pf["closed_trades"] = pf["closed_trades"][drop_trades:]
    eq = [{"ts": int(t), "equity": float(e) + cash_shift} for t, e in res.equity.itertuples(index=False)]
    risk = {"initial_equity": pf["initial_cash"], "day": None, "day_start_equity": 0.0, "daily_halt_day": None,
            "killed": False, "kill_reason": ""}
    (d / "state.json").write_text(json.dumps({"portfolio": pf, "risk": risk, "strategy": {}, "funding_tracker": [],
                                              "last_ts": last, "started_ms": start}))
    (d / "equity.jsonl").write_text("".join(json.dumps(r) + "\n" for r in eq))
    (d / "fills.jsonl").write_text("".join(json.dumps(asdict(f)) + "\n" for f in res.fills))
    ev = [{"ts": last, "type": "event", "msg": m} for m in events]
    ev += [{"ts": last, "type": "skipped_cycle", "errors": []}] * skipped
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in ev))


def _run(s):
    return validate(s, "funding_carry", trading_cli.Path(s.paper.state_dir) / "funding_carry",
                    f"{s.storage.dir}/history", fetch=lambda url: None)


def _check(res, name):
    return next(c for c in res.checks if c["name"] == name)


@pytest.fixture
def world(settings):
    store = _history(settings)
    res = _replay(settings, store)
    assert res.portfolio.closed_trades, "synthetic scenario must produce trades"
    return settings, res


def test_pass_when_paper_matches_backtest(world):
    s, bt = world
    _write_paper(s, bt)
    r = _run(s)
    assert r.passed, r.report
    assert r.paper["days"] >= 14 and r.paper["closed_trades"] == r.backtest["closed_trades"] > 0
    assert abs(r.paper["pnl"] - r.backtest["pnl"]) < 1e-6
    assert r.report.rstrip().endswith(PASS_LINE)


def test_small_gap_within_tolerance_passes(world):
    s, bt = world
    _write_paper(s, bt, cash_shift=-0.02 * abs(bt.portfolio.equity_curve[-1][1] - bt.portfolio.initial_cash))
    assert _run(s).passed


def test_fail_too_few_days(world):
    s, bt = world
    _write_paper(s, bt, start=END - 5 * DAY)
    r = _run(s)
    assert not r.passed and not _check(r, "paper days")["passed"]
    assert r.report.rstrip().endswith(FAIL_LINE)


def test_fail_on_kill_switch_event(world):
    s, bt = world
    _write_paper(s, bt, events=["KILL SWITCH: total loss limit"])
    r = _run(s)
    assert not r.passed and not _check(r, "no kill-switch")["passed"]
    assert r.paper["kill_switch_events"] == 1


def test_fail_on_large_pnl_gap(world):
    s, bt = world
    _write_paper(s, bt, cash_shift=-40.0)
    r = _run(s)
    assert not _check(r, "paper vs backtest PnL gap")["passed"]
    assert not r.passed
    assert "max(|paper PnL|, |backtest PnL|, 1 USD)" in r.report
    assert r.report.rstrip().endswith(FAIL_LINE)


def test_skipped_cycles_and_trade_diff_are_informational(world):
    s, bt = world
    _write_paper(s, bt, skipped=5000, drop_trades=1)
    r = _run(s)
    assert r.passed
    assert not _check(r, "skipped cycles")["passed"]
    assert "diff" in _check(r, "trade count difference")["detail"]


def test_missing_history_fails_gracefully(world, tmp_path):
    s, bt = world
    _write_paper(s, bt)
    r = validate(s, "funding_carry", trading_cli.Path(s.paper.state_dir) / "funding_carry",
                 tmp_path / "empty", fetch=lambda url: None)
    assert not r.passed and not _check(r, "backtest replay")["passed"]


def test_cli_exit_codes(world, capsys, monkeypatch, tmp_path):
    s, bt = world
    monkeypatch.chdir(tmp_path)
    _write_paper(s, bt)
    assert trading_cli.validate_cmd(s, "funding_carry") == 0
    out = capsys.readouterr().out
    assert PASS_LINE in out
    assert (tmp_path / "reports" / "validate_funding_carry.md").exists()
    (trading_cli.Path(s.paper.state_dir) / "funding_carry" / "events.jsonl").write_text(
        json.dumps({"ts": END, "type": "event", "msg": "KILL SWITCH: x"}) + "\n")
    assert trading_cli.validate_cmd(s, "funding_carry") == 1
