"""simulate.py: calldata, harness initcode layout, revert decoding, the eth_call path against a fake RPC."""
import json
import os
from dataclasses import replace

import eth_abi
import pytest
from eth_utils import keccak

from arb.defi import simulate as sim
from arb.defi.chain import ExecutionReverted
from arb.defi.pools import Snapshot
from arb.defi.route import Hop, Route, Venue
from arb.defi.scanner import BlockCosts
from arb.defi.simulate import (BUILTIN_ERRORS, HARNESS_ARGS_ABI, HARNESS_RESULT_ABI, SimRequest, SimulatorUnavailable,
                               build_execute_arb_calldata, build_harness_initcode, calldata_gas, decode_revert,
                               simulate)
from tests.defi_fakes import (AAVE, USDC, WETH, FakeNode, FakeTransport, addr, make_client, ok, registry, rpc_err,
                              v2_spec, v3_spec)
from tests.test_defi_route import V1_CALLDATA, V1_CALLDATA_HASH, route_v1

BYTECODE = "0x600160005260206000f3"


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    p = tmp_path / "Harness.json"
    p.write_text(json.dumps({"bytecode": BYTECODE}))
    monkeypatch.setattr(sim, "HARNESS_ARTIFACT", p)
    return p


def request(block=100):
    v3 = v3_spec(0xC6, WETH, USDC)  # its address differs from golden V1, so build a matching small route instead
    v2 = v2_spec(0x57, WETH, USDC)
    route = Route(WETH.address, 10 ** 18, 0, 1_792_900_000, (
        Hop(v3.address, WETH.address, USDC.address, Venue.UNI_V3, True),
        Hop(v2.address, USDC.address, WETH.address, Venue.UNI_V2, False)))
    return SimRequest("arbitrum", block, route, AAVE, (v3, v2), {WETH.address: 10 ** 18, USDC.address: 0})


def costs(l1=0, tip=0, base=20_000_000):
    return BlockCosts("arbitrum", 100, base, tip, {2: l1, 3: l1}, 2000.0, {"WETH": 2000.0, "USDC": 1.0})


def test_calldata_matches_golden():
    cd = build_execute_arb_calldata(route_v1())
    assert "0x" + cd.hex() == V1_CALLDATA and "0x" + keccak(cd).hex() == V1_CALLDATA_HASH


def test_calldata_gas():
    assert calldata_gas(b"\x00\x00\x01\xff") == 4 + 4 + 16 + 16
    cd = build_execute_arb_calldata(route_v1())
    assert calldata_gas(cd) == sum(4 if b == 0 else 16 for b in cd)


def test_initcode_layout(artifact):
    req = request()
    cd = build_execute_arb_calldata(req.route)
    init = build_harness_initcode(req, cd)
    code = bytes.fromhex(BYTECODE[2:])
    assert init.startswith(code)
    aave, loan, tokens, pools, data = eth_abi.decode(HARNESS_ARGS_ABI, init[len(code):])
    assert aave == AAVE and loan == WETH.address and data == cd
    assert sorted(tokens) == sorted([(WETH.address, 10 ** 18), (USDC.address, 0)])
    assert pools == ((req.pools[0].address, 0, 0), (req.pools[1].address, 2, 30))  # feeBps only for UNI_V2
    assert init == build_harness_initcode(req, cd, artifact)


def test_missing_or_empty_artifact(tmp_path):
    with pytest.raises(SimulatorUnavailable, match="not found"):
        build_harness_initcode(request(), b"", tmp_path / "nope.json")
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"bytecode": "0x"}))
    with pytest.raises(SimulatorUnavailable):
        build_harness_initcode(request(), b"", p)


# ---- decode_revert ------------------------------------------------------------------------------------------------
SAMPLES = {"address": addr(0xAB), "uint256": 12345, "int256": -7}


def test_decode_every_builtin_error(tmp_path):
    for sig in BUILTIN_ERRORS:
        name, rest = sig.split("(", 1)
        parts = [x.strip().split(" ") for x in rest.rstrip(")").split(",") if x.strip()]
        types = [t for t, _ in parts]
        vals = [SAMPLES[t] for t in types]
        data = keccak(text=f"{name}({','.join(types)})")[:4] + eth_abi.encode(types, vals)
        out = decode_revert(data, tmp_path / "missing.json")
        assert out.startswith(name + "(") and out.endswith(")"), out
        for (t, n), v in zip(parts, vals):
            assert f"{n}={v}" in out, (out, n, v)


