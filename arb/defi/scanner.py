"""Candidate enumeration, per-block costs and `scan_block` -> Opportunity (design 5.5).

Money math: token amounts and gas are integers (wei / raw units) end to end; floats appear only when converting a
final integer to USD for reporting, and in the marginal-rate screen (a filter, never a result).
"""
from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Literal, Sequence

import eth_abi

from arb.config import DefiCfg, Settings
from arb.costs import CostModel
from arb.defi.chain import SIM_CALLER, ChainClient
from arb.defi.pools import (STABLES, Call, Multicall, PoolRegistry, PoolSpec, PoolState, Snapshot, V2State, V3State,
                            read_snapshot, read_ticks, sel, word)
from arb.defi.quote import SizeResult, aave_premium, marginal_rate, optimal_amount, quote_route
from arb.defi.route import Hop, Route, Venue, encode_execute_arb
from arb.defi.v3math import Q96
from arb.models import Leg, Opportunity

BASE_GAS_ORACLE = "0x420000000000000000000000000000000000000f"
ARBITRUM_NODE_INTERFACE = "0x00000000000000000000000000000000000000c8"
S_L1_FEE_UPPER_BOUND = sel("getL1FeeUpperBound(uint256)")  # 0xf1c7a58b
S_GAS_ESTIMATE_L1 = sel("gasEstimateL1Component(address,bool,bytes)")  # 0x77d488a2
assert S_L1_FEE_UPPER_BOUND.hex() == "f1c7a58b" and S_GAS_ESTIMATE_L1.hex() == "77d488a2"
L1_SIZE_PAD = 100  # Base: size passed to getL1FeeUpperBound = calldata length + 100


def short(addr: str) -> str:
    return f"{addr[:6]}..{addr[-4:]}"


@dataclass(frozen=True)
class Candidate:
    chain: str
    kind: Literal["two_pool", "triangular"]
    loan_symbol: str
    hops: tuple[Hop, ...]  # amounts are not part of a candidate
    pools: tuple[PoolSpec, ...]
    key: str


@dataclass(frozen=True)
class GasModel:
    base_units: int
    v3_hop_units: int
    v2_hop_units: int

    def units(self, hops: Sequence[Hop]) -> int:
        total = self.base_units
        for h in hops:
            total += self.v3_hop_units if h.venue in (Venue.UNI_V3, Venue.PANCAKE_V3) else self.v2_hop_units
        return total


@dataclass(frozen=True)
class BlockCosts:
    chain: str
    block: int
    base_fee_wei: int
    tip_wei: int  # from config; Arbitrum default 0 (FCFS ignores tips)
    l1_fee_wei_by_hops: dict[int, int]  # Base: GasPriceOracle; Arbitrum: L1 gas units * base fee
    eth_usd: float
    token_usd: dict[str, float]  # stables = 1.0 (assumption, noted in risk_notes); others from the deepest pool
    price_sources: dict[str, str] = field(default_factory=dict)  # symbol -> note, for risk_notes


@dataclass
class ScanResult:
    chain: str
    block: int
    timestamp_ms: int
    candidates: int
    screened_in: int  # passed marginal_rate
    gross_positive: int  # best size has profit_raw > 0 before gas
    opportunities: list[Opportunity]  # net_pnl_usd >= min_profit_usd
    errors: list[str]
    complete: bool  # False if any read failed; never silently "no opportunity"
    duration_s: float
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)  # kind -> candidates/screened_in/gross_positive/surviving
    costs: BlockCosts | None = None  # costs the block was evaluated with (None if the read failed)


# ---- candidates ---------------------------------------------------------------------------------------------
def _mk_hop(spec: PoolSpec, token_in: str, token_out: str) -> Hop:
    return Hop(spec.address, token_in, token_out, spec.venue, token_in == spec.token0.address)


def _key(chain: str, loan: str, pools: Sequence[PoolSpec]) -> str:
    return f"{chain} {loan} " + ">".join(f"{p.dex}:{short(p.address)}" for p in pools)


def _make_candidate(chain: str, kind: str, loan: str, tokens_path: Sequence[str],
                    pools: Sequence[PoolSpec]) -> Candidate:
    hops = tuple(_mk_hop(p, tokens_path[i], tokens_path[i + 1]) for i, p in enumerate(pools))
    return Candidate(chain, kind, loan, hops, tuple(pools), _key(chain, loan, pools))  # type: ignore[arg-type]


