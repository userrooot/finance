"""quote.py: v2 formulas, premium, v3 stepping, sizing against brute force, exhausted handling."""
import math

from arb.defi.quote import (HopQuote, aave_premium, marginal_rate, optimal_amount, quote_hop, quote_route, quote_v2,
                            quote_v3, v2_composite)
from arb.defi.route import Hop, Venue
from arb.defi.v3math import get_sqrt_ratio_at_tick
from tests.defi_fakes import ARB, USDC, WETH, tick_for_price, v2_spec, v2_state, v3_spec, v3_state


# ---- v2 formulas, hand computed -------------------------------------------------------------------------------
def test_uniswap_formula_hand_computed():
    st = v2_state(v2_spec(1), 1000, 2000)  # WETH/USDC reserves
    q = quote_v2(st, 100, True)
    # 100*997 = 99700; 99700*2000 = 199_400_000; 1000*1000 + 99700 = 1_099_700; floor(181.3..) = 181
    assert q.amount_out == 181
    assert q.fee_paid == 100 - 99700 // 1000 == 1
    # other direction: in 2000-side reserve 2000, out reserve 1000: 100*997*1000 // (2000*1000 + 99700)
    assert quote_v2(st, 100, False).amount_out == 99_700_000 // 2_099_700 == 47


def test_camelot_formula_hand_computed():
    st = v2_state(v2_spec(1, dex="camelot_v2"), 1000, 2000, fee=500)  # 0.5% on a 100000 denominator
    # a2 = 100*(100000-500) = 9_950_000; out = 9_950_000*2000 // (1000*100000 + 9_950_000) = 19_900_000_000 // 109_950_000
    assert quote_v2(st, 100, True).amount_out == 180
    # per-direction fee: token1 input uses fee1
    st2 = v2_state(v2_spec(1, dex="camelot_v2"), 1000, 2000, fee=500)
    from dataclasses import replace
    st2 = replace(st2, fee1=1000)
    a2 = 100 * (100000 - 1000)
    assert quote_v2(st2, 100, False).amount_out == a2 * 1000 // (2000 * 100000 + a2)


def test_aerodrome_formula_deducts_fee_before_curve_with_own_floor():
    st = v2_state(v2_spec(1, dex="aerodrome_v2"), 1000, 2000, fee=50)  # 50 bps over 10000
    # 199 in: fee = 199*50//10000 = 0, so nothing is deducted at all (floor)
    assert quote_v2(st, 199, True).amount_out == 199 * 2000 // (1000 + 199) == 331
    # the Uniswap-style multiply would give a different (lower) number
    cam = v2_state(v2_spec(2, dex="camelot_v2"), 1000, 2000, fee=500)  # same 0.5%, Camelot rounding
    assert quote_v2(cam, 199, True).amount_out == 330 != 331
    # 2000 in: fee = 2000*50//10000 = 10; a2 = 1990
    assert quote_v2(st, 2000, True).amount_out == 1990 * 2000 // (1000 + 1990)
    assert quote_v2(st, 2000, True).fee_paid == 10


def test_v2_empty_reserves_are_flagged():
    st = v2_state(v2_spec(1), 0, 2000)
    assert quote_v2(st, 10, True).exhausted


def test_aave_premium_is_ceil():
    # SPEC-DEVIATION: the design says half-up; the deployed pool rounds up (docs/FLASH_ARB_LOOKUPS.md (d))
    assert aave_premium(999) == 1
    assert aave_premium(1999) == 1
    assert aave_premium(1_000_000_000) == 500_000
    assert aave_premium(20_001) == 11
    assert aave_premium(0) == 0
    assert aave_premium(10_000, 9) == 9


# ---- sizing: v2 --------------------------------------------------------------------------------------------------
def two_hop_v2():
    a = v2_state(v2_spec(0xA), 1_000_000, 2_000_000_000)  # 2000 USDC / WETH, sushi
    b = v2_state(v2_spec(0xB, dex="camelot_v2"), 1_000_000, 2_100_000_000, fee=300)  # 2100, camelot
    hops = (Hop(b.pool.address, WETH.address, USDC.address, b.pool.venue, True),
            Hop(a.pool.address, USDC.address, WETH.address, a.pool.venue, False))
    return [b, a], hops


