"""scanner.py: candidates, costs, evaluation and scan_block end to end on a fake chain."""
import pytest

from arb.config import Settings, load_settings
from arb.costs import CostModel
from arb.defi.pools import read_snapshot
from arb.defi.quote import aave_premium, quote_route
from arb.defi.route import Hop, Venue
from arb.defi.scanner import (ARBITRUM_NODE_INTERFACE, BASE_GAS_ORACLE, S_GAS_ESTIMATE_L1, S_L1_FEE_UPPER_BOUND,
                              GasModel, _min_profit_raw, calldata_len, compute_token_usd, enumerate_candidates,
                              evaluate, read_block_costs, scan_block, short)
from tests.defi_fakes import (ARB, USDC, WETH, FakeNode, addr, chain_node, defi_cfg, make_client, pool_node, registry,
                              tick_for_price, v2_spec, v2_state, v3_spec, v3_state, w)

ETH_USD = 2000.0


def settings(**defi_over) -> Settings:
    base = load_settings()
    return base.model_copy(update={"defi": defi_cfg(**defi_over)})


def world(gap: bool = True, v3_leg: bool = False):
    """WETH/USDC on sushi_v2 (2000) and camelot_v2 (2100 if gap else 2000); optionally a v3 pool at 2000."""
    a = v2_spec(0xA1)
    b = v2_spec(0xB1, dex="camelot_v2")
    pools = [a, b]
    states = [v2_state(a, 1000 * 10 ** 18, 2_000_000 * 10 ** 6),
              v2_state(b, 1000 * 10 ** 18, (2_100_000 if gap else 2_000_000) * 10 ** 6, fee=300)]
    if v3_leg:
        c = v3_spec(0xC1, fee_ppm=500, spacing=10)
        tick = tick_for_price(2000e6 / 1e18) // 10 * 10
        pools.append(c)
        states.append(v3_state(c, tick, 5 * 10 ** 15, {tick - 400: 10 ** 15, tick + 400: -10 ** 15}))
    reg = registry(pools, tvl={p.address: 4e6 for p in pools})
    node = FakeNode(base_fee=20_000_000)
    chain_node(node, ETH_USD)
    for s in states:
        pool_node(node, s)
    node.set(ARBITRUM_NODE_INTERFACE, S_GAS_ESTIMATE_L1, lambda a: w(10_000) + w(0) + w(0))
    return reg, states, node


def test_calldata_len_and_gas_model():
    assert [calldata_len(n) for n in (2, 3, 4)] == [548, 708, 868]
    gm = GasModel(120_000, 120_000, 75_000)
    h = lambda v: Hop(addr(1), addr(2), addr(3), v, True)  # noqa: E731
    assert gm.units([h(Venue.UNI_V3), h(Venue.UNI_V2)]) == 120_000 + 120_000 + 75_000
    assert gm.units([h(Venue.PANCAKE_V3), h(Venue.SOLIDLY_V2), h(Venue.UNI_V3)]) == 120_000 + 2 * 120_000 + 75_000


def test_enumerate_two_pool_candidates_and_keys():
    reg, _, _ = world(v3_leg=True)
    cands = [c for c in enumerate_candidates(reg, ["WETH"], max_hops=2) if c.kind == "two_pool"]
    assert len(cands) == 3 * 2  # ordered pairs of 3 pools
    c = next(c for c in cands if c.pools[0].dex == "sushi_v2" and c.pools[1].dex == "camelot_v2")
    assert c.key == f"arbitrum WETH sushi_v2:{short(addr(0xA1))}>camelot_v2:{short(addr(0xB1))}"
    assert c.key == "arbitrum WETH sushi_v2:0x0000..00a1>camelot_v2:0x0000..00b1"
    assert [h.token_in for h in c.hops] == [WETH.address, USDC.address]
    assert [h.zero_for_one for h in c.hops] == [True, False]
    assert c.hops[0].venue == Venue.UNI_V2 and c.hops[1].venue == Venue.SOLIDLY_V2
    assert len({c.key for c in cands}) == len(cands)
    # a pair with a single pool produces nothing
    assert enumerate_candidates(registry([v2_spec(1)]), ["WETH", "USDC"]) == []
    # USDC as loan token works too
    assert any(c.loan_symbol == "USDC" for c in enumerate_candidates(reg, ["USDC"], max_hops=2))


