"""Paper-vs-backtest validation: the gate that must PASS before the first live stage.

Replays the paper period (started_ms .. last_ts) through the backtest engine with the same settings and
strategy, then compares both runs and checks the `live.gate` conditions from settings.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from arb.backtest.data import HistoryStore
from arb.backtest.engine import parse_timeframe_ms, run_backtest
from arb.config import Settings
from arb.core.portfolio import Portfolio

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
FUNDING_INTERVAL_MS = 8 * 3_600_000
SKIP_WARN_SHARE = 0.05
PASS_LINE = "GATE: PASS — eligible for the $50 live stage"
FAIL_LINE = "GATE: FAIL — stay on paper"


@dataclass
class ValidationResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)  # {name, passed, detail, required}
    paper: dict = field(default_factory=dict)
    backtest: dict = field(default_factory=dict)
    report: str = ""


# --- paper side --------------------------------------------------------------------------
def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # tolerate a torn last line
    return out


def _metrics(pf: Portfolio, start_equity: float, end_equity: float, days: float, tax_rate: float) -> dict:
    trades = pf.closed_trades
    pnl = end_equity - start_equity
    taxable = sum(t.pnl for t in trades if t.pnl > 0)
    tax = taxable * tax_rate
    orders = float(pf.exec_stats.get("maker_orders", 0.0))
    fills = float(pf.exec_stats.get("maker_fills", 0.0))
    return {
        "days": days, "closed_trades": len(trades), "fees": pf.fees_paid, "funding": pf.funding_received,
        "pnl": pnl, "tax": tax, "after_tax_pnl": pnl - tax,
        "maker_orders": orders, "maker_fills": fills, "maker_fill_rate_pct": fills / orders * 100 if orders else None,
    }


def load_paper(settings: Settings, state_dir: str | Path) -> dict:
    d = Path(state_dir)
    sp = d / "state.json"
    if not sp.exists():
        raise FileNotFoundError(f"no paper state at {sp}; run the paper trader first")
    state = json.loads(sp.read_text())
    pf = Portfolio.from_dict(state["portfolio"])
    equity_rows = _jsonl(d / "equity.jsonl")
    events = _jsonl(d / "events.jsonl")
    fills = _jsonl(d / "fills.jsonl")
    started = state.get("started_ms")
    last = state.get("last_ts")
    if started is None or last is None:
        raise ValueError("paper state has no started_ms/last_ts yet (no completed cycle)")
    start_eq = pf.initial_cash
    end_eq = float(equity_rows[-1]["equity"]) if equity_rows else pf.equity()
    m = _metrics(pf, start_eq, end_eq, (last - started) / DAY_MS, settings.tax.rate_on_gains)
    kills = [e for e in events if "KILL SWITCH" in str(e.get("msg", ""))]
    skipped = [e for e in events if e.get("type") == "skipped_cycle"]
    cycles = len(equity_rows) + len(skipped)
    m.update({"kill_switch_events": len(kills), "killed": bool(state.get("risk", {}).get("killed")),
              "skipped_cycles": len(skipped), "cycles": cycles,
              "skipped_share_pct": len(skipped) / cycles * 100 if cycles else 0.0,
              "fills": len(fills), "start_equity": start_eq, "end_equity": end_eq,
              "started_ms": int(started), "last_ts": int(last)})
    return m


# --- backtest side -----------------------------------------------------------------------
def warmup_ms(strategy, bar_ms: int) -> int:
    """History needed before the paper start so the strategy is warm on day one."""
    cfg = getattr(strategy, "cfg", None)
    need = 0
    if getattr(cfg, "avg_window", None):
        need = max(need, int(cfg.avg_window) * FUNDING_INTERVAL_MS)
    if getattr(cfg, "lookback_bars", None):
        need = max(need, int(cfg.lookback_bars) * bar_ms)
    return need + DAY_MS  # one day of slack


def _covered(settings: Settings, store: HistoryStore, req, lo: int, hi: int) -> bool:
    tf = settings.backtest.timeframe
    try:
        for ex, sym, _ in req.quotes:
            ts = store.load_ohlcv(ex, sym, tf)["ts"]
            if ts.empty or ts.min() > lo + DAY_MS or ts.max() < hi - 2 * parse_timeframe_ms(tf):
                return False
        for ex, sym in req.funding:
            ts = store.load_funding(ex, sym)["ts"]
            if ts.empty or ts.min() > lo + DAY_MS or ts.max() < hi - FUNDING_INTERVAL_MS - DAY_MS:
                return False
    except FileNotFoundError:
        return False
    return True


def ensure_history(settings: Settings, store: HistoryStore, req, lo: int, hi: int, source: str,
                   fetch: Callable[[str], bytes | None] | None) -> str:
    """Make sure the store covers [lo, hi]; returns a short note about what was done."""
    if _covered(settings, store, req, lo, hi):
        return "history already covers the period"
    exchanges = {ex for ex, _, _ in req.quotes}
    if source != "binance-vision" or exchanges != {"binance"}:
        return "history incomplete; using what the store has (no importer for this source/exchange)"
    from arb.backtest.binance_vision import import_history

    today = datetime.now(UTC).date()
    first = datetime.fromtimestamp(lo / 1000, tz=UTC).date()
    months = max(0, (today.year - first.year) * 12 + today.month - first.month)
    import_history(store, list(req.quotes), list(req.funding), months, settings.backtest.timeframe,
                   fetch=fetch, today=today)
    return f"imported binance-vision history ({months} months)"


def run_replay(settings: Settings, strategy, store: HistoryStore, started_ms: int, last_ts: int, source: str,
               fetch) -> tuple[dict, str]:
    tf = settings.backtest.timeframe
    bar_ms = parse_timeframe_ms(tf)
    req = strategy.requirements()
    lo = started_ms - warmup_ms(strategy, bar_ms)
    note = ensure_history(settings, store, req, lo, last_ts, source, fetch)
    ohlcv = {(ex, sym): store.load_ohlcv(ex, sym, tf) for ex, sym, _ in req.quotes}
    funding = {(ex, sym): store.load_funding(ex, sym) for ex, sym in req.funding}
    res = run_backtest(settings, strategy, ohlcv, funding, start_ms=lo, end_ms=last_ts, warmup_until_ms=started_ms)
    initial = res.portfolio.initial_cash
    end_eq = float(res.equity["equity"].iloc[-1]) if len(res.equity) else initial
    days = (int(res.equity["ts"].iloc[-1]) - started_ms) / DAY_MS if len(res.equity) else 0.0
    m = _metrics(res.portfolio, initial, end_eq, days, settings.tax.rate_on_gains)
    m.update({"bars": len(res.equity), "start_equity": initial, "end_equity": end_eq,
              "kill_switch_events": sum("KILL SWITCH" in e for e in res.events),
              "rejected_orders": len(res.rejections)})
    return m, note


# --- checks + report ---------------------------------------------------------------------
def _check(name: str, passed: bool, detail: str, required: bool = True) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail, "required": required}


def build_checks(settings: Settings, paper: dict, bt: dict) -> list[dict]:
    g = settings.live.gate
    checks = [
        _check("paper days", paper["days"] >= g.min_paper_days,
               f"{paper['days']:.2f} days vs required >= {g.min_paper_days:g}"),
        _check("no kill-switch", paper["kill_switch_events"] == 0 and not paper["killed"],
               f"{paper['kill_switch_events']} kill-switch event(s); risk state killed={paper['killed']}"),
        _check("after-tax paper PnL", paper["after_tax_pnl"] >= g.min_after_tax_pnl_usd,
               f"${paper['after_tax_pnl']:+,.2f} vs required >= ${g.min_after_tax_pnl_usd:,.2f}"),
    ]
    p, b = paper["pnl"], bt["pnl"]
    scale = max(abs(p), abs(b), 1.0)
    gap = abs(p - b)
    limit = g.max_pnl_gap_pct / 100 * scale
    checks.append(_check("paper vs backtest PnL gap", gap <= limit,
                         f"|{p:+,.2f} - {b:+,.2f}| = ${gap:,.2f} vs limit {g.max_pnl_gap_pct:g}% x "
                         f"max(|paper|, |backtest|, 1 USD) = ${limit:,.2f}"))
    checks.append(_check("trade count difference", True,
                         f"paper {paper['closed_trades']} vs backtest {bt['closed_trades']} "
                         f"(diff {paper['closed_trades'] - bt['closed_trades']:+d}); informational", required=False))
    share = paper["skipped_share_pct"]
    checks.append(_check("skipped cycles", share < SKIP_WARN_SHARE * 100,
                         f"{paper['skipped_cycles']} of {paper['cycles']} cycles skipped ({share:.1f}%), "
                         f"warning above {SKIP_WARN_SHARE:.0%}; informational", required=False))
    return checks


def _fmt(v, kind: str = "f") -> str:
    if v is None:
        return "n/a"
    if kind == "d":
        return f"{int(v)}"
    if kind == "p":
        return f"{v:.1f}%"
    return f"{v:+,.2f}" if kind == "s" else f"{v:,.2f}"


def build_report(strategy_name: str, settings: Settings, paper: dict, bt: dict, checks: list[dict],
                 passed: bool, notes: list[str]) -> str:
    g = settings.live.gate
    rows = [("Days", "days", "f"), ("Closed trades", "closed_trades", "d"), ("Fees (USD)", "fees", "f"),
            ("Funding (USD)", "funding", "s"), ("PnL (USD)", "pnl", "s"),
            (f"After-tax PnL (USD, {settings.tax.rate_on_gains:.1%})", "after_tax_pnl", "s"),
            ("Maker orders", "maker_orders", "d"), ("Maker fills", "maker_fills", "d"),
            ("Maker fill rate", "maker_fill_rate_pct", "p"),
            ("Kill-switch events", "kill_switch_events", "d")]
    L = [f"# Paper vs backtest validation: {strategy_name}", "",
         f"Paper period: {datetime.fromtimestamp(paper['started_ms'] / 1000, tz=UTC):%Y-%m-%d %H:%M} to "
         f"{datetime.fromtimestamp(paper['last_ts'] / 1000, tz=UTC):%Y-%m-%d %H:%M} UTC. "
         "The backtest replays the same period with the same settings, warmed up on the preceding history.", "",
         "| Metric | Paper | Backtest |", "|---|---|---|"]
    for label, key, kind in rows:
        L.append(f"| {label} | {_fmt(paper.get(key), kind)} | {_fmt(bt.get(key), kind)} |")
    L.append(f"| Skipped cycles | {paper['skipped_cycles']} of {paper['cycles']} | n/a |")
    L += ["", "## Gate checks", "",
          f"PnL gap formula: |paper PnL - backtest PnL| <= max_pnl_gap_pct ({g.max_pnl_gap_pct:g}%) / 100 x "
          "max(|paper PnL|, |backtest PnL|, 1 USD).", ""]
    for c in checks:
        tag = "PASS" if c["passed"] else ("FAIL" if c["required"] else "WARN")
        L.append(f"- {tag} {c['name']}: {c['detail']}")
    if notes:
        L += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    L += ["", PASS_LINE if passed else FAIL_LINE, ""]
    return "\n".join(L)


def validate(settings: Settings, strategy_name: str, state_dir: str | Path, history_dir: str | Path,
             source: str = "binance-vision", fetch=None) -> ValidationResult:
    from arb.trading_cli import build_strategy  # local: trading_cli imports this module's callers

    paper = load_paper(settings, state_dir)
    notes: list[str] = []
    bt: dict = {"days": 0.0, "closed_trades": 0, "fees": 0.0, "funding": 0.0, "pnl": 0.0, "tax": 0.0,
                "after_tax_pnl": 0.0, "maker_orders": 0.0, "maker_fills": 0.0, "maker_fill_rate_pct": None,
                "kill_switch_events": 0}
    bt_error = None
    try:
        strategy = build_strategy(strategy_name, settings)
        bt, note = run_replay(settings, strategy, HistoryStore(history_dir), paper["started_ms"], paper["last_ts"],
                              source, fetch)
        notes.append(note)
    except Exception as e:  # missing/unfetchable history must fail the gate, not crash it
        log.warning("backtest replay failed: %s", e)
        bt_error = f"{type(e).__name__}: {e}"
    checks = build_checks(settings, paper, bt)
    checks.insert(0, _check("backtest replay", bt_error is None and bt.get("bars", 0) > 0,
                            bt_error or f"{bt.get('bars', 0)} bars replayed"))
    passed = all(c["passed"] for c in checks if c["required"])
    report = build_report(strategy_name, settings, paper, bt, checks, passed, notes)
    return ValidationResult(passed, checks, paper, bt, report)
