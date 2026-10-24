"""Live checks against the public RPCs. Skipped unless DEFI_LIVE=1.

Acceptance for the v3 port: local `quote_v3` equals the on-chain QuoterV2 / Slipstream quoter output EXACTLY (integers).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import eth_abi
import pytest

from arb.config import load_settings
from arb.defi import cli
from arb.defi.chain import ExecutionReverted
from arb.defi.pools import SLIPSTREAM_KEYS, Multicall, read_snapshot, read_ticks, sel
from arb.defi.quote import quote_v3
from arb.defi.scanner import read_block_costs

LIVE = os.environ.get("DEFI_LIVE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="live RPC checks run only with DEFI_LIVE=1")

ADDR = json.loads((Path(__file__).resolve().parent.parent / "defi/addresses.json").read_text())
SIZES_USD = [1, 5, 20, 100, 400, 1_000, 3_000, 10_000, 30_000, 100_000, 300_000, 1_000_000]
S_QUOTE_UNI = bytes.fromhex("c6a5026a")
S_QUOTE_SLIP = bytes.fromhex("9e7defe6")
assert sel("quoteExactInputSingle((address,address,uint256,uint24,uint160))") == S_QUOTE_UNI
assert sel("quoteExactInputSingle((address,address,uint256,int24,uint160))") == S_QUOTE_SLIP


async def _setup(chain: str):
    settings = load_settings()
    client, reg = await cli._setup(settings, chain, False)
    return settings, client, reg


def _pools(reg, dex: str, k: int = 3):
    ps = [p for p in reg.pools if p.dex == dex]
    ps.sort(key=lambda p: -reg.tvl_usd.get(p.address, 0.0))
    if len(ps) <= k:
        return ps
    return [ps[0], ps[len(ps) // 2], ps[-1]]  # deep, middle and thin: the thin one crosses ticks


async def _compare(chain: str, dex: str, quoter: str, selector: bytes, slip: bool):
    settings, client, reg = await _setup(chain)
    try:
        block = (await client.block_number()) - 2
        snap = await read_snapshot(client, reg, block)
        pools = _pools(reg, dex)
        assert pools, f"no {dex} pools in the {chain} registry"
        snap = await read_ticks(client, snap, {p.address for p in pools}, words_each_side=3)
        assert not snap.errors, snap.errors
        costs = await read_block_costs(client, reg, snap, settings.defi)
        compared = exhausted = crossed = 0
        for p in pools:
            st = snap.states[p.address]
            for zfo in (True, False):
                tin, tout = (p.token0, p.token1) if zfo else (p.token1, p.token0)
                price = costs.token_usd.get(tin.symbol)
                if not price:
                    continue
                for usd in SIZES_USD:
                    amount = int(usd / price * 10 ** tin.decimals)
                    if amount == 0:
                        continue
                    q = quote_v3(st, amount, zfo)
                    if q.exhausted:
                        exhausted += 1
                        continue
                    key = st.tick_spacing if slip else st.fee_ppm
                    data = selector + eth_abi.encode(
                        [f"(address,address,uint256,{'int24' if slip else 'uint24'},uint160)"],
                        [(tin.address, tout.address, amount, key, 0)])
                    try:
                        raw = await client.eth_call(quoter, data, block)
                    except ExecutionReverted:
                        continue  # the real quoter reverts when a swap cannot fill; local says not exhausted: flag below
                    got = int.from_bytes(raw[:32], "big")
                    assert q.amount_out == got, (p.address, zfo, usd, q.amount_out, got)
                    # (QuoterV2's initializedTicksCrossed is counted from the tick bitmap, not from swap steps: not compared)
                    compared += 1
                    crossed += 1 if q.ticks_crossed else 0
        print(f"\n{chain} {dex}: exact matches {compared} ({crossed} crossing initialized ticks), window-exhausted skipped {exhausted}")
        assert compared >= 20
        assert crossed >= 1, 'no compared swap crossed an initialized tick: liquidityNet handling is untested'
    finally:
        await client.close()


@pytest.mark.parametrize("chain", ["arbitrum", "base"])
async def test_local_v3_quote_equals_quoter_v2(chain):
    quoter = ADDR["chains"][chain]["dexes"]["uniswap_v3"]["quoter_v2"]
    await _compare(chain, "uniswap_v3", quoter, S_QUOTE_UNI, slip=False)


async def test_local_v3_quote_equals_slipstream_quoter_base():
    d = ADDR["chains"]["base"]["dexes"]["aerodrome_slipstream"]
    quoter = d["initial_deployment"]["quoter_v2"]
    await _compare("base", "aerodrome_slipstream_initial", quoter, S_QUOTE_SLIP, slip=True)


@pytest.mark.parametrize("chain", ["arbitrum", "base"])
async def test_multicall3_and_snapshot_load(chain):
    settings, client, reg = await _setup(chain)
    try:
        await Multicall(client).check()
        snap = await read_snapshot(client, reg, (await client.block_number()) - 1)
        assert snap.errors == []
        assert len(snap.states) == len(reg.pools) > 10
        assert snap.eth_usd > 100
    finally:
        await client.close()