def test_enumerate_triangular_respects_top_k_and_order():
    pools = [v2_spec(1, WETH, USDC), v2_spec(2, WETH, USDC, dex="camelot_v2"), v2_spec(3, ARB, USDC),
             v2_spec(4, WETH, ARB), v2_spec(5, WETH, ARB, dex="camelot_v2")]
    tvl = {addr(1): 5, addr(2): 9, addr(3): 1, addr(4): 3, addr(5): 7}
    reg = registry(pools, tvl, tokens=(WETH, USDC, ARB))
    tri = [c for c in enumerate_candidates(reg, ["WETH"], tri_top_k=1) if c.kind == "triangular"]
    # WETH->USDC->ARB->WETH and WETH->ARB->USDC->WETH, one pool per edge (the deepest)
    assert len(tri) == 2
    fwd = next(c for c in tri if c.hops[0].token_out == USDC.address)
    assert [p.address for p in fwd.pools] == [addr(2), addr(3), addr(5)]
    assert [h.token_in for h in fwd.hops] == [WETH.address, USDC.address, ARB.address]
    assert fwd.hops[-1].token_out == WETH.address
    assert not [c for c in enumerate_candidates(reg, ["WETH"], max_hops=2) if c.kind == "triangular"]


def test_compute_token_usd_prices_derived_token_from_deepest_pool():
    pools = [v2_spec(1, WETH, USDC), v2_spec(3, ARB, USDC), v2_spec(4, WETH, ARB)]
    reg = registry(pools, {addr(3): 10, addr(4): 100}, tokens=(WETH, USDC, ARB))
    states = {p.address: s for p, s in zip(pools, [v2_state(pools[0], 10 ** 18, 2000 * 10 ** 6),
                                                   v2_state(pools[1], 100 * 10 ** 18, 50 * 10 ** 6),   # 0.5 USDC / ARB
                                                   v2_state(pools[2], 10 ** 18, 4000 * 10 ** 18)])}  # 0.5 ... via WETH
    from arb.defi.pools import Snapshot
    snap = Snapshot("arbitrum", 1, 1, 1, ETH_USD, states, [])
    usd, notes = compute_token_usd(reg, snap)
    assert usd["USDC"] == 1.0 and usd["WETH"] == ETH_USD
    assert usd["ARB"] == pytest.approx(0.5)  # WETH pool is deeper (tvl 100): 1 WETH = 4000 ARB -> 2000/4000
    assert notes["ARB"] == f"price of ARB from pool {addr(4)}" and "priced at $1" in notes["USDC"]


async def test_costs_arbitrum_uses_node_interface_and_base_uses_oracle():
    reg, _, node = world()
    client = make_client(node)
    snap = await read_snapshot(client, reg, 900)
    costs = await read_block_costs(client, reg, snap, defi_cfg())
    assert costs.l1_fee_wei_by_hops == {2: 10_000 * node.base_fee, 3: 10_000 * node.base_fee}
    assert costs.tip_wei == 0 and costs.base_fee_wei == node.base_fee and costs.eth_usd == ETH_USD
    assert costs.token_usd == {"WETH": ETH_USD, "USDC": 1.0}
    # Base: GasPriceOracle getL1FeeUpperBound(len + 100) through Multicall3
    reg_b = registry(reg.pools, chain="base")
    sizes = []
    node.set(BASE_GAS_ORACLE, S_L1_FEE_UPPER_BOUND, lambda a: (sizes.append(int.from_bytes(a, "big")), w(7))[1])
    costs_b = await read_block_costs(client, reg_b, snap, defi_cfg())
    assert sizes == [548 + 100, 708 + 100] and costs_b.l1_fee_wei_by_hops == {2: 7, 3: 7}
    assert costs_b.tip_wei == 10_000_000  # 0.01 gwei


async def test_costs_failure_raises():
    reg, _, node = world()
    del node.contracts[ARBITRUM_NODE_INTERFACE]
    client = make_client(node)
    snap = await read_snapshot(client, reg, 900)
    with pytest.raises(Exception):
        await read_block_costs(client, reg, snap, defi_cfg())


def test_min_profit_raw_is_ceiled():
    # (5.4e12 wei gas = 0.0108 USD at 2000, + 0.05) / 2000 USD per ETH = 3.04e-5 ETH
    raw = _min_profit_raw(5_400_000_000_000, 2000.0, 0.05, 2000.0, 18)
    assert raw == 30_400_000_000_000
    assert _min_profit_raw(1, 2000.0, 0.0, 1.0, 6) == 1  # any gas rounds up to one unit


