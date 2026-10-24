"""Async JSON-RPC client with a rate limiter, retries, typed errors and failure tracking (design 5.2).

Read-only by construction: the only methods this module knows are reads (eth_call, eth_getBlockByNumber, ...).
Every final failure is appended to `ChainClient.failures` and raised; nothing returns an empty result on failure.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from arb.config import DefiChainCfg

SIM_CALLER = "0x000000000000000000000000000000000000dead"  # `from` for every eth_call; not a key

BACKOFFS_S = (0.5, 1.0, 2.0, 4.0)
MAX_RETRY_AFTER_S = 30.0
STATE_UNAVAILABLE_MARKERS = ("missing trie node", "header not found", "historical state", "pruned",
                             "state not available", "no state available", "unknown block", "block not found")


class RpcError(Exception):
    def __init__(self, method: str, code: int | None, message: str, data: bytes | None = None):
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"{method}: [{code}] {message}")


class RateLimited(RpcError):
    """HTTP 429 or JSON-RPC -32005 after retries."""


class StateUnavailable(RpcError):
    """Node does not serve the requested block state."""


class ExecutionReverted(RpcError):
    """eth_call reverted; `.data` holds the revert bytes (may be b"")."""


class TransportError(RpcError):
    """Timeouts, connection errors, 5xx after retries."""


class Transport(Protocol):
    async def post(self, url: str, payload: dict) -> tuple[int, dict, dict[str, str]]: ...  # (status, json, headers)


class AiohttpTransport:
    """Default transport. trust_env=True so the agent proxy (HTTPS_PROXY) is honoured."""

    def __init__(self, timeout_s: float = 10.0):
        self.timeout_s = timeout_s
        self._session = None

    async def post(self, url: str, payload: dict) -> tuple[int, dict, dict[str, str]]:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        async with self._session.post(url, json=payload) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {"_raw": body}
            return resp.status, body, headers

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


class RateLimiter:
    """Token bucket: `rate_per_s` tokens per second, at most `burst` stored."""

    def __init__(self, rate_per_s: float, burst: int, clock=time.monotonic, sleep=asyncio.sleep):
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate_per_s must be > 0 and burst >= 1")
        self.rate = rate_per_s
        self.burst = burst
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._last = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(float(self.burst), self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)


@dataclass(frozen=True)
class BlockHeader:
    number: int
    timestamp: int
    base_fee_wei: int
    hash: str


def block_param(block: int | str) -> str:
    """Block numbers are always sent as hex; tags ('latest', ...) pass through."""
    if isinstance(block, bool):
        raise TypeError("block must be int or str")
    if isinstance(block, int):
        if block < 0:
            raise ValueError("negative block number")
        return hex(block)
    return block


def _block_label(method: str, params: list) -> str:
    try:
        if method in ("eth_call", "eth_getCode", "eth_getBalance", "eth_getStorageAt"):
            return str(params[1] if method != "eth_getStorageAt" else params[2])
        if method == "eth_getBlockByNumber":
            return str(params[0])
    except IndexError:
        pass
    return "-"


def _hex_to_bytes(h: Any) -> bytes:
    if not isinstance(h, str) or not h.startswith("0x"):
        return b""
    try:
        return bytes.fromhex(h[2:] if len(h) % 2 == 0 else "0" + h[2:])
    except ValueError:
        return b""


def _to_int(v: Any, what: str, method: str) -> int:
    if isinstance(v, int):
        return v
    try:
        return int(v, 16)
    except (TypeError, ValueError):
        raise RpcError(method, None, f"bad {what}: {v!r}") from None


class ChainClient:
    def __init__(self, name: str, chain_id: int, rpc_url: str, limiter: RateLimiter, *,
                 transport: Transport | None = None, timeout_s: float = 10.0, max_retries: int = 4,
                 sleep=asyncio.sleep):
        self.name = name
        self.expected_chain_id = chain_id
        self.rpc_url = rpc_url
        self.limiter = limiter
        self.transport: Transport = transport or AiohttpTransport(timeout_s)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._id = 0
        self.calls = 0
        self.failures: list[str] = []

    # ---- core -----------------------------------------------------------------------------------------------
    def _fail(self, method: str, params: list, err: RpcError) -> RpcError:
        self.failures.append(f"{method}@{_block_label(method, params)}: {type(err).__name__}: {err.message[:200]}")
        return err

    async def request(self, method: str, params: list) -> Any:
        self.calls += 1
        last: RpcError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                await self._sleep(delay)
            delay = BACKOFFS_S[min(attempt, len(BACKOFFS_S) - 1)]
            await self.limiter.acquire()
            self._id += 1
            payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
            try:
                status, body, headers = await self.transport.post(self.rpc_url, payload)
            except (asyncio.TimeoutError, TimeoutError, OSError) as e:
                last = TransportError(method, None, f"{type(e).__name__}: {e}")
                continue
            except Exception as e:  # aiohttp.ClientError and friends, without importing aiohttp here
                if type(e).__module__.split(".")[0] in ("aiohttp", "asyncio"):
                    last = TransportError(method, None, f"{type(e).__name__}: {e}")
                    continue
                raise
            if status == 429:
                last = RateLimited(method, 429, "HTTP 429 Too Many Requests")
                delay = max(delay, self._retry_after(headers))
                continue
            if status >= 500:
                last = TransportError(method, status, f"HTTP {status}")
                delay = max(delay, self._retry_after(headers))
                continue
            if status != 200:
                raise self._fail(method, params, TransportError(method, status, f"HTTP {status}: {str(body)[:120]}"))
            err = body.get("error")
            if err:
                exc = self._classify(method, err)
                if isinstance(exc, RateLimited):
                    last = exc
                    continue
                raise self._fail(method, params, exc)
            if "result" not in body:
                raise self._fail(method, params, RpcError(method, None, f"malformed response: {str(body)[:120]}"))
            return body["result"]
        assert last is not None
        raise self._fail(method, params, last)

    @staticmethod
    def _retry_after(headers: dict[str, str]) -> float:
        try:
            return min(float(headers.get("retry-after", "0")), MAX_RETRY_AFTER_S)
        except ValueError:
            return 0.0

    @staticmethod
    def _classify(method: str, err: dict) -> RpcError:
        code = err.get("code")
        msg = str(err.get("message", ""))
        data = err.get("data")
        low = msg.lower()
        if code == -32005 or "rate limit" in low or "too many requests" in low:
            return RateLimited(method, code, msg)
        if any(m in low for m in STATE_UNAVAILABLE_MARKERS):
            return StateUnavailable(method, code, msg)
        if code == 3 or "execution reverted" in low or "revert" in low:
            raw = _hex_to_bytes(data) if isinstance(data, str) else b""
            return ExecutionReverted(method, code, msg, raw)
        return RpcError(method, code, msg)

    # ---- typed helpers --------------------------------------------------------------------------------------
    async def chain_id(self) -> int:
        return _to_int(await self.request("eth_chainId", []), "chain id", "eth_chainId")

    async def block_number(self) -> int:
        return _to_int(await self.request("eth_blockNumber", []), "block number", "eth_blockNumber")

    async def header(self, block: int | str) -> BlockHeader:
        res = await self.request("eth_getBlockByNumber", [block_param(block), False])
        if not res:
            raise self._fail("eth_getBlockByNumber", [block_param(block), False],
                             StateUnavailable("eth_getBlockByNumber", None, "block not found"))
        if res.get("baseFeePerGas") is None:
            raise self._fail("eth_getBlockByNumber", [block_param(block), False],
                             RpcError("eth_getBlockByNumber", None, "block has no baseFeePerGas"))
        return BlockHeader(number=_to_int(res["number"], "number", "eth_getBlockByNumber"),
                           timestamp=_to_int(res["timestamp"], "timestamp", "eth_getBlockByNumber"),
                           base_fee_wei=_to_int(res["baseFeePerGas"], "baseFeePerGas", "eth_getBlockByNumber"),
                           hash=str(res.get("hash", "")))

    async def eth_call(self, to: str | None, data: bytes, block: int | str, *,
                       from_: str = SIM_CALLER, gas: int | None = None) -> bytes:
        call: dict[str, str] = {"from": from_, "data": "0x" + data.hex()}
        if to is not None:
            call["to"] = to
        if gas is not None:
            call["gas"] = hex(gas)
        res = await self.request("eth_call", [call, block_param(block)])
        return _hex_to_bytes(res)

    async def get_code(self, address: str, block: int | str = "latest") -> bytes:
        return _hex_to_bytes(await self.request("eth_getCode", [address, block_param(block)]))

    def drain_failures(self) -> list[str]:
        out, self.failures = self.failures, []
        return out

    async def close(self) -> None:
        closer = getattr(self.transport, "close", None)
        if closer is not None:
            await closer()


def resolve_rpc_url(cfg: "DefiChainCfg") -> str:
    """Env var first (RPC URLs with keys live in .env), then the public default."""
    return os.environ.get(cfg.rpc_url_env) or cfg.rpc_url_default


async def connect(name: str, cfg: "DefiChainCfg", expected_chain_id: int, *,
                  transport: Transport | None = None, sleep=asyncio.sleep) -> ChainClient:
    client = ChainClient(name, expected_chain_id, resolve_rpc_url(cfg), RateLimiter(cfg.max_rps, cfg.burst),
                         transport=transport, sleep=sleep)
    try:
        got = await client.chain_id()
    except BaseException:
        await client.close()
        raise
    if got != expected_chain_id:
        await client.close()
        raise RpcError("eth_chainId", None, f"{name}: RPC reports chain id {got}, expected {expected_chain_id}")
    return client
