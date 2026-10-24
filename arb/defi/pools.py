"""Pool registry (from defi/addresses.json), Multicall3 batching and per-block state snapshots (design 5.3).

Static facts (token order, tick spacing, factory check) are read once into registry.json. Per block we read only
what changes: slot0/liquidity/fee for v3, reserves and fees for v2, the ETH/USD Chainlink feed and the header.
Per-pool read failures are collected in `Snapshot.errors` and those pools are absent from `states`; they are never
silently treated as "no opportunity".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import eth_abi
from eth_utils import keccak

from arb.defi.chain import ChainClient, RpcError
from arb.defi.route import Venue

if TYPE_CHECKING:  # pragma: no cover
    from arb.config import DefiCfg

MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"  # checked with get_code at startup; missing code -> RpcError

V3_DEXES = {"uniswap_v3", "sushi_v3", "pancake_v3",
            "aerodrome_slipstream_initial", "aerodrome_slipstream_gaugecaps", "aerodrome_slipstream_gaugesv3"}
V2_DEXES = {"sushi_v2", "camelot_v2", "aerodrome_v2"}
V2Formula = Literal["uniswap", "camelot", "aerodrome"]

SLIPSTREAM_KEYS = {"aerodrome_slipstream_initial": "initial_deployment",
                   "aerodrome_slipstream_gaugecaps": "gauge_caps_deployment",
                   "aerodrome_slipstream_gaugesv3": "gauges_v3_deployment"}
STABLES = {"USDC", "USDT", "DAI", "USDC.e"}
SUSHI_V2_FEE_BPS = 30


def sel(signature: str) -> bytes:
    return keccak(text=signature)[:4]


S_TOKEN0, S_TOKEN1 = sel("token0()"), sel("token1()")
S_TICK_SPACING = sel("tickSpacing()")
S_STABLE, S_STABLE_SWAP = sel("stable()"), sel("stableSwap()")
S_SLOT0, S_LIQUIDITY, S_FEE = sel("slot0()"), sel("liquidity()"), sel("fee()")
S_GET_RESERVES = sel("getReserves()")
S_T0_FEE, S_T1_FEE = sel("token0FeePercent()"), sel("token1FeePercent()")
S_GET_FEE = sel("getFee(address,bool)")
S_TICK_BITMAP, S_TICKS = sel("tickBitmap(int16)"), sel("ticks(int24)")
S_LATEST_ROUND = sel("latestRoundData()")
S_AGGREGATE3 = sel("aggregate3((address,bool,bytes)[])")  # 0x82ad56cb
assert S_AGGREGATE3.hex() == "82ad56cb"

CAMELOT_FEE_DEN = 100_000
AERODROME_FEE_DEN = 10_000
UNISWAP_V2_FEE_DEN = 1_000
UNISWAP_V2_FEE_NUM = 3


def venue_for(dex: str) -> Venue:
    if dex in ("uniswap_v3", "sushi_v3") or dex.startswith("aerodrome_slipstream"):
        return Venue.UNI_V3
    if dex == "pancake_v3":
        return Venue.PANCAKE_V3
    if dex == "sushi_v2":
        return Venue.UNI_V2
    if dex in ("camelot_v2", "aerodrome_v2"):
        return Venue.SOLIDLY_V2
    raise KeyError(f"unsupported dex {dex}")


def formula_for(dex: str) -> V2Formula | None:
    return {"sushi_v2": "uniswap", "camelot_v2": "camelot", "aerodrome_v2": "aerodrome"}.get(dex)


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int


@dataclass(frozen=True)
class PoolSpec:
    chain: str
    dex: str
    venue: Venue
    address: str
    token0: Token  # by address order, read on-chain (the "pair" label is NOT address order)
    token1: Token
    fee_ppm: int | None  # v3 static tier from addresses.json; None for Slipstream (dynamic) and v2
    tick_spacing: int | None
    v2_formula: V2Formula | None
    v2_fee_bps: int | None  # UNI_V2 whitelist value (30 for sushi_v2); None otherwise

    @property
    def is_v3(self) -> bool:
        return self.venue in (Venue.UNI_V3, Venue.PANCAKE_V3)

    def token(self, address: str) -> Token:
        a = address.lower()
        if a == self.token0.address:
            return self.token0
        if a == self.token1.address:
            return self.token1
        raise KeyError(address)

    def other(self, address: str) -> Token:
        return self.token1 if address.lower() == self.token0.address else self.token0


@dataclass(frozen=True)
class V2State:
    pool: PoolSpec
    reserve0: int
    reserve1: int
    fee0: int  # input-fee numerator when token0 is the input
    fee1: int  # input-fee numerator when token1 is the input
    fee_den: int  # 1000 (uniswap: fee = 3), 100000 (camelot), 10000 (aerodrome)
    block: int


@dataclass(frozen=True)
class V3State:
    pool: PoolSpec
    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee_ppm: int  # read from fee() every block
    tick_spacing: int
    liquidity_net: dict[int, int]  # initialized ticks inside the loaded window; empty until read_ticks
    word_lo: int  # loaded bitmap words [word_lo, word_hi]; word_lo > word_hi means "nothing loaded"
    word_hi: int
    block: int


PoolState = V2State | V3State


@dataclass(frozen=True)
class Snapshot:
    chain: str
    block: int
    timestamp: int
    base_fee_wei: int
    eth_usd: float  # Chainlink feed in addresses.json, read at the same block; 0.0 if the read failed (see errors)
    states: dict[str, PoolState]  # by pool address
    errors: list[str]  # per-pool read failures; those pools are absent from states


@dataclass(frozen=True)
class Call:
    target: str
    data: bytes
    allow_failure: bool = True


@dataclass(frozen=True)
class CallResult:
    success: bool
    data: bytes


class Multicall:
    def __init__(self, client: ChainClient, address: str = MULTICALL3, chunk: int = 100):
        self.client = client
        self.address = address
        self.chunk = chunk

    async def check(self) -> None:
        code = await self.client.get_code(self.address)
        if not code:
            raise RpcError("eth_getCode", None, f"{self.client.name}: no Multicall3 code at {self.address}")

    async def aggregate(self, calls: list[Call], block: int | str) -> list[CallResult]:
        out: list[CallResult] = []
        for i in range(0, len(calls), self.chunk):
            part = calls[i:i + self.chunk]
            data = S_AGGREGATE3 + eth_abi.encode(["(address,bool,bytes)[]"],
                                                 [[(c.target, c.allow_failure, c.data) for c in part]])
            raw = await self.client.eth_call(self.address, data, block)
            try:
                (res,) = eth_abi.decode(["(bool,bytes)[]"], raw)
            except Exception as e:
                raise RpcError("eth_call", None, f"multicall decode failed: {type(e).__name__}: {e}") from None
            if len(res) != len(part):
                raise RpcError("eth_call", None, f"multicall returned {len(res)} results for {len(part)} calls")
            out.extend(CallResult(bool(ok), bytes(rd)) for ok, rd in res)
        return out


# ---- word decoding helpers ----------------------------------------------------------------------------------
def word(data: bytes, i: int) -> int:
    chunk = data[32 * i:32 * i + 32]
    if len(chunk) < 32:
        raise ValueError("return data too short")
    return int.from_bytes(chunk, "big")


def signed(v: int, bits: int = 256) -> int:
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def addr_word(data: bytes, i: int = 0) -> str:
    return "0x" + data[32 * i + 12:32 * i + 32].hex()


# ---- registry -----------------------------------------------------------------------------------------------
class PoolRegistry:
    def __init__(self, chain: str, tokens: dict[str, Token], pools: list[PoolSpec], meta: dict | None = None):
        self.chain = chain
        self.tokens = tokens
        self.pools = pools
        self.meta: dict = meta or {}
        self._by_addr = {p.address: p for p in pools}

    @property
    def tvl_usd(self) -> dict[str, float]:
        return self.meta.get("tvl_usd", {})

    def by_address(self, address: str) -> PoolSpec:
        return self._by_addr[address.lower()]

    def token_by_address(self, address: str) -> Token:
        a = address.lower()
        for t in self.tokens.values():
            if t.address == a:
                return t
        raise KeyError(address)

    def pools_for_pair(self, a: str, b: str) -> list[PoolSpec]:
        ta, tb = self.tokens[a].address, self.tokens[b].address
        return [p for p in self.pools if {p.token0.address, p.token1.address} == {ta, tb}]

    def to_json(self) -> dict:
        def tok(t: Token) -> dict:
            return {"symbol": t.symbol, "address": t.address, "decimals": t.decimals}

        return {"chain": self.chain,
                "tokens": {s: tok(t) for s, t in self.tokens.items()},
                "pools": [{"chain": p.chain, "dex": p.dex, "venue": int(p.venue), "address": p.address,
                           "token0": p.token0.address, "token1": p.token1.address, "fee_ppm": p.fee_ppm,
                           "tick_spacing": p.tick_spacing, "v2_formula": p.v2_formula, "v2_fee_bps": p.v2_fee_bps}
                          for p in self.pools],
                "meta": self.meta}

    @classmethod
    def from_json(cls, d: dict) -> "PoolRegistry":
        tokens = {s: Token(t["symbol"], t["address"].lower(), int(t["decimals"])) for s, t in d["tokens"].items()}
        by_addr = {t.address: t for t in tokens.values()}
        pools = [PoolSpec(chain=p["chain"], dex=p["dex"], venue=Venue(p["venue"]), address=p["address"].lower(),
                          token0=by_addr[p["token0"]], token1=by_addr[p["token1"]], fee_ppm=p["fee_ppm"],
                          tick_spacing=p["tick_spacing"], v2_formula=p["v2_formula"], v2_fee_bps=p["v2_fee_bps"])
                 for p in d["pools"]]
        return cls(d["chain"], tokens, pools, d.get("meta", {}))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")


def estimate_tvl_usd(entry: dict, eth_usd: float) -> float | None:
    """Rough TVL from the balances recorded in addresses.json. Stables at $1, WETH at the feed price.
    One priced side is doubled (balanced-pool assumption); no priced side -> None (pool is kept)."""
    sides = []
    for pair_sym in entry["pair"].split("/"):
        bal = entry.get(f"balance_{pair_sym}")
        if bal is None:
            continue
        if pair_sym in STABLES:
            sides.append(float(bal))
        elif pair_sym == "WETH":
            sides.append(float(bal) * eth_usd)
    if not sides:
        return None
    return sum(sides) * (2 if len(sides) == 1 else 1)


def _factory_for(dex: str, ch: dict) -> tuple[str, str]:
    """(factory address, lookup kind) for the lookalike check."""
    dexes = ch["dexes"]
    if dex in ("uniswap_v3", "sushi_v3", "pancake_v3"):
        return dexes[dex]["factory"], "fee"
    if dex in SLIPSTREAM_KEYS:
        return dexes["aerodrome_slipstream"][SLIPSTREAM_KEYS[dex]]["pool_factory"], "spacing"
    if dex in ("sushi_v2", "camelot_v2"):
        return dexes[dex]["factory"], "pair"
    if dex == "aerodrome_v2":
        return dexes["aerodrome_v2"]["pool_factory"], "stable"
    raise KeyError(dex)


def _factory_call(kind: str, factory: str, t0: str, t1: str, entry: dict, tick_spacing: int | None,
                  stable: bool) -> Call:
    if kind == "fee":
        data = sel("getPool(address,address,uint24)") + eth_abi.encode(["address", "address", "uint24"],
                                                                        [t0, t1, int(entry["fee"])])
    elif kind == "spacing":
        data = sel("getPool(address,address,int24)") + eth_abi.encode(["address", "address", "int24"],
                                                                       [t0, t1, int(tick_spacing or 0)])
    elif kind == "pair":
        data = sel("getPair(address,address)") + eth_abi.encode(["address", "address"], [t0, t1])
    else:
        data = sel("getPool(address,address,bool)") + eth_abi.encode(["address", "address", "bool"], [t0, t1, stable])
    return Call(factory, data)


async def build_registry(client: ChainClient, chain: str, addresses: dict, cfg: "DefiCfg",
                         cache_path: Path) -> PoolRegistry:
    """Build the registry once per chain and cache it to `cache_path` (rebuild by deleting it / --refresh)."""
    ccfg = cfg.chains[chain]
    ch = addresses["chains"][chain]
    mc = Multicall(client)
    await mc.check()
    block = await client.block_number()
    eth_usd = float(ch["eth_usd_feed_chainlink"]["price_usd"])
    tokens: dict[str, Token] = {}
    for sym in ccfg.tokens:
        t = ch["tokens"][sym]
        tokens[sym] = Token(sym, t["address"].lower(), int(t["decimals_onchain"]))
    by_addr = {t.address: t for t in tokens.values()}
    rejected: list[dict] = []
    tvl: dict[str, float] = {}

    # 1. static filter from addresses.json
    cands: list[dict] = []
    for e in ch["pools"]:
        if e["dex"] not in ccfg.venues or e["dex"] not in (V3_DEXES | V2_DEXES):
            continue
        a, b = e["pair"].split("/")
        if a not in tokens or b not in tokens:
            continue
        if e["dex"] in V3_DEXES:
            if not e.get("liquidity_raw"):
                continue
        elif not (e.get("reserve_token0_raw") and e.get("reserve_token1_raw")):
            continue
        est = estimate_tvl_usd(e, eth_usd)
        if est is not None and est < cfg.min_pool_tvl_usd:
            continue
        cands.append(e)

    # 2. on-chain token order, tick spacing, stable flags
    calls: list[Call] = []
    plan: list[tuple[dict, int]] = []
    for e in cands:
        p = e["pool"]
        start = len(calls)
        calls += [Call(p, S_TOKEN0), Call(p, S_TOKEN1)]
        if e["dex"] in V3_DEXES:
            calls.append(Call(p, S_TICK_SPACING))
        elif e["dex"] == "aerodrome_v2":
            calls.append(Call(p, S_STABLE))
        elif e["dex"] == "camelot_v2":
            calls.append(Call(p, S_STABLE_SWAP))
        plan.append((e, start))
    res = await mc.aggregate(calls, block)
    info: list[dict] = []
    for e, start in plan:
        r = res[start:start + (3 if e["dex"] in V3_DEXES or e["dex"] in ("aerodrome_v2", "camelot_v2") else 2)]
        if not all(x.success and len(x.data) >= 32 for x in r):
            rejected.append({"pool": e["pool"], "dex": e["dex"], "reason": "static read failed"})
            continue
        t0, t1 = addr_word(r[0].data), addr_word(r[1].data)
        if t0 not in by_addr or t1 not in by_addr:
            rejected.append({"pool": e["pool"], "dex": e["dex"], "reason": "token not in chain token list"})
            continue
        spacing = signed(word(r[2].data, 0), 24) if e["dex"] in V3_DEXES else None
        stable = bool(word(r[2].data, 0)) if e["dex"] in ("aerodrome_v2", "camelot_v2") else False
        if stable:
            rejected.append({"pool": e["pool"], "dex": e["dex"], "reason": "stable pool (v1 quoter has no stable curve)"})
            continue
        info.append({"e": e, "t0": t0, "t1": t1, "spacing": spacing})

    # 3. factory check (lookalike defence)
    calls = []
    for i in info:
        factory, kind = _factory_for(i["e"]["dex"], ch)
        calls.append(_factory_call(kind, factory, i["t0"], i["t1"], i["e"], i["spacing"], False))
    res = await mc.aggregate(calls, block) if calls else []
    pools: list[PoolSpec] = []
    for i, r in zip(info, res):
        e = i["e"]
        if not r.success or len(r.data) < 32 or addr_word(r.data) != e["pool"].lower():
            got = addr_word(r.data) if r.success and len(r.data) >= 32 else "call failed"
            rejected.append({"pool": e["pool"], "dex": e["dex"], "reason": f"factory lookup returned {got}"})
            continue
        dex = e["dex"]
        is_slip = dex in SLIPSTREAM_KEYS
        pools.append(PoolSpec(
            chain=chain, dex=dex, venue=venue_for(dex), address=e["pool"].lower(),
            token0=by_addr[i["t0"]], token1=by_addr[i["t1"]],
            fee_ppm=int(e["fee"]) if dex in V3_DEXES and not is_slip else None,
            tick_spacing=i["spacing"], v2_formula=formula_for(dex),
            v2_fee_bps=SUSHI_V2_FEE_BPS if dex == "sushi_v2" else None))
        est = estimate_tvl_usd(e, eth_usd)
        if est is not None:
            tvl[e["pool"].lower()] = est
    meta = {"chain_id": int(ch["chain_id"]), "built_at_block": block, "eth_feed": ch["eth_usd_feed_chainlink"]["address"].lower(),
            "eth_feed_decimals": int(ch["eth_usd_feed_chainlink"]["decimals"]),
            "aave_pool": ch["aave_v3"]["pool"].lower(),
            "aerodrome_factory": (ch["dexes"].get("aerodrome_v2", {}).get("pool_factory", "") or "").lower(),
            "rejected": rejected, "tvl_usd": tvl}
    reg = PoolRegistry(chain, tokens, pools, meta)
    reg.save(cache_path)
    return reg


async def load_or_build_registry(client: ChainClient, chain: str, addresses: dict, cfg: "DefiCfg",
                                 cache_path: Path, refresh: bool = False) -> PoolRegistry:
    if cache_path.exists() and not refresh:
        return PoolRegistry.from_json(json.loads(cache_path.read_text(encoding="utf-8")))
    return await build_registry(client, chain, addresses, cfg, cache_path)


# ---- per-block state ----------------------------------------------------------------------------------------
def _pool_calls(spec: PoolSpec, aerodrome_factory: str) -> list[Call]:
    p = spec.address
    if spec.is_v3:
        return [Call(p, S_SLOT0), Call(p, S_LIQUIDITY), Call(p, S_FEE)]
    if spec.v2_formula == "camelot":
        return [Call(p, S_GET_RESERVES), Call(p, S_T0_FEE), Call(p, S_T1_FEE)]
    if spec.v2_formula == "aerodrome":
        return [Call(p, S_GET_RESERVES),
                Call(aerodrome_factory, S_GET_FEE + eth_abi.encode(["address", "bool"], [p, False]))]
    return [Call(p, S_GET_RESERVES)]


def _decode_pool(spec: PoolSpec, r: list[CallResult], block: int) -> PoolState:
    if not all(x.success for x in r):
        raise ValueError("call failed")
    if spec.is_v3:
        sqrt_p, tick = word(r[0].data, 0), signed(word(r[0].data, 1), 24)
        if len(r[0].data) < 64:
            raise ValueError("slot0 too short")
        if sqrt_p == 0:
            raise ValueError("pool not initialized")
        return V3State(spec, sqrt_p, tick, word(r[1].data, 0), word(r[2].data, 0), spec.tick_spacing or 0,
                       {}, 0, -1, block)
    r0, r1 = word(r[0].data, 0), word(r[0].data, 1)
    if spec.v2_formula == "camelot":
        return V2State(spec, r0, r1, word(r[1].data, 0), word(r[2].data, 0), CAMELOT_FEE_DEN, block)
    if spec.v2_formula == "aerodrome":
        fee = word(r[1].data, 0)
        return V2State(spec, r0, r1, fee, fee, AERODROME_FEE_DEN, block)
    return V2State(spec, r0, r1, UNISWAP_V2_FEE_NUM, UNISWAP_V2_FEE_NUM, UNISWAP_V2_FEE_DEN, block)


async def read_snapshot(client: ChainClient, registry: PoolRegistry, block: int) -> Snapshot:
    """Stage 0: header, ETH/USD feed and every pool's cheap state at `block`. Raises only if the header fails."""
    mc = Multicall(client)
    header = await client.header(block)
    afac = registry.meta.get("aerodrome_factory", "")
    feed = registry.meta.get("eth_feed")
    errors: list[str] = []
    states: dict[str, PoolState] = {}
    eth_usd = 0.0

    groups: list[tuple[PoolSpec | None, list[Call]]] = []
    if feed:
        groups.append((None, [Call(feed, S_LATEST_ROUND)]))
    groups += [(p, _pool_calls(p, afac)) for p in registry.pools]
    batches: list[list[tuple[PoolSpec | None, list[Call]]]] = [[]]
    n = 0
    for g in groups:
        if n + len(g[1]) > mc.chunk:
            batches.append([])
            n = 0
        batches[-1].append(g)
        n += len(g[1])
    for batch in batches:
        flat = [c for _, cs in batch for c in cs]
        try:
            res = await mc.aggregate(flat, block)
        except Exception as e:
            for spec, _ in batch:
                errors.append(f"{'eth_usd feed' if spec is None else spec.dex + ':' + spec.address}: "
                              f"multicall failed: {type(e).__name__}: {str(e)[:120]}")
            continue
        i = 0
        for spec, cs in batch:
            r = res[i:i + len(cs)]
            i += len(cs)
            try:
                if spec is None:
                    if not r[0].success or len(r[0].data) < 160:
                        raise ValueError("latestRoundData failed")
                    eth_usd = signed(word(r[0].data, 1)) / 10 ** int(registry.meta.get("eth_feed_decimals", 8))
                    if eth_usd <= 0:
                        eth_usd = 0.0
                        raise ValueError("non-positive price")
                else:
                    states[spec.address] = _decode_pool(spec, r, block)
            except Exception as e:
                errors.append(f"{'eth_usd feed' if spec is None else spec.dex + ':' + spec.address}: "
                              f"{type(e).__name__}: {e}")
    if feed and eth_usd == 0.0 and not any(x.startswith("eth_usd") for x in errors):
        errors.append("eth_usd feed: no price")
    return Snapshot(registry.chain, block, header.timestamp, header.base_fee_wei, eth_usd, states, errors)


