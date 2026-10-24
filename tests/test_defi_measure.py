"""measure.py: metric formulas on synthetic records, episode grouping, report tables, store, live loop on fakes."""
from datetime import date

import pytest

from arb.config import load_settings
from arb.costs import CostModel
from arb.defi import measure
from arb.defi.chain import StateUnavailable
from arb.defi.measure import (build_defi_report, compute_chain_metrics, find_episodes, implied_profit_raw,
                              overall_verdict, render_report, replay_stats, run_measurement, sampled_hours, sim_record)
from arb.defi.scanner import ARBITRUM_NODE_INTERFACE, S_GAS_ESTIMATE_L1, ScanResult
from arb.defi.simulate import SimResult
from arb.defi.store import DefiStore
from arb.models import Opportunity
from arb.report import build_report
from tests.defi_fakes import FakeClock, FakeNode, chain_node, defi_cfg, make_client, pool_node, w
from tests.test_defi_scanner import world

SETTINGS = load_settings().model_copy(update={"defi": defi_cfg()})
TAX = CostModel(SETTINGS)
CCFG = SETTINGS.defi.chains["arbitrum"]
T0 = 1_800_000_000


def brow(block, keys=(), complete=True, gross=None, ts=None, kind="two_pool", net=0.5):
    return {"chain": "arbitrum", "block": block, "timestamp": ts if ts is not None else T0 + block * 10,
            "candidates": 100, "screened_in": 5, "gross_positive": len(keys) if gross is None else gross,
            "surviving_keys": list(keys), "surviving": [{"key": k, "kind": kind, "net_usd": net, "capacity_usd": 1000.0} for k in keys],
            "best_net_usd": net if keys else None, "errors": [] if complete else ["x"], "complete": complete,
            "by_kind": {kind: {"candidates": 100, "screened_in": 5, "gross_positive": len(keys), "surviving": len(keys)},
                        "triangular" if kind == "two_pool" else "two_pool": {"candidates": 10, "screened_in": 1, "gross_positive": 0, "surviving": 0}}}


def srow(key, offset, net, success=True, gas_usd=0.03, persisted=True, qerr=None, mix="a+b", df=False):
    hit = (success and net >= 0.05)
    return {"chain": "arbitrum", "key": key, "block_found": 1, "offset": offset, "block_sim": 1 + offset, "timestamp": T0,
            "kind": "two_pool", "venue_mix": mix, "success": None if df else success, "net_usd": None if df else net,
            "gas_estimate_tx": None if df else 300_000, "gas_cost_usd": None if df else gas_usd, "persisted": persisted,
            "data_failure": df, "hit": None if df else hit, "below_threshold": None if df else (success and not hit),
            "miss_cost_usd": None if df else (0.0 if hit else gas_usd), "quote_error_bps": qerr, "predicted_net_usd": 0.5}


def test_episodes_group_consecutive_samples_and_skip_incomplete():
    blocks = [brow(1, ["a"]), brow(2, ["a", "b"]), brow(3, ["x"], complete=False), brow(4, ["a"]), brow(5, []),
              brow(6, ["a"])]
    eps = find_episodes(blocks)
    # a: 1..4 is one episode (the incomplete sample neither ends nor starts anything), b: 2, a again at 6 after a gap at 5
    assert [(e["key"], e["block"]) for e in eps] == [("a", 1), ("b", 2), ("a", 6)]
    assert find_episodes([]) == []


def test_sampled_hours_uses_median_spacing_of_complete_samples():
    blocks = [brow(i, ts=T0 + 10 * i) for i in range(1, 361)]  # 360 samples 10 s apart = 1 h
    assert sampled_hours(blocks, 99) == pytest.approx(1.0)
    blocks[0]["complete"] = False
    assert sampled_hours(blocks, 99) == pytest.approx(359 * 10 / 3600)
    assert sampled_hours([], 10) == 0.0