def enumerate_candidates(registry: PoolRegistry, loan_symbols: Sequence[str],
                         max_hops: int = 3, tri_top_k: int = 3) -> list[Candidate]:
    out: list[Candidate] = []
    by_pair: dict[frozenset[str], list[PoolSpec]] = {}
    for p in registry.pools:
        by_pair.setdefault(frozenset((p.token0.address, p.token1.address)), []).append(p)

    def top(a: str, b: str, k: int) -> list[PoolSpec]:
        ps = by_pair.get(frozenset((a, b)), [])
        return sorted(ps, key=lambda p: (-registry.tvl_usd.get(p.address, 0.0), p.address))[:k]

    for loan in loan_symbols:
        if loan not in registry.tokens:
            continue
        la = registry.tokens[loan].address
        # two_pool: loan -> other on pool i, other -> loan on pool j
        for pair, pools in sorted(by_pair.items(), key=lambda kv: sorted(kv[0])):
            if la not in pair or len(pools) < 2:
                continue
            other = next(a for a in pair if a != la)
            for pi, pj in itertools.permutations(sorted(pools, key=lambda p: p.address), 2):
                out.append(_make_candidate(registry.chain, "two_pool", loan, [la, other, la], [pi, pj]))
        # triangular: loan -> A -> B -> loan
        if max_hops >= 3:
            addrs = [t.address for s, t in registry.tokens.items() if s != loan]
            for a, b in itertools.permutations(addrs, 2):
                e1, e2, e3 = top(la, a, tri_top_k), top(a, b, tri_top_k), top(b, la, tri_top_k)
                for p1, p2, p3 in itertools.product(e1, e2, e3):
                    out.append(_make_candidate(registry.chain, "triangular", loan, [la, a, b, la], [p1, p2, p3]))
    return out


# ---- costs --------------------------------------------------------------------------------------------------
def dummy_route(n_hops: int) -> Route:
    z = "0x" + "00" * 20
    return Route(z, 1, 0, 1, tuple(Hop(z, z, z, Venue.UNI_V3, True) for _ in range(n_hops)))


def calldata_len(n_hops: int) -> int:
    return len(encode_execute_arb(dummy_route(n_hops)))  # 548 for 2 hops, 708 for 3, +160 per extra hop


def _spot_price(state: PoolState, token_addr: str) -> float | None:
    """Raw other-per-raw token price of `token_addr` in `state`'s pool (marginal, no fee)."""
    pool = state.pool
    if isinstance(state, V3State):
        p = (state.sqrt_price_x96 / Q96) ** 2  # token1 per token0
    else:
        if state.reserve0 <= 0:
            return None
        p = state.reserve1 / state.reserve0
    if p <= 0:
        return None
    return p if token_addr == pool.token0.address else 1.0 / p


def compute_token_usd(registry: PoolRegistry, snapshot: Snapshot) -> tuple[dict[str, float], dict[str, str]]:
    """Stables at $1 (an assumption, noted in risk_notes), WETH at the Chainlink price, every other token from the
    deepest pool against an already priced token."""
    usd: dict[str, float] = {}
    notes: dict[str, str] = {}
    for sym in registry.tokens:
        if sym in STABLES:
            usd[sym] = 1.0
            notes[sym] = "stablecoin priced at $1"
    if "WETH" in registry.tokens and snapshot.eth_usd > 0:
        usd["WETH"] = snapshot.eth_usd
        notes["WETH"] = "WETH priced from the Chainlink ETH/USD feed"
    for _ in range(2):
        for sym, tok in registry.tokens.items():
            if sym in usd:
                continue
            best: tuple[float, PoolSpec, float] | None = None
            for p in registry.pools:
                if tok.address not in (p.token0.address, p.token1.address):
                    continue
                other = p.other(tok.address)
                if other.symbol not in usd or p.address not in snapshot.states:
                    continue
                raw = _spot_price(snapshot.states[p.address], tok.address)
                if raw is None:
                    continue
                price = raw * 10 ** (tok.decimals - other.decimals) * usd[other.symbol]
                tvl = registry.tvl_usd.get(p.address, 0.0)
                if best is None or tvl > best[0]:
                    best = (tvl, p, price)
            if best is not None:
                usd[sym] = best[2]
                notes[sym] = f"price of {sym} from pool {best[1].address}"
    return usd, notes


