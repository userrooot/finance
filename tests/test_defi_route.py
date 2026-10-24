"""Golden vectors (design 5.9) shared with the Solidity side, and validate_route parity with FlashArb._validateRoute."""
import pytest
from eth_utils import keccak

from arb.defi.route import (EXECUTE_ARB_SELECTOR, Hop, Route, RouteError, Venue, encode_execute_arb,
                            encode_flash_params, route_hash, validate_route)
from tests.defi_fakes import ARB, USDC, WETH, addr, registry, v2_spec, v3_spec

W = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
U = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
A = "0x912CE59144191C1204E64559FE8253a0e49E6548"

V1_CALLDATA = "0xe77fcf52000000000000000000000000000000000000000000000000000000000000002000000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab10000000000000000000000000000000000000000000000000de0b6b3a764000000000000000000000000000000000000000000000000000000005af3107a4000000000000000000000000000000000000000000000000000000000006add7ba000000000000000000000000000000000000000000000000000000000000000a00000000000000000000000000000000000000000000000000000000000000002000000000000000000000000c6962004f452be9203591991d15f6b388e09e8d000000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab1000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e58310000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000100000000000000000000000057b85fef094e10b5eecdf350af688299e9553378000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e583100000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab100000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000000000000000000000000000000000000"
V1_CALLDATA_HASH = "0xfd08794518b3a2d01555e7e06a3c07442085aa3ea179bf9425aec650880fbe59"
V1_PARAMS_HASH = "0x1697f7ad346426d962e700b7f783c44c0dc40f51c7d1987d768a0e9087cc5e4f"
V2_CALLDATA = "0xe77fcf520000000000000000000000000000000000000000000000000000000000000020000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e5831000000000000000000000000000000000000000000000000000000009502f9000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000006add7ba000000000000000000000000000000000000000000000000000000000000000a00000000000000000000000000000000000000000000000000000000000000003000000000000000000000000b0f6ca40411360c03d41c5ffc5f179b8403cdcf8000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e5831000000000000000000000000912ce59144191c1204e64559fe8253a0e49e654800000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000a6c5c7d189fa4eb5af8ba34e63dcdd3a635d433f000000000000000000000000912ce59144191c1204e64559fe8253a0e49e654800000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab100000000000000000000000000000000000000000000000000000000000000030000000000000000000000000000000000000000000000000000000000000000000000000000000000000000d9e2a1a61b6e61b275cec326465d417e52c1b95c00000000000000000000000082af49447d8a07e3bd95bd0d56f35241523fbab1000000000000000000000000af88d065e77c8cc2239327c5edb3a432268e583100000000000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000001"
V2_CALLDATA_HASH = "0x4baf5fda4d2877fb705513ad98acebb3e984325816bb8185b442c0dda606fc9e"
V2_PARAMS_HASH = "0x07d1edac5b16433bbd716f0663544de7cb05a5699993d09470b1a282353f0a2d"


def route_v1() -> Route:
    return Route(W, 1000000000000000000, 100000000000000, 1792900000, (
        Hop("0xC6962004f452bE9203591991D15f6b388e09E8D0", W, U, Venue.UNI_V3, True),
        Hop("0x57b85FEf094e10b5eeCDF350Af688299E9553378", U, W, Venue.UNI_V2, False)))


def route_v2() -> Route:
    return Route(U, 2500000000, 0, 1792900000, (
        Hop("0xb0f6cA40411360c03d41C5fFc5F179b8403CdcF8", U, A, Venue.UNI_V3, False),
        Hop("0xa6c5C7D189fA4eB5Af8ba34E63dCDD3a635D433f", A, W, Venue.SOLIDLY_V2, False),
        Hop("0xd9e2a1a61B6E61b275cEc326465d417e52C1b95c", W, U, Venue.PANCAKE_V3, True)))


def test_selector():
    assert EXECUTE_ARB_SELECTOR.hex() == "e77fcf52"
    assert keccak(text="executeArb((address,uint256,uint256,uint64,(address,address,address,uint8,bool)[]))")[:4].hex() == "e77fcf52"


def test_golden_v1():
    cd = encode_execute_arb(route_v1())
    assert len(cd) == 548
    assert "0x" + cd.hex() == V1_CALLDATA
    assert "0x" + keccak(cd).hex() == V1_CALLDATA_HASH
    assert len(encode_flash_params(0, route_v1())) == 576
    assert route_hash(0, route_v1()) == V1_PARAMS_HASH


def test_golden_v2():
    cd = encode_execute_arb(route_v2())
    assert len(cd) == 708
    assert "0x" + cd.hex() == V2_CALLDATA
    assert "0x" + keccak(cd).hex() == V2_CALLDATA_HASH
    assert len(encode_flash_params(0, route_v2())) == 736
    assert route_hash(0, route_v2()) == V2_PARAMS_HASH


