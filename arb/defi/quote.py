"""Local exact-integer quoting for v2 and v3 pools, route quotes, the marginal-rate screen and size optimisation
(design 5.4). QuoterV2 is only used in tests (tests/test_defi_quote_live.py) to prove `quote_v3`.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Literal, Sequence

from arb.defi.pools import PoolState, V2State, V3State
from arb.defi.route import Hop
from arb.defi.v3math import (MAX_SQRT_RATIO, MAX_TICK, MIN_SQRT_RATIO, MIN_TICK, Q96, compute_swap_step,
                             get_sqrt_ratio_at_tick)

NEG_INF = -(1 << 400)  # "exhausted quote" sentinel for the integer profit search


@dataclass(frozen=True)
class HopQuote:
    amount_in: int
    amount_out: int
    fee_paid: int  # raw units of tokenIn (v3: sum of fee_amount per step; v2: a - a_after_fee)
    ticks_crossed: int
    exhausted: bool  # v3: left the loaded window or ran out of liquidity -> quote is invalid


@dataclass(frozen=True)
class SizeResult:
    amount_in: int
    amount_out: int
    premium: int
    profit_raw: int  # amount_out - amount_in - premium (can be negative)
    quotes: list[HopQuote]
    method: Literal["closed_form", "golden_section"]
    capped: bool  # hit max_in or the window edge


def aave_premium(amount: int, premium_bps: int = 5) -> int:
    """Aave PercentageMath.percentMul: round half up."""
    return (amount * premium_bps + 5000) // 10000


# ---- v2 -----------------------------------------------------------------------------------------------------
def quote_v2(state: V2State, amount_in: int, zero_for_one: bool) -> HopQuote:
    r_in, r_out = (state.reserve0, state.reserve1) if zero_for_one else (state.reserve1, state.reserve0)
    fee = state.fee0 if zero_for_one else state.fee1
    den = state.fee_den
    if amount_in <= 0 or r_in <= 0 or r_out <= 0:
        return HopQuote(amount_in, 0, 0, 0, exhausted=amount_in > 0)
    if state.pool.v2_formula == "aerodrome":
        # Aerodrome deducts the fee from the input, with its own floor, before the curve
        a2 = amount_in - amount_in * fee // den
        out = a2 * r_out // (r_in + a2)
        fee_paid = amount_in * fee // den
    else:  # uniswap (997/1000) and camelot ((100000 - f)/100000) share the same form
        a2 = amount_in * (den - fee)
        out = a2 * r_out // (r_in * den + a2)
        fee_paid = amount_in - a2 // den
    return HopQuote(amount_in, out, fee_paid, 0, exhausted=False)


# ---- v3 -----------------------------------------------------------------------------------------------------
def _next_initialized_tick(ticks: list[int], state: V3State, tick: int, zero_for_one: bool) -> tuple[int, bool] | None:
    """TickBitmap.nextInitializedTickWithinOneWord on the loaded window. None = word outside the window."""
    sp = state.tick_spacing
    compressed = tick // sp  # floor toward -inf, as the pool does
    if not zero_for_one:
        compressed += 1
    word_pos = compressed >> 8
    if word_pos < state.word_lo or word_pos > state.word_hi:
        return None
    word_start = compressed - (compressed & 255)
    if zero_for_one:
        i = bisect.bisect_right(ticks, compressed * sp) - 1  # largest tick <= compressed*sp
        if i >= 0 and ticks[i] // sp >= word_start:
            return ticks[i], True
        return word_start * sp, False
    i = bisect.bisect_left(ticks, compressed * sp)  # smallest tick >= compressed*sp
    if i < len(ticks) and ticks[i] // sp <= word_start + 255:
        return ticks[i], True
    return (word_start + 255) * sp, False


def quote_v3(state: V3State, amount_in: int, zero_for_one: bool) -> HopQuote:
    """Exact-input swap through the loaded tick window, step for step like UniswapV3Pool.swap
    (including the word-boundary steps, whose fee rounding matters for an exact match)."""
    if amount_in <= 0:
        return HopQuote(amount_in, 0, 0, 0, False)
    limit = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
    ticks = sorted(state.liquidity_net)
    sqrt_p, tick, liq = state.sqrt_price_x96, state.tick, state.liquidity
    remaining, out, fee_total, crossed = amount_in, 0, 0, 0
    exhausted = False
    while remaining > 0 and sqrt_p != limit:
        nxt = _next_initialized_tick(ticks, state, tick, zero_for_one)
        if nxt is None:
            exhausted = True
            break
        tick_next, initialized = nxt
        tick_next = max(MIN_TICK, min(MAX_TICK, tick_next))
        sqrt_next = get_sqrt_ratio_at_tick(tick_next)
        target = max(sqrt_next, limit) if zero_for_one else min(sqrt_next, limit)
        sqrt_p, a_in, a_out, fee = compute_swap_step(sqrt_p, target, liq, remaining, state.fee_ppm)
        remaining -= a_in + fee
        out += a_out
        fee_total += fee
        if sqrt_p == sqrt_next:
            if initialized:
                net = state.liquidity_net[tick_next]
                liq += -net if zero_for_one else net
                crossed += 1
                if liq < 0:
                    exhausted = True
                    break
            tick = tick_next - 1 if zero_for_one else tick_next
        # else the step consumed all remaining input (exact input), the loop ends
    if remaining > 0:
        exhausted = True  # price limit reached with input left over: partial fill
    return HopQuote(amount_in, out, fee_total, crossed, exhausted)


def quote_hop(state: PoolState, amount_in: int, zero_for_one: bool) -> HopQuote:
    if isinstance(state, V3State):
        return quote_v3(state, amount_in, zero_for_one)
    return quote_v2(state, amount_in, zero_for_one)


def quote_route(states: Sequence[PoolState], hops: Sequence[Hop], amount_in: int) -> list[HopQuote]:
    quotes: list[HopQuote] = []
    x = amount_in
    for st, h in zip(states, hops):
        q = quote_hop(st, x, h.zero_for_one)
        quotes.append(q)
        x = q.amount_out
    return quotes


# ---- screen and sizing ----------------------------------------------------------------------------------------
def _v2_g(state: V2State, zero_for_one: bool) -> float:
    fee = state.fee0 if zero_for_one else state.fee1
    return 1.0 - fee / state.fee_den  # uniswap 0.997, camelot 1 - f/1e5, aerodrome 1 - fee/1e4


def marginal_rate(states: Sequence[PoolState], hops: Sequence[Hop]) -> float:
    """Product over hops of (marginal price in the hop direction x (1 - fee)), raw-unit out per raw-unit in."""
    rate = 1.0
    for st, h in zip(states, hops):
        if isinstance(st, V3State):
            p = (st.sqrt_price_x96 / Q96) ** 2
            px = p if h.zero_for_one else 1.0 / p
            rate *= px * (1.0 - st.fee_ppm / 1e6)
        else:
            r_in, r_out = (st.reserve0, st.reserve1) if h.zero_for_one else (st.reserve1, st.reserve0)
            if r_in <= 0 or r_out <= 0:
                return 0.0
            rate *= r_out / r_in * _v2_g(st, h.zero_for_one)
    return rate


def v2_composite(states: Sequence[V2State], hops: Sequence[Hop]) -> tuple[float, float, float]:
    """A chain of v2 hops is one map f(x) = A x / (B + C x)."""
    a, b, c = 1.0, 1.0, 0.0
    for st, h in zip(states, hops):
        r_in, r_out = (st.reserve0, st.reserve1) if h.zero_for_one else (st.reserve1, st.reserve0)
        g = _v2_g(st, h.zero_for_one)
        a, b, c = g * r_out * a, r_in * b, r_in * c + g * a
    return a, b, c


def _profit(states: Sequence[PoolState], hops: Sequence[Hop], x: int, premium_bps: int) -> int:
    if x <= 0:
        return NEG_INF
    qs = quote_route(states, hops, x)
    if any(q.exhausted for q in qs):
        return NEG_INF
    return qs[-1].amount_out - x - aave_premium(x, premium_bps)


def _golden_max(f, lo: int, hi: int, iters: int) -> int:
    """Integer golden-section search for the maximiser of a concave f on [lo, hi], then a local scan.

    Runs at least `iters` shrink steps (up to 3x that) until the bracket is a few units wide; a 1e24-wide range
    needs about 115 steps to reach integer resolution."""
    a, b = lo, hi
    cap = max(iters, 1) * 3
    n = 0
    while b - a > 8 and n < cap:
        n += 1
        d = (b - a) * 618033988749894848 // 10 ** 18
        c1, c2 = b - d, a + d
        if c1 >= c2:
            break
        if f(c1) < f(c2):
            a = c1
        else:
            b = c2
    best_x, best_v = a, f(a)
    for x in range(max(lo, a - 3), min(hi, b + 3) + 1):
        v = f(x)
        if v > best_v:
            best_x, best_v = x, v
    return best_x


def _window_edge(states: Sequence[PoolState], hops: Sequence[Hop], max_in: int) -> tuple[int, bool]:
    """Largest x <= max_in whose route quote has no exhausted hop. Returns (x, capped_by_window)."""
    def ok(x: int) -> bool:
        return not any(q.exhausted for q in quote_route(states, hops, x))

    if ok(max_in):
        return max_in, False
    x = max(max_in // 1024, 1)
    last_ok = 0
    while x < max_in and ok(x):
        last_ok = x
        x *= 2
    lo, hi = last_ok, min(x, max_in)  # lo is ok (or 0), hi is not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo, True


def _result(states, hops, x: int, premium_bps: int, method, capped: bool) -> SizeResult:
    qs = quote_route(states, hops, x) if x > 0 else []
    out = qs[-1].amount_out if qs else 0
    prem = aave_premium(x, premium_bps) if x > 0 else 0
    return SizeResult(x, out, prem, out - x - prem, qs, method, capped)


def optimal_amount(states: Sequence[PoolState], hops: Sequence[Hop], max_in: int,
                   premium_bps: int = 5, iters: int = 64) -> SizeResult:
    if max_in <= 0:
        return _result(states, hops, 0, premium_bps, "golden_section", True)
    f = lambda x: _profit(states, hops, x, premium_bps)  # noqa: E731
    if all(isinstance(s, V2State) for s in states):
        a, b, c = v2_composite(states, hops)  # type: ignore[arg-type]
        p = premium_bps / 1e4
        if c <= 0 or a <= 0:
            return _result(states, hops, 0, premium_bps, "closed_form", False)
        disc = a * b / (1.0 + p)
        xs = (math.sqrt(disc) - b) / c
        if not xs > 0:
            return _result(states, hops, 0, premium_bps, "closed_form", False)
        capped = xs >= max_in
        x0 = min(int(xs), max_in)
        lo = max(1, int(x0 * 0.9))
        hi = min(max_in, int(x0 * 1.1) + 1)
        best = _golden_max(f, lo, max(hi, lo), 20)
        return _result(states, hops, best, premium_bps, "closed_form", capped)
    hi, capped = _window_edge(states, hops, max_in)
    if hi <= 0:
        return _result(states, hops, 0, premium_bps, "golden_section", True)
    best = _golden_max(f, 1, hi, iters)
    return _result(states, hops, best, premium_bps, "golden_section", capped or best >= hi)
