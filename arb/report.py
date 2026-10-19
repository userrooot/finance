"""Daily markdown report built from stored opportunities and scan logs."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from arb.storage import OpportunityStore

TOP_N = 15


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def _f(v: object, fmt: str = "{:.2f}") -> str:
    return "-" if v is None or pd.isna(v) else fmt.format(v)


def build_report(store: OpportunityStore, day: date) -> str:
    opps = store.load_opportunities(day)
    scans = store.load_scans(day)
    n_scans = len(scans)
    n_errors = int(sum(len(e) for e in scans["errors"])) if n_scans else 0
    out = [f"# Arbitrage scan report - {day.isoformat()}", "",
           f"Scan cycles: {n_scans} | Errors: {n_errors}", ""]
    if opps.empty:
        out += ["No opportunities passed the cost filters.", "",
                "This is a valid result: after fees, slippage and tax, no edge was found in this period.", ""]
        return "\n".join(out)

    # scan-cycle denominator: logged scans, else distinct opportunity timestamps
    total = n_scans or opps["timestamp_ms"].nunique()
    total = max(total, 1)
    opps = opps.copy()
    opps["annualized_pct"] = pd.to_numeric(opps["annualized_pct"], errors="coerce")

    rows = []
    for strat, g in opps.groupby("strategy"):
        rows.append([strat, str(g["timestamp_ms"].nunique()), str(g["key"].nunique()),
                     _f(g["net_bps"].mean()), _f(g["net_bps"].median()),
                     _f(g["net_after_tax_usd"].mean()), _f(g["capacity_usd"].mean(), "{:.0f}"),
                     _f(g["annualized_pct"].max(), "{:.1f}")])
    out += ["## Summary by strategy", "",
            _table(["strategy", "scans seen", "distinct keys", "avg net bps", "median net bps",
                    "avg after-tax $", "avg capacity $", "max annualized %"], rows), ""]

    agg = (opps.groupby(["strategy", "key"])
           .agg(seen=("timestamp_ms", "nunique"), bps=("net_bps", "mean"),
                tax=("net_after_tax_usd", "mean"), cap=("capacity_usd", "mean"),
                ann=("annualized_pct", "mean"), holding=("holding", "last"))
           .reset_index())
    agg["pct"] = agg["seen"] / total * 100
    agg["score"] = agg["pct"] / 100 * agg["tax"]
    agg = agg.sort_values("score", ascending=False).head(TOP_N)
    rows = [[r.strategy, r.key, str(r.seen), _f(r.pct, "{:.0f}%"), _f(r.bps), _f(r.tax),
             _f(r.cap, "{:.0f}"), _f(r.ann, "{:.1f}"), str(r.holding)] for r in agg.itertuples()]
    out += [f"## Top {TOP_N} by (% of scans seen x avg after-tax $)", "",
            _table(["strategy", "key", "times seen", "% of scans", "avg net bps", "avg after-tax $",
                    "avg capacity $", "avg annualized %", "holding"], rows), "",
            "## How to read", "",
            "- Repeated sightings of a persistent carry/basis key are the same position, not additive profits.",
            "- Instant arbs seen in many scans may be stale or uncatchable.",
            "- Numbers are after fees, slippage and tax, but before execution failures.", ""]
    return "\n".join(out)


def write_report(store: OpportunityStore, day: date, out_dir: str | Path = "reports") -> Path:
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    path = path / f"{day.isoformat()}.md"
    path.write_text(build_report(store, day), encoding="utf-8")
    return path