def test_replay_stats_ev_formulas():
    rows = [srow("k", 0, 0.50), srow("k", 0, 0.30), srow("k", 0, 0.02, gas_usd=0.04),  # won below threshold
            srow("k", 0, -0.03, success=False, gas_usd=0.02), srow("k", 0, 0, df=True)]
    st = replay_stats(rows, eps_per_day=10.0, tax=TAX)
    assert (st["replays"], st["data_failures"], st["hits"], st["below"]) == (4, 1, 2, 1)
    assert st["hit_rate"] == 0.5
    assert st["median_profit"] == pytest.approx(0.40) and st["mean_profit"] == pytest.approx(0.40)
    assert st["mean_miss_cost"] == pytest.approx((0.04 + 0.02) / 2)
    assert st["ev"] == pytest.approx(0.5 * 0.40 - 0.5 * 0.03)
    at = (0.50 * (1 - 0.312) + 0.30 * (1 - 0.312)) / 2
    assert st["ev_after_tax"] == pytest.approx(0.5 * at - 0.5 * 0.03)
    assert st["projected_per_day"] == pytest.approx(10 * st["ev_after_tax"])
    assert st["median_gas"] == 300_000
    empty = replay_stats([srow("k", 0, 0, df=True)], 5.0, TAX)
    assert empty["replays"] == 0 and empty["hit_rate"] is None and empty["ev"] is None and empty["projected_per_day"] is None


def opp(profit_raw=1000, amount=10 ** 6, net=0.4):
    return Opportunity("defi_two_pool", T0 * 1000, "arbitrum WETH k", [], 100.0, 1.0, {}, net, net, 1.0, 10.0, "instant",
                       details={"kind": "two_pool", "loan_symbol": "WETH", "amount_in_raw": amount,
                                "profit_raw": profit_raw, "hops": [{"dex": "sushi_v2"}, {"dex": "camelot_v2"}]})


def simres(success=True, profit=1000, net=0.4, reason=None, gas_usd=0.03):
    return SimResult("arbitrum", 5, success, reason, profit, 200_000, 300_000, gas_usd, net + gas_usd, net,
                     "0x" + "ab" * 32)


def test_sim_record_classifies_hit_below_threshold_and_miss():
    hit = sim_record("arbitrum", opp(), simres(net=0.40), 0, 5, T0, 0.05, True)
    assert hit["hit"] and not hit["below_threshold"] and hit["miss_cost_usd"] == 0.0 and hit["block_sim"] == 5
    assert hit["quote_error_bps"] == pytest.approx(0.0) and hit["venue_mix"] == "camelot_v2+sushi_v2"
    below = sim_record("arbitrum", opp(), simres(net=0.01), 2, 5, T0, 0.05, True)
    assert not below["hit"] and below["below_threshold"] and below["miss_cost_usd"] == 0.03 and below["block_sim"] == 7
    rev = simres(False, 0, -0.03, "InsufficientProfit(balance=900, required=1000)")
    miss = sim_record("arbitrum", opp(), rev, 0, 5, T0, 0.05, True)
    assert not miss["hit"] and not miss["below_threshold"] and miss["miss_cost_usd"] == 0.03
    assert implied_profit_raw(rev) == -100
    assert miss["quote_error_bps"] == pytest.approx(abs(1000 - -100) / 10 ** 6 * 1e4)
    other = simres(False, 0, -0.03, "PartialFill(hop=0, paid=1, amountIn=2)")
    assert implied_profit_raw(other) is None
    assert sim_record("arbitrum", opp(), other, 0, 5, T0, 0.05, True)["quote_error_bps"] is None
    fail = sim_record("arbitrum", opp(), None, 8, 5, T0, 0.05, None, "StateUnavailable: missing trie node")
    assert fail["data_failure"] and fail["hit"] is None and fail["error"].startswith("StateUnavailable")


