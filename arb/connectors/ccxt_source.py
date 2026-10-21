"""MarketData implementation over ccxt (public endpoints only, no API keys)."""
from __future__ import annotations

import asyncio
import logging
import time

import ccxt.async_support as ccxt

from arb.models import FundingInfo, OrderBook

log = logging.getLogger(__name__)


class CcxtSource:
    def __init__(self, exchanges: list[str], concurrency: int = 8):
        self.exchanges = exchanges
        self._clients = {name: getattr(ccxt, name)({"enableRateLimit": True, "aiohttp_trust_env": True}) for name in exchanges}
        self._markets: dict[str, dict[str, dict]] = {}
        self._sem = asyncio.Semaphore(concurrency)

    async def markets(self, exchange: str) -> dict[str, dict]:
        if exchange not in self._markets:
            self._markets[exchange] = await self._clients[exchange].load_markets()
        return self._markets[exchange]

    async def order_book(self, exchange: str, symbol: str, depth: int = 20) -> OrderBook:
        async with self._sem:
            ob = await self._clients[exchange].fetch_order_book(symbol, limit=depth)
        # Derivative books (e.g. OKX swaps) quote size in contracts; scanners expect base units.
        market = (await self.markets(exchange)).get(symbol, {})
        mult = float(market.get("contractSize") or 1.0) if market.get("contract") else 1.0
        return OrderBook(
            exchange=exchange,
            symbol=symbol,
            bids=[(float(p), float(q) * mult) for p, q, *_ in ob["bids"]],
            asks=[(float(p), float(q) * mult) for p, q, *_ in ob["asks"]],
            timestamp_ms=ob.get("timestamp") or int(time.time() * 1000),
        )

    async def tickers(self, exchange: str, symbols: list[str] | None = None) -> dict[str, dict]:
        async with self._sem:
            return await self._clients[exchange].fetch_tickers(symbols)

    async def funding(self, exchange: str, symbols: list[str]) -> list[FundingInfo]:
        client = self._clients[exchange]
        async with self._sem:
            raw = await client.fetch_funding_rates(symbols)
        out = []
        for sym, fr in raw.items():
            if fr.get("fundingRate") is None:
                continue
            out.append(
                FundingInfo(
                    exchange=exchange,
                    symbol=sym,
                    rate=float(fr["fundingRate"]),
                    interval_hours=_interval_hours(fr.get("interval")),
                    mark_price=float(fr.get("markPrice") or 0.0),
                    index_price=float(fr["indexPrice"]) if fr.get("indexPrice") else None,
                    next_funding_ms=fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp"),
                    avg_rate_7d=await self._avg_funding_7d(exchange, sym),
                )
            )
        return out

    async def _avg_funding_7d(self, exchange: str, symbol: str) -> float | None:
        since = int((time.time() - 7 * 86400) * 1000)
        try:
            async with self._sem:
                hist = await self._clients[exchange].fetch_funding_rate_history(symbol, since=since, limit=100)
        except Exception as e:  # history endpoint is optional; scanners fall back to the current rate
            log.debug("funding history unavailable for %s %s: %s", exchange, symbol, e)
            return None
        rates = [float(h["fundingRate"]) for h in hist if h.get("fundingRate") is not None]
        return sum(rates) / len(rates) if rates else None

    async def funding_history(self, exchange: str, symbol: str, since_ms: int) -> list[tuple[int, float]]:
        client = self._clients[exchange]
        out: dict[int, float] = {}
        since = since_ms
        for _ in range(50):  # pagination safety cap
            async with self._sem:
                batch = await client.fetch_funding_rate_history(symbol, since=since, limit=1000)
            rows = [(int(h["timestamp"]), float(h["fundingRate"])) for h in batch
                    if h.get("timestamp") is not None and h.get("fundingRate") is not None]
            new = [r for r in rows if r[0] not in out]
            if not new:
                break
            out.update(new)
            since = max(r[0] for r in rows) + 1
        return sorted(out.items())

    async def ohlcv(self, exchange: str, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        client = self._clients[exchange]
        tf_ms = client.parse_timeframe(timeframe) * 1000
        since = int(time.time() * 1000) - limit * tf_ms
        rows: list[list[float]] = []
        while len(rows) < limit:
            async with self._sem:
                batch = await client.fetch_ohlcv(symbol, timeframe, since=since, limit=min(1000, limit - len(rows)))
            if not batch:
                break
            rows.extend(batch)
            since = batch[-1][0] + tf_ms
        return rows[-limit:]

    async def close(self) -> None:
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)


def _interval_hours(interval: str | None) -> float:
    if not interval:
        return 8.0
    unit, n = interval[-1], float(interval[:-1])
    return n if unit == "h" else n / 60 if unit == "m" else n * 24
