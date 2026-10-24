"""pools.py: Multicall3, registry build with the lookalike defence, snapshots, tick windows, failure surfacing."""
import json

import eth_abi
import pytest

from arb.defi.chain import RpcError
from arb.defi.pools import (S_AGGREGATE3, Call, Multicall, PoolRegistry, V2State, V3State, build_registry,
                            estimate_tvl_usd, load_or_build_registry, read_snapshot, read_ticks, sel, venue_for)
from arb.defi.route import Venue
from tests.defi_fakes import (ARB, FEED, MULTICALL, USDC, WETH, FakeNode, addr, addr_pad, chain_node, defi_cfg,
                              make_client, pool_node, registry, tick_for_price, v2_spec, v2_state, v3_spec, v3_state, w)


def test_aggregate3_selector_and_venue_mapping():
    assert S_AGGREGATE3.hex() == "82ad56cb"
    assert venue_for("uniswap_v3") == venue_for("sushi_v3") == venue_for("aerodrome_slipstream_initial") == Venue.UNI_V3
    assert venue_for("pancake_v3") == Venue.PANCAKE_V3
    assert venue_for("sushi_v2") == Venue.UNI_V2
    assert venue_for("camelot_v2") == venue_for("aerodrome_v2") == Venue.SOLIDLY_V2
    with pytest.raises(KeyError):
        venue_for("camelot_algebra_v3")


async def test_multicall_roundtrip_chunking_and_allow_failure():
    node = FakeNode()
    node.set(addr(1), b"\x11\x11\x11\x11", lambda a: w(7))
    c = make_client(node)
    mc = Multicall(c, chunk=100)
    calls = [Call(addr(1), b"\x11\x11\x11\x11")] * 250 + [Call(addr(2), b"\x22\x22\x22\x22")]
    res = await mc.aggregate(calls, 500)
    assert len(res) == 251 and all(r.success and r.data == w(7) for r in res[:250]) and not res[250].success
    assert sum(1 for r in node.requests if r["method"] == "eth_call") == 3  # 251 calls / 100 per request
    assert set(node.call_blocks) == {"0x1f4"}  # block numbers go out as hex


async def test_multicall_check_requires_code():
    node = FakeNode()
    await Multicall(make_client(node)).check()
    del node.code[MULTICALL]
    with pytest.raises(RpcError):
        await Multicall(make_client(node)).check()


def test_tvl_estimate():
    e = {"pair": "WETH/USDC", "balance_WETH": 1.0, "balance_USDC": 2000.0}
    assert estimate_tvl_usd(e, 2000.0) == 4000.0
    assert estimate_tvl_usd({"pair": "ARB/USDC", "balance_ARB": 5, "balance_USDC": 100.0}, 2000.0) == 200.0
    assert estimate_tvl_usd({"pair": "ARB/WBTC", "balance_ARB": 5, "balance_WBTC": 1}, 2000.0) is None  # unpriced: kept


def test_registry_json_roundtrip_and_lookup():
    pools = [v3_spec(1), v2_spec(2), v2_spec(3, ARB, WETH, dex="camelot_v2")]
    reg = registry(pools, tokens=(WETH, USDC, ARB))
    again = PoolRegistry.from_json(json.loads(json.dumps(reg.to_json())))
    assert [p.address for p in again.pools] == [p.address for p in pools]
    assert again.pools[2].v2_formula == "camelot" and again.pools[0].fee_ppm == 500
    assert again.by_address(addr(2).upper().replace("0X", "0x")).dex == "sushi_v2"
    assert len(again.pools_for_pair("USDC", "WETH")) == 2
    assert again.pools_for_pair("ARB", "WETH")[0].address == addr(3)
    with pytest.raises(KeyError):
        again.by_address(addr(99))


# ---- snapshot -----------------------------------------------------------------------------------------------
def build_world():
    p3 = v3_spec(0x31, fee_ppm=500, spacing=10)
    p2 = v2_spec(0x21)
    pc = v2_spec(0x22, dex="camelot_v2")
    pa = v2_spec(0x23, dex="aerodrome_v2", chain="base")
    reg = registry([p3, p2, pc, pa])
    tick = tick_for_price(2e-9) // 10 * 10
    states = [v3_state(p3, tick, 10 ** 15), v2_state(p2, 10 ** 18, 2000 * 10 ** 6),
              v2_state(pc, 5 * 10 ** 17, 1000 * 10 ** 6, fee=250), v2_state(pa, 7 * 10 ** 17, 1400 * 10 ** 6, fee=40)]
    node = FakeNode()
    chain_node(node, 2345.5)
    for s in states:
        pool_node(node, s)
    return reg, states, node