def synthetic(n_episodes, hit_net=0.50, hits_at_k=None, df_rate=0.0, n_samples=2400):
    """n_episodes distinct episodes over n_samples x 10 s, each replayed at every offset (k=8 hit iff in `hits_at_k`).
    2400 samples (6.67 h) clears the 2000-complete-samples minimum of the verdict."""
    blocks, sims = [], []
    keys = [f"k{i}" for i in range(n_episodes)]
    for i in range(1, n_samples + 1):
        ks = [keys[i - 1]] if i <= n_episodes else []
        blocks.append(brow(i, ks, complete=not (df_rate and i % round(1 / df_rate) == 0)))
    for i, k in enumerate(keys):
        for off in (0, 2, 8):
            hit = off == 0 or (hits_at_k or set()) >= {i} or i in (hits_at_k or set())
            sims.append(srow(k, off, hit_net if hit else -0.03, success=hit, persisted=hit, qerr=0.2 if off == 0 else None,
                             mix="sushi_v2+uniswap_v3"))
    return blocks, sims


def test_compute_chain_metrics_tables_and_go_verdict():
    blocks, sims = synthetic(40, hits_at_k=set(range(40)))
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    assert m["samples"] == 2400 and m["complete"] == 2400 and m["data_failure_rate"] == 0.0
    hours = 2400 * 10 / 3600
    assert m["episodes"] == 40 and m["hours"] == pytest.approx(hours) and m["episodes_per_day"] == pytest.approx(40 / hours * 24)
    assert m["route_key_episodes_per_day"] == pytest.approx(m["episodes_per_day"])  # no correlated routes here
    assert m["surviving_rate"] == pytest.approx(40 / 2400) and m["gross_rate"] == pytest.approx(40 / 2400)
    assert m["kinds"]["two_pool"]["episodes_per_day"] == pytest.approx(40 / hours * 24) and m["kinds"]["triangular"]["episodes_per_day"] == 0
    assert m["kinds"]["two_pool"]["net_med"] == 0.5 and m["kinds"]["two_pool"]["capacity_med"] == 1000.0
    assert m["persistence"] == {0: 1.0, 2: 1.0, 8: 1.0}
    assert m["realistic_k"] == 8
    r8 = m["replay"][8]
    assert r8["replays"] == 40 and r8["hit_rate"] == 1.0
    assert r8["ev_after_tax"] == pytest.approx(0.5 * (1 - 0.312))
    assert m["verdict"] == "GO-TO-NEXT-PHASE" and m["verdict_inputs"]["episodes_replayed"] == 40
    assert m["quote_accuracy"] == {"sushi_v2+uniswap_v3": {"n": 40, "median": 0.2, "p90": 0.2}}
    assert len(m["top_keys"]) == 15 and m["top_keys"][0]["hit0"] == 1.0 and m["top_keys"][0]["hitk"] == 1.0


def test_verdict_no_go_when_projection_is_below_the_bar_and_data_is_good():
    blocks, sims = synthetic(40, hits_at_k=set())  # nothing hits at k=8: every attempt only costs gas
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    r8 = m["replay"][8]
    assert r8["hit_rate"] == 0.0 and r8["ev_after_tax"] == pytest.approx(-0.03) and m["persistence"][8] == 0.0
    assert m["verdict"] == "NO-GO"


def test_verdict_inconclusive_on_too_few_episodes_or_bad_data():
    blocks, sims = synthetic(29, hits_at_k=set(range(29)))
    assert compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)["verdict"] == "INCONCLUSIVE (DATA)"
    blocks, sims = synthetic(40, hits_at_k=set(range(40)), df_rate=0.1)  # 10% of samples incomplete
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    assert m["data_failure_rate"] == pytest.approx(0.1, abs=0.01) and m["verdict"] == "INCONCLUSIVE (DATA)"
    # a replay that hit a data failure is not an attempt
    blocks, sims = synthetic(40, hits_at_k=set(range(40)))
    sims = [dict(r, data_failure=True, hit=None, below_threshold=None, miss_cost_usd=None, net_usd=None, success=None,
                 gas_cost_usd=None, gas_estimate_tx=None) if r["offset"] == 8 and r["key"] == "k0" else r for r in sims]
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    assert m["replay"][8]["replays"] == 39 and m["replay"][8]["data_failures"] == 1


def test_overall_verdict():
    g, n, i = ({"verdict": v} for v in ("GO-TO-NEXT-PHASE", "NO-GO", "INCONCLUSIVE (DATA)"))
    assert overall_verdict([g, i]) == "GO-TO-NEXT-PHASE" and overall_verdict([n, n]) == "NO-GO"
    assert overall_verdict([n, i]) == "INCONCLUSIVE (DATA)" and overall_verdict([]) == "INCONCLUSIVE (DATA)"