def test_decode_insufficient_profit_format():
    data = keccak(text="InsufficientProfit(uint256,uint256)")[:4] + eth_abi.encode(["uint256", "uint256"], [5, 9])
    assert decode_revert(data) == "InsufficientProfit(balance=5, required=9)"


def test_decode_error_panic_unknown_and_short():
    err = bytes.fromhex("08c379a0") + eth_abi.encode(["string"], ["nope"])
    assert decode_revert(err) == "Error(nope)"
    assert decode_revert(bytes.fromhex("4e487b71") + eth_abi.encode(["uint256"], [0x11])) == "Panic(0x11)"
    assert decode_revert(bytes.fromhex("4e487b71") + eth_abi.encode(["uint256"], [0x21])) == "Panic(0x21)"
    assert decode_revert(bytes.fromhex("deadbeef00")) == "0xdeadbeef00"
    assert decode_revert(b"") == "0x" and decode_revert(b"\x01\x02") == "0x0102"
    # truncated arguments fall back to hex instead of raising
    bad = keccak(text="InsufficientProfit(uint256,uint256)")[:4] + b"\x01"
    assert decode_revert(bad).startswith("0x")


def test_decode_uses_artifact_abi_for_extra_errors(tmp_path):
    p = tmp_path / "FlashArb.json"
    p.write_text(json.dumps({"abi": [{"type": "error", "name": "BrandNew", "inputs": [{"name": "who", "type": "address"}]},
                                     {"type": "function", "name": "x", "inputs": []}]}))
    data = keccak(text="BrandNew(address)")[:4] + eth_abi.encode(["address"], [addr(5)])
    assert decode_revert(data, p) == f"BrandNew(who={addr(5)})"
    assert decode_revert(data, tmp_path / "other.json").startswith("0x")  # not known without the artifact


@pytest.mark.skipif(not sim.FLASHARB_ARTIFACT.exists(), reason="contracts/artifacts/src/FlashArb.sol/FlashArb.json not built yet")
def test_builtin_error_table_matches_the_compiled_contract():
    abi = json.loads(sim.FLASHARB_ARTIFACT.read_text())["abi"]
    compiled = {keccak(text=f"{e['name']}({','.join(i['type'] for i in e['inputs'])})")[:4]: e["name"]
                for e in abi if e["type"] == "error"}
    spec = {"NotAContract", "RenounceDisabled", "InvalidPoolConfig", "NotAavePool", "BadInitiator", "NoActiveRoute",
            "RouteHashMismatch", "LoanMismatch", "BadHopCount", "RouteNotClosed", "HopChainBroken", "PoolNotWhitelisted",
            "VenueMismatch", "PoolTokenMismatch", "DirectionMismatch", "DuplicatePool", "TokenNotAllowed", "LoanTooLarge",
            "ZeroAmount", "AmountTooLarge", "DeadlineExpired", "UnexpectedCallback", "BadCallbackAmount", "PartialFill",
            "ZeroOutput", "BalanceDeltaMismatch", "InsufficientProfit"}
    assert spec <= set(compiled.values())
    for sig in BUILTIN_ERRORS:
        name, rest = sig.split("(", 1)
        types = [x.strip().split(" ")[0] for x in rest.rstrip(")").split(",") if x.strip()]
        sel = keccak(text=f"{name}({','.join(types)})")[:4]
        if name in spec:
            assert sel in compiled, f"selector of {sig} not in the compiled FlashArb ABI"


# ---- simulate() against a fake RPC ------------------------------------------------------------------------------------
def harness_reply(ok_, profit, gas, rev=b""):
    return eth_abi.encode(HARNESS_RESULT_ABI, [ok_, profit, gas, rev])