async def test_snapshot_decodes_every_pool_type():
    reg, states, node = build_world()
    snap = await read_snapshot(make_client(node), reg, 900)
    assert snap.errors == [] and snap.block == 900
    assert snap.timestamp == node.ts0 + 900 and snap.base_fee_wei == node.base_fee
    assert snap.eth_usd == pytest.approx(2345.5)
    v3 = snap.states[addr(0x31)]
    assert isinstance(v3, V3State) and v3.tick == states[0].tick and v3.sqrt_price_x96 == states[0].sqrt_price_x96
    assert v3.liquidity == 10 ** 15 and v3.fee_ppm == 500 and v3.liquidity_net == {} and v3.word_lo > v3.word_hi
    sushi, camelot, aero = (snap.states[addr(n)] for n in (0x21, 0x22, 0x23))
    assert isinstance(sushi, V2State) and (sushi.reserve0, sushi.fee0, sushi.fee_den) == (10 ** 18, 3, 1000)
    assert (camelot.fee0, camelot.fee1, camelot.fee_den) == (250, 250, 100_000)
    assert (aero.fee0, aero.fee_den, aero.reserve1) == (40, 10_000, 1400 * 10 ** 6)


async def test_negative_tick_is_signed():
    reg, states, node = build_world()
    assert states[0].tick < 0
    snap = await read_snapshot(make_client(node), reg, 5)
    assert snap.states[addr(0x31)].tick == states[0].tick


async def test_pool_read_failure_is_an_error_not_silence():
    reg, states, node = build_world()
    del node.contracts[addr(0x21)]  # the sushi pair stops answering
    snap = await read_snapshot(make_client(node), reg, 5)
    assert addr(0x21) not in snap.states and len(snap.states) == 3
    assert len(snap.errors) == 1 and "sushi_v2" in snap.errors[0] and addr(0x21) in snap.errors[0]


async def test_uninitialized_v3_pool_and_missing_feed_are_errors():
    reg, states, node = build_world()
    node.set(addr(0x31), sel("slot0()"), bytes(32 * 7))
    del node.contracts[FEED]
    snap = await read_snapshot(make_client(node), reg, 5)
    assert addr(0x31) not in snap.states
    assert snap.eth_usd == 0.0
    assert any("not initialized" in e for e in snap.errors) and any(e.startswith("eth_usd") for e in snap.errors)


async def test_whole_batch_failure_marks_every_pool_in_it():
    reg, states, node = build_world()
    node.http_error = lambda p: (500, {}, {}) if p["method"] == "eth_call" else None
    c = make_client(node, max_retries=0)
    snap = await read_snapshot(c, reg, 5)
    assert snap.states == {} and len(snap.errors) == 5  # 4 pools + the feed
    assert c.failures  # and the RPC failure itself is recorded


async def test_header_failure_raises():
    reg, states, node = build_world()
    node.http_error = lambda p: (500, {}, {}) if p["method"] == "eth_getBlockByNumber" else None
    with pytest.raises(RpcError):
        await read_snapshot(make_client(node, max_retries=0), reg, 5)