def test_report_renders_all_seven_tables_even_with_no_data():
    blocks, sims = synthetic(40, hits_at_k=set(range(40)), n_samples=360)
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    text = render_report([m], {"arbitrum": {"mode": "live", "rpc_errors": {"RateLimited": 3}, "state_window_blocks": 345600,
                                           "sim_backend": "public RPC"}}, date(2026, 10, 25), date(2026, 10, 26))
    for h in ("## 1. Coverage", "## 2. Gap test", "## 3. Persistence", "## 4. Replay", "## 5. Top 15 keys",
              "## 6. Quote accuracy", "## 7. Verdict"):
        assert h in text
    assert "RateLimited: 3" in text and "only 360 complete samples" in text
    # review T4 (M-02): 360 samples (1 h) can no longer produce a GO, whatever the projection says
    assert "VERDICT: INCONCLUSIVE (DATA)" in text and "complete samples < 2000" in text
    assert "median scan latency" in text and "route keys:" in text
    empty = compute_chain_metrics("base", [], [], SETTINGS.defi.chains["base"], SETTINGS)
    t2 = render_report([empty], {}, date(2026, 10, 25), date(2026, 10, 25))
    assert "INCONCLUSIVE (DATA)" in t2 and "## 6. Quote accuracy" in t2


# ---- store and report from disk ---------------------------------------------------------------------------------------
def test_store_partitions_by_day_and_feeds_the_standard_report(tmp_path):
    store = DefiStore(tmp_path, "arbitrum")
    day = date.fromtimestamp(0).replace(year=2026, month=10, day=25)
    t = int(__import__("datetime").datetime(2026, 10, 25, 23, 59, 59, tzinfo=__import__("datetime").timezone.utc).timestamp())
    store.append_blocks([brow(1, ["a"], ts=t), brow(2, [], ts=t + 2)])  # crosses midnight UTC
    assert len(store.load_blocks(date(2026, 10, 25))) == 1 and len(store.load_blocks(date(2026, 10, 26))) == 1
    store.append_sims([dict(srow("a", 0, 0.4), timestamp=t)])
    assert len(store.load_sims(date(2026, 10, 25))) == 1
    store.append_meta([{"ts_ms": t * 1000, "kind": "probe", "state_window_blocks": 5}])
    assert store.load_meta(date(2026, 10, 25)).iloc[0]["state_window_blocks"] == 5
    assert (tmp_path / "arbitrum" / "blocks" / "2026-10-25.jsonl").exists()
    # standard opportunity flow + scan log with errors: arb.report.build_report works unchanged
    o = opp()
    o.timestamp_ms = t * 1000
    store.append([o])
    store.log_scan(t * 1000, {"candidates": 4}, ["pool read failed"], 1.2)
    rep = build_report(store, date(2026, 10, 25))
    assert "defi_two_pool" in rep and "Missing data is not the same as no edge" in rep


def test_build_defi_report_from_disk(tmp_path):
    st = SETTINGS.model_copy(update={"defi": defi_cfg(storage_dir=str(tmp_path))})
    store = DefiStore(tmp_path, "arbitrum")
    blocks, sims = synthetic(5, n_samples=360)
    for r in blocks:
        r["timestamp"] = int(__import__("datetime").datetime(2026, 10, 25, 12, tzinfo=__import__("datetime").timezone.utc).timestamp()) + r["block"]
    for r in sims:
        r["timestamp"] = blocks[0]["timestamp"]
    store.append_blocks(blocks)
    store.append_sims(sims)
    store.append_meta([{"ts_ms": blocks[0]["timestamp"] * 1000, "kind": "run", "start": "2026-10-25 12:00", "end": "2026-10-25 13:00",
                        "rpc_errors": {"RateLimited": 2}, "sim_backend": "same RPC as state reads"}])
    text = build_defi_report(st, ["arbitrum"], date(2026, 10, 25), date(2026, 10, 25))
    assert "RateLimited: 2" in text and "360/360" in text
    path = measure.write_defi_report(st, ["arbitrum"], date(2026, 10, 25), date(2026, 10, 25), tmp_path / "rep")
    assert path.name == "2026-10-25_2026-10-25.md" and path.read_text() == text


