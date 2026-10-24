"""Uniswap v3 integer math port: constants, TickMath, and SwapMath cases from the v3-core test suite."""
import random
from decimal import Decimal, getcontext

import pytest

from arb.defi.v3math import (MAX_SQRT_RATIO, MAX_TICK, MIN_SQRT_RATIO, MIN_TICK, Q96, compute_swap_step,
                             get_amount0_delta, get_amount1_delta, get_next_sqrt_price_from_input,
                             get_sqrt_ratio_at_tick, mul_div, mul_div_rounding_up)

getcontext().prec = 90


def encode_price_sqrt(r1: int, r0: int) -> int:
    """v3-core test helper: floor(sqrt(r1/r0) * 2^96)."""
    return int((Decimal(r1) / Decimal(r0)).sqrt() * Decimal(2 ** 96))


def test_tick_constants():
    assert get_sqrt_ratio_at_tick(0) == 2 ** 96
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO == 4295128739
    assert get_sqrt_ratio_at_tick(MAX_TICK) == MAX_SQRT_RATIO == 1461446703485210103287273052203988822378723970342
    with pytest.raises(ValueError):
        get_sqrt_ratio_at_tick(MAX_TICK + 1)
    with pytest.raises(ValueError):
        get_sqrt_ratio_at_tick(MIN_TICK - 1)


def test_tick_ratio_monotonic_and_symmetric():
    prev = 0
    for t in range(-60, 61, 3):
        r = get_sqrt_ratio_at_tick(t)
        assert r > prev
        prev = r
    # sqrt(1.0001^t) * sqrt(1.0001^-t) = 1 up to rounding in Q96
    for t in (1, 7, 100, 5000, 200000):
        assert abs(get_sqrt_ratio_at_tick(t) * get_sqrt_ratio_at_tick(-t) - Q96 * Q96) <= Q96 * Q96 // 10 ** 15


def test_tick_ratio_matches_decimal_power():
    rnd = random.Random(1)
    ticks = [rnd.randint(-100_000, 100_000) for _ in range(200)] + [1, -1, 2, 4, 8, 2 ** 16, -(2 ** 16), 2 ** 18 - 1]
    for t in ticks:
        exact = (Decimal("1.0001") ** Decimal(t)).sqrt() * Decimal(2 ** 96)
        # TickMath multiplies 128-bit constants, so large |tick| keeps fewer significant bits: error <= 1 unit or 1e-18 relative
        assert abs(Decimal(get_sqrt_ratio_at_tick(t)) - exact) <= max(Decimal(1), exact * Decimal('1e-18'))


def test_mul_div():
    assert mul_div(7, 3, 2) == 10
    assert mul_div_rounding_up(7, 3, 2) == 11
    assert mul_div_rounding_up(6, 3, 2) == 9
    with pytest.raises(ZeroDivisionError):
        mul_div(1, 1, 0)


def test_amount_deltas_round_trip():
    a, b, liq = encode_price_sqrt(1, 1), encode_price_sqrt(121, 100), 10 ** 18
    up0, dn0 = get_amount0_delta(a, b, liq, True), get_amount0_delta(a, b, liq, False)
    up1, dn1 = get_amount1_delta(a, b, liq, True), get_amount1_delta(a, b, liq, False)
    assert 0 <= up0 - dn0 <= 2 and 0 <= up1 - dn1 <= 1
    assert get_amount0_delta(b, a, liq, False) == dn0  # argument order does not matter


def test_swap_step_capped_at_target_one_for_zero():
    # v3-core SwapMath.spec: exact in, capped at the price target
    price, target = encode_price_sqrt(1, 1), encode_price_sqrt(101, 100)
    assert target == 79623317895830914510639640423
    e18 = 10 ** 18
    nxt, amt_in, amt_out, fee = compute_swap_step(price, target, 2 * e18, e18, 600)
    assert (amt_in, fee, amt_out) == (9975124224178055, 5988667735148, 9925619580021728)
    assert nxt == target
    assert amt_in + fee < e18


def test_swap_step_fully_spent_one_for_zero():
    price, target = encode_price_sqrt(1, 1), encode_price_sqrt(1000, 100)
    e18 = 10 ** 18
    nxt, amt_in, amt_out, fee = compute_swap_step(price, target, 2 * e18, e18, 600)
    assert (amt_in, fee, amt_out) == (999400000000000000, 600000000000000, 666399946655997866)
    assert amt_in + fee == e18
    assert nxt == get_next_sqrt_price_from_input(price, 2 * e18, e18 - fee, False)
    assert nxt < target


def test_swap_step_zero_for_one_capped_and_spent():
    e18 = 10 ** 18
    price = encode_price_sqrt(1, 1)
    target = encode_price_sqrt(99, 100)
    nxt, amt_in, amt_out, fee = compute_swap_step(price, target, 2 * e18, e18, 600)
    assert nxt == target and amt_in + fee < e18 and amt_out > 0
    far = encode_price_sqrt(10, 100)
    nxt, amt_in, amt_out, fee = compute_swap_step(price, far, 2 * e18, e18, 600)
    assert amt_in + fee == e18 and nxt > far


def test_swap_step_zero_liquidity_moves_to_target():
    p, t = encode_price_sqrt(1, 1), encode_price_sqrt(2, 1)
    assert compute_swap_step(p, t, 0, 1000, 3000) == (t, 0, 0, 0)


def test_swap_step_properties_random():
    rnd = random.Random(7)
    for _ in range(300):
        p = get_sqrt_ratio_at_tick(rnd.randint(-5000, 5000))
        t = get_sqrt_ratio_at_tick(rnd.randint(-5000, 5000))
        if p == t:
            continue
        liq = rnd.randint(10 ** 6, 10 ** 22)
        amount = rnd.randint(1, 10 ** 21)
        fee = rnd.choice([100, 500, 3000, 10000])
        nxt, a_in, a_out, f = compute_swap_step(p, t, liq, amount, fee)
        assert a_in + f <= amount
        if nxt != t:
            assert a_in + f == amount  # all input is consumed when the target is not reached
        # the price moves toward the target and never past it
        assert (p <= nxt <= t) if t > p else (t <= nxt <= p)
