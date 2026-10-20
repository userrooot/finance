"""Markdown summary of a paper-trading state directory."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def _ts(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC") if ms else "n/a"


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


def paper_report(state_dir: str | Path, tax_rate: float = 0.312) -> str:
    d = Path(state_dir)
    sp = d / "state.json"
    if not sp.exists():
        return f"# Paper trading report\n\nNo paper trading state found in `{d}` yet. Run the paper trader first.\n"
    state = json.loads(sp.read_text())
    pf, risk = state["portfolio"], state["risk"]
    equity_rows = _jsonl(d / "equity.jsonl")
    events = _jsonl(d / "events.jsonl")

    start = pf["initial_cash"]
    curve = [r["equity"] for r in equity_rows]
    now_eq = curve[-1] if curve else start
    ret = (now_eq / start - 1) * 100 if start else 0.0
    peak, mdd = start, 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak else 0.0)

    trades = pf["closed_trades"]
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trades)
    taxable = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    tax = taxable * tax_rate
    skipped = [e for e in events if e.get("type") == "skipped_cycle"]

    L = ["# Paper trading report", "",
         f"- Started: {_ts(state.get('started_ms'))}",
         f"- Last update: {_ts(state.get('last_ts'))}",
         f"- Equity: {now_eq:,.2f} USDT (start {start:,.2f}), return {ret:+.2f}%",
         f"- Max drawdown: {mdd * 100:.2f}%", "",
         "## Closed trades", "",
         f"- Count: {len(trades)}",
         f"- Win rate: {(wins / len(trades) * 100):.1f}%" if trades else "- Win rate: n/a",
         f"- Total PnL (net of fees, incl. funding): {total_pnl:+,.2f} USDT",
         f"- Fees paid: {pf['fees_paid']:,.2f} USDT",
         f"- Funding received: {pf['funding_received']:+,.2f} USDT",
         f"- Estimated tax at {tax_rate:.1%} on positive trade PnL ({taxable:,.2f}): {tax:,.2f} USDT",
         f"- PnL after estimated tax: {total_pnl - tax:+,.2f} USDT", "",
         "## Open positions", ""]
    if pf["positions"]:
        L += ["| Group | Exchange | Symbol | Market | Qty | Avg price |", "|---|---|---|---|---|---|"]
        L += [f"| {p['group_id']} | {p['exchange']} | {p['symbol']} | {p['market']} | {p['qty']:.6g} | {p['avg_price']:.2f} |"
              for p in pf["positions"]]
    else:
        L.append("None.")
    halted = risk.get("daily_halt_day") is not None and risk.get("daily_halt_day") == risk.get("day")
    L += ["", "## Risk state", "",
          f"- Kill switch: {'KILLED - ' + risk.get('kill_reason', '') if risk.get('killed') else 'off'}",
          f"- Daily halt: {'HALTED for ' + str(risk.get('daily_halt_day')) if halted else 'off'}", "",
          "## Data health", "", f"- Skipped cycles: {len(skipped)}", "", "## Recent events", ""]
    recent = [e for e in events if e.get("type") != "skipped_cycle"][-10:]
    if recent:
        for e in recent:
            L.append(f"- {_ts(e.get('ts'))}: " + (e.get("msg") or f"rejected {e.get('symbol')} {e.get('side')}: {e.get('reason')}"))
    else:
        L.append("None.")
    return "\n".join(L) + "\n"
