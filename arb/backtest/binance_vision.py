"""Import history from the Binance public archive (data.binance.vision) into HistoryStore parquet files.

Only the archive is used (the Binance API may be geo-blocked). Spot and USD-M perp 1h klines plus
USD-M funding rates. Missing files (404) are skipped, never fatal.
"""
from __future__ import annotations

import csv
import io
import logging
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from arb.backtest.data import FUNDING_COLS, OHLCV_COLS, HistoryStore, safe_symbol

log = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision/data/"
MAX_WORKERS = 8
RETRIES = 3
TIMEOUT_S = 60


def default_fetch(url: str) -> bytes | None:
    """GET url; None on 404, retry with backoff on other errors, raise after RETRIES attempts."""
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err: Exception = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            err = e
        if attempt == RETRIES - 1:
            raise err
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _rows(blob: bytes) -> list[list[str]]:
    """CSV rows of the first file in a zip, header row (non-numeric first cell) dropped."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8")
    rows = [r for r in csv.reader(io.StringIO(text)) if r]
    if rows:
        try:
            float(rows[0][0])
        except ValueError:
            rows = rows[1:]
    return rows


def parse_klines(blob: bytes) -> pd.DataFrame:
    out = []
    for r in _rows(blob):
        ts = int(r[0])
        if ts > 10 ** 14:  # microseconds (spot since 2025)
            ts //= 1000
        out.append((ts, *(float(x) for x in r[1:6])))
    return pd.DataFrame(out, columns=OHLCV_COLS)


def parse_funding(blob: bytes) -> pd.DataFrame:
    out = []
    for r in _rows(blob):
        ts = int(r[0])
        out.append((int(round(ts / 1000)) * 1000, float(r[2])))  # calc_time has a few ms of jitter
    return pd.DataFrame(out, columns=FUNDING_COLS)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _complete_months(today: date, months: int) -> list[str]:
    cur, out = _month_start(today), []
    for _ in range(months):
        cur = _month_start(cur - timedelta(days=1))
        out.append(cur.strftime("%Y-%m"))
    return out[::-1]


def _kline_urls(symbol: str, perp: bool, timeframe: str, months: list[str], days: list[date]) -> list[str]:
    root = f"{BASE_URL}{'futures/um' if perp else 'spot'}"
    mk = lambda kind, stamp: f"{root}/{kind}/klines/{symbol}/{timeframe}/{symbol}-{timeframe}-{stamp}.zip"
    return [mk("monthly", m) for m in months] + [mk("daily", d.isoformat()) for d in days]


def import_history(store: HistoryStore, quotes: list[tuple[str, str, str]], funding: list[tuple[str, str]],
                   months: int, timeframe: str = "1h", fetch: Callable[[str], bytes | None] | None = None,
                   today: date | None = None) -> dict[str, int]:
    """Download archive files and merge them into the store. Returns total rows per file key
    (`<exchange>/<safe_symbol>_<timeframe>` or `<exchange>/<safe_symbol>_funding`)."""
    if timeframe != "1h":
        raise ValueError(f"binance-vision import supports only timeframe '1h', got {timeframe!r}")
    fetch = fetch or default_fetch
    today = today or pd.Timestamp.now("UTC").date()
    month_list = _complete_months(today, months)
    first_day = _month_start(today)
    days = [first_day + timedelta(days=i) for i in range((today - first_day).days)]  # up to yesterday

    jobs: list[tuple[str, str, object, list[str], Callable[[bytes], pd.DataFrame], list[str]]] = []
    for ex, sym, _market in quotes:
        if ex != "binance":
            log.info("binance-vision: skipping %s %s (only binance supported)", ex, sym)
            continue
        base, _, settle = sym.partition(":")
        perp = bool(settle)
        api_sym = base.replace("/", "")
        key = f"{ex}/{safe_symbol(sym)}_{timeframe}"
        jobs.append((key, sym, store.ohlcv_path(ex, sym, timeframe),
                     _kline_urls(api_sym, perp, timeframe, month_list, days), parse_klines, ex))
    for ex, sym in funding:
        if ex != "binance":
            log.info("binance-vision: skipping funding %s %s (only binance supported)", ex, sym)
            continue
        api_sym = sym.partition(":")[0].replace("/", "")
        urls = [f"{BASE_URL}futures/um/monthly/fundingRate/{api_sym}/{api_sym}-fundingRate-{m}.zip"
                for m in month_list]
        log.info("binance-vision: funding for current month %s not in the archive yet (no daily files)",
                 today.strftime("%Y-%m"))
        jobs.append((f"{ex}/{safe_symbol(sym)}_funding", sym, store.funding_path(ex, sym), urls,
                     parse_funding, ex))

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for key, sym, path, urls, parse, _ex in jobs:
            blobs = list(pool.map(fetch, urls))
            frames = []
            for url, blob in zip(urls, blobs):
                if blob is None:
                    log.info("binance-vision: not found, skipping %s", url)
                    continue
                frames.append(parse(blob))
            frames = [f for f in frames if not f.empty]
            if frames:
                df = store.merge_save(path, pd.concat(frames, ignore_index=True))
            else:
                log.warning("binance-vision: no data for %s", key)
                df = HistoryStore._read(path)
            counts[key] = 0 if df is None else len(df)
    return counts
