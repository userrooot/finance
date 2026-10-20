"""In-memory MarketData for scanner unit tests. Values are hand-built test fixtures, not market data."""
from __future__ import annotations

from arb.models import FundingInfo, OrderBook


def spot_market(base: str, quote: str = "USDT") -> dict:
    return {"symbol": f"{base}/{quote}", "base": base, "quote": quote, "type": "spot", "active": True,
            "linear": None, "settle": None, "expiry": None, "contractSize": None}


def perp_market(base: str, quote: str = "USDT", contract_size: float = 1.0) -> dict:
    return {"symbol": f"{base}/{quote}:{quote}", "base": base, "quote": quote, "type": "swap", "active": True,
            "linear": True, "settle": quote, "expiry": None, "contractSize": contract_size}


def future_market(base: str, expiry_ms: int, code: str, quote: str = "USDT") -> dict:
    return {"symbol": f"{base}/{quote}:{quote}-{code}", "base": base, "quote": quote, "type": "future",
            "active": True, "linear": True, "settle": quote, "expiry": expiry_ms, "contractSize": 1.0}


class FakeSource:
    def __init__(self):
        self.exchanges: list[str] = []
        self._markets: dict[str, dict[str, dict]] = {}
        self._books: dict[tuple[str, str], OrderBook] = {}
        self._funding: dict[str, list[FundingInfo]] = {}
        self._ohlcv: dict[tuple[str, str], list[list[float]]] = {}
        self._funding_hist: dict[tuple[str, str], list[tuple[int, float]]] = {}

    def add_market(self, exchange: str, market: dict) -> None:
        if exchange not in self.exchanges:
            self.exchanges.append(exchange)
        self._markets.setdefault(exchange, {})[market["symbol"]] = market

    def add_book(self, exchange: str, symbol: str, bids, asks, ts: int = 0) -> None:
        self._books[(exchange, symbol)] = OrderBook(exchange, symbol, list(bids), list(asks), ts)

    def add_funding(self, info: FundingInfo) -> None:
        self._funding.setdefault(info.exchange, []).append(info)

    def add_ohlcv(self, exchange: str, symbol: str, rows: list[list[float]]) -> None:
        self._ohlcv[(exchange, symbol)] = rows

    def add_funding_history(self, exchange: str, symbol: str, rows: list[tuple[int, float]]) -> None:
        self._funding_hist[(exchange, symbol)] = list(rows)

    async def markets(self, exchange):
        return self._markets.get(exchange, {})

    async def order_book(self, exchange, symbol, depth=20):
        return self._books[(exchange, symbol)]

    async def tickers(self, exchange, symbols=None):
        out = {}
        for (ex, sym), b in self._books.items():
            if ex == exchange and (symbols is None or sym in symbols):
                out[sym] = {"symbol": sym, "bid": b.best_bid, "ask": b.best_ask,
                            "bidVolume": b.bids[0][1] if b.bids else None,
                            "askVolume": b.asks[0][1] if b.asks else None,
                            "last": b.best_bid}
        return out

    async def funding(self, exchange, symbols):
        return [f for f in self._funding.get(exchange, []) if f.symbol in symbols]

    async def funding_history(self, exchange, symbol, since_ms):
        return sorted(r for r in self._funding_hist.get((exchange, symbol), []) if r[0] >= since_ms)

    async def ohlcv(self, exchange, symbol, timeframe, limit):
        return self._ohlcv[(exchange, symbol)][-limit:]

    async def close(self):
        pass