# ---- scan_block end to end -------------------------------------------------------------------------------------
async def test_known_gap_yields_one_opportunity_with_exact_cost_breakdown():
    reg, states, node = world(gap=True)
    st = settings()
    res = await scan_block(make_client(node), reg, 900, st)
    assert res.complete and res.errors == []
    assert res.block == 900 and res.timestamp_ms == (node.ts0 + 900) * 1000
    assert res.candidates == 4  # (A,B) and (B,A) for each of the two loan tokens
    assert res.gross_positive >= 1 and len(res.opportunities) >= 1
    opp = next(o for o in res.opportunities if o.details["loan_symbol"] == "WETH")
    assert opp.key == f"arbitrum WETH camelot_v2:{short(addr(0xB1))}>sushi_v2:{short(addr(0xA1))}"
    assert opp.strategy == "defi_two_pool" and opp.holding == "instant"
    d = opp.details
    assert d["chain"] == "arbitrum" and d["block"] == 900 and d["gas_units"] == 120_000 + 2 * 75_000
    # recompute the money independently from the integer details
    x, out, prem = d["amount_in_raw"], d["amount_out_raw"], d["premium_raw"]
    assert prem == aave_premium(x) and d["profit_raw"] == out - x - prem > 0
    pools = [reg.by_address(h["pool"]) for h in d["hops"]]
    hops = [Hop(h["pool"], h["token_in"], h["token_out"], Venue(h["venue"]), h["zero_for_one"]) for h in d["hops"]]
    qs = quote_route([states_by(states, p) for p in pools], hops, x)
    assert qs[-1].amount_out == out and [q.amount_out for q in qs] == [h["amount_out"] for h in d["hops"]]
    l2 = d["gas_units"] * node.base_fee / 1e18 * ETH_USD
    l1 = d["l1_fee_wei"] / 1e18 * ETH_USD
    assert d["l1_fee_wei"] == 10_000 * node.base_fee and d["tip_wei"] == 0
    fees = qs[0].fee_paid / 1e18 * ETH_USD + qs[1].fee_paid / 1e6 * 1.0
    assert opp.costs_usd == pytest.approx({"flash_premium": prem / 1e18 * ETH_USD, "swap_fees": fees,
                                          "l2_gas": l2, "l1_data": l1, "priority_tip": 0.0})
    assert opp.net_pnl_usd == pytest.approx((out - x - prem) / 1e18 * ETH_USD - l2 - l1)
    assert opp.gross_pnl_usd == pytest.approx(opp.net_pnl_usd + sum(opp.costs_usd.values()))
    assert opp.net_after_tax_usd == pytest.approx(CostModel(st).after_tax(opp.net_pnl_usd))
    assert opp.net_bps == pytest.approx(opp.net_pnl_usd / opp.notional_usd * 1e4)
    assert opp.notional_usd == pytest.approx(x / 1e18 * ETH_USD)
    assert opp.capacity_usd >= opp.notional_usd and opp.capacity_usd <= st.defi.max_loan_usd + 1e-6
    assert [l.exchange for l in opp.legs] == ["arbitrum:camelot_v2", "arbitrum:sushi_v2"]
    assert [l.symbol for l in opp.legs] == ["WETH/USDC", "USDC/WETH"] and all(l.side == "sell" for l in opp.legs)
    assert opp.legs[0].price == pytest.approx(qs[0].amount_out / 1e6 / (x / 1e18))
    assert "stablecoin priced at $1" in opp.risk_notes
    assert d["min_profit_raw"] > 0
    assert aave_premium(x) == prem
    assert res.opportunities == sorted(res.opportunities, key=lambda o: o.net_pnl_usd, reverse=True)


def states_by(states, pool):
    return next(s for s in states if s.pool.address == pool.address)


async def test_no_gap_is_rejected_by_the_marginal_screen():
    reg, states, node = world(gap=False)
    res = await scan_block(make_client(node), reg, 900, settings())
    assert res.complete and res.screened_in == 0 and res.gross_positive == 0 and res.opportunities == []
    assert res.candidates > 0


