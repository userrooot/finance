"""Exact integer port of Uniswap v3 TickMath, FullMath, SqrtPriceMath and SwapMath (exact-input only).

Python ints, no floats. Solidity overflow reverts are not emulated except where the original branches on them
(the amount0 price update). The same step math is used by Uniswap v3, Sushi v3, Pancake v3 and Slipstream.
"""
from __future__ import annotations

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
Q96 = 1 << 96
UINT160_MAX = (1 << 160) - 1
UINT256_MAX = (1 << 256) - 1
FEE_DEN = 1_000_000

# TickMath magic constants: bit i of |tick| multiplies the Q128.128 ratio by 1.0001^(-2^i / 2).
_TICK_MULS = (
    (0x2, 0xfff97272373d413259a46990580e213a),
    (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
    (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0),
    (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053),
    (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54),
    (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9),
    (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5),
    (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6),
    (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604),
    (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError(f"tick {tick} out of range")
    abs_tick = -tick if tick < 0 else tick
    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if abs_tick & 0x1 else 1 << 128
    for bit, mul in _TICK_MULS:
        if abs_tick & bit:
            ratio = (ratio * mul) >> 128
    if tick > 0:
        ratio = UINT256_MAX // ratio
    # Q128.128 -> Q64.96, rounding up
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def mul_div(a: int, b: int, d: int) -> int:
    if d == 0:
        raise ZeroDivisionError("mul_div by zero")
    return (a * b) // d


def mul_div_rounding_up(a: int, b: int, d: int) -> int:
    if d == 0:
        raise ZeroDivisionError("mul_div by zero")
    return -((-(a * b)) // d)


def _div_rounding_up(a: int, d: int) -> int:
    return -((-a) // d)


def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    num1 = liquidity << 96
    num2 = sqrt_b - sqrt_a
    if sqrt_a <= 0:
        raise ValueError("sqrt_a must be positive")
    if round_up:
        return _div_rounding_up(mul_div_rounding_up(num1, num2, sqrt_b), sqrt_a)
    return mul_div(num1, num2, sqrt_b) // sqrt_a


def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if round_up:
        return mul_div_rounding_up(liquidity, sqrt_b - sqrt_a, Q96)
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


def _next_sqrt_from_amount0_rounding_up(sqrt_p: int, liquidity: int, amount: int) -> int:
    """Price after adding `amount` of token0 (price goes down)."""
    if amount == 0:
        return sqrt_p
    numerator1 = liquidity << 96
    product = amount * sqrt_p
    if product <= UINT256_MAX:
        denominator = numerator1 + product
        if denominator >= numerator1 and denominator <= UINT256_MAX:
            return mul_div_rounding_up(numerator1, sqrt_p, denominator)
    return _div_rounding_up(numerator1, numerator1 // sqrt_p + amount)


def _next_sqrt_from_amount1_rounding_down(sqrt_p: int, liquidity: int, amount: int) -> int:
    """Price after adding `amount` of token1 (price goes up)."""
    if amount <= UINT160_MAX:
        quotient = (amount << 96) // liquidity
    else:
        quotient = mul_div(amount, Q96, liquidity)
    return sqrt_p + quotient


def get_next_sqrt_price_from_input(sqrt_p: int, liquidity: int, amount_in: int, zero_for_one: bool) -> int:
    if sqrt_p <= 0 or liquidity <= 0:
        raise ValueError("sqrt_p and liquidity must be positive")
    if zero_for_one:
        return _next_sqrt_from_amount0_rounding_up(sqrt_p, liquidity, amount_in)
    return _next_sqrt_from_amount1_rounding_down(sqrt_p, liquidity, amount_in)


def compute_swap_step(sqrt_cur: int, sqrt_target: int, liquidity: int,
                      amount_remaining: int, fee_ppm: int) -> tuple[int, int, int, int]:
    """One swap step, exact input. Returns (sqrt_next, amount_in, amount_out, fee_amount)."""
    zero_for_one = sqrt_cur >= sqrt_target
    remaining_less_fee = mul_div(amount_remaining, FEE_DEN - fee_ppm, FEE_DEN)
    if zero_for_one:
        amount_in = get_amount0_delta(sqrt_target, sqrt_cur, liquidity, True)
    else:
        amount_in = get_amount1_delta(sqrt_cur, sqrt_target, liquidity, True)
    if remaining_less_fee >= amount_in:
        sqrt_next = sqrt_target
    else:
        sqrt_next = get_next_sqrt_price_from_input(sqrt_cur, liquidity, remaining_less_fee, zero_for_one)
    reached = sqrt_target == sqrt_next
    if zero_for_one:
        if not reached:
            amount_in = get_amount0_delta(sqrt_next, sqrt_cur, liquidity, True)
        amount_out = get_amount1_delta(sqrt_next, sqrt_cur, liquidity, False)
    else:
        if not reached:
            amount_in = get_amount1_delta(sqrt_cur, sqrt_next, liquidity, True)
        amount_out = get_amount0_delta(sqrt_cur, sqrt_next, liquidity, False)
    if not reached:
        fee_amount = amount_remaining - amount_in  # exact input: the remainder is the fee
    else:
        fee_amount = mul_div_rounding_up(amount_in, fee_ppm, FEE_DEN - fee_ppm)
    return sqrt_next, amount_in, amount_out, fee_amount
