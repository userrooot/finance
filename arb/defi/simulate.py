"""eth_call simulation of the exact `executeArb` calldata through the constructor-only harness (design 5.6).

No deployment, no key, no transaction: one `eth_call` with no `to` and `data = initcode ++ abi.encode(args)` that
runs FlashArb inside the harness against real chain state and returns (success, profit, gasUsed, revertData).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import eth_abi
from eth_utils import keccak

from arb.defi.chain import SIM_CALLER, ChainClient, ExecutionReverted, RpcError
from arb.defi.pools import PoolSpec
from arb.defi.route import Route, Venue, encode_execute_arb
from arb.defi.scanner import BlockCosts

_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_ARTIFACT = _ROOT / "contracts/artifacts/src/sim/FlashArbSimHarness.sol/FlashArbSimHarness.json"
FLASHARB_ARTIFACT = _ROOT / "contracts/artifacts/src/FlashArb.sol/FlashArb.json"
HARNESS_ARGS_ABI = ["address", "address", "(address,uint256)[]", "(address,uint8,uint16)[]", "bytes"]
HARNESS_RESULT_ABI = ["bool", "uint256", "uint256", "bytes"]
SIM_GAS = 25_000_000
TX_BASE_GAS = 21_000

# Error signatures from design 3.10 plus OZ and harness errors: decode_revert works without the artifact.
BUILTIN_ERRORS = (
    "NotAContract(address account)", "RenounceDisabled()", "InvalidPoolConfig(address pool)", "NotAavePool()",
    "BadInitiator(address initiator)", "NoActiveRoute()", "RouteHashMismatch()", "LoanMismatch()",
    "BadHopCount(uint256 n)", "RouteNotClosed()", "HopChainBroken(uint256 hop)", "PoolNotWhitelisted(address pool)",
    "VenueMismatch(uint256 hop)", "PoolTokenMismatch(uint256 hop)", "DirectionMismatch(uint256 hop)",
    "DuplicatePool(address pool)", "TokenNotAllowed(address token)", "LoanTooLarge(uint256 amount, uint256 cap)",
    "ZeroAmount()", "AmountTooLarge()", "DeadlineExpired(uint256 deadline, uint256 nowTs)",
    "UnexpectedCallback(address caller)", "BadCallbackAmount(int256 amount0Delta, int256 amount1Delta)",
    "PartialFill(uint256 hop, uint256 paid, uint256 amountIn)", "ZeroOutput(uint256 hop)",
    "BalanceDeltaMismatch(uint256 hop, address token, uint256 expected, uint256 actual)",
    "InsufficientProfit(uint256 balance, uint256 required)",
    "OwnableUnauthorizedAccount(address account)", "OwnableInvalidOwner(address owner)", "EnforcedPause()",
    "ExpectedPause()", "ReentrancyGuardReentrantCall()", "SafeERC20FailedOperation(address token)",
    "HarnessProfitMismatch(uint256 returned, uint256 measured)",
)


class SimulatorUnavailable(RuntimeError):
    """Artifact missing, or the RPC refuses the creation eth_call."""


@dataclass(frozen=True)
class SimRequest:
    chain: str
    block: int | str
    route: Route
    aave_pool: str
    pools: tuple[PoolSpec, ...]  # whitelisted inside the harness
    token_caps: dict[str, int]  # token address -> maxLoan raw (loan token must be present)


@dataclass(frozen=True)
class SimResult:
    chain: str
    block: int | str
    success: bool
    revert_reason: str | None  # "InsufficientProfit(balance=..., required=...)", "Error(...)", "Panic(0x11)", "0x<hex>"
    profit_raw: int
    gas_used_inner: int
    gas_estimate_tx: int  # inner + 21000 + calldata gas (16/nonzero byte, 4/zero byte)
    gas_cost_usd: float  # gas_estimate_tx * (base_fee + tip) + l1 fee
    profit_usd: float
    net_usd: float  # profit_usd - gas_cost_usd
    calldata_hash: str  # keccak256(executeArb calldata), 0x-hex


def build_execute_arb_calldata(route: Route) -> bytes:
    return encode_execute_arb(route)


def _load_artifact(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SimulatorUnavailable(f"artifact not found: {path} (build contracts first)") from None


def build_harness_initcode(req: SimRequest, calldata: bytes, artifact: Path | None = None) -> bytes:
    artifact = artifact or HARNESS_ARTIFACT
    code = _load_artifact(artifact).get("bytecode", "")
    if not isinstance(code, str) or len(code) <= 2:
        raise SimulatorUnavailable(f"artifact {artifact} has no bytecode")
    tokens = [(t.lower(), int(cap)) for t, cap in sorted(req.token_caps.items())]
    pools = [(p.address, int(p.venue), int(p.v2_fee_bps or 0) if p.venue == Venue.UNI_V2 else 0) for p in req.pools]
    args = eth_abi.encode(HARNESS_ARGS_ABI, [req.aave_pool.lower(), req.route.loan_token, tokens, pools, calldata])
    return bytes.fromhex(code[2:] if code.startswith("0x") else code) + args


def _error_table(abi_path: Path | None) -> dict[bytes, tuple[str, list[tuple[str, str]]]]:
    table: dict[bytes, tuple[str, list[tuple[str, str]]]] = {}

    def add(name: str, inputs: list[tuple[str, str]]) -> None:
        sig = f"{name}({','.join(t for _, t in inputs)})"
        table[keccak(text=sig)[:4]] = (name, inputs)

    for s in BUILTIN_ERRORS:
        name, rest = s.split("(", 1)
        inputs = []
        for part in filter(None, (x.strip() for x in rest.rstrip(")").split(","))):
            typ, nm = part.split(" ", 1)
            inputs.append((nm, typ))
        add(name, inputs)
    if abi_path is not None and abi_path.exists():
        try:
            for item in json.loads(abi_path.read_text(encoding="utf-8")).get("abi", []):
                if item.get("type") == "error":
                    add(item["name"], [(i.get("name") or f"arg{k}", i["type"]) for k, i in enumerate(item["inputs"])])
        except (ValueError, KeyError):
            pass  # the built-in table still applies
    return table


def decode_revert(data: bytes, abi_path: Path | None = None) -> str:
    abi_path = abi_path or FLASHARB_ARTIFACT
    if len(data) < 4:
        return "0x" + data.hex()
    selector, body = data[:4], data[4:]
    try:
        if selector == bytes.fromhex("08c379a0"):
            (msg,) = eth_abi.decode(["string"], body)
            return f"Error({msg})"
        if selector == bytes.fromhex("4e487b71"):
            (code,) = eth_abi.decode(["uint256"], body)
            return f"Panic({hex(code)})"
        entry = _error_table(abi_path).get(selector)
        if entry is not None:
            name, inputs = entry
            vals = eth_abi.decode([t for _, t in inputs], body) if inputs else ()
            return f"{name}({', '.join(f'{n}={v}' for (n, _), v in zip(inputs, vals))})"
    except Exception:
        pass
    return "0x" + data.hex()


def calldata_gas(calldata: bytes) -> int:
    return sum(4 if b == 0 else 16 for b in calldata)


async def simulate(client: ChainClient, req: SimRequest, costs: BlockCosts) -> SimResult:
    cd = build_execute_arb_calldata(req.route)
    initcode = build_harness_initcode(req, cd)
    try:
        raw = await client.eth_call(None, initcode, req.block, from_=SIM_CALLER, gas=SIM_GAS)
    except ExecutionReverted as e:
        # the harness itself reverted (constructor): not a route result, a broken simulator
        raise SimulatorUnavailable(f"harness creation call reverted: {decode_revert(e.data or b'')}") from e
    except RpcError as e:
        if type(e) is RpcError:  # plain RpcError: gas cap, code size, creation calls unsupported ...
            raise SimulatorUnavailable(f"RPC refuses the creation eth_call: {e.message[:200]}") from e
        raise  # rate limit / state unavailable / transport: data failures, not simulator problems
    try:
        ok, profit, gas_inner, rev = eth_abi.decode(HARNESS_RESULT_ABI, raw)
    except Exception as e:
        raise SimulatorUnavailable(f"harness returned undecodable data ({len(raw)} bytes): {e}") from e
    gas_tx = int(gas_inner) + TX_BASE_GAS + calldata_gas(cd)
    n = len(req.route.hops)
    gas_wei = gas_tx * (costs.base_fee_wei + costs.tip_wei) + costs.l1_fee_wei_by_hops.get(n, 0)
    gas_usd = gas_wei / 1e18 * costs.eth_usd
    loan_tok = None
    for p in req.pools:
        for t in (p.token0, p.token1):
            if t.address == req.route.loan_token:
                loan_tok = t
    if loan_tok is None:
        raise SimulatorUnavailable("loan token is not a token of any whitelisted pool")
    price = costs.token_usd.get(loan_tok.symbol, 0.0)
    profit_usd = int(profit) / 10 ** loan_tok.decimals * price
    return SimResult(chain=req.chain, block=req.block, success=bool(ok),
                     revert_reason=None if ok else decode_revert(bytes(rev)), profit_raw=int(profit),
                     gas_used_inner=int(gas_inner), gas_estimate_tx=gas_tx, gas_cost_usd=gas_usd,
                     profit_usd=profit_usd, net_usd=profit_usd - gas_usd,
                     calldata_hash="0x" + keccak(cd).hex())