async def test_gross_positive_but_below_threshold_does_not_survive():
    reg, states, node = world(gap=True)
    res = await scan_block(make_client(node), reg, 900, settings(min_profit_usd=10 ** 9))
    assert res.gross_positive >= 1 and res.opportunities == [] and res.complete


async def test_gas_can_kill_a_small_gap():
    reg, states, node = world(gap=True)
    node.base_fee = 10 ** 15  # absurd base fee: 270k gas costs ~$540
    res = await scan_block(make_client(node), reg, 900, settings())
    assert res.gross_positive >= 1 and res.opportunities == []


async def test_v3_leg_reads_ticks_and_matches_local_quote():
    reg, states, node = world(gap=True, v3_leg=True)
    res = await scan_block(make_client(node), reg, 900, settings())
    assert res.complete, res.errors
    v3_opps = [o for o in res.opportunities if any(h["venue"] == int(Venue.UNI_V3) for h in o.details["hops"])]
    assert v3_opps
    o = v3_opps[0]
    assert o.details["sizing"] == "golden_section"
    pools = [reg.by_address(h["pool"]) for h in o.details["hops"]]
    full = [states_by(states, p) for p in pools]
    hops = [Hop(h["pool"], h["token_in"], h["token_out"], Venue(h["venue"]), h["zero_for_one"]) for h in o.details["hops"]]
    assert quote_route(full, hops, o.details["amount_in_raw"])[-1].amount_out == o.details["amount_out_raw"]


async def test_pool_read_failure_makes_the_scan_incomplete_not_empty():
    reg, states, node = world(gap=True)
    del node.contracts[addr(0xB1)]  # the pool with the gap stops answering
    res = await scan_block(make_client(node), reg, 900, settings())
    assert not res.complete and any(addr(0xB1) in e for e in res.errors)
    assert res.opportunities == []  # and this must never be read as "no gap": complete is False


async def test_total_rpc_failure_is_incomplete_with_errors():
    reg, states, node = world()
    node.http_error = lambda p: (500, {}, {})
    c = make_client(node, max_retries=0)
    res = await scan_block(c, reg, 900, settings())
    assert not res.complete and res.errors and res.candidates == 0 and res.opportunities == []
    assert c.failures


async def test_costs_failure_is_incomplete():
    reg, states, node = world()
    del node.contracts[ARBITRUM_NODE_INTERFACE]
    res = await scan_block(make_client(node), reg, 900, settings())
    assert not res.complete and any(e.startswith("costs:") for e in res.errors)


async def test_tick_read_failure_is_reported():
    reg, states, node = world(gap=True, v3_leg=True)
    from arb.defi.pools import sel
    del node.contracts[addr(0xC1)][sel("tickBitmap(int16)")]
    res = await scan_block(make_client(node), reg, 900, settings())
    assert not res.complete and any("tick read failed" in e for e in res.errors)


def test_evaluate_wrapper_returns_none_without_a_screen_pass():
    reg, states, node = world(gap=False)
    from arb.defi.pools import Snapshot
    from arb.defi.scanner import BlockCosts
    snap = Snapshot("arbitrum", 1, 1, 20_000_000, ETH_USD, {s.pool.address: s for s in states}, [])
    costs = BlockCosts("arbitrum", 1, 20_000_000, 0, {2: 0, 3: 0}, ETH_USD, {"WETH": ETH_USD, "USDC": 1.0})
    c = enumerate_candidates(reg, ["WETH"], max_hops=2)[0]
    assert evaluate(c, snap, costs, settings()) is None


# ---- review T4 regressions (each fails on the pre-review code) ------------------------------------------------------
async def test_p05_arbitrum_l1_estimate_uses_realistic_calldata_and_the_returned_base_fee():
    from arb.defi.scanner import l1_probe_route
    from arb.defi.route import encode_execute_arb
    reg, _, node = world()
    seen = []

    def gas_l1(args):
        import eth_abi
        _, _, cd = eth_abi.decode(["address", "bool", "bytes"], args)
        seen.append(cd)
        return w(1_000) + w(30_000_000) + w(0)  # (gasEstimateForL1, baseFee, l1BaseFeeEstimate)
    node.set(ARBITRUM_NODE_INTERFACE, S_GAS_ESTIMATE_L1, gas_l1)
    client = make_client(node)
    snap = await read_snapshot(client, reg, 900)
    costs = await read_block_costs(client, reg, snap, defi_cfg())
    # priced at the baseFee NodeInterface divided by, not the header's (20_000_000 here)
    assert costs.l1_fee_wei_by_hops == {2: 1_000 * 30_000_000, 3: 1_000 * 30_000_000}
    # the probed calldata has a real route's length and is mostly non-zero (an all-zero route compresses ~35% better)
    assert [len(cd) for cd in seen] == [calldata_len(2), calldata_len(3)]
    # (ABI padding keeps ~70% zeros even in a real route: 2 hops have 167 non-zero bytes; the old dummy had 11)
    assert all(sum(1 for b in cd if b) > 0.25 * len(cd) for cd in seen)
    assert len(encode_execute_arb(l1_probe_route(4))) == calldata_len(4)


