"""Binance public-archive importer tests with an injected fake fetch (no network)."""
import io
import zipfile
from datetime import date

import pandas as pd
import pytest

from arb.backtest.binance_vision import import_history
from arb.backtest.data import HistoryStore

H = 3_600_000
BASE = "https://data.binance.vision/data/"
TODAY = date(2026, 10, 4)
OCT1 = int(pd.Timestamp("2026-10-01", tz="UTC").timestamp() * 1000)
SEP1 = int(pd.Timestamp("2026-09-01", tz="UTC").timestamp() * 1000)
KL_HEADER = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tb,tbq,ignore\n"


def zipped(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("f.csv", text)
    return buf.getvalue()


def kl(t0, n, header=False, scale=1, close=1.5):
    body = "".join(f"{(t0 + i * H) * scale},1.0,2.0,0.5,{close + i},10.0,{(t0 + i * H + H - 1) * scale},1,1,1,1,0\n"
                   for i in range(n))
    return zipped((KL_HEADER if header else "") + body)


def fund(rows, header=True):
    return zipped(("calc_time,funding_interval_hours,last_funding_rate\n" if header else "")
                  + "".join(f"{t},8,{r}\n" for t, r in rows))


class Fake:
    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, url):
        self.calls.append(url)
        return self.files.get(url)


def test_spot_no_header_micros_and_current_month_daily(tmp_path):
    store = HistoryStore(tmp_path)
    fake = Fake({
        BASE + "spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-09.zip": kl(SEP1, 3, scale=1000),
        BASE + "spot/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10-01.zip": kl(OCT1, 2, scale=1000),
        BASE + "spot/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10-03.zip": kl(OCT1 + 48 * H, 1),  # ms, old style
    })
    counts = import_history(store, [("binance", "BTC/USDT", "spot")], [], months=2, fetch=fake, today=TODAY)
    assert counts == {"binance/BTC_USDT_1h": 6}
    df = store.load_ohlcv("binance", "BTC/USDT", "1h")
    assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
    assert df["ts"].tolist()[0] == SEP1 and df["ts"].dtype.kind == "i" and df["ts"].is_monotonic_increasing
    # daily only for Oct 1..3 (yesterday), monthly for Aug + Sep; 404s skipped
    urls = set(fake.calls)
    assert BASE + "spot/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10-03.zip" in urls
    assert not any("2026-10-04" in u or "monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-10" in u for u in urls)
    assert any("2026-08.zip" in u for u in urls)


def test_perp_header_and_funding_rounding(tmp_path):
    store = HistoryStore(tmp_path)
    fake = Fake({
        BASE + "futures/um/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-09.zip": kl(SEP1, 4, header=True),
        BASE + "futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2026-09.zip":
            fund([(SEP1 + 5, 0.0001), (SEP1 + 8 * H - 3, 0.0002)]),
    })
    counts = import_history(store, [("binance", "BTC/USDT:USDT", "perp")], [("binance", "BTC/USDT:USDT")],
                            months=1, fetch=fake, today=TODAY)
    assert counts == {"binance/BTC_USDT_USDT_1h": 4, "binance/BTC_USDT_USDT_funding": 2}
    f = store.load_funding("binance", "BTC/USDT:USDT")
    assert list(f.columns) == ["ts", "rate"]
    assert f["ts"].tolist() == [SEP1, SEP1 + 8 * H]
    assert f["rate"].tolist() == [0.0001, 0.0002]
    assert store.load_ohlcv("binance", "BTC/USDT:USDT", "1h")["close"].iloc[0] == 1.5
    assert any("futures/um/daily/klines" in u for u in fake.calls)
    assert not any("daily/fundingRate" in u for u in fake.calls)


def test_merge_with_existing_dedupes(tmp_path):
    store = HistoryStore(tmp_path)
    path = store.ohlcv_path("binance", "ETH/USDT", "1h")
    path.parent.mkdir(parents=True)
    pd.DataFrame({"ts": [SEP1 - H, SEP1], "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 9.0}
                 ).to_parquet(path, index=False)
    fake = Fake({BASE + "spot/monthly/klines/ETHUSDT/1h/ETHUSDT-1h-2026-09.zip": kl(SEP1, 3, close=7.0)})
    counts = import_history(store, [("binance", "ETH/USDT", "spot")], [], months=1, fetch=fake, today=TODAY)
    assert counts["binance/ETH_USDT_1h"] == 4
    df = store.load_ohlcv("binance", "ETH/USDT", "1h")
    assert df["ts"].is_unique and df["ts"].is_monotonic_increasing
    assert df["close"].tolist() == [9.0, 7.0, 8.0, 9.0]  # new rows replace the overlapping one


def test_404_and_non_binance_skipped(tmp_path):
    store = HistoryStore(tmp_path)
    fake = Fake({})
    counts = import_history(store, [("binance", "NEW/USDT", "spot"), ("okx", "BTC/USDT", "spot")],
                            [("okx", "BTC/USDT:USDT")], months=1, fetch=fake, today=TODAY)
    assert counts == {"binance/NEW_USDT_1h": 0}
    assert not any("okx" in u or "BTCUSDT" in u for u in fake.calls)
    assert not store.ohlcv_path("binance", "NEW/USDT", "1h").exists()


def test_only_1h_supported(tmp_path):
    with pytest.raises(ValueError, match="1h"):
        import_history(HistoryStore(tmp_path), [], [], months=1, timeframe="5m", fetch=Fake({}), today=TODAY)