def test_addresses_are_lowercased():
    r = route_v1()
    assert r.loan_token == r.loan_token.lower() and all(h.pool == h.pool.lower() for h in r.hops)


# ---- validate_route: same error names as the contract (U-05..U-08) --------------------------------------------
P_V3 = addr(0x11)
P_V2 = addr(0x22)
P_V2B = addr(0x33)
P_ARB = addr(0x44)
NOW = 1_800_000_000


@pytest.fixture
def reg():
    pools = [v3_spec(0x11, WETH, USDC), v2_spec(0x22, WETH, USDC), v2_spec(0x33, WETH, USDC, dex="camelot_v2"),
             v2_spec(0x44, ARB, WETH)]
    return registry(pools, tokens=(WETH, USDC, ARB))


def caps(extra=None):
    c = {WETH.address: 10**20, USDC.address: 10**12}
    c.update(extra or {})
    return c


def good(**over) -> Route:
    base = dict(loan_token=WETH.address, loan_amount=10**18, min_profit=0, deadline=NOW + 100, hops=(
        Hop(P_V3, WETH.address, USDC.address, Venue.UNI_V3, True),
        Hop(P_V2, USDC.address, WETH.address, Venue.UNI_V2, False)))
    base.update(over)
    return Route(**base)


def expect(code, route, reg, c=None, hop=None, now=NOW):
    with pytest.raises(RouteError) as e:
        validate_route(route, reg, c if c is not None else caps(), now)
    assert e.value.code == code
    if hop is not None:
        assert e.value.hop == hop


def test_valid_route_passes(reg):
    validate_route(good(), reg, caps(), NOW)


def test_hop_count(reg):
    h = good().hops
    expect("BadHopCount", good(hops=h[:1]), reg)
    expect("BadHopCount", good(hops=h * 3), reg)


def test_amount_checks(reg):
    expect("ZeroAmount", good(loan_amount=0), reg)
    expect("LoanTooLarge", good(loan_amount=10**20 + 1), reg)
    expect("LoanTooLarge", good(), reg, c={WETH.address: 0, USDC.address: 1})
    expect("AmountTooLarge", good(loan_amount=2**255), reg, c={WETH.address: 2**256 - 1, USDC.address: 1})


def test_deadline(reg):
    expect("DeadlineExpired", good(deadline=NOW - 1), reg)
    validate_route(good(deadline=NOW), reg, caps(), NOW)  # inclusive


def test_route_not_closed(reg):
    h = good().hops
    expect("RouteNotClosed", good(hops=(h[0], Hop(P_V2, USDC.address, ARB.address, Venue.UNI_V2, False))), reg)
    expect("RouteNotClosed", good(loan_token=USDC.address, loan_amount=10**6), reg)


def test_pool_not_whitelisted(reg):
    h = good().hops
    expect("PoolNotWhitelisted", good(hops=(Hop(addr(0x99), WETH.address, USDC.address, Venue.UNI_V3, True), h[1])), reg, hop=0)


def test_venue_mismatch_and_enum_panic(reg):
    h = good().hops
    expect("VenueMismatch", good(hops=(Hop(P_V3, WETH.address, USDC.address, Venue.PANCAKE_V3, True), h[1])), reg, hop=0)
    expect("Panic0x21", good(hops=(Hop(P_V3, WETH.address, USDC.address, 7, True), h[1])), reg)


def test_pool_token_mismatch(reg):
    h = good().hops
    bad = Hop(P_ARB, USDC.address, WETH.address, Venue.UNI_V2, False)  # P_ARB holds ARB/WETH, not USDC
    expect("PoolTokenMismatch", good(hops=(h[0], bad)), reg, hop=1)


def test_direction_mismatch(reg):
    h = good().hops
    expect("DirectionMismatch", good(hops=(Hop(P_V3, WETH.address, USDC.address, Venue.UNI_V3, False), h[1])), reg, hop=0)


def test_token_not_allowed(reg):
    expect("TokenNotAllowed", good(), reg, c={WETH.address: 10**20}, hop=0)


def test_hop_chain_broken(reg):
    h = good().hops
    # hop1 starts from WETH although hop0 ends in USDC; pool 0x44 is ARB/WETH so it is a valid pool, wrong chain
    broken = Hop(P_ARB, WETH.address, ARB.address, Venue.UNI_V2, False)
    expect("HopChainBroken", good(hops=(h[0], broken, Hop(P_V2, USDC.address, WETH.address, Venue.UNI_V2, False))), reg,
           c=caps({ARB.address: 0}), hop=1)


def test_duplicate_pool(reg):
    r = Route(WETH.address, 10**18, 0, NOW + 5, (
        Hop(P_V2, WETH.address, USDC.address, Venue.UNI_V2, True), Hop(P_V2, USDC.address, WETH.address, Venue.UNI_V2, False)))
    expect("DuplicatePool", r, reg, hop=1)