async def test_p08_stale_chainlink_price_is_a_data_failure():
    from tests.defi_fakes import FEED
    from arb.defi.pools import S_LATEST_ROUND
    reg, _, node = world()
    node.set(FEED, S_LATEST_ROUND, w(1) + w(2000 * 10 ** 8) + w(0) + w(node.ts0 - 30 * 3600) + w(1))  # 30 h old
    res = await scan_block(make_client(node), reg, 900, settings())
    assert not res.complete and any("stale" in e for e in res.errors)


def _window_world():
    """v3 WETH/USDC (spacing 1, 1 bp) at 1800, a deep sushi pair at 2000: an 11% gap. One bitmap word is 256 ticks
    (~2.6%), so the first +-1-word window cannot hold the optimum."""
    from tests.defi_fakes import tick_for_price as tfp
    v3 = v3_spec(0xC2, fee_ppm=100, spacing=1)
    a = v2_spec(0xA1)
    tick = tfp(1800e6 / 1e18)
    states = [v3_state(v3, tick, 3 * 10 ** 16, {}), v2_state(a, 1000 * 10 ** 18, 2_000_000 * 10 ** 6)]
    reg = registry([v3, a], tvl={v3.address: 4e6, a.address: 4e6})
    node = FakeNode(base_fee=20_000_000)
    chain_node(node, ETH_USD)
    for s in states:
        pool_node(node, s)
    node.set(ARBITRUM_NODE_INTERFACE, S_GAS_ESTIMATE_L1, lambda a: w(10_000) + w(0) + w(0))
    return reg, states, node


async def test_p09_optimum_at_the_tick_window_edge_is_resized_with_a_wider_window():
    from arb.defi.pools import read_ticks
    from arb.defi.quote import _window_edge
    reg, states, node = _window_world()
    client = make_client(node)
    res = await scan_block(client, reg, 900, settings(max_loan_usd=5_000_000.0))
    assert res.complete, res.errors
    o = max(res.opportunities, key=lambda o: o.net_pnl_usd)
    hops = [Hop(h["pool"], h["token_in"], h["token_out"], Venue(h["venue"]), h["zero_for_one"]) for h in o.details["hops"]]
    narrow = await read_ticks(client, await read_snapshot(client, reg, 900), {states[0].pool.address})
    st = [narrow.states[h.pool] for h in hops]
    edge, capped = _window_edge(st, hops, 10 ** 30)
    assert capped
    assert o.details["amount_in_raw"] > edge  # the old code stopped at the first window's edge


# ---- tick-crossing gas (security review section 7, coordinator request) -------------------------------------------
def test_gas_model_charges_crossed_ticks_and_the_pancake_lm_hook():
    from arb.defi.quote import HopQuote
    gm = GasModel(100_000, 150_000, 80_000, tick_units=25_000, pancake_lm_units=26_000, pancake_lm_tick_units=10_000)
    h = lambda v: Hop(addr(1), addr(2), addr(3), v, True)  # noqa: E731
    q = lambda t: HopQuote(1, 1, 0, t, False)  # noqa: E731
    hops = [h(Venue.UNI_V3), h(Venue.PANCAKE_V3), h(Venue.UNI_V2)]
    assert gm.units(hops) == 100_000 + 150_000 + (150_000 + 26_000) + 80_000  # no quotes: zero ticks
    # 93 ticks on the Pancake hop (the Arbitrum 1 bps pool for 1 WETH) and 8 on the Uniswap hop
    assert gm.units(hops, [q(8), q(93), q(0)]) == (100_000 + 150_000 + 8 * 25_000 + 150_000 + 26_000 + 93 * 35_000
                                                   + 80_000)
    assert gm.units(hops, [q(8), q(93), q(0)]) > 3_500_000