def three_hop_v2():
    a = v2_state(v2_spec(0xA), 1_000_000, 2_000_000_000)  # WETH/USDC
    c = v2_state(v2_spec(0xC, ARB, USDC), 4_000_000, 2_000_000_000)  # ARB/USDC, 500 USDC per ARB
    d = v2_state(v2_spec(0xD, WETH, ARB, dex="aerodrome_v2"), 1_000_000, 3_500_000, fee=30)  # WETH/ARB
    hops = (Hop(a.pool.address, WETH.address, USDC.address, a.pool.venue, True),
            Hop(c.pool.address, USDC.address, ARB.address, c.pool.venue, False),
            Hop(d.pool.address, ARB.address, WETH.address, d.pool.venue, False))
    return [a, c, d], hops


def brute_best(states, hops, hi, premium_bps=5):
    best_x, best = 0, None
    for x in range(1, hi + 1):
        q = quote_route(states, hops, x)
        p = q[-1].amount_out - x - aave_premium(x, premium_bps)
        if best is None or p > best:
            best_x, best = x, p
    return best_x, best


def test_closed_form_matches_brute_force_two_hops():
    states, hops = two_hop_v2()
    res = optimal_amount(states, hops, 300_000)
    assert res.method == "closed_form" and not res.capped
    _, best = brute_best(states, hops, 300_000)
    assert res.profit_raw == best > 0
    assert res.premium == aave_premium(res.amount_in)
    assert res.amount_out - res.amount_in - res.premium == res.profit_raw


def test_closed_form_matches_brute_force_three_hops():
    states, hops = three_hop_v2()
    res = optimal_amount(states, hops, 300_000)
    assert res.method == "closed_form"
    _, best = brute_best(states, hops, 300_000)
    assert res.profit_raw == best > 0


def test_closed_form_x_star_formula():
    from dataclasses import replace
    states, hops = two_hop_v2()
    states = [replace(s, reserve0=s.reserve0 * 10 ** 9, reserve1=s.reserve1 * 10 ** 9) for s in states]
    a, b, c = v2_composite(states, hops)
    x_star = (math.sqrt(a * b / 1.0005) - b) / c
    res = optimal_amount(states, hops, 10 ** 15)
    assert abs(res.amount_in - x_star) <= 0.001 * x_star


def test_no_trade_when_no_gap():
    a = v2_state(v2_spec(0xA), 1_000_000, 2_000_000_000)
    b = v2_state(v2_spec(0xB), 1_000_000, 2_000_000_000)
    hops = (Hop(b.pool.address, WETH.address, USDC.address, b.pool.venue, True),
            Hop(a.pool.address, USDC.address, WETH.address, a.pool.venue, False))
    res = optimal_amount([b, a], hops, 10 ** 6)
    assert res.amount_in == 0 and res.profit_raw == 0
    assert marginal_rate([b, a], hops) < 1.0


def test_size_is_capped_by_max_in():
    states, hops = two_hop_v2()
    res = optimal_amount(states, hops, 1000)
    assert res.capped and res.amount_in <= 1000


def test_marginal_rate_values():
    st = v2_state(v2_spec(1), 1_000_000, 2_000_000_000)
    hop = Hop(st.pool.address, WETH.address, USDC.address, st.pool.venue, True)
    assert abs(marginal_rate([st], [hop]) - 2000 * 0.997) < 1e-9
    v3 = v3_state(v3_spec(2), 0, 10 ** 15, fee_ppm=500)
    h3 = Hop(v3.pool.address, WETH.address, USDC.address, v3.pool.venue, True)
    assert abs(marginal_rate([v3], [h3]) - 0.9995) < 1e-12
    h3b = Hop(v3.pool.address, USDC.address, WETH.address, v3.pool.venue, False)
    assert abs(marginal_rate([v3], [h3b]) - 0.9995) < 1e-12


# ---- v3 ---------------------------------------------------------------------------------------------------------
TICK = tick_for_price(2000e6 / 1e18) // 10 * 10
L = 10 ** 15


def test_v3_single_range_matches_one_swap_step_and_conserves_fee():
    st = v3_state(v3_spec(5), TICK, L)
    q = quote_v3(st, 10 ** 17, True)
    assert not q.exhausted and q.ticks_crossed == 0
    assert 0 < q.fee_paid <= 10 ** 17 * 500 // 1_000_000 + 1
    # more input never gives less output
    assert quote_v3(st, 2 * 10 ** 17, True).amount_out > q.amount_out