# ---- ticks ------------------------------------------------------------------------------------------------------
async def test_read_ticks_loads_window_with_negative_words():
    reg, states, node = build_world()
    s3 = states[0]
    ticks = {s3.tick - 500: 4 * 10 ** 14, s3.tick + 300: -3 * 10 ** 14, s3.tick - 100000: 123}
    full = v3_state(s3.pool, s3.tick, s3.liquidity, ticks)
    pool_node(node, full)  # the fake node now serves bitmap/ticks for these ticks
    c = make_client(node)
    snap = await read_snapshot(c, reg, 50)
    out = await read_ticks(c, snap, {addr(0x31)}, words_each_side=1)
    st = out.states[addr(0x31)]
    w0 = (s3.tick // 10) >> 8
    assert (st.word_lo, st.word_hi) == (w0 - 1, w0 + 1)
    assert st.liquidity_net == {s3.tick - 500: 4 * 10 ** 14, s3.tick + 300: -3 * 10 ** 14}  # far tick is outside the window
    assert snap.states[addr(0x31)].liquidity_net == {}  # input snapshot untouched (frozen replace)


async def test_read_ticks_failure_removes_pool_and_reports():
    reg, states, node = build_world()
    c = make_client(node)
    snap = await read_snapshot(c, reg, 50)
    del node.contracts[addr(0x31)][sel("tickBitmap(int16)")]
    out = await read_ticks(c, snap, {addr(0x31)})
    assert addr(0x31) not in out.states and any("tick read failed" in e for e in out.errors)
    assert addr(0x21) in out.states  # untouched pools stay


async def test_read_ticks_ignores_v2_and_unrequested_pools():
    reg, states, node = build_world()
    c = make_client(node)
    snap = await read_snapshot(c, reg, 50)
    n = len(node.requests)
    assert await read_ticks(c, snap, {addr(0x21)}) is snap
    assert len(node.requests) == n


# ---- build_registry -------------------------------------------------------------------------------------------------
def addresses_fixture():
    t = lambda a, d: {"address": a, "decimals_onchain": d}  # noqa: E731
    pool = lambda dex, n, pair, **kw: {"dex": dex, "pair": pair, "pool": addr(n), **kw}  # noqa: E731
    return {"chains": {"arbitrum": {
        "chain_id": 42161,
        "aave_v3": {"pool": "0x794a61358D6845594F94dc1DB02A252b5b4814aD", "flashloan_premium_total_bps": 5},
        "eth_usd_feed_chainlink": {"address": FEED, "decimals": 8, "price_usd": 2000.0},
        "tokens": {"WETH": t(WETH.address, 18), "USDC": t(USDC.address, 6), "ARB": t(ARB.address, 18)},
        "dexes": {"uniswap_v3": {"factory": addr(0xF1)}, "sushi_v2": {"factory": addr(0xF2)},
                  "camelot_v2": {"factory": addr(0xF3)}},
        "pools": [
            pool("uniswap_v3", 0x31, "WETH/USDC", fee=500, liquidity_raw=5, balance_WETH=100.0, balance_USDC=200000.0),
            pool("uniswap_v3", 0x32, "WETH/USDC", fee=500, liquidity_raw=5, balance_WETH=100.0, balance_USDC=200000.0),  # lookalike
            pool("uniswap_v3", 0x33, "WETH/USDC", fee=100, liquidity_raw=0, balance_WETH=1.0, balance_USDC=2000.0),  # no liquidity
            pool("uniswap_v3", 0x34, "WETH/USDC", fee=100, liquidity_raw=5, balance_WETH=0.001, balance_USDC=2.0),  # too small
            pool("sushi_v2", 0x21, "WETH/USDC", reserve_token0_raw=1, reserve_token1_raw=1, balance_WETH=10.0, balance_USDC=20000.0),
            pool("camelot_v2", 0x22, "ARB/WETH", reserve_token0_raw=1, reserve_token1_raw=1, balance_ARB=1.0, balance_WETH=10.0),
            pool("camelot_v2", 0x23, "ARB/WETH", reserve_token0_raw=1, reserve_token1_raw=1, balance_ARB=1.0, balance_WETH=10.0),  # stable
            pool("camelot_algebra_v3", 0x24, "WETH/USDC", liquidity_raw=5),  # venue not configured
            pool("uniswap_v3", 0x35, "WETH/DAI", fee=500, liquidity_raw=5),  # token not configured
        ]}}}


async def test_build_registry_filters_factory_checks_and_caches(tmp_path):
    node = FakeNode()
    for n in (0x31, 0x32, 0x34):
        node.set(addr(n), sel("token0()"), addr_pad(WETH.address))
        node.set(addr(n), sel("token1()"), addr_pad(USDC.address))
        node.set(addr(n), sel("tickSpacing()"), w(10))
    for n in (0x21,):
        node.set(addr(n), sel("token0()"), addr_pad(WETH.address))
        node.set(addr(n), sel("token1()"), addr_pad(USDC.address))
    for n, stable in ((0x22, 0), (0x23, 1)):
        node.set(addr(n), sel("token0()"), addr_pad(WETH.address))
        node.set(addr(n), sel("token1()"), addr_pad(ARB.address))
        node.set(addr(n), sel("stableSwap()"), w(stable))
    node.set(addr(0xF1), sel("getPool(address,address,uint24)"), lambda a: addr_pad(addr(0x31)))  # 0x32 is a lookalike
    node.set(addr(0xF2), sel("getPair(address,address)"), lambda a: addr_pad(addr(0x21)))
    node.set(addr(0xF3), sel("getPair(address,address)"), lambda a: addr_pad(addr(0x22)))
    cfg = defi_cfg(min_pool_tvl_usd=1000.0)
    cfg.chains["arbitrum"].venues.append("camelot_v2")
    path = tmp_path / "registry.json"
    reg = await build_registry(make_client(node), "arbitrum", addresses_fixture(), cfg, path)
    assert [p.address for p in reg.pools] == [addr(0x31), addr(0x21), addr(0x22)]
    reasons = {r["pool"]: r["reason"] for r in reg.meta["rejected"]}
    assert "factory lookup" in reasons[addr(0x32)] and "stable" in reasons[addr(0x23)]
    p3 = reg.by_address(addr(0x31))
    assert p3.fee_ppm == 500 and p3.tick_spacing == 10 and p3.venue == Venue.UNI_V3
    assert reg.by_address(addr(0x21)).v2_fee_bps == 30 and reg.by_address(addr(0x22)).v2_formula == "camelot"
    assert reg.by_address(addr(0x22)).token1.symbol == "ARB"
    assert reg.meta["aave_pool"] == "0x794a61358d6845594f94dc1db02a252b5b4814ad" and reg.tvl_usd[addr(0x31)] == 400000.0
    assert path.exists()
    # second call reads the cache and makes no RPC request
    n = len(node.requests)
    again = await load_or_build_registry(make_client(node), "arbitrum", addresses_fixture(), cfg, path)
    assert len(node.requests) == n and len(again.pools) == 3