# ---- live loop on fakes -------------------------------------------------------------------------------------------------
class SimStub:
    def __init__(self, fail_offsets=()):
        self.calls, self.fail_offsets = [], set(fail_offsets)

    async def __call__(self, client, req, costs):
        off = req.block - 999
        self.calls.append((req.block, req.route.min_profit, costs.base_fee_wei))
        if off in self.fail_offsets:
            raise StateUnavailable("eth_call", None, "missing trie node")
        return SimResult("arbitrum", req.block, True, None, 5 * 10 ** 15, 250_000, 320_000, 0.02, 9.98, 9.96, "0x" + "cd" * 32)


async def no_wait(client, block, **kw):
    return None


async def test_run_measurement_samples_replays_each_episode_once_and_records_failures(tmp_path, monkeypatch):
    reg, states, node = world(gap=True)
    stub = SimStub(fail_offsets={8})
    monkeypatch.setattr(measure, "simulate", stub)
    store = DefiStore(tmp_path, "arbitrum")
    clock = FakeClock()
    seen = []
    summary = await run_measurement("arbitrum", make_client(node), make_client(node), reg, SETTINGS, store, hours=1,
                                    max_samples=3, clock=lambda: 1_800_000_000 + clock.t, sleep=clock.sleep, wait=no_wait,
                                    on_sample=seen.append)
    assert summary.samples == 3 and summary.incomplete == 0 and len(seen) == 3
    # the same gap persists in all three samples (static fake chain). Its two surviving routes (WETH loan
    # sushi>camelot, USDC loan camelot>sushi) share both pools: one attempt, one episode, replayed once (review M-01)
    n_keys = len(seen[0]["surviving_keys"])
    assert n_keys >= 2 and summary.episodes_started == 1 and len(seen[0]["attempt_keys"]) == 1
    assert seen[0]["attempt_keys"][0] == max(seen[0]["surviving"], key=lambda s: s["net_usd"])["key"]
    assert summary.replays == 3
    day = date(2027, 1, 15)
    ts = seen[0]["timestamp"]
    d = __import__("datetime").datetime.fromtimestamp(ts, __import__("datetime").timezone.utc).date()
    blocks, sims = store.load_blocks(d), store.load_sims(d)
    assert len(blocks) == 3 and len(sims) == 3
    assert set(sims["offset"]) == {0, 2, 8}
    k8 = sims[sims["offset"] == 8]
    assert k8["data_failure"].all() and k8["error"].str.startswith("StateUnavailable").all()
    k0 = sims[sims["offset"] == 0]
    assert k0["hit"].all() and (k0["net_usd"] == 9.96).all()
    assert all(mp == 0 for _, mp, _ in stub.calls)  # replays use minProfit = 0
    meta = store.load_meta(d).iloc[0]
    assert meta["kind"] == "run" and meta["samples"] == 3 and str(meta["sim_backend"]) == "separate sim RPC (fake)"
    assert len(store.load_opportunities(d)) == 3 * n_keys
    assert len(store.load_scans(d)) == 3
    del day


async def test_incomplete_samples_are_stored_flagged_and_never_replayed(tmp_path, monkeypatch):
    reg, states, node = world(gap=True)
    monkeypatch.setattr(measure, "simulate", SimStub())
    node.http_error = lambda p: (500, {}, {}) if p["method"] == "eth_call" else None
    store = DefiStore(tmp_path, "arbitrum")
    clock = FakeClock()
    c = make_client(node, max_retries=0)
    summary = await run_measurement("arbitrum", c, c, reg, SETTINGS, store, hours=1, max_samples=2,
                                    clock=lambda: 1_800_000_000 + clock.t, sleep=clock.sleep, wait=no_wait)
    assert summary.samples == 2 and summary.incomplete == 2 and summary.replays == 0 and summary.episodes_started == 0
    assert summary.rpc_errors["TransportError"] > 0
    d = __import__("datetime").datetime.fromtimestamp(1_800_000_000, __import__("datetime").timezone.utc).date()
    blocks = store.load_blocks(d)
    assert not blocks["complete"].any() and all(len(e) for e in blocks["errors"])


