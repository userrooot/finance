"""Historical data download (ccxt) and parquet cache for backtests."""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

import ccxt.async_support as ccxt_async
import pandas as pd

OHLCV_COLS = ["ts", "open", "high", "low", "close", "volume"]
FUNDING_COLS = ["ts", "rate"]
PAGE_LIMIT = 1000


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def _default_client(exchange: str) -> Any:
    return getattr(ccxt_async, exchange)({"enableRateLimit": True, "aiohttp_trust_env": True})  # honour HTTPS_PROXY


def _merge(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    df = new if old is None or old.empty else pd.concat([old, new], ignore_index=True)
    return df.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)


class HistoryStore:
    def __init__(self, base_dir: str | Path = "data/history", client_factory: Callable[[str], Any] | None = None):
        self.base = Path(base_dir)
        self.client_factory = client_factory or _default_client

    # --- paths ---------------------------------------------------------------------------
    def ohlcv_path(self, exchange: str, symbol: str, timeframe: str) -> Path:
        return self.base / exchange / f"{safe_symbol(symbol)}_{timeframe}.parquet"

    def funding_path(self, exchange: str, symbol: str) -> Path:
        return self.base / exchange / f"{safe_symbol(symbol)}_funding.parquet"

    # --- download ------------------------------------------------------------------------
    async def download_ohlcv(self, exchange: str, symbol: str, timeframe: str,
                             since_ms: int, until_ms: int) -> pd.DataFrame:
        def parse(rows: list) -> list[tuple]:
            return [(int(r[0]), *(float(x) for x in r[1:6])) for r in rows]

        async def fetch(client: Any, since: int) -> list[tuple]:
            return parse(await client.fetch_ohlcv(symbol, timeframe, since=since, limit=PAGE_LIMIT))

        rows = await self._paginate(exchange, fetch, since_ms, until_ms)
        path = self.ohlcv_path(exchange, symbol, timeframe)
        return self._save(path, _merge(self._read(path), pd.DataFrame(rows, columns=OHLCV_COLS)))

    async def download_funding(self, exchange: str, symbol: str, since_ms: int, until_ms: int) -> pd.DataFrame:
        async def fetch(client: Any, since: int) -> list[tuple]:
            rows = await client.fetch_funding_rate_history(symbol, since=since, limit=PAGE_LIMIT)
            return [(int(r["timestamp"]), float(r["fundingRate"])) for r in rows]

        rows = await self._paginate(exchange, fetch, since_ms, until_ms)
        path = self.funding_path(exchange, symbol)
        return self._save(path, _merge(self._read(path), pd.DataFrame(rows, columns=FUNDING_COLS)))

    async def _paginate(self, exchange: str, fetch: Callable, since_ms: int, until_ms: int) -> list[tuple]:
        client = self.client_factory(exchange)
        out: list[tuple] = []
        try:
            since = since_ms
            while since <= until_ms:
                page = await fetch(client, since)
                if not page:
                    break
                last = max(r[0] for r in page)
                out += [r for r in page if since_ms <= r[0] <= until_ms]
                if last < since:  # no progress: exchange ignored `since` or returned stale data
                    break
                since = last + 1
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                res = close()
                if inspect.isawaitable(res):
                    await res
        return out

    # --- load ----------------------------------------------------------------------------
    def load_ohlcv(self, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
        path = self.ohlcv_path(exchange, symbol, timeframe)
        df = self._read(path)
        if df is None:
            raise FileNotFoundError(f"no cached OHLCV for {exchange} {symbol} {timeframe} at {path}; run download first")
        return df[OHLCV_COLS]

    def load_funding(self, exchange: str, symbol: str) -> pd.DataFrame:
        path = self.funding_path(exchange, symbol)
        df = self._read(path)
        if df is None:
            raise FileNotFoundError(f"no cached funding for {exchange} {symbol} at {path}; run download first")
        return df[FUNDING_COLS]

    def merge_save(self, path: Path, new: pd.DataFrame) -> pd.DataFrame:
        """Merge `new` rows into the parquet at `path` (dedupe on ts, keep latest) and save."""
        return self._save(path, _merge(self._read(path), new))

    # --- io ------------------------------------------------------------------------------
    @staticmethod
    def _read(path: Path) -> pd.DataFrame | None:
        return pd.read_parquet(path) if path.exists() else None

    @staticmethod
    def _save(path: Path, df: pd.DataFrame) -> pd.DataFrame:
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
        return df