async def read_block_costs(client: ChainClient, registry: PoolRegistry, snapshot: Snapshot,
                           cfg: DefiCfg) -> BlockCosts:
    ccfg = cfg.chains[registry.chain]
    token_usd, notes = compute_token_usd(registry, snapshot)
    l1: dict[int, int] = {}
    hop_counts = list(range(2, max(cfg.max_hops, 2) + 1))
    if registry.chain == "base":
        calls = [Call(BASE_GAS_ORACLE, S_L1_FEE_UPPER_BOUND + eth_abi.encode(["uint256"], [calldata_len(n) + L1_SIZE_PAD]))
                 for n in hop_counts]
        res = await Multicall(client).aggregate(calls, snapshot.block)
        for n, r in zip(hop_counts, res):
            if not r.success or len(r.data) < 32:
                raise RuntimeError(f"base: getL1FeeUpperBound({n} hops) failed")
            l1[n] = word(r.data, 0)
    elif registry.chain == "arbitrum":
        for n in hop_counts:
            cd = encode_execute_arb(dummy_route(n))
            data = S_GAS_ESTIMATE_L1 + eth_abi.encode(["address", "bool", "bytes"], [SIM_CALLER, False, cd])
            raw = await client.eth_call(ARBITRUM_NODE_INTERFACE, data, snapshot.block)
            if len(raw) < 32:
                raise RuntimeError(f"arbitrum: gasEstimateL1Component({n} hops) returned no data")
            l1[n] = word(raw, 0) * snapshot.base_fee_wei  # gasEstimateForL1 (L2 gas units) x base fee
    else:
        raise ValueError(f"no L1 data fee model for chain {registry.chain}")
    return BlockCosts(registry.chain, snapshot.block, snapshot.base_fee_wei, int(round(ccfg.tip_gwei * 1e9)), l1,
                      snapshot.eth_usd, token_usd, notes)


# ---- evaluation ---------------------------------------------------------------------------------------------
@dataclass
class Assessment:
    screened: bool = False
    gross_positive: bool = False
    opportunity: Opportunity | None = None


def _wei_to_usd(wei: int, eth_usd: float) -> float:
    return wei / 1e18 * eth_usd


def _raw_to_human(raw: int, decimals: int) -> float:
    return raw / 10 ** decimals


def _max_in_raw(settings: Settings, price: float, decimals: int) -> int:
    return int(settings.defi.max_loan_usd / price * 10 ** decimals)  # type: ignore[union-attr]


def _gas_wei(units: int, costs: BlockCosts, n_hops: int) -> tuple[int, int, int]:
    return units * costs.base_fee_wei, costs.l1_fee_wei_by_hops[n_hops], units * costs.tip_wei