async def test_run_stops_when_the_data_failure_rate_is_too_high(tmp_path, monkeypatch):
    reg, states, node = world()
    n = {"i": 0}

    async def fake_scan(client, registry, block, settings):
        n["i"] += 1
        ok_ = n["i"] % 10 != 0  # 10% incomplete
        return ScanResult("arbitrum", block, (1_800_000_000 + n["i"]) * 1000, 0, 0, 0, [], [] if ok_ else ["e"], ok_, 0.0)

    monkeypatch.setattr(measure, "scan_block", fake_scan)
    clock = FakeClock()
    summary = await run_measurement("arbitrum", make_client(node), make_client(node), reg, SETTINGS,
                                    DefiStore(tmp_path, "arbitrum"), hours=24, clock=lambda: 1_800_000_000 + clock.t,
                                    sleep=clock.sleep, wait=no_wait)
    assert summary.samples == 200 and "exceeds 5%" in summary.stopped_reason and "INCONCLUSIVE (DATA)" in summary.stopped_reason


async def test_wait_for_block_polls_until_the_block_exists_or_times_out():
    node = FakeNode()
    node.head = 10
    clock = FakeClock()
    c = make_client(node)

    async def grow(delay):
        node.head += 1
        await clock.sleep(delay)

    await measure.wait_for_block(c, 13, clock=clock, sleep=grow)
    assert node.head == 13
    with pytest.raises(StateUnavailable):
        await measure.wait_for_block(c, 10_000, timeout_s=2, clock=clock, sleep=clock.sleep)


def test_backend_label_never_contains_the_url_path():
    a = make_client(FakeNode())
    b = make_client(FakeNode())
    b.rpc_url = "https://rpc.example.com/v2/SECRETKEY"
    assert measure.backend_label(a, a) == "same RPC as state reads"
    label = measure.backend_label(a, b)
    assert "rpc.example.com" in label and "SECRETKEY" not in label


# ---- review T4 regressions (each fails on the pre-review code) ------------------------------------------------------
A = "sushi_v2:0xaaaa..0001"
B = "camelot_v2:0xbbbb..0002"
C = "uniswap_v3:0xcccc..0003"
D = "uniswap_v3:0xdddd..0004"


def test_m01_routes_sharing_a_pool_are_one_attempt_not_several_episodes():
    """One mispriced pair seen as WETH-loan A>B, USDC-loan B>A and a triangle through A: one gap, one attempt."""
    weth_ab, usdc_ba, tri = f"arbitrum WETH {A}>{B}", f"arbitrum USDC {B}>{A}", f"arbitrum WETH {A}>{C}>{D}"
    other = f"arbitrum WETH {C}>{D}"  # disjoint from the first gap? no: shares C and D with the triangle
    b1 = brow(1, [])
    b1["surviving"] = [{"key": weth_ab, "kind": "two_pool", "net_usd": 0.40, "capacity_usd": 1.0},
                       {"key": usdc_ba, "kind": "two_pool", "net_usd": 0.39, "capacity_usd": 1.0},
                       {"key": tri, "kind": "triangular", "net_usd": 0.10, "capacity_usd": 1.0},
                       {"key": other, "kind": "two_pool", "net_usd": 0.20, "capacity_usd": 1.0}]
    assert [s["key"] for s in measure.select_attempts(b1["surviving"])] == [weth_ab, other]
    # next sample: the USDC direction is now the better route of the same gap -> a continuation, not a new episode
    b2 = brow(2, [])
    b2["surviving"] = [dict(b1["surviving"][0], net_usd=0.30), dict(b1["surviving"][1], net_usd=0.35)]
    eps = find_episodes([b1, b2])
    assert [e["key"] for e in eps] == [weth_ab, other]
    assert len(find_episodes([b1, b2], by_route_key=True)) == 4  # the old count: every route its own episode
    assert measure.key_pools(weth_ab) == {"0xaaaa..0001", "0xbbbb..0002"}