def _dense_tick_world(base_fee: int):
    """v3 WETH/USDC, spacing 1, an initialized tick at every tick (a thin 1 bp pool), cheap vs a deep v2 pair."""
    from tests.defi_fakes import tick_for_price as tfp
    v3 = v3_spec(0xC3, fee_ppm=100, spacing=1)
    a = v2_spec(0xA1)
    tick = tfp(1990e6 / 1e18)
    nets = {t: (10 ** 13 if t > tick else -10 ** 13) for t in range(tick - 200, tick + 200) if t != tick}
    states = [v3_state(v3, tick, 10 ** 16, nets), v2_state(a, 1000 * 10 ** 18, 2_000_000 * 10 ** 6)]
    reg = registry([v3, a], tvl={v3.address: 4e6, a.address: 4e6})
    node = FakeNode(base_fee=base_fee)
    chain_node(node, ETH_USD)
    for s in states:
        pool_node(node, s)
    node.set(ARBITRUM_NODE_INTERFACE, S_GAS_ESTIMATE_L1, lambda a: w(1_000) + w(0) + w(0))
    return reg, states, node


async def test_reported_gas_counts_the_ticks_the_chosen_size_crosses_and_size_maximises_net():
    from arb.defi.quote import optimal_amount
    reg, states, node = _dense_tick_world(base_fee=200_000_000)  # 0.2 gwei: one crossed tick ~ $0.01
    st = settings(min_profit_usd=-1e9)
    res = await scan_block(make_client(node), reg, 900, st)
    assert res.complete, res.errors
    o = max(res.opportunities, key=lambda o: o.net_pnl_usd)
    d = o.details
    ticks = [h["ticks_crossed"] for h in d["hops"]]
    gm = GasModel.from_cfg(st.defi.gas)
    hops = [Hop(h["pool"], h["token_in"], h["token_out"], Venue(h["venue"]), h["zero_for_one"]) for h in d["hops"]]
    assert sum(ticks) > 0
    assert d["gas_units"] == gm.units(hops) + sum(ticks) * st.defi.gas.tick_units  # old code: gm.units(hops) only
    assert d["gas_limit"] == -(-d["gas_units"] * 13 // 10)
    # the raw-profit optimum crosses more ticks; with tick gas the chosen size is smaller and nets more
    full = [states_by(states, reg.by_address(h.pool)) for h in hops]
    raw_opt = optimal_amount(full, hops, 10 ** 30)
    assert d["amount_in_raw"] < raw_opt.amount_in

    def net(x):
        qs = quote_route(full, hops, x)
        u = gm.units(hops, qs)
        return ((qs[-1].amount_out - x - aave_premium(x)) / 10 ** 6 if hops[0].token_in == USDC.address else
                (qs[-1].amount_out - x - aave_premium(x)) / 1e18 * ETH_USD) - u * node.base_fee / 1e18 * ETH_USD
    assert net(d["amount_in_raw"]) > net(raw_opt.amount_in)


def test_sizing_never_goes_past_the_pool_liquidity():
    """Liquidity only within +-50 ticks: a size that would drain it is exhausted and never chosen (in the real pool
    it would walk to the price limit and burn 13M+ gas before PartialFill)."""
    from arb.defi.quote import optimal_amount
    from tests.defi_fakes import tick_for_price as tfp
    v3 = v3_spec(0xC4, fee_ppm=100, spacing=1)
    tick = tfp(1800e6 / 1e18)
    L = 10 ** 16
    st3 = v3_state(v3, tick, L, {tick - 50: L, tick + 50: -L})
    v2 = v2_state(v2_spec(0xA1), 1000 * 10 ** 18, 2_000_000 * 10 ** 6)  # 11% gap: wants far more than the range holds
    hops = (Hop(v3.address, USDC.address, WETH.address, v3.venue, False),
            Hop(v2.pool.address, WETH.address, USDC.address, v2.pool.venue, True))
    res = optimal_amount([st3, v2], hops, 10 ** 15)
    assert res.amount_in > 0 and not any(q.exhausted for q in res.quotes) and res.capped
    assert quote_route([st3, v2], hops, res.amount_in * 2)[0].exhausted
