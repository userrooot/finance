"""`python -m arb defi ...` command implementations (read-only; nothing here sends a transaction)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from pathlib import Path

from arb.config import Settings
from arb.defi import measure
from arb.defi.chain import ChainClient, RateLimiter, RpcError, connect
from arb.defi.pools import PoolRegistry, load_or_build_registry
from arb.defi.scanner import scan_block
from arb.defi.store import DefiStore

log = logging.getLogger("arb.defi")


def load_addresses(settings: Settings) -> dict:
    cfg = settings.defi
    assert cfg is not None
    addresses = json.loads(Path(cfg.addresses_file).read_text(encoding="utf-8"))
    for chain in cfg.chains:
        bps = addresses["chains"][chain]["aave_v3"]["flashloan_premium_total_bps"]
        if bps != cfg.premium_bps:
            raise SystemExit(f"config defi.premium_bps={cfg.premium_bps} but {cfg.addresses_file} says "
                             f"{chain} Aave premium is {bps} bps; refusing to run with a wrong cost model")
    return addresses


def _check(settings: Settings, chain: str) -> None:
    if settings.defi is None or not settings.defi.enabled:
        raise SystemExit("defi is not configured or not enabled in config/settings.yaml")
    if chain not in settings.defi.chains:
        raise SystemExit(f"unknown chain {chain!r}; configured: {', '.join(settings.defi.chains)}")


async def _setup(settings: Settings, chain: str, refresh: bool) -> tuple[ChainClient, PoolRegistry]:
    cfg = settings.defi
    assert cfg is not None
    addresses = load_addresses(settings)
    client = await connect(chain, cfg.chains[chain], int(addresses["chains"][chain]["chain_id"]))
    try:
        reg = await load_or_build_registry(client, chain, addresses, cfg, Path(cfg.storage_dir) / chain / "registry.json", refresh)
    except BaseException:
        await client.close()
        raise
    return client, reg


async def cmd_probe(settings: Settings, args) -> int:
    client, reg = await _setup(settings, args.chain, args.refresh)
    try:
        out = await measure.probe(client, reg, settings)
        print(f"chain {args.chain}: id {out['chain_id']} (expected match: {out['chain_id_ok']}), head {out['head']}")
        print(f"Multicall3: {'present' if out['multicall3'] else 'MISSING ' + out.get('multicall3_error', '')}")
        for d, r in out["depths"].items():
            print(f"  state at head-{d}: {r}")
        print(f"state window: {out['state_window_blocks']} blocks")
        print(f"harness: {out['harness']}")
        DefiStore(settings.defi.storage_dir, args.chain).append_meta(  # type: ignore[union-attr]
            [{"ts_ms": int(time.time() * 1000), "kind": "probe", "state_window_blocks": out["state_window_blocks"],
              "harness": out["harness"][:120]}])
        for f in client.drain_failures():
            print(f"  rpc failure: {f}")
        return 0
    finally:
        await client.close()


async def cmd_scan(settings: Settings, args) -> int:
    cfg = settings.defi
    assert cfg is not None
    client, reg = await _setup(settings, args.chain, args.refresh)
    store = DefiStore(cfg.storage_dir, args.chain)
    incomplete = 0
    try:
        head = await client.block_number()
        print(f"{args.chain}: registry {len(reg.pools)} pools ({len(reg.meta.get('rejected', []))} rejected), head {head}")
        for n in range(head - args.blocks, head):
            t0 = time.time()
            res = await scan_block(client, reg, n, settings)
            rec = measure.block_record(args.chain, res, int(t0))
            store.append_blocks([rec])
            store.append(res.opportunities)
            store.log_scan(rec["timestamp"] * 1000, {"candidates": res.candidates, "screened_in": res.screened_in,
                                                     "gross_positive": res.gross_positive,
                                                     "opportunities": len(res.opportunities)}, res.errors, res.duration_s)
            print(f"block {n}: candidates {res.candidates}, screened-in {res.screened_in}, gross-positive "
                  f"{res.gross_positive}, surviving {len(res.opportunities)}, {res.duration_s:.1f}s"
                  f"{'' if res.complete else '  INCOMPLETE'}")
            for o in res.opportunities[:10]:
                print(f"  {o.key}: net ${o.net_pnl_usd:.4f} ({o.net_bps:.1f} bps) after-tax ${o.net_after_tax_usd:.4f} "
                      f"capacity ${o.capacity_usd:.0f}")
            if not res.complete:
                incomplete += 1
                print(f"  DATA FAILURE: {len(res.errors)} read errors; this block says nothing about opportunities. "
                      f"First: {res.errors[0]}")
        client.drain_failures()
    finally:
        await client.close()
    if incomplete:
        print(f"DATA FAILURE: {incomplete} of {args.blocks} blocks incomplete. Results were stored and flagged; "
              "consider a keyed RPC in .env (ARBITRUM_RPC / BASE_RPC).")
        return 2
    return 0


async def cmd_measure(settings: Settings, args) -> int:
    cfg = settings.defi
    assert cfg is not None
    client, reg = await _setup(settings, args.chain, args.refresh)
    sim_client = client
    if args.sim_rpc:
        sim_client = ChainClient(args.chain, int(reg.meta["chain_id"]), args.sim_rpc,
                                 RateLimiter(cfg.chains[args.chain].max_rps, cfg.chains[args.chain].burst))
    store = DefiStore(cfg.storage_dir, args.chain)
    try:
        summary = await measure.run_measurement(args.chain, client, sim_client, reg, settings, store, args.hours,
                                                on_sample=lambda r: log.info("%s block %s complete=%s surviving=%d",
                                                                             args.chain, r["block"], r["complete"],
                                                                             len(r["surviving_keys"])))
    finally:
        await client.close()
        if sim_client is not client:
            await sim_client.close()
    print(f"{args.chain}: {summary.samples} samples ({summary.incomplete} incomplete), "
          f"{summary.episodes_started} episodes, {summary.replays} replays, rpc errors {dict(summary.rpc_errors)}")
    if summary.stopped_reason:
        print("STOPPED:", summary.stopped_reason)
        return 2
    return 0


def cmd_report(settings: Settings, args) -> int:
    cfg = settings.defi
    assert cfg is not None
    chains = args.chain or list(cfg.chains)
    for c in chains:
        _check(settings, c)
    path = measure.write_defi_report(settings, chains, date.fromisoformat(args.from_day), date.fromisoformat(args.to_day))
    print(path.read_text(encoding="utf-8"))
    print(f"\nSaved to {path}")
    return 0


def run(settings: Settings, args) -> int:
    if settings.defi is None or not settings.defi.enabled:
        raise SystemExit("defi is not configured or not enabled in config/settings.yaml")
    if args.defi_cmd == "report":
        return cmd_report(settings, args)
    _check(settings, args.chain)
    fn = {"probe": cmd_probe, "scan": cmd_scan, "measure": cmd_measure}[args.defi_cmd]
    try:
        return asyncio.run(fn(settings, args))
    except RpcError as e:
        print(f"DATA FAILURE: {type(e).__name__}: {e.message[:300]}\nNo results were produced; this is not 'no opportunity'.")
        return 2
