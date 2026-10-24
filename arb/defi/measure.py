"""Plan step 5: probe, live sampling with replays, metric formulas and report tables (design section 6).

Live mode only: every `sample_every_s` we scan the newest block's state and, for every gap that newly appears
(an episode start), replay the exact route through the harness at N+k for each lateness offset k. Data failures
(rate limits, missing state, a broken simulator) are recorded as such and never counted as a miss or as "no gap".
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from arb.config import DefiChainCfg, Settings
from arb.costs import CostModel
from arb.defi.chain import ChainClient, RateLimited, RpcError, StateUnavailable, TransportError
from arb.defi.pools import Multicall, PoolRegistry, read_snapshot, sel
from arb.defi.route import Hop, Route, Venue
from arb.defi.scanner import BlockCosts, ScanResult, read_block_costs, scan_block
from arb.defi.simulate import SimResult, SimRequest, SimulatorUnavailable, simulate
from arb.defi.store import DefiStore
from arb.report import _f, _table

log = logging.getLogger("arb.defi")

REALISTIC_K = {"arbitrum": 8, "base": 1}
BLOCK_TIME_S = {"arbitrum": 0.25, "base": 2.0}
DAY_BLOCKS = {"arbitrum": 345_600, "base": 43_200}
PROBE_DEPTHS = (1, 16, 128, 1024, 10_000, 100_000)
MAX_REPLAYS_PER_SAMPLE = 10
DATA_FAILURE_STOP = 0.05
DATA_FAILURE_WINDOW = 200
VERDICT_USD_PER_DAY = 1.0
VERDICT_MIN_EPISODES = 30
MIN_COMPLETE_SAMPLES = 2000
DATA_ERRORS = (RateLimited, StateUnavailable, TransportError, RpcError, SimulatorUnavailable)


# ---- record builders ----------------------------------------------------------------------------------------
def block_record(chain: str, scan: ScanResult, fallback_ts: int) -> dict:
    return {"chain": chain, "block": scan.block, "timestamp": scan.timestamp_ms // 1000 or fallback_ts,
            "candidates": scan.candidates, "screened_in": scan.screened_in, "gross_positive": scan.gross_positive,
            "surviving_keys": [o.key for o in scan.opportunities],
            "surviving": [{"key": o.key, "kind": o.details.get("kind"), "net_usd": o.net_pnl_usd,
                           "capacity_usd": o.capacity_usd} for o in scan.opportunities],
            "best_net_usd": max((o.net_pnl_usd for o in scan.opportunities), default=None),
            "attempt_keys": [s["key"] for s in select_attempts([{"key": o.key, "net_usd": o.net_pnl_usd}
                                                                for o in scan.opportunities])],
            "errors": scan.errors, "complete": scan.complete, "by_kind": scan.by_kind,
            "duration_s": scan.duration_s}


_INSUFFICIENT = re.compile(r"InsufficientProfit\(balance=(\d+), required=(\d+)\)")


def implied_profit_raw(res: SimResult) -> int | None:
    """Profit the route would have made: the harness profit on success, balance - required (minProfit = 0) when
    the only failure is InsufficientProfit, otherwise unknown."""
    if res.success:
        return res.profit_raw
    m = _INSUFFICIENT.match(res.revert_reason or "")
    return int(m.group(1)) - int(m.group(2)) if m else None


def sim_record(chain: str, opp, res: SimResult | None, offset: int, block_found: int, ts_found: int,
               min_profit_usd: float, persisted: bool | None, error: str | None = None) -> dict:
    d = opp.details
    pred_profit = d["profit_raw"]
    rec = {"chain": chain, "key": opp.key, "block_found": block_found, "offset": offset,
           "block_sim": block_found + offset, "timestamp": ts_found, "kind": d.get("kind"),
           "loan_symbol": d.get("loan_symbol"), "loan_amount_raw": d["amount_in_raw"],
           "venue_mix": "+".join(sorted({str(h["dex"]) for h in d["hops"]})),
           "predicted_net_usd": opp.net_pnl_usd, "predicted_profit_raw": pred_profit, "persisted": persisted,
           "data_failure": res is None, "error": error}
    if res is None:
        rec.update(success=None, revert_reason=None, profit_raw=None, net_usd=None, gas_estimate_tx=None,
                   gas_cost_usd=None, calldata_hash=None, hit=None, below_threshold=None, miss_cost_usd=None,
                   quote_error_bps=None)
        return rec
    hit = res.success and res.net_usd >= min_profit_usd
    implied = implied_profit_raw(res)
    qerr = abs(pred_profit - implied) / d["amount_in_raw"] * 1e4 if implied is not None and d["amount_in_raw"] else None
    rec.update(success=res.success, revert_reason=res.revert_reason, profit_raw=res.profit_raw,
               net_usd=res.net_usd, gas_estimate_tx=res.gas_estimate_tx, gas_cost_usd=res.gas_cost_usd,
               calldata_hash=res.calldata_hash, hit=hit, below_threshold=res.success and not hit,
               miss_cost_usd=0.0 if hit else res.gas_cost_usd, quote_error_bps=qerr)
    return rec


# ---- metrics (pure) -----------------------------------------------------------------------------------------
def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def key_pools(key: str) -> frozenset[str]:
    """Pool ids of a candidate key ("<chain> <loan> dex:0xabcd..1234>dex:..."): the short pool addresses."""
    parts = key.split(" ", 2)
    return frozenset(x.split(":", 1)[-1] for x in parts[-1].split(">")) if len(parts) == 3 else frozenset((key,))


def select_attempts(survivors: list[dict]) -> list[dict]:
    """The attempts a bot could actually make in one block: greedily the best `net_usd` first, then every next
    survivor that shares no pool with one already chosen (review T4, issue M-01).

    Survivors that share a pool trade against the same mispricing (WETH-loan A>B and USDC-loan B>A, the same gap
    through three fee tiers, triangles through the dislocated pool...). Executing one closes the others, so they are
    one attempt, not several. Counting every key as its own episode multiplied episodes/day, and with it the
    projected $/day, by the number of routes per gap."""
    chosen: list[dict] = []
    used: set[str] = set()
    for s in sorted(survivors, key=lambda s: (-(s.get("net_usd") or 0.0), s["key"])):
        pools = key_pools(s["key"])
        if pools & used:
            continue
        chosen.append(s)
        used |= pools
    return chosen


def _gap_cap_s(ts: list[int], default_interval_s: float) -> float:
    """Spacing above which two consecutive samples are treated as a gap in coverage (restart, outage)."""
    deltas = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    step = statistics.median(deltas) if deltas else default_interval_s
    return max(5 * step, 120.0)


def find_episodes(blocks: list[dict], default_interval_s: float = 10.0, *, by_route_key: bool = False) -> list[dict]:
    """An episode is a gap that a bot would attempt once. It starts at a complete sample where an attempt
    (`select_attempts`) is a key that did not survive in the previous complete sample, and lasts while the key keeps
    surviving. Incomplete samples are unknown: they neither start nor end an episode. A coverage gap (restart,
    outage, see `_gap_cap_s`) ends every episode, as it does in the live loop.

    by_route_key=True counts every surviving key on its own (design 6.1 literally): reported for transparency only,
    it double-counts correlated routes."""
    episodes: list[dict] = []
    prev: set[str] = set()
    prev_pools: frozenset[str] = frozenset()
    complete = sorted((b for b in blocks if b.get("complete")), key=lambda b: b["block"])
    cap = _gap_cap_s([b["timestamp"] for b in complete], default_interval_s)
    last_ts: int | None = None
    for b in complete:
        if last_ts is not None and b["timestamp"] - last_ts > cap:
            prev, prev_pools = set(), frozenset()
        last_ts = b["timestamp"]
        surv = b.get("surviving", [])
        if by_route_key:
            new = [s for s in surv if s["key"] not in prev]
            attempt_pools: frozenset[str] = frozenset()
        else:
            attempts = select_attempts(surv)
            new = [s for s in attempts if is_new_attempt(s["key"], prev, prev_pools)]
            attempt_pools = frozenset().union(*(key_pools(s["key"]) for s in attempts)) if attempts else frozenset()
        for s in new:
            episodes.append({"key": s["key"], "block": b["block"], "kind": s.get("kind"), "net_usd": s["net_usd"],
                             "capacity_usd": s["capacity_usd"]})
        prev, prev_pools = {s["key"] for s in surv}, attempt_pools
    return episodes


def is_new_attempt(key: str, prev_surviving: set[str], prev_attempt_pools: frozenset[str]) -> bool:
    """A new episode starts unless the route survived in the previous complete sample, or it shares a pool with one
    of that sample's attempts (the same mispriced pool, now best closed through another counter-pool or loan token:
    the earlier attempt would already have closed it). Seen live on Base: one thin pool paired with three venues."""
    return key not in prev_surviving and not (key_pools(key) & prev_attempt_pools)


def sampled_hours(blocks: list[dict], default_interval_s: float) -> float:
    """Observed hours: the real time between consecutive samples (a coverage gap counts as one normal step), times
    the share of samples that are complete (review T4, issue M-03).

    The old formula, complete samples x median spacing, under-counted time whenever sampling slowed down (rate-limit
    back-off, slow scans, replays sharing the limiter), which mostly happens when there is activity, and so inflated
    episodes/day."""
    if not blocks:
        return 0.0
    ts = sorted(b["timestamp"] for b in blocks)
    deltas = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    step = statistics.median(deltas) if deltas else default_interval_s
    cap = _gap_cap_s(ts, default_interval_s)
    total = sum(d if d <= cap else step for d in deltas) + step  # the last sample covers one step too
    n_complete = sum(1 for b in blocks if b.get("complete"))
    return total * n_complete / len(blocks) / 3600.0


def replay_stats(rows: list[dict], eps_per_day: float, tax: CostModel) -> dict:
    ok = [r for r in rows if not r.get("data_failure")]
    hits = [r for r in ok if r["hit"]]
    below = [r for r in ok if r["below_threshold"]]
    misses = [r for r in ok if not r["hit"]]
    n = len(ok)
    h = len(hits) / n if n else None
    mean_net = _mean([r["net_usd"] for r in hits]) or 0.0
    mean_net_at = _mean([tax.after_tax(r["net_usd"]) for r in hits]) or 0.0
    miss_cost = _mean([r["miss_cost_usd"] for r in misses]) or 0.0
    ev = None if h is None else h * mean_net - (1 - h) * miss_cost
    ev_at = None if h is None else h * mean_net_at - (1 - h) * miss_cost
    return {"replays": n, "data_failures": len(rows) - n, "hits": len(hits), "below": len(below), "hit_rate": h,
            "median_profit": _median([r["net_usd"] for r in hits]), "mean_profit": _mean([r["net_usd"] for r in hits]),
            "median_gas": _median([r["gas_estimate_tx"] for r in ok]),
            "gas_usd": _median([r["gas_cost_usd"] for r in ok]), "mean_miss_cost": miss_cost if misses else None,
            "ev": ev, "ev_after_tax": ev_at,
            "projected_per_day": None if ev_at is None else eps_per_day * ev_at}


def compute_chain_metrics(chain: str, blocks: list[dict], sims: list[dict], ccfg: DefiChainCfg,
                          settings: Settings) -> dict:
    cost = CostModel(settings)
    complete = [b for b in blocks if b.get("complete")]
    n_all, n_ok = len(blocks), len(complete)
    df_rate = (n_all - n_ok) / n_all if n_all else None
    hours = sampled_hours(blocks, ccfg.sample_every_s)
    episodes = find_episodes(blocks, ccfg.sample_every_s)
    eps_day = len(episodes) / hours * 24 if hours else 0.0
    key_eps = find_episodes(blocks, ccfg.sample_every_s, by_route_key=True)
    durations = [b["duration_s"] for b in complete if b.get("duration_s") is not None]
    out: dict = {"chain": chain, "samples": n_all, "complete": n_ok, "data_failure_rate": df_rate,
                 "hours": hours, "episodes": len(episodes), "episodes_per_day": eps_day,
                 "route_key_episodes_per_day": len(key_eps) / hours * 24 if hours else 0.0,
                 "scan_latency_s": _median(durations),
                 "gross_rate": sum(1 for b in complete if b["gross_positive"] > 0) / n_ok if n_ok else None,
                 "surviving_rate": sum(1 for b in complete if b["surviving_keys"]) / n_ok if n_ok else None}
    kinds = {}
    for kind in ("two_pool", "triangular"):
        bk = [b.get("by_kind", {}).get(kind, {}) for b in complete]
        surv = [s for b in complete for s in b.get("surviving", []) if s.get("kind") == kind]
        k_eps = [e for e in episodes if e["kind"] == kind]
        kinds[kind] = {
            "candidates_med": _median([x.get("candidates", 0) for x in bk]),
            "screened_med": _median([x.get("screened_in", 0) for x in bk]),
            "gross_rate": sum(1 for x in bk if x.get("gross_positive", 0) > 0) / n_ok if n_ok else None,
            "surviving_rate": sum(1 for x in bk if x.get("surviving", 0) > 0) / n_ok if n_ok else None,
            "episodes_per_day": len(k_eps) / hours * 24 if hours else 0.0,
            "net_med": _median([s["net_usd"] for s in surv]), "net_p90": _p90([s["net_usd"] for s in surv]),
            "capacity_med": _median([s["capacity_usd"] for s in surv])}
    out["kinds"] = kinds
    offsets = sorted(set(ccfg.lateness_blocks))
    by_k = {k: [r for r in sims if r["offset"] == k] for k in offsets}
    out["persistence"] = {}
    for k in offsets:
        known = [r for r in by_k[k] if r.get("persisted") is not None]
        out["persistence"][k] = (sum(1 for r in known if r["persisted"]) / len(known)) if known else None
    out["replay"] = {k: replay_stats(by_k[k], eps_day, cost) for k in offsets}
    realistic = REALISTIC_K.get(chain, offsets[-1] if offsets else 0)
    if realistic not in offsets and offsets:
        realistic = offsets[-1]
    out["realistic_k"] = realistic
    keys: dict[str, dict] = {}
    for e in episodes:
        d = keys.setdefault(e["key"], {"episodes": 0, "nets": []})
        d["episodes"] += 1
        d["nets"].append(e["net_usd"])
    top = []
    for key, d in keys.items():
        def hr(k: int) -> float | None:
            rs = [r for r in by_k.get(k, []) if r["key"] == key and not r.get("data_failure")]
            return sum(1 for r in rs if r["hit"]) / len(rs) if rs else None
        top.append({"key": key, "episodes": d["episodes"], "hit0": hr(0), "hitk": hr(realistic),
                    "mean_net": _mean(d["nets"])})
    out["top_keys"] = sorted(top, key=lambda t: (-t["episodes"], -(t["mean_net"] or 0)))[:15]
    qa: dict[str, list[float]] = {}
    for r in by_k.get(0, []):
        if r.get("quote_error_bps") is not None:
            qa.setdefault(r["venue_mix"], []).append(r["quote_error_bps"])
    out["quote_accuracy"] = {mix: {"n": len(v), "median": _median(v), "p90": _p90(v)} for mix, v in sorted(qa.items())}
    rs = out["replay"].get(realistic, {})
    n_replayed = rs.get("replays", 0)
    n_rep_all = n_replayed + rs.get("data_failures", 0)
    replay_df = rs.get("data_failures", 0) / n_rep_all if n_rep_all else None
    proj = rs.get("projected_per_day")
    # Data conditions (design 6.3 item 7 and 6.4/T5; review T4, issue M-02): enough complete samples, a low sample
    # data-failure rate, >= 30 replayed episodes, and replays that themselves mostly ran (a replay data failure is
    # dropped from the hit rate, so a high replay failure rate would leave a self-selected subset).
    reasons = []
    if df_rate is None or df_rate >= DATA_FAILURE_STOP:
        reasons.append("sample data-failure rate")
    if n_ok < MIN_COMPLETE_SAMPLES:
        reasons.append(f"complete samples < {MIN_COMPLETE_SAMPLES}")
    if n_replayed < VERDICT_MIN_EPISODES:
        reasons.append(f"episodes replayed < {VERDICT_MIN_EPISODES}")
    if replay_df is not None and replay_df >= DATA_FAILURE_STOP:
        reasons.append("replay data-failure rate")
    if reasons:
        verdict = "INCONCLUSIVE (DATA)"
    else:
        verdict = "GO-TO-NEXT-PHASE" if proj is not None and proj > VERDICT_USD_PER_DAY else "NO-GO"
    out["verdict"] = verdict
    out["verdict_inputs"] = {"realistic_k": realistic, "episodes_replayed": n_replayed, "data_failure_rate": df_rate,
                             "complete_samples": n_ok, "replay_data_failure_rate": replay_df,
                             "projected_after_tax_per_day": proj, "inconclusive_because": reasons}
    return out


def overall_verdict(per_chain: list[dict]) -> str:
    verdicts = [m["verdict"] for m in per_chain]
    if "GO-TO-NEXT-PHASE" in verdicts:
        return "GO-TO-NEXT-PHASE"
    if verdicts and all(v == "NO-GO" for v in verdicts):
        return "NO-GO"
    return "INCONCLUSIVE (DATA)"


# ---- report ---------------------------------------------------------------------------------------------------
def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def render_report(per_chain: list[dict], meta: dict[str, dict], from_day: date, to_day: date) -> str:
    out = [f"# DeFi flash-arb measurement - {from_day.isoformat()} to {to_day.isoformat()}", "",
           "Simulation only: no transaction was sent and no key exists. Missing data is not the same as no edge.", ""]
    rows = []
    for m in per_chain:
        mt = meta.get(m["chain"], {})
        errs = ", ".join(f"{k}: {v}" for k, v in sorted(mt.get("rpc_errors", {}).items())) or "none"
        lat = m.get("scan_latency_s")
        bt = BLOCK_TIME_S.get(m["chain"])
        lat_s = "-" if lat is None else f"{lat:.1f} s" + (f" (~{lat / bt:.0f} blocks)" if bt else "")
        rows.append([m["chain"], mt.get("mode", "live"), mt.get("start", "-"), mt.get("end", "-"),
                     f"{m['complete']}/{m['samples']}", f"{m['hours']:.2f}", _pct(m["data_failure_rate"]), errs,
                     str(mt.get("state_window_blocks", "-")), str(mt.get("sim_backend", "-")), lat_s])
    out += ["## 1. Coverage", "",
            _table(["chain", "mode", "start", "end", "complete/all samples", "observed hours", "data-failure rate",
                    "RPC errors by type", "state window (blocks)", "sim backend", "median scan latency"], rows), "",
            "Scan latency is how long this pipeline needs to read and evaluate one block. A bot built on it would "
            "land at least that many blocks after N, usually far beyond the realistic offset k.", ""]
    for m in per_chain:
        if m["complete"] < MIN_COMPLETE_SAMPLES:
            out += [f"**{m['chain']}: only {m['complete']} complete samples (minimum {MIN_COMPLETE_SAMPLES}).**", ""]
    rows = []
    for m in per_chain:
        for kind, k in m["kinds"].items():
            rows.append([m["chain"], kind, _f(k["candidates_med"], "{:.0f}"), _f(k["screened_med"], "{:.0f}"),
                         _pct(k["gross_rate"]), _pct(k["surviving_rate"]), _f(k["episodes_per_day"], "{:.1f}"),
                         _f(k["net_med"]), _f(k["net_p90"]), _f(k["capacity_med"], "{:.0f}")])
        rows.append([m["chain"], "all (independent attempts)", "-", "-", _pct(m["gross_rate"]),
                     _pct(m["surviving_rate"]),
                     f"{m['episodes_per_day']:.1f} (route keys: {m.get('route_key_episodes_per_day', 0.0):.1f})",
                     "-", "-", "-"])
    out += ["## 2. Gap test", "",
            _table(["chain", "kind", "candidates/block (median)", "screened-in/block (median)", "gross-gap rate",
                    "surviving-gap rate", "episodes/day", "median net $", "p90 net $", "median capacity $"], rows), "",
            "Episodes count independent attempts: survivors that share a pool are one attempt (the best one). The "
            "route-key count treats every surviving route as its own episode and over-counts correlated routes; "
            "the projection uses the independent count. Net $ and capacity columns are over all surviving routes.", ""]
    rows = [[m["chain"], str(k), _pct(v)] for m in per_chain for k, v in m["persistence"].items()]
    out += ["## 3. Persistence", "", _table(["chain", "k", "persistence(k)"], rows), ""]
    rows = []
    for m in per_chain:
        for k, r in m["replay"].items():
            rows.append([m["chain"], str(k), str(r["replays"]), str(r["hits"]), str(r["below"]), _pct(r["hit_rate"]),
                         f"{_f(r['median_profit'])} / {_f(r['mean_profit'])}", _f(r["median_gas"], "{:.0f}"),
                         _f(r["gas_usd"], "{:.4f}"), _f(r["mean_miss_cost"], "{:.4f}"), _f(r["ev"], "{:.4f}"),
                         _f(r["ev_after_tax"], "{:.4f}"), _f(r["projected_per_day"])])
    out += ["## 4. Replay", "",
            _table(["chain", "offset k (blocks)", "replays", "hits", "won, below threshold", "hit rate",
                    "profit per hit (median / mean) $", "median gas", "gas $", "mean miss cost $", "EV/attempt $",
                    "after-tax EV/attempt $", "projected after-tax $/day"], rows), "",
            "Offset 0 is a best case that a remote bot polling a public RPC could not reach.", ""]
    rows = [[m["chain"], t["key"], str(t["episodes"]), _pct(t["hit0"]), _pct(t["hitk"]), _f(t["mean_net"])]
            for m in per_chain for t in m["top_keys"]]
    out += ["## 5. Top 15 keys", "",
            _table(["chain", "key", "episodes", "hit rate k=0", "hit rate realistic k", "mean net $"], rows), ""]
    rows = [[m["chain"], mix, str(q["n"]), _f(q["median"], "{:.3f}"), _f(q["p90"], "{:.3f}")]
            for m in per_chain for mix, q in m["quote_accuracy"].items()]
    out += ["## 6. Quote accuracy", "",
            _table(["chain", "venue mix", "replays at k=0", "median error bps", "p90 error bps"], rows), "",
            "A median above 1 bp is a bug to fix before reading any other table.", ""]
    out += ["## 7. Verdict", ""]
    for m in per_chain:
        vi = m["verdict_inputs"]
        why = vi.get("inconclusive_because") or []
        out.append(f"- {m['chain']}: **{m['verdict']}** (k={vi['realistic_k']}, episodes replayed={vi['episodes_replayed']}, "
                   f"complete samples={vi.get('complete_samples', m['complete'])}, "
                   f"data-failure rate={_pct(vi['data_failure_rate'])}, "
                   f"replay data-failure rate={_pct(vi.get('replay_data_failure_rate'))}, "
                   f"projected after-tax $/day={_f(vi['projected_after_tax_per_day'])})"
                   + (f"; inconclusive because: {', '.join(why)}" if why else ""))
    out += ["", f"VERDICT: {overall_verdict(per_chain)}", ""]
    return "\n".join(out)


def _records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    return [{k: (None if (not isinstance(v, (list, dict)) and pd.isna(v)) else v) for k, v in r.items()}
            for r in df.to_dict("records")]


def build_defi_report(settings: Settings, chains: list[str], from_day: date, to_day: date) -> str:
    cfg = settings.defi
    assert cfg is not None
    per_chain, meta = [], {}
    for chain in chains:
        store = DefiStore(cfg.storage_dir, chain)
        blocks, sims, metas = [], [], []
        d = from_day
        while d <= to_day:
            blocks += _records(store.load_blocks(d))
            sims += _records(store.load_sims(d))
            metas += _records(store.load_meta(d))
            d += timedelta(days=1)
        per_chain.append(compute_chain_metrics(chain, blocks, sims, cfg.chains[chain], settings))
        mt: dict = {"mode": "live", "rpc_errors": {}}
        for r in metas:
            if r["kind"] == "probe":
                w = r.get("state_window_blocks")
                mt["state_window_blocks"] = None if w is None else int(w)
            elif r["kind"] == "run":
                for t, c in (r.get("rpc_errors") or {}).items():
                    mt["rpc_errors"][t] = mt["rpc_errors"].get(t, 0) + c
                mt["sim_backend"] = r.get("sim_backend", mt.get("sim_backend"))
                mt["start"] = min(mt.get("start", r["start"]), r["start"])
                mt["end"] = max(mt.get("end", r["end"]), r["end"])
        meta[chain] = mt
    return render_report(per_chain, meta, from_day, to_day)


def write_defi_report(settings: Settings, chains: list[str], from_day: date, to_day: date,
                      out_dir: str | Path = "reports/defi") -> Path:
    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)
    path = path / f"{from_day.isoformat()}_{to_day.isoformat()}.md"
    path.write_text(build_defi_report(settings, chains, from_day, to_day), encoding="utf-8")
    return path


# ---- probe --------------------------------------------------------------------------------------------------
async def probe(client: ChainClient, registry: PoolRegistry, settings: Settings, sim_client: ChainClient | None = None) -> dict:
    """Section 6.4 step 1: chain id, Multicall3, state window, one unprofitable harness simulation."""
    import eth_abi

    cfg = settings.defi
    assert cfg is not None
    out: dict = {"chain": registry.chain}
    out["chain_id"] = await client.chain_id()
    out["chain_id_ok"] = out["chain_id"] == registry.meta.get("chain_id")
    mc = Multicall(client)
    try:
        await mc.check()
        out["multicall3"] = True
    except RpcError as e:
        out["multicall3"] = False
        out["multicall3_error"] = str(e)[:200]
    head = await client.block_number()
    out["head"] = head
    weth = registry.tokens.get("WETH")
    aave = registry.meta.get("aave_pool")
    window = 0
    depths = list(PROBE_DEPTHS) + [DAY_BLOCKS.get(registry.chain, 100_000)]
    out["depths"] = {}
    if weth and aave:
        data = sel("balanceOf(address)") + eth_abi.encode(["address"], [aave])
        for d in sorted(set(depths)):
            try:
                await client.eth_call(weth.address, data, head - d)
                out["depths"][d] = "ok"
                window = max(window, d)
            except RpcError as e:
                out["depths"][d] = f"{type(e).__name__}: {e.message[:80]}"
    out["state_window_blocks"] = window
    out["harness"] = "not run"
    sc = sim_client or client
    try:
        weth_pools = sorted(registry.pools_for_pair("WETH", "USDC"), key=lambda p: -registry.tvl_usd.get(p.address, 0))
        if len(weth_pools) < 2:
            raise SimulatorUnavailable("need two WETH/USDC pools for the probe route")
        p1, p2 = weth_pools[0], weth_pools[1]
        wa, ua = weth.address, registry.tokens["USDC"].address
        hops = (Hop(p1.address, wa, ua, p1.venue, wa == p1.token0.address),
                Hop(p2.address, ua, wa, p2.venue, ua == p2.token0.address))
        amount = 10 ** 16  # 0.01 WETH
        route = Route(wa, amount, 0, int(time.time()) + 3600, hops)
        n = head - 1
        snap = await read_snapshot(client, registry, n)
        costs = await read_block_costs(client, registry, snap, cfg)
        req = SimRequest(registry.chain, n, route, aave, (p1, p2), {wa: amount, ua: 0}, cfg.sim_gas)
        res = await simulate(sc, req, costs)
        out["harness"] = ("ok: success=%s reason=%s gas_inner=%d" % (res.success, res.revert_reason, res.gas_used_inner))
        out["harness_result"] = res
    except SimulatorUnavailable as e:
        out["harness"] = f"unavailable: {str(e)[:200]}"
    except RpcError as e:
        out["harness"] = f"data failure: {type(e).__name__}: {e.message[:200]}"
    return out


# ---- live run -----------------------------------------------------------------------------------------------
async def wait_for_block(client: ChainClient, target: int, *, timeout_s: float = 60.0, poll_s: float = 0.4,
                         clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep) -> None:
    end = clock() + timeout_s
    while True:
        if await client.block_number() >= target:
            return
        if clock() > end:
            raise StateUnavailable("eth_blockNumber", None, f"block {target} not reached within {timeout_s}s")
        await sleep(poll_s)


async def replay_opportunity(scan_client: ChainClient, sim_client: ChainClient, registry: PoolRegistry,
                             settings: Settings, opp, costs_n: BlockCosts, offset: int, block_found: int,
                             ts_found: int, wait=wait_for_block) -> dict:
    cfg = settings.defi
    assert cfg is not None
    block_sim = block_found + offset
    persisted: bool | None = True if offset == 0 else None
    try:
        if offset:
            await wait(scan_client, block_sim)
            later = await scan_block(scan_client, registry, block_sim, settings)
            persisted = (opp.key in {o.key for o in later.opportunities}) if later.complete else None
        hdr = await sim_client.header(block_sim)
        costs_k = replace(costs_n, block=block_sim, base_fee_wei=hdr.base_fee_wei)  # L1 fee carried over from N
        d = opp.details
        hops = tuple(Hop(h["pool"], h["token_in"], h["token_out"], Venue(h["venue"]), h["zero_for_one"])
                     for h in d["hops"])
        route = Route(d["loan_token"], d["amount_in_raw"], 0, ts_found + 3600, hops)
        pools = tuple(registry.by_address(h.pool) for h in hops)
        caps = {t: 0 for h in hops for t in (h.token_in, h.token_out)}
        caps[route.loan_token] = route.loan_amount
        res = await simulate(sim_client, SimRequest(registry.chain, block_sim, route,
                                                    registry.meta["aave_pool"], pools, caps, cfg.sim_gas), costs_k)
    except DATA_ERRORS as e:
        return sim_record(registry.chain, opp, None, offset, block_found, ts_found, cfg.min_profit_usd, persisted,
                          f"{type(e).__name__}: {str(e)[:200]}")
    return sim_record(registry.chain, opp, res, offset, block_found, ts_found, cfg.min_profit_usd, persisted)


def backend_label(client: ChainClient, sim_client: ChainClient) -> str:
    """Host only: RPC URLs may carry API keys and must never reach stored files."""
    from urllib.parse import urlparse

    if sim_client is client:
        return "same RPC as state reads"
    return f"separate sim RPC ({urlparse(sim_client.rpc_url).hostname})"


def _failure_type(f: str) -> str:
    parts = f.split(": ", 2)
    return parts[1] if len(parts) > 2 else "unknown"


@dataclass
class RunSummary:
    chain: str
    samples: int = 0
    incomplete: int = 0
    episodes_started: int = 0
    replays: int = 0
    rpc_errors: Counter = field(default_factory=Counter)
    stopped_reason: str = ""
    start_ms: int = 0
    end_ms: int = 0


async def run_measurement(chain: str, client: ChainClient, sim_client: ChainClient, registry: PoolRegistry,
                          settings: Settings, store: DefiStore, hours: float, *, max_samples: int | None = None,
                          clock: Callable[[], float] = time.time, sleep=asyncio.sleep,
                          wait=wait_for_block, on_sample: Callable[[dict], None] | None = None) -> RunSummary:
    cfg = settings.defi
    assert cfg is not None
    ccfg = cfg.chains[chain]
    summary = RunSummary(chain, start_ms=int(clock() * 1000))
    interval = ccfg.sample_every_s
    end = clock() + hours * 3600
    prev_keys: set[str] = set()
    prev_pools: frozenset[str] = frozenset()
    last_ok_ts: int | None = None
    recent: list[bool] = []
    pending: set[asyncio.Task] = set()

    async def replays_for(opp, costs, block_found, ts_found):
        rows = await asyncio.gather(*(replay_opportunity(client, sim_client, registry, settings, opp, costs, k,
                                                         block_found, ts_found, wait)
                                      for k in sorted(set(ccfg.lateness_blocks))))
        store.append_sims(list(rows))
        summary.replays += len(rows)

    while clock() < end and (max_samples is None or summary.samples < max_samples):
        started = clock()
        try:
            head = await client.block_number()
            scan = await scan_block(client, registry, head - 1, settings)
        except Exception as e:  # block number itself failed
            scan = ScanResult(chain, 0, 0, 0, 0, 0, [], [f"head: {type(e).__name__}: {str(e)[:200]}"], False, 0.0)
        rec = block_record(chain, scan, int(started))
        store.append_blocks([rec])
        store.append(scan.opportunities)
        store.log_scan(rec["timestamp"] * 1000, {"candidates": scan.candidates, "screened_in": scan.screened_in,
                                                 "gross_positive": scan.gross_positive,
                                                 "opportunities": len(scan.opportunities)},
                       scan.errors, scan.duration_s)
        summary.samples += 1
        if not scan.complete:
            summary.incomplete += 1
        for f in client.drain_failures() + (sim_client.drain_failures() if sim_client is not client else []):
            summary.rpc_errors[_failure_type(f)] += 1
        recent = (recent + [scan.complete])[-DATA_FAILURE_WINDOW:]
        if on_sample:
            on_sample(rec)
        if scan.complete and scan.costs is not None:
            if last_ok_ts is not None and rec["timestamp"] - last_ok_ts > max(5 * interval, 120.0):
                prev_keys, prev_pools = set(), frozenset()  # coverage gap: find_episodes starts new episodes too
            last_ok_ts = rec["timestamp"]
            cur = {o.key for o in scan.opportunities}
            by_key = {o.key: o for o in scan.opportunities}
            # same rule as find_episodes: one attempt per independent gap, replayed when it starts a new episode
            attempts = [by_key[s["key"]] for s in select_attempts(rec["surviving"])]
            new = [o for o in attempts if is_new_attempt(o.key, prev_keys, prev_pools)][:MAX_REPLAYS_PER_SAMPLE]
            prev_keys = cur
            prev_pools = frozenset().union(*(key_pools(o.key) for o in attempts)) if attempts else frozenset()
            for opp in new:
                summary.episodes_started += 1
                t = asyncio.create_task(replays_for(opp, scan.costs, scan.block, rec["timestamp"]))
                pending.add(t)
                t.add_done_callback(pending.discard)
        if len(recent) >= DATA_FAILURE_WINDOW and recent.count(False) / len(recent) > DATA_FAILURE_STOP:
            summary.stopped_reason = (f"data-failure rate {recent.count(False) / len(recent):.1%} over the last "
                                      f"{len(recent)} samples exceeds {DATA_FAILURE_STOP:.0%}: stopping (INCONCLUSIVE (DATA)). "
                                      "Put a free-tier RPC URL in .env (ARBITRUM_RPC / BASE_RPC) and re-run.")
            break
        if any("RateLimited" in e for e in scan.errors) or summary.rpc_errors.get("RateLimited", 0) > summary.samples:
            interval = min(interval * 2, 60.0)
            log.warning("%s: rate limited, sampling every %.0fs", chain, interval)
        await sleep(max(0.0, interval - (clock() - started)))
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)
    summary.end_ms = int(clock() * 1000)
    store.append_meta([{"ts_ms": summary.end_ms, "kind": "run", "mode": "live",
                        "start": time.strftime("%Y-%m-%d %H:%M", time.gmtime(summary.start_ms / 1000)),
                        "end": time.strftime("%Y-%m-%d %H:%M", time.gmtime(summary.end_ms / 1000)),
                        "samples": summary.samples, "incomplete": summary.incomplete,
                        "rpc_errors": dict(summary.rpc_errors), "sim_backend": backend_label(client, sim_client),
                        "stopped_reason": summary.stopped_reason}])
    return summary
