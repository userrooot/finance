"""chain.py: rate limiter timing (fake clock), retries and backoff, error classification, failure tracking."""
import asyncio

import pytest

from arb.defi.chain import (ChainClient, ExecutionReverted, RateLimited, RateLimiter, RpcError, StateUnavailable,
                            TransportError, block_param, connect)
from tests.defi_fakes import FakeClock, FakeNode, FakeTransport, defi_cfg, make_client, ok, rpc_err


async def test_rate_limiter_burst_then_refill():
    clock = FakeClock()
    lim = RateLimiter(2.0, 3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await lim.acquire()
    assert clock.sleeps == []  # the burst is free
    await lim.acquire()  # needs one full token: 0.5 s at 2/s
    assert clock.sleeps == [0.5]
    await lim.acquire()
    assert clock.t == pytest.approx(1.0)
    clock.t += 100  # idle time refills only up to the burst size
    for _ in range(3):
        await lim.acquire()
    n = len(clock.sleeps)
    await lim.acquire()
    assert len(clock.sleeps) == n + 1


async def test_rate_limiter_rejects_bad_config():
    with pytest.raises(ValueError):
        RateLimiter(0, 1)


async def test_every_request_goes_through_the_limiter():
    clock = FakeClock()
    t = FakeTransport(handler=lambda p: ok("0x1"))
    c = make_client(t, rate=1.0, burst=1, clock=clock)
    await c.block_number()
    await c.block_number()
    await c.block_number()
    assert clock.t == pytest.approx(2.0)  # 1 rps, burst 1


async def test_success_and_hex_block_params():
    t = FakeTransport(handler=lambda p: ok("0x"))
    c = make_client(t)
    await c.eth_call("0xabc", b"\x01\x02", 255)
    req = t.requests[0]
    assert req["method"] == "eth_call" and req["params"][1] == "0xff"
    assert req["params"][0]["from"] == "0x000000000000000000000000000000000000dead"
    assert req["params"][0]["data"] == "0x0102"
    assert block_param(10) == "0xa" and block_param("latest") == "latest"
    await c.eth_call(None, b"", "latest", gas=25_000_000)
    assert "to" not in t.requests[1]["params"][0] and t.requests[1]["params"][0]["gas"] == hex(25_000_000)


async def test_retry_backoff_on_429_then_success_honours_retry_after():
    t = FakeTransport([(429, {}, {"retry-after": "3"}), (429, {}, {}), ok("0x10")])
    c = make_client(t)
    assert await c.block_number() == 16
    assert c.clock.sleeps[:2] == [3.0, 1.0]  # Retry-After beats the 0.5 s backoff; then the 1 s step
    assert c.failures == [] and c.calls == 1


async def test_429_exhausts_retries_and_is_recorded():
    t = FakeTransport(handler=lambda p: (429, {}, {}))
    c = make_client(t)
    with pytest.raises(RateLimited):
        await c.block_number()
    assert c.clock.sleeps == [0.5, 1.0, 2.0, 4.0]
    assert len(t.requests) == 5
    assert len(c.failures) == 1 and c.failures[0].startswith("eth_blockNumber@-: RateLimited:")


async def test_rpc_32005_is_rate_limit_and_retried():
    t = FakeTransport([rpc_err(-32005, "rate limit exceeded"), ok("0x2")])
    c = make_client(t)
    assert await c.block_number() == 2


async def test_5xx_and_timeouts_retry_then_raise_transport_error():
    t = FakeTransport([(502, {}, {}), asyncio.TimeoutError(), ConnectionResetError("boom"), (503, {}, {}), (500, {}, {})])
    c = make_client(t)
    with pytest.raises(TransportError):
        await c.chain_id()
    assert len(t.requests) == 5 and len(c.failures) == 1


async def test_timeout_then_success():
    t = FakeTransport([asyncio.TimeoutError(), ok("0xa4b1")])
    c = make_client(t)
    assert await c.chain_id() == 42161


async def test_state_unavailable_is_not_retried():
    for msg in ("missing trie node abc", "header not found", "historical state is not available", "state is pruned"):
        t = FakeTransport([rpc_err(-32000, msg)])
        c = make_client(t)
        with pytest.raises(StateUnavailable):
            await c.eth_call("0xabc", b"", 5)
        assert len(t.requests) == 1 and c.clock.sleeps == []
        assert c.failures[0].startswith("eth_call@0x5: StateUnavailable:")


async def test_revert_carries_data_and_is_not_retried():
    t = FakeTransport([rpc_err(3, "execution reverted", "0x08c379a0" + "00" * 4)])
    c = make_client(t)
    with pytest.raises(ExecutionReverted) as e:
        await c.eth_call("0xabc", b"", "latest")
    assert e.value.data == bytes.fromhex("08c379a0") + bytes(4)
    assert len(t.requests) == 1
    t = FakeTransport([rpc_err(-32000, "execution reverted")])
    c = make_client(t)
    with pytest.raises(ExecutionReverted) as e:
        await c.eth_call("0xabc", b"", "latest")
    assert e.value.data == b""


async def test_other_rpc_errors_and_malformed_bodies_raise_plain_rpc_error():
    c = make_client(FakeTransport([rpc_err(-32602, "invalid params")]))
    with pytest.raises(RpcError) as e:
        await c.block_number()
    assert type(e.value) is RpcError and e.value.code == -32602
    c = make_client(FakeTransport([(200, {"jsonrpc": "2.0", "id": 1}, {})]))
    with pytest.raises(RpcError):
        await c.block_number()
    c = make_client(FakeTransport([(403, {}, {})]))
    with pytest.raises(TransportError):
        await c.block_number()  # policy denial: no retry
    assert len(c.transport.requests) == 1


async def test_failure_messages_are_truncated_and_drained():
    t = FakeTransport([rpc_err(-32000, "x" * 1000)])
    c = make_client(t)
    with pytest.raises(RpcError):
        await c.block_number()
    assert len(c.failures[0]) < 300
    assert c.drain_failures() and c.failures == []


async def test_header_parsing_and_missing_block():
    node = FakeNode()
    c = make_client(node)
    h = await c.header(7)
    assert (h.number, h.timestamp, h.base_fee_wei) == (7, node.ts0 + 7, node.base_fee)
    c2 = make_client(FakeTransport([ok(None)]))
    with pytest.raises(StateUnavailable):
        await c2.header(7)
    c3 = make_client(FakeTransport([ok({"number": "0x1", "timestamp": "0x2"})]))
    with pytest.raises(RpcError):
        await c3.header(1)  # no base fee: cannot price gas


async def test_connect_checks_chain_id(monkeypatch):
    cfg = defi_cfg().chains["arbitrum"]
    monkeypatch.delenv(cfg.rpc_url_env, raising=False)
    node = FakeNode()
    client = await connect("arbitrum", cfg, 42161, transport=node)
    assert client.rpc_url == "http://fake"
    monkeypatch.setenv(cfg.rpc_url_env, "http://keyed.example/abc")
    client = await connect("arbitrum", cfg, 42161, transport=node)
    assert client.rpc_url == "http://keyed.example/abc"  # env var wins over the default
    with pytest.raises(RpcError):
        await connect("arbitrum", cfg, 1, transport=node)


async def test_connect_allows_a_hardhat_fork_only_when_asked(monkeypatch):
    cfg = defi_cfg().chains["arbitrum"]
    monkeypatch.delenv(cfg.rpc_url_env, raising=False)
    fork = FakeTransport(handler=lambda p: ok(hex(31337)))
    with pytest.raises(RpcError):
        await connect("arbitrum", cfg, 42161, transport=fork)
    client = await connect("arbitrum", cfg, 42161, transport=fork, allow_chain_ids=(31337,))
    assert await client.chain_id() == 31337
