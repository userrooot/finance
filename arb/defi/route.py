"""Route types, ABI encoding and pre-flight validation for FlashArb (docs/FLASH_ARB_DESIGN.md 5.9).

The encoding is the shared contract with the Solidity side: tests/test_defi_route.py pins it to the golden
vectors in section 5.9. Everything here is integer / bytes only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

import eth_abi
from eth_utils import keccak

if TYPE_CHECKING:  # pragma: no cover
    from arb.defi.pools import PoolRegistry

MAX_HOPS = 4
INT256_MAX = 2**255 - 1


class Venue(IntEnum):
    UNI_V3 = 0
    PANCAKE_V3 = 1
    UNI_V2 = 2
    SOLIDLY_V2 = 3


@dataclass(frozen=True)
class Hop:
    pool: str
    token_in: str
    token_out: str
    venue: Venue
    zero_for_one: bool

    def __post_init__(self) -> None:
        for name in ("pool", "token_in", "token_out"):
            object.__setattr__(self, name, getattr(self, name).lower())

    def as_abi(self) -> tuple:
        return (self.pool, self.token_in, self.token_out, int(self.venue), self.zero_for_one)


@dataclass(frozen=True)
class Route:
    loan_token: str
    loan_amount: int
    min_profit: int
    deadline: int
    hops: tuple[Hop, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "loan_token", self.loan_token.lower())
        object.__setattr__(self, "hops", tuple(self.hops))

    def as_abi(self) -> tuple:
        return (self.loan_token, self.loan_amount, self.min_profit, self.deadline, [h.as_abi() for h in self.hops])


HOP_ABI = "(address,address,address,uint8,bool)"
ROUTE_ABI = "(address,uint256,uint256,uint64,(address,address,address,uint8,bool)[])"
EXECUTE_ARB_SELECTOR = bytes.fromhex("e77fcf52")


class RouteError(ValueError):
    """`code` is the name of the Solidity error the contract would raise for the same route."""

    def __init__(self, code: str, hop: int | None = None):
        self.code = code
        self.hop = hop
        super().__init__(code if hop is None else f"{code}(hop={hop})")


def encode_execute_arb(route: Route) -> bytes:
    return EXECUTE_ARB_SELECTOR + eth_abi.encode([ROUTE_ABI], [route.as_abi()])


def encode_flash_params(bal_before: int, route: Route) -> bytes:
    return eth_abi.encode(["uint256", ROUTE_ABI], [bal_before, route.as_abi()])


def route_hash(bal_before: int, route: Route) -> str:
    return "0x" + keccak(encode_flash_params(bal_before, route)).hex()


def validate_route(route: Route, registry: "PoolRegistry", caps: dict[str, int], now_ts: int) -> None:
    """Same checks, same order as FlashArb._validateRoute. `caps` = allowed token address -> maxLoan (0 = not borrowable).

    The registry stands in for the on-chain whitelist: a pool is "enabled" iff the registry knows it.
    """
    allowed = {t.lower(): cap for t, cap in caps.items()}
    # ABI decoding of an out-of-range enum reverts before any contract logic runs. The compiled contract reverts
    # with EMPTY data here (measured by the contract task), not Panic(0x21) as the design's U-05 row says.
    # SPEC-DEVIATION: code "EmptyRevert" instead of Panic0x21.
    for h in route.hops:
        if int(h.venue) not in (0, 1, 2, 3):
            raise RouteError("EmptyRevert")
    n = len(route.hops)
    if n < 2 or n > MAX_HOPS:
        raise RouteError("BadHopCount")
    if route.loan_amount <= 0:
        raise RouteError("ZeroAmount")
    if route.loan_amount > allowed.get(route.loan_token, 0):
        raise RouteError("LoanTooLarge")
    if route.loan_amount > INT256_MAX:
        raise RouteError("AmountTooLarge")
    if now_ts > route.deadline:
        raise RouteError("DeadlineExpired")
    if route.hops[0].token_in != route.loan_token or route.hops[-1].token_out != route.loan_token:
        raise RouteError("RouteNotClosed")
    seen: set[str] = set()
    for i, h in enumerate(route.hops):
        try:
            spec = registry.by_address(h.pool)
        except KeyError:
            raise RouteError("PoolNotWhitelisted", i) from None
        if h.venue != spec.venue:
            raise RouteError("VenueMismatch", i)
        t0, t1 = spec.token0.address.lower(), spec.token1.address.lower()
        if h.token_in == h.token_out or {h.token_in, h.token_out} != {t0, t1}:
            raise RouteError("PoolTokenMismatch", i)
        if h.zero_for_one != (h.token_in == t0):
            raise RouteError("DirectionMismatch", i)
        for tok in (h.token_in, h.token_out):
            if tok not in allowed:
                raise RouteError("TokenNotAllowed", i)
        if i > 0 and h.token_in != route.hops[i - 1].token_out:
            raise RouteError("HopChainBroken", i)
        if h.pool in seen:
            raise RouteError("DuplicatePool", i)
        seen.add(h.pool)
