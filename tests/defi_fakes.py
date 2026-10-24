"""Offline fakes for the arb.defi tests. Every number is a hand-built fixture, not market data."""
from __future__ import annotations

import asyncio
import math
from typing import Any, Callable

import eth_abi

from arb.config import DefiCfg, DefiChainCfg, DefiGasCfg
from arb.defi.chain import ChainClient, RateLimiter
from arb.defi.pools import (CAMELOT_FEE_DEN, PoolRegistry, PoolSpec, Token, V2State, V3State, sel,
                            S_AGGREGATE3, S_GET_RESERVES, S_SLOT0, S_LIQUIDITY, S_FEE, S_LATEST_ROUND,
                            S_TICK_BITMAP, S_TICKS)
from arb.defi.route import Venue
from arb.defi.v3math import get_sqrt_ratio_at_tick

WETH = Token("WETH", "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", 18)
USDC = Token("USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6)
ARB = Token("ARB", "0x912ce59144191c1204e64559fe8253a0e49e6548", 18)
AAVE = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
FEED = "0x639fe6ab55c921f74e7fac1ee960c0b6293ba612"
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"
NODE_INTERFACE = "0x00000000000000000000000000000000000000c8"


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def v2_spec(n: int, t0: Token = WETH, t1: Token = USDC, dex: str = "sushi_v2", chain: str = "arbitrum") -> PoolSpec:
    formula = {"sushi_v2": "uniswap", "camelot_v2": "camelot", "aerodrome_v2": "aerodrome"}[dex]
    venue = Venue.UNI_V2 if dex == "sushi_v2" else Venue.SOLIDLY_V2
    return PoolSpec(chain, dex, venue, addr(n), t0, t1, None, None, formula, 30 if dex == "sushi_v2" else None)


def v3_spec(n: int, t0: Token = WETH, t1: Token = USDC, fee_ppm: int = 500, spacing: int = 10,
            dex: str = "uniswap_v3", chain: str = "arbitrum") -> PoolSpec:
    venue = Venue.PANCAKE_V3 if dex == "pancake_v3" else Venue.UNI_V3
    return PoolSpec(chain, dex, venue, addr(n), t0, t1, fee_ppm, spacing, None, None)


def v2_state(spec: PoolSpec, r0: int, r1: int, fee: int | None = None, block: int = 100) -> V2State:
    if spec.v2_formula == "uniswap":
        return V2State(spec, r0, r1, 3, 3, 1000, block)
    if spec.v2_formula == "camelot":
        f = 300 if fee is None else fee
        return V2State(spec, r0, r1, f, f, CAMELOT_FEE_DEN, block)
    f = 30 if fee is None else fee
    return V2State(spec, r0, r1, f, f, 10_000, block)


def tick_for_price(raw_price_token1_per_token0: float) -> int:
    return math.floor(math.log(raw_price_token1_per_token0) / math.log(1.0001))


def v3_state(spec: PoolSpec, tick: int, liquidity: int, liquidity_net: dict[int, int] | None = None,
             words: tuple[int, int] | None = None, fee_ppm: int | None = None, block: int = 100) -> V3State:
    """Pool sitting exactly at `tick`. `words` = loaded bitmap word range (default: the 3 words around tick)."""
    sp = spec.tick_spacing or 10
    w = (tick // sp) >> 8
    lo, hi = words if words is not None else (w - 1, w + 1)
    return V3State(spec, get_sqrt_ratio_at_tick(tick), tick, liquidity, fee_ppm if fee_ppm is not None else (spec.fee_ppm or 500),
                   sp, dict(liquidity_net or {}), lo, hi, block)


def registry(pools: list[PoolSpec], tvl: dict[str, float] | None = None, chain: str = "arbitrum",
             tokens: tuple[Token, ...] = (WETH, USDC)) -> PoolRegistry:
    meta = {"chain_id": 42161 if chain == "arbitrum" else 8453, "aave_pool": AAVE, "eth_feed": FEED,
            "eth_feed_decimals": 8, "aerodrome_factory": addr(0xAE), "tvl_usd": tvl or {}, "rejected": []}
    return PoolRegistry(chain, {t.symbol: t for t in tokens}, pools, meta)


def defi_cfg(**over: Any) -> DefiCfg:
    chains = {
        "arbitrum": DefiChainCfg(rpc_url_env="TEST_ARB_RPC", rpc_url_default="http://fake", max_rps=1000, burst=1000,
                                 tokens=["WETH", "USDC", "ARB"], venues=["uniswap_v3", "sushi_v2"],
                                 lateness_blocks=[0, 2, 8]),
        "base": DefiChainCfg(rpc_url_env="TEST_BASE_RPC", rpc_url_default="http://fake", max_rps=1000, burst=1000,
                             tokens=["WETH", "USDC"], venues=["uniswap_v3"], lateness_blocks=[0, 1, 2], tip_gwei=0.01)}
    base = dict(enabled=True, min_profit_usd=0.05, max_loan_usd=50_000.0, max_hops=3, tri_top_k=3,
                gas=DefiGasCfg(), chains=chains)
    base.update(over)
    return DefiCfg(**base)


# ---- scripted transport -----------------------------------------------------------------------------------
class FakeTransport:
    """Pops scripted replies. A reply is (status, json, headers), an Exception to raise, or a callable(payload)."""

    def __init__(self, replies: list | None = None, handler: Callable[[dict], tuple] | None = None):
        self.replies = list(replies or [])
        self.handler = handler
        self.requests: list[dict] = []

    async def post(self, url: str, payload: dict):
        self.requests.append(payload)
        if self.handler is not None:
            return self.handler(payload)
        r = self.replies.pop(0)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return r(payload)
        return r


def ok(result: Any) -> tuple[int, dict, dict]:
    return 200, {"jsonrpc": "2.0", "id": 1, "result": result}, {}


def rpc_err(code: int, message: str, data: str | None = None) -> tuple[int, dict, dict]:
    e: dict = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return 200, {"jsonrpc": "2.0", "id": 1, "error": e}, {}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def make_client(transport, *, name: str = "arbitrum", chain_id: int = 42161, rate: float = 1000.0, burst: int = 1000,
                max_retries: int = 4, clock: FakeClock | None = None) -> ChainClient:
    clock = clock or FakeClock()
    limiter = RateLimiter(rate, burst, clock=clock, sleep=clock.sleep)
    c = ChainClient(name, chain_id, "http://fake", limiter, transport=transport, max_retries=max_retries,
                    sleep=clock.sleep)
    c.clock = clock  # type: ignore[attr-defined]
    return c


# ---- fake chain node ----------------------------------------------------------------------------------------
def w(v: int) -> bytes:
    return (v % (1 << 256)).to_bytes(32, "big")


def sword(args: bytes) -> int:
    """First ABI word as a signed integer."""
    v = int.from_bytes(args[:32], "big")
    return v - (1 << 256) if v >= 1 << 255 else v


def addr_pad(a: str) -> bytes:
    return bytes(12) + bytes.fromhex(a[2:])


class FakeNode:
    """JSON-RPC node: headers, code, direct eth_call handlers and Multicall3 aggregate3 dispatch.

    `contracts[address][selector]` is bytes or a callable(args_bytes) -> bytes (raise `Revert` to fail the call).
    `fail_multicall_for` makes eth_call to Multicall3 raise a transport-level 500 when it returns True.
    """

    class Revert(Exception):
        pass

    def __init__(self, base_fee: int = 20_000_000, timestamp0: int = 1_800_000_000) -> None:
        self.contracts: dict[str, dict[bytes, Any]] = {}
        self.head = 1000
        self.base_fee = base_fee
        self.ts0 = timestamp0
        self.code: dict[str, bytes] = {MULTICALL: b"\x60\x00"}
        self.requests: list[dict] = []
        self.http_error: Callable[[dict], tuple | None] | None = None
        self.call_blocks: list[Any] = []
        self.create: Callable[[bytes], bytes] | None = None  # handler for creation eth_calls (no `to`)
        self.creation_calls: list[dict] = []

    def set(self, address: str, selector: bytes, value: Any) -> None:
        self.contracts.setdefault(address.lower(), {})[selector] = value

    def _call(self, to: str, data: bytes) -> bytes:
        h = self.contracts.get(to.lower())
        if h is None or data[:4] not in h:
            raise FakeNode.Revert()
        v = h[data[:4]]
        return v(data[4:]) if callable(v) else v

    async def post(self, url: str, payload: dict):
        self.requests.append(payload)
        if self.http_error is not None:
            r = self.http_error(payload)
            if r is not None:
                return r
        m, p = payload["method"], payload["params"]
        if m == "eth_chainId":
            return ok(hex(42161))
        if m == "eth_blockNumber":
            return ok(hex(self.head))
        if m == "eth_getCode":
            return ok("0x" + self.code.get(p[0].lower(), b"").hex())
        if m == "eth_getBlockByNumber":
            n = self.head if p[0] == "latest" else int(p[0], 16)
            return ok({"number": hex(n), "timestamp": hex(self.ts0 + n), "baseFeePerGas": hex(self.base_fee),
                       "hash": "0x" + f"{n:064x}"})
        if m == "eth_call":
            call, block = p[0], p[1]
            self.call_blocks.append(block)
            data = bytes.fromhex(call["data"][2:])
            to = call.get("to", "").lower()
            try:
                if not to:
                    self.creation_calls.append(call)
                    if self.create is None:
                        return rpc_err(-32000, "contract creation calls are not supported")
                    return ok("0x" + self.create(data).hex())
                if to == MULTICALL and data[:4] == S_AGGREGATE3:
                    (calls,) = eth_abi.decode(["(address,bool,bytes)[]"], data[4:])
                    res = []
                    for tgt, allow, cd in calls:
                        try:
                            res.append((True, self._call(tgt, cd)))
                        except FakeNode.Revert:
                            if not allow:
                                raise
                            res.append((False, b""))
                    return ok("0x" + eth_abi.encode(["(bool,bytes)[]"], [res]).hex())
                return ok("0x" + self._call(to, data).hex())
            except FakeNode.Revert:
                return rpc_err(3, "execution reverted", "0x")
        return rpc_err(-32601, f"method {m} not found")


def pool_node(node: FakeNode, state: V2State | V3State) -> None:
    """Install the view functions the snapshot reads for one pool state."""
    a = state.pool.address
    if isinstance(state, V3State):
        node.set(a, S_SLOT0, w(state.sqrt_price_x96) + w(state.tick) + bytes(32 * 5))
        node.set(a, S_LIQUIDITY, w(state.liquidity))
        node.set(a, S_FEE, w(state.fee_ppm))
        bm: dict[int, int] = {}
        for t in state.liquidity_net:
            c = t // state.tick_spacing
            bm[c >> 8] = bm.get(c >> 8, 0) | (1 << (c & 255))
        node.set(a, S_TICK_BITMAP, lambda args: w(bm.get(sword(args), 0)))
        node.set(a, S_TICKS, lambda args: w(0) + w(state.liquidity_net.get(sword(args), 0)) + bytes(32 * 6))
    else:
        node.set(a, S_GET_RESERVES, w(state.reserve0) + w(state.reserve1) + w(0))
        if state.pool.v2_formula == "camelot":
            node.set(a, sel("token0FeePercent()"), w(state.fee0))
            node.set(a, sel("token1FeePercent()"), w(state.fee1))
        elif state.pool.v2_formula == "aerodrome":
            node.set(addr(0xAE), sel("getFee(address,bool)"), w(state.fee0))


def chain_node(node: FakeNode, eth_usd: float = 2000.0) -> None:
    # updatedAt = the fake chain's genesis timestamp: fresh enough for every block the tests read
    node.set(FEED, S_LATEST_ROUND, w(1) + w(int(eth_usd * 1e8)) + w(node.ts0) + w(node.ts0) + w(1))


async def run(coro):
    return await coro


def run_sync(coro):
    return asyncio.run(coro)
