"""DefiStore: the daily-JSONL opportunity store plus per-block samples, replay simulations and run metadata."""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from arb.storage import OpportunityStore, _day

log = logging.getLogger("arb.defi")

BLOCK_COLUMNS = ["chain", "block", "timestamp", "candidates", "screened_in", "gross_positive", "surviving_keys",
                 "best_net_usd", "errors", "complete"]
SIM_COLUMNS = ["chain", "key", "block_found", "offset", "block_sim", "success", "revert_reason", "profit_raw",
               "net_usd", "gas_estimate_tx", "gas_cost_usd", "predicted_net_usd", "calldata_hash"]
META_COLUMNS = ["ts_ms", "kind"]


class DefiStore(OpportunityStore):
    """base_dir = f"{storage_dir}/{chain}"; reuses _append/_load and the daily JSONL layout."""

    def __init__(self, storage_dir: str | Path, chain: str):
        super().__init__(Path(storage_dir) / chain)
        self.chain = chain

    def _append_rows(self, kind: str, rows: list[dict], ts_key: str, scale: int) -> None:
        by_day: dict[date, list[dict]] = {}
        for r in rows:
            by_day.setdefault(_day(int(r[ts_key]) * scale), []).append(r)
        for day, recs in by_day.items():
            self._append(self._path(kind, day), recs)

    @staticmethod
    def _append(path: Path, records: list[dict]) -> None:
        """One write per call, and a torn last line from a killed run is terminated first, so the next record
        never gets glued onto it."""
        if not records:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(rec, default=str) + "\n" for rec in records)
        with open(path, "a+b") as f:
            if f.tell() > 0:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    text = "\n" + text
            f.write(text.encode("utf-8"))
            f.flush()

    @staticmethod
    def _load(path: Path, columns: list[str]) -> pd.DataFrame:
        """Like OpportunityStore._load, but a torn line (the process was killed mid-append, the next run appended
        after it) is skipped and counted instead of making the whole day unreadable (review T4, issue R-02).
        `pd.DataFrame.attrs["skipped_lines"]` says how many lines were dropped."""
        if not path.exists():
            return pd.DataFrame(columns=columns)
        rows, bad = [], 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1
        if bad:
            log.warning("%s: skipped %d unreadable line(s)", path, bad)
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=columns)
        df.attrs["skipped_lines"] = bad
        return df

    def append_blocks(self, rows: list[dict]) -> None:
        self._append_rows("blocks", rows, "timestamp", 1000)  # block timestamp is in seconds

    def append_sims(self, rows: list[dict]) -> None:
        self._append_rows("sims", rows, "timestamp", 1000)  # timestamp of block_found, seconds

    def append_meta(self, rows: list[dict]) -> None:
        self._append_rows("meta", rows, "ts_ms", 1)

    def load_blocks(self, day: date) -> pd.DataFrame:
        return self._load(self._path("blocks", day), BLOCK_COLUMNS)

    def load_sims(self, day: date) -> pd.DataFrame:
        return self._load(self._path("sims", day), SIM_COLUMNS)

    def load_meta(self, day: date) -> pd.DataFrame:
        return self._load(self._path("meta", day), META_COLUMNS)