def assess(candidate: Candidate, snapshot: Snapshot, costs: BlockCosts, settings: Settings,
           screen_only: bool = False) -> Assessment:
    cfg = settings.defi
    assert cfg is not None
    out = Assessment()
    states = [snapshot.states[p.address] for p in candidate.pools]
    if marginal_rate(states, candidate.hops) <= 1.0 + cfg.premium_bps / 1e4:
        return out
    out.screened = True
    if screen_only:
        return out
    loan_tok = candidate.pools[0].token(candidate.hops[0].token_in)
    price = costs.token_usd.get(loan_tok.symbol)
    if not price or price <= 0:
        raise ValueError(f"no USD price for loan token {loan_tok.symbol}")
    dec = loan_tok.decimals
    max_in = _max_in_raw(settings, price, dec)
    size = optimal_amount(states, candidate.hops, max_in, cfg.premium_bps)
    if size.amount_in <= 0 or size.profit_raw <= 0 or any(q.exhausted for q in size.quotes):
        return out
    out.gross_positive = True

    gm = GasModel(cfg.gas.base_units, cfg.gas.v3_hop_units, cfg.gas.v2_hop_units)
    units = gm.units(candidate.hops)
    n = len(candidate.hops)
    l2_wei, l1_wei, tip_wei = _gas_wei(units, costs, n)
    l2_usd, l1_usd, tip_usd = (_wei_to_usd(w, costs.eth_usd) for w in (l2_wei, l1_wei, tip_wei))
    gas_usd = l2_usd + l1_usd + tip_usd
    profit_usd = _raw_to_human(size.profit_raw, dec) * price
    net = profit_usd - gas_usd

    # swap fees: fee_paid_i in raw tokenIn_i, priced; fall back to the fee fraction of the loan value
    swap_fees = 0.0
    notes = {costs.price_sources.get(loan_tok.symbol, "")}
    for h, q, pool in zip(candidate.hops, size.quotes, candidate.pools):
        t_in = pool.token(h.token_in)
        p_in = costs.token_usd.get(t_in.symbol)
        if p_in:
            swap_fees += _raw_to_human(q.fee_paid, t_in.decimals) * p_in
            notes.add(costs.price_sources.get(t_in.symbol, ""))
        elif q.amount_in:
            swap_fees += q.fee_paid / q.amount_in * _raw_to_human(size.amount_in, dec) * price
            notes.add(f"price of {t_in.symbol} unavailable; swap fee estimated from the loan value")
    notional = _raw_to_human(size.amount_in, dec) * price
    cost_map = {"flash_premium": _raw_to_human(size.premium, dec) * price, "swap_fees": swap_fees,
                "l2_gas": l2_usd, "l1_data": l1_usd, "priority_tip": tip_usd}
    gross = net + sum(cost_map.values())
    if net < cfg.min_profit_usd:
        return out  # gross positive but does not survive costs

    # capacity: largest loan with net >= 0, bisection on [x*, max_in]
    def net_at(x: int) -> float:
        qs = quote_route(states, candidate.hops, x)
        if any(q.exhausted for q in qs):
            return -math.inf
        return _raw_to_human(qs[-1].amount_out - x - aave_premium(x, cfg.premium_bps), dec) * price - gas_usd

    if net_at(max_in) >= 0:
        cap_x = max_in
    else:
        lo, hi = size.amount_in, max_in
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if net_at(mid) >= 0:
                lo = mid
            else:
                hi = mid
        cap_x = lo
    min_profit_raw = _min_profit_raw(l2_wei + l1_wei + tip_wei, costs.eth_usd, cfg.min_profit_usd, price, dec)

    legs = []
    hop_details = []
    for h, q, pool in zip(candidate.hops, size.quotes, candidate.pools):
        t_in, t_out = pool.token(h.token_in), pool.token(h.token_out)
        hin, hout = _raw_to_human(q.amount_in, t_in.decimals), _raw_to_human(q.amount_out, t_out.decimals)
        legs.append(Leg(exchange=f"{candidate.chain}:{pool.dex}", symbol=f"{t_in.symbol}/{t_out.symbol}",
                        market="spot", side="sell", price=hout / hin if hin else 0.0, qty=hin))
        hop_details.append({"pool": pool.address, "dex": pool.dex, "venue": int(pool.venue), "token_in": h.token_in,
                            "token_out": h.token_out, "zero_for_one": h.zero_for_one, "amount_in": q.amount_in,
                            "amount_out": q.amount_out, "fee_paid": q.fee_paid})
    risk = sorted(x for x in notes if x)
    if size.capped:
        risk.append("v3 window edge reached" if any(isinstance(s, V3State) for s in states) else "capped at max_loan_usd")
    if any(p.dex.startswith("aerodrome_slipstream") for p in candidate.pools):
        risk.append("slipstream fee is dynamic (read per block)")
    after_tax = CostModel(settings).after_tax(net)
    out.opportunity = Opportunity(
        strategy="defi_two_pool" if candidate.kind == "two_pool" else "defi_triangular",
        timestamp_ms=snapshot.timestamp * 1000, key=candidate.key, legs=legs, notional_usd=notional,
        gross_pnl_usd=gross, costs_usd=cost_map, net_pnl_usd=net, net_after_tax_usd=after_tax,
        net_bps=net / notional * 1e4 if notional else 0.0, capacity_usd=_raw_to_human(cap_x, dec) * price,
        holding="instant", risk_notes=risk,
        details={"chain": candidate.chain, "block": snapshot.block, "loan_token": loan_tok.address,
                 "loan_symbol": loan_tok.symbol, "kind": candidate.kind, "amount_in_raw": size.amount_in,
                 "amount_out_raw": size.amount_out, "premium_raw": size.premium, "profit_raw": size.profit_raw,
                 "min_profit_raw": min_profit_raw, "gas_units": units, "base_fee_wei": costs.base_fee_wei,
                 "tip_wei": costs.tip_wei, "l1_fee_wei": l1_wei, "eth_usd": costs.eth_usd, "hops": hop_details,
                 "sizing": size.method})
    return out