async def read_ticks(client: ChainClient, snapshot: Snapshot, pools: set[str], words_each_side: int = 1) -> Snapshot:
    """Stage 1: tick bitmap words around the current tick and liquidityNet of every initialized tick in them.
    A pool whose tick read fails is removed from `states` and the failure is added to `errors`."""
    mc = Multicall(client)
    states = dict(snapshot.states)
    errors = list(snapshot.errors)
    targets = [s for a, s in snapshot.states.items() if a in pools and isinstance(s, V3State)]
    if not targets:
        return snapshot

    def fail(s: V3State, why: str) -> None:
        states.pop(s.pool.address, None)
        errors.append(f"{s.pool.dex}:{s.pool.address}: tick read failed: {why}")

    # round 1: bitmap words
    calls: list[Call] = []
    words: dict[str, list[int]] = {}
    for s in targets:
        w = (s.tick // s.tick_spacing) >> 8
        ws = list(range(w - words_each_side, w + words_each_side + 1))
        words[s.pool.address] = ws
        calls += [Call(s.pool.address, S_TICK_BITMAP + eth_abi.encode(["int16"], [x])) for x in ws]
    try:
        res = await mc.aggregate(calls, snapshot.block)
    except Exception as e:
        for s in targets:
            fail(s, f"{type(e).__name__}: {str(e)[:120]}")
        return replace(snapshot, states=states, errors=errors)
    i = 0
    ticks_to_read: dict[str, list[int]] = {}
    for s in targets:
        ws = words[s.pool.address]
        r = res[i:i + len(ws)]
        i += len(ws)
        if not all(x.success and len(x.data) >= 32 for x in r):
            fail(s, "tickBitmap call failed")
            continue
        found = []
        for wpos, x in zip(ws, r):
            bm = word(x.data, 0)
            for bit in range(256):
                if (bm >> bit) & 1:
                    found.append((wpos * 256 + bit) * s.tick_spacing)
        ticks_to_read[s.pool.address] = found
    # round 2: liquidityNet of each initialized tick
    calls = []
    for s in targets:
        for t in ticks_to_read.get(s.pool.address, []):
            calls.append(Call(s.pool.address, S_TICKS + eth_abi.encode(["int24"], [t])))
    try:
        res = await mc.aggregate(calls, snapshot.block) if calls else []
    except Exception as e:
        for s in targets:
            if s.pool.address in states:
                fail(s, f"{type(e).__name__}: {str(e)[:120]}")
        return replace(snapshot, states=states, errors=errors)
    i = 0
    for s in targets:
        ts = ticks_to_read.get(s.pool.address)
        if ts is None:
            continue
        r = res[i:i + len(ts)]
        i += len(ts)
        if not all(x.success and len(x.data) >= 64 for x in r):
            fail(s, "ticks() call failed")
            continue
        net = {t: signed(word(x.data, 1), 128) for t, x in zip(ts, r)}
        ws = words[s.pool.address]
        states[s.pool.address] = replace(s, liquidity_net=net, word_lo=ws[0], word_hi=ws[-1])
    return replace(snapshot, states=states, errors=errors)