def test_v3_crossing_reduces_liquidity_going_down():
    plain = v3_state(v3_spec(5), TICK, L)
    crossing = v3_state(v3_spec(5), TICK, L, {TICK - 100: L // 2})  # half the liquidity ends at TICK-100
    big = 5 * 10 ** 17  # moves the price past TICK - 100
    q_plain, q_cross = quote_v3(plain, big, True), quote_v3(crossing, big, True)
    assert q_cross.ticks_crossed == 1 and q_plain.ticks_crossed == 0
    assert q_cross.amount_out < q_plain.amount_out
    # small swaps that stay inside the first range are unaffected by the tick
    assert quote_v3(crossing, 10 ** 12, True).amount_out == quote_v3(plain, 10 ** 12, True).amount_out


def test_v3_crossing_up_adds_liquidity_net():
    crossing = v3_state(v3_spec(5), TICK, L, {TICK + 100: L // 2})  # extra liquidity starts above
    plain = v3_state(v3_spec(5), TICK, L)
    big = 10 * 10 ** 9  # USDC in (6 decimals): 10_000 USDC moves the price past TICK + 100
    q_c, q_p = quote_v3(crossing, big, False), quote_v3(plain, big, False)
    assert q_c.ticks_crossed == 1
    assert q_c.amount_out > q_p.amount_out


def test_v3_exhausted_when_leaving_the_loaded_window():
    w = (TICK // 10) >> 8
    st = v3_state(v3_spec(5), TICK, 10 ** 12, words=(w, w))
    q = quote_v3(st, 10 ** 18, True)
    assert q.exhausted
    unloaded = v3_state(v3_spec(5), TICK, L, words=(1, 0))  # nothing loaded at all
    assert quote_v3(unloaded, 1000, True).exhausted


def test_v3_zero_amount_and_dispatch():
    st = v3_state(v3_spec(5), TICK, L)
    assert quote_v3(st, 0, True) == HopQuote(0, 0, 0, 0, False)
    v2 = v2_state(v2_spec(1), 1000, 2000)
    assert quote_hop(v2, 100, True) == quote_v2(v2, 100, True)
    assert quote_hop(st, 100, True) == quote_v3(st, 100, True)


def v3_route():
    v3 = v3_state(v3_spec(5), TICK, L, {TICK - 300: L // 2, TICK - 1500: L // 4})
    v2 = v2_state(v2_spec(6), 50 * 10 ** 18, 99_000 * 10 ** 6)  # 1980 USDC per WETH: cheap WETH
    hops = (Hop(v3.pool.address, WETH.address, USDC.address, v3.pool.venue, True),
            Hop(v2.pool.address, USDC.address, WETH.address, v2.pool.venue, False))
    return [v3, v2], hops


def test_golden_section_within_0_01_percent_of_brute_force():
    states, hops = v3_route()
    max_in = 20 * 10 ** 18
    res = optimal_amount(states, hops, max_in)
    assert res.method == "golden_section" and res.profit_raw > 0
    step = max_in // 4000
    grid_best = max(quote_route(states, hops, x)[-1].amount_out - x - aave_premium(x) for x in range(step, max_in, step))
    assert res.profit_raw >= grid_best
    assert res.profit_raw <= grid_best * (1 + 1e-4)
    # local optimality at integer resolution
    for d in (1, 2, 3, -1, -2, -3):
        x = res.amount_in + d
        q = quote_route(states, hops, x)
        assert q[-1].amount_out - x - aave_premium(x) <= res.profit_raw


def test_exhausted_quotes_cap_the_size_and_never_look_profitable_beyond_the_window():
    w = (TICK // 10) >> 8
    v3 = v3_state(v3_spec(5), TICK, 10 ** 13, words=(w, w))
    v2 = v2_state(v2_spec(6), 500 * 10 ** 18, 990_000 * 10 ** 6)
    hops = (Hop(v3.pool.address, WETH.address, USDC.address, v3.pool.venue, True),
            Hop(v2.pool.address, USDC.address, WETH.address, v2.pool.venue, False))
    res = optimal_amount([v3, v2], hops, 10 ** 22)
    assert res.capped
    assert not any(q.exhausted for q in res.quotes)
    assert quote_route([v3, v2], hops, 10 ** 22)[0].exhausted


def test_v3_tick_math_floor_for_negative_ticks():
    # negative tick that is not a multiple of the spacing: the word lookup must floor toward -inf
    st = v3_state(v3_spec(5), TICK - 3, L, {TICK - 10: L // 3})
    assert st.tick == TICK - 3
    q = quote_v3(st, 10 ** 16, True)
    assert not q.exhausted
    assert get_sqrt_ratio_at_tick(st.tick) == st.sqrt_price_x96
