"""DefiStore: the daily-JSONL opportunity store plus per-block samples, replay simulations and run metadata."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from arb.storage import OpportunityStore, _day

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