def test_m01_projection_uses_independent_attempts():
    blocks, sims = synthetic(40, hits_at_k=set(range(40)))
    for b in blocks:  # every episode (its own pair of pools) is seen through a second route (opposite loan token)
        b["surviving"] = [dict(s, key=f"arbitrum WETH sushi_v2:0xa-{s['key']}>camelot_v2:0xb-{s['key']}")
                          for s in b["surviving"]]
        b["surviving"] += [dict(s, key=s["key"].replace("WETH", "USDC"), net_usd=0.1) for s in b["surviving"]]
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    assert m["route_key_episodes_per_day"] == pytest.approx(2 * m["episodes_per_day"])
    assert m["replay"][8]["projected_per_day"] == pytest.approx(m["episodes_per_day"] * m["replay"][8]["ev_after_tax"])


def test_m02_verdict_needs_2000_complete_samples():
    blocks, sims = synthetic(40, hits_at_k=set(range(40)), n_samples=360)  # 1 hour, 40 profitable episodes
    m = compute_chain_metrics("arbitrum", blocks, sims, CCFG, SETTINGS)
    assert m["replay"][8]["projected_per_day"] > 1.0
    assert m["verdict"] == "INCONCLUSIVE (DATA)"  # the old code said GO-TO-NEXT-PHASE
    assert m["verdict_inputs"]["inconclusive_because"] == ["complete samples < 2000"]


def test_m02_verdict_inconclusive_when_replays_mostly_failed_to_run():
    blocks, sims = synthetic(40, hits_at_k=set(range(40)))
    extra = [dict(srow(f"x{i}", 8, 0, df=True)) for i in range(5)]  # 5 / 45 = 11% of k=8 replays did not run
    m = compute_chain_metrics("arbitrum", blocks, sims + extra, CCFG, SETTINGS)
    assert m["verdict"] == "INCONCLUSIVE (DATA)" and "replay data-failure rate" in m["verdict_inputs"]["inconclusive_because"]


def test_m03_sampled_hours_follow_real_time_when_sampling_slows_down():
    # 180 samples 10 s apart, then 180 samples 30 s apart (rate-limit back-off): 1800 + 5400 s observed
    ts = [T0 + 10 * i for i in range(180)] + [T0 + 1800 + 30 * i for i in range(180)]
    blocks = [brow(i + 1, ts=t) for i, t in enumerate(ts)]
    assert sampled_hours(blocks, 10) == pytest.approx(7200 / 3600, rel=0.01)  # old formula: 360 x 10 s = 1 h
    # a 2-hour outage in the middle counts as one step, not as observed time
    blocks2 = [brow(i + 1, ts=T0 + 10 * i) for i in range(180)] + [brow(200 + i, ts=T0 + 9000 + 10 * i) for i in range(180)]
    assert sampled_hours(blocks2, 10) == pytest.approx(3600 / 3600, rel=0.01)


def test_m03_a_coverage_gap_ends_episodes():
    blocks = [brow(1, ["a"], ts=T0), brow(2, ["a"], ts=T0 + 10), brow(3, ["a"], ts=T0 + 20),
              brow(4, ["a"], ts=T0 + 7200)]  # restart two hours later: a new attempt
    assert [e["block"] for e in find_episodes(blocks)] == [1, 4]


def test_r02_store_survives_a_torn_line(tmp_path):
    store = DefiStore(tmp_path, "arbitrum")
    path = tmp_path / "arbitrum" / "blocks" / "2027-01-15.jsonl"
    t = int(__import__("datetime").datetime(2027, 1, 15, 12, tzinfo=__import__("datetime").timezone.utc).timestamp())
    store.append_blocks([brow(1, ["a"], ts=t)])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"chain": "arbitrum", "block": 2, "timest')  # killed mid-write
    store.append_blocks([brow(3, ["a"], ts=t + 20)])  # the resumed run appends after it
    df = store.load_blocks(date(2027, 1, 15))
    assert list(df["block"]) == [1, 3] and df.attrs["skipped_lines"] == 1