def _min_profit_raw(gas_wei_total: int, eth_usd: float, min_profit_usd: float, price: float, decimals: int) -> int:
    """ceil((gas + threshold) in loan-token raw units)."""
    usd = Decimal(gas_wei_total) / Decimal(10 ** 18) * Decimal(repr(eth_usd)) + Decimal(repr(min_profit_usd))
    return int((usd / Decimal(repr(price)) * Decimal(10 ** decimals)).to_integral_value(rounding=ROUND_CEILING))


def evaluate(candidate: Candidate, snapshot: Snapshot, costs: BlockCosts, settings: Settings) -> Opportunity | None:
    return assess(candidate, snapshot, costs, settings).opportunity


async def scan_block(client: ChainClient, registry: PoolRegistry, block: int, settings: Settings) -> ScanResult:
    cfg = settings.defi
    assert cfg is not None
    t0 = time.monotonic()
    errors: list[str] = []
    kinds = {k: {"candidates": 0, "screened_in": 0, "gross_positive": 0, "surviving": 0} for k in ("two_pool", "triangular")}

    costs: BlockCosts | None = None

    def done(ts_ms: int, cands: int, screened: int, gross: int, opps: list[Opportunity]) -> ScanResult:
        return ScanResult(registry.chain, block, ts_ms, cands, screened, gross, opps, errors, not errors,
                          time.monotonic() - t0, kinds, costs)

    try:
        snap = await read_snapshot(client, registry, block)
    except Exception as e:
        errors.append(f"snapshot: {type(e).__name__}: {str(e)[:200]}")
        return done(0, 0, 0, 0, [])
    errors.extend(snap.errors)
    ts_ms = snap.timestamp * 1000
    try:
        costs = await read_block_costs(client, registry, snap, cfg)
    except Exception as e:
        costs = None
        errors.append(f"costs: {type(e).__name__}: {str(e)[:200]}")
        return done(ts_ms, 0, 0, 0, [])
    loans = []
    for sym in cfg.loan_tokens:
        if sym not in registry.tokens:
            continue
        if costs.token_usd.get(sym, 0) <= 0:
            errors.append(f"price: no USD price for loan token {sym}")
            continue
        loans.append(sym)
    cands = [c for c in enumerate_candidates(registry, loans, cfg.max_hops, cfg.tri_top_k)
             if all(p.address in snap.states for p in c.pools)]
    for c in cands:
        kinds[c.kind]["candidates"] += 1
    screened = [c for c in cands if assess(c, snap, costs, settings, screen_only=True).screened]
    for c in screened:
        kinds[c.kind]["screened_in"] += 1
    v3_pools = {p.address for c in screened for p in c.pools if p.is_v3}
    if v3_pools:
        snap = await _read_ticks_safe(client, snap, v3_pools, errors)
        # candidates that lost a pool to a tick-read failure are not evaluated; the failure is in `errors`
    opps: list[Opportunity] = []
    gross = 0
    for c in screened:
        if not all(p.address in snap.states for p in c.pools):
            continue
        try:
            a = assess(c, snap, costs, settings)
        except Exception as e:
            errors.append(f"evaluate {c.key}: {type(e).__name__}: {str(e)[:200]}")
            continue
        if a.gross_positive:
            gross += 1
            kinds[c.kind]["gross_positive"] += 1
        if a.opportunity is not None:
            opps.append(a.opportunity)
            kinds[c.kind]["surviving"] += 1
    opps.sort(key=lambda o: o.net_pnl_usd, reverse=True)
    return done(ts_ms, len(cands), len(screened), gross, opps)


async def _read_ticks_safe(client: ChainClient, snap: Snapshot, pools: set[str], errors: list[str]) -> Snapshot:
    before = len(snap.errors)
    try:
        new = await read_ticks(client, snap, pools)
    except Exception as e:
        errors.append(f"ticks: {type(e).__name__}: {str(e)[:200]}")
        states = {a: s for a, s in snap.states.items() if a not in pools}
        return Snapshot(snap.chain, snap.block, snap.timestamp, snap.base_fee_wei, snap.eth_usd, states, snap.errors)
    errors.extend(new.errors[before:])
    return new