async def test_simulate_success(artifact):
    node = FakeNode()
    node.create = lambda data: harness_reply(True, 3 * 10 ** 15, 250_000)
    req = request()
    res = await simulate(make_client(node), req, costs(l1=10 ** 11))
    assert res.success and res.revert_reason is None and res.profit_raw == 3 * 10 ** 15
    cd = build_execute_arb_calldata(req.route)
    assert res.gas_used_inner == 250_000
    assert res.gas_estimate_tx == 250_000 + 21_000 + calldata_gas(cd)
    assert res.gas_cost_usd == pytest.approx((res.gas_estimate_tx * 20_000_000 + 10 ** 11) / 1e18 * 2000)
    assert res.profit_usd == pytest.approx(3 * 10 ** 15 / 1e18 * 2000)
    assert res.net_usd == pytest.approx(res.profit_usd - res.gas_cost_usd)
    assert res.calldata_hash == "0x" + keccak(cd).hex() and res.block == 100
    call = node.creation_calls[0]
    assert call["from"] == "0x000000000000000000000000000000000000dead" and call["gas"] == hex(25_000_000)
    assert "to" not in call and call["data"].startswith("0x600160005260206000f3")
    assert node.call_blocks[-1] == hex(100)


async def test_simulate_tip_is_priced(artifact):
    node = FakeNode()
    node.create = lambda data: harness_reply(True, 0, 100_000)
    a = await simulate(make_client(node), request(), costs(tip=0))
    b = await simulate(make_client(node), request(), costs(tip=10_000_000))
    assert b.gas_cost_usd > a.gas_cost_usd


async def test_simulate_route_revert_is_a_result_not_an_exception(artifact):
    rev = keccak(text="InsufficientProfit(uint256,uint256)")[:4] + eth_abi.encode(["uint256", "uint256"], [90, 100])
    node = FakeNode()
    node.create = lambda data: harness_reply(False, 0, 180_000, rev)
    res = await simulate(make_client(node), request(), costs())
    assert not res.success and res.revert_reason == "InsufficientProfit(balance=90, required=100)"
    assert res.profit_raw == 0 and res.gas_used_inner == 180_000 and res.net_usd < 0


async def test_simulate_refused_creation_call_is_simulator_unavailable(artifact):
    node = FakeNode()  # no create handler: "contract creation calls are not supported"
    with pytest.raises(SimulatorUnavailable, match="refuses"):
        await simulate(make_client(node), request(), costs())
    c = make_client(FakeTransport([rpc_err(-32000, "gas required exceeds allowance (550000000)")]))
    with pytest.raises(SimulatorUnavailable):
        await simulate(c, request(), costs())


async def test_simulate_constructor_revert_is_simulator_unavailable(artifact):
    c = make_client(FakeTransport([rpc_err(3, "execution reverted", "0x08c379a0" + eth_abi.encode(["string"], ["boom"]).hex())]))
    with pytest.raises(SimulatorUnavailable, match="boom"):
        await simulate(c, request(), costs())


async def test_simulate_data_failures_propagate_untouched(artifact):
    from arb.defi.chain import RateLimited, StateUnavailable
    c = make_client(FakeTransport([rpc_err(-32000, "missing trie node")]))
    with pytest.raises(StateUnavailable):
        await simulate(c, request(), costs())
    c = make_client(FakeTransport(handler=lambda p: (429, {}, {})), max_retries=0)
    with pytest.raises(RateLimited):
        await simulate(c, request(), costs())


async def test_simulate_garbage_result(artifact):
    node = FakeNode()
    node.create = lambda data: b"\x01\x02"
    with pytest.raises(SimulatorUnavailable, match="undecodable"):
        await simulate(make_client(node), request(), costs())


async def test_simulate_without_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(sim, "HARNESS_ARTIFACT", tmp_path / "nope.json")
    with pytest.raises(SimulatorUnavailable):
        await simulate(make_client(FakeNode()), request(), costs())


_HARNESS = sim.HARNESS_ARTIFACT


@pytest.mark.skipif(os.environ.get("DEFI_LIVE") != "1" or not _HARNESS.exists(),
                    reason="live harness check needs DEFI_LIVE=1 and contracts/artifacts/src/sim/FlashArbSimHarness.sol/"
                           "FlashArbSimHarness.json (built by the contract task); until then simulate() is only "
                           "tested against a fake RPC")
@pytest.mark.parametrize("chain", ["arbitrum", "base"])
async def test_live_harness_rejects_an_unprofitable_route(chain):
    from arb.config import load_settings
    from arb.defi import cli, measure

    settings = load_settings()
    client, reg = await cli._setup(settings, chain, False)
    try:
        out = await measure.probe(client, reg, settings)
        assert out["harness"].startswith("ok: success=False reason=InsufficientProfit"), out["harness"]
    finally:
        await client.close()