def test_m01_same_thin_pool_through_another_counter_pool_is_the_same_episode():
    """Seen live on Base: thin pool X vs Y in one sample, X vs Z (better) in the next. Same mispriced pool X:
    an attempt in the first sample would have closed it, so it is one episode."""
    xy, xz = f"base USDC {A}>{C}", f"base USDC {A}>{D}"
    b1, b2 = brow(1, [xy]), brow(2, [])
    b2["surviving"] = [{"key": xy, "kind": "two_pool", "net_usd": 0.1, "capacity_usd": 1.0},
                       {"key": xz, "kind": "two_pool", "net_usd": 0.3, "capacity_usd": 1.0}]
    b3 = brow(3, [])
    b3["surviving"] = [{"key": f"base USDC {A}>{B}", "kind": "two_pool", "net_usd": 0.2, "capacity_usd": 1.0}]
    assert [e["key"] for e in find_episodes([b1, b2, b3])] == [xy]
    assert measure.is_new_attempt(xz, {xy}, measure.key_pools(xy)) is False
    assert measure.is_new_attempt(f"base USDC {B}>{D}", set(), measure.key_pools(xy)) is True


def test_replay_needing_more_gas_than_the_estimate_limit_is_an_out_of_gas_miss():
    """Harness runs with 25M gas; a real tx carries estimate x 1.3. An exhausting hop (13M gas) or a route that
    crosses far more ticks than predicted is a miss that costs the whole limit, not a hit or a cheap revert."""
    from arb.defi.scanner import BlockCosts
    costs = BlockCosts("arbitrum", 5, 20_000_000, 0, {2: 10 ** 12}, 2000.0, {"WETH": 2000.0})
    big = SimResult("arbitrum", 5, True, None, 10 ** 15, 13_000_000, 13_030_000, 0.52, 2.0, 1.48, "0x" + "ab" * 32)
    capped = measure.apply_gas_limit(big, 600_000, costs, 2)
    expect = (600_000 * 20_000_000 + 10 ** 12) / 1e18 * 2000.0
    assert not capped.success and capped.revert_reason.startswith("OutOfGas(limit=600000")
    assert capped.gas_estimate_tx == 600_000 and capped.gas_cost_usd == pytest.approx(expect)
    assert capped.net_usd == pytest.approx(-expect) and capped.gas_used_inner == 13_000_000
    rec = sim_record("arbitrum", opp(), capped, 0, 5, T0, 0.05, True)
    assert not rec["hit"] and not rec["below_threshold"] and rec["miss_cost_usd"] == pytest.approx(expect)
    assert rec["quote_error_bps"] is None
    # within the limit: unchanged
    ok_ = SimResult("arbitrum", 5, True, None, 10 ** 15, 400_000, 430_000, 0.02, 2.0, 1.98, "0x" + "ab" * 32)
    assert measure.apply_gas_limit(ok_, 600_000, costs, 2) is ok_ and measure.apply_gas_limit(ok_, None, costs, 2) is ok_


async def test_replays_apply_the_scanner_gas_limit(tmp_path, monkeypatch):
    reg, states, node = world(gap=True)

    async def heavy(client, req, costs):  # every replay needs 13M gas (pool exhaustion)
        return SimResult("arbitrum", req.block, True, None, 5 * 10 ** 15, 13_000_000, 13_030_000, 0.6, 9.98, 9.38,
                         "0x" + "cd" * 32)
    monkeypatch.setattr(measure, "simulate", heavy)
    store = DefiStore(tmp_path, "arbitrum")
    clock = FakeClock()
    s = await run_measurement("arbitrum", make_client(node), make_client(node), reg, SETTINGS, store, hours=1,
                              max_samples=1, clock=lambda: 1_800_000_000 + clock.t, sleep=clock.sleep, wait=no_wait)
    assert s.replays == 3
    d = __import__("datetime").datetime.fromtimestamp(1_800_000_000 + 1000, __import__("datetime").timezone.utc).date()
    sims = store.load_sims(d)
    assert len(sims) == 3 and not sims["hit"].any() and sims["revert_reason"].str.startswith("OutOfGas").all()
