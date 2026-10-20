"""HistoryStore tests with a fake ccxt client (no network)."""
import pytest

from arb.backtest.data import HistoryStore

H = 3_600_000
T0 = 1_700_000_000_000


class FakeClient:
    def __init__(self, n=2500, max_page=1000):
        self.n, self.max_page, self.calls, self.closed = n, max_page, [], False
        self.ts = [T0 + i * H for i in range(n)]

    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls.append(since)
        rows = [[t, 1.0 + i, 2.0, 0.5, 1.5 + i, 10.0] for i, t in enumerate(self.ts) if t >= since]
        return rows[:min(limit, self.max_page)]

    async def fetch_funding_rate_history(self, symbol, since=None, limit=None):
        rows = [{"timestamp": t, "fundingRate": 0.0001 * (i % 3)} for i, t in enumerate(self.ts) if t >= since]
        return rows[:min(limit, 100)]

    async def close(self):
        self.closed = True


async def test_ohlcv_pagination_roundtrip_and_close(tmp_path):
    client = FakeClient()
    store = HistoryStore(tmp_path, client_factory=lambda ex: client)
    df = await store.download_ohlcv("binance", "BTC/USDT", "1h", T0, T0 + 2499 * H)
    assert len(df) == 2500 and df["ts"].is_monotonic_increasing and df["ts"].is_unique
    assert len(client.calls) == 3 and client.closed
    assert (tmp_path / "binance" / "BTC_USDT_1h.parquet").exists()
    loaded = store.load_ohlcv("binance", "BTC/USDT", "1h")
    assert list(loaded.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert loaded["ts"].dtype.kind == "i" and len(loaded) == 2500
    assert loaded["close"].iloc[5] == 6.5


async def test_until_bound_and_merge_dedupe(tmp_path):
    store = HistoryStore(tmp_path, client_factory=lambda ex: FakeClient())
    a = await store.download_ohlcv("binance", "BTC/USDT", "1h", T0, T0 + 99 * H)
    assert len(a) == 100
    b = await store.download_ohlcv("binance", "BTC/USDT", "1h", T0 + 50 * H, T0 + 149 * H)
    assert len(b) == 150 and b["ts"].is_unique and b["ts"].is_monotonic_increasing
    assert len(store.load_ohlcv("binance", "BTC/USDT", "1h")) == 150


async def test_funding_download_swap_symbol(tmp_path):
    store = HistoryStore(tmp_path, client_factory=lambda ex: FakeClient(n=450))
    df = await store.download_funding("binance", "BTC/USDT:USDT", T0, T0 + 449 * H)
    assert len(df) == 450  # paginated across 100-row pages
    assert (tmp_path / "binance" / "BTC_USDT_USDT_funding.parquet").exists()
    loaded = store.load_funding("binance", "BTC/USDT:USDT")
    assert list(loaded.columns) == ["ts", "rate"]
    assert loaded["rate"].iloc[1] == pytest.approx(0.0001)


async def test_stops_when_no_progress(tmp_path):
    class Stuck(FakeClient):
        async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            self.calls.append(since)
            return [[T0, 1, 1, 1, 1, 1]]  # always the same stale page

    client = Stuck()
    store = HistoryStore(tmp_path, client_factory=lambda ex: client)
    df = await store.download_ohlcv("binance", "BTC/USDT", "1h", T0, T0 + 10 * H)
    assert len(df) == 1 and len(client.calls) <= 2


def test_missing_files_raise_clear_error(tmp_path):
    store = HistoryStore(tmp_path)
    with pytest.raises(FileNotFoundError, match="no cached OHLCV"):
        store.load_ohlcv("binance", "BTC/USDT", "1h")
    with pytest.raises(FileNotFoundError, match="no cached funding"):
        store.load_funding("binance", "BTC/USDT:USDT")
