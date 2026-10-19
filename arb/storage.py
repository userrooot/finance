"""Append-only JSONL storage for opportunities and scan-cycle logs."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from arb.models import Opportunity

OPP_COLUMNS = ["strategy", "timestamp_ms", "key", "legs", "notional_usd", "gross_pnl_usd", "costs_usd",
               "net_pnl_usd", "net_after_tax_usd", "net_bps", "capacity_usd", "holding",
               "annualized_pct", "risk_notes", "details"]
SCAN_COLUMNS = ["ts_ms", "counts", "errors", "duration_s"]


def _day(ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).date()


class OpportunityStore:
    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir)

    def _path(self, kind: str, day: date) -> Path:
        return self.base / kind / f"{day.isoformat()}.jsonl"

    @staticmethod
    def _append(path: Path, records: list[dict]) -> None:
        if not records:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, default=str) + "\n")

    @staticmethod
    def _load(path: Path, columns: list[str]) -> pd.DataFrame:
        if not path.exists():
            return pd.DataFrame(columns=columns)
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        if not rows:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(rows)

    def append(self, opps: list[Opportunity]) -> None:
        by_day: dict[date, list[dict]] = {}
        for opp in opps:
            by_day.setdefault(_day(opp.timestamp_ms), []).append(opp.to_record())
        for day, recs in by_day.items():
            self._append(self._path("opportunities", day), recs)

    def log_scan(self, ts_ms: int, counts: dict[str, int], errors: list[str], duration_s: float) -> None:
        rec = {"ts_ms": ts_ms, "counts": counts, "errors": errors, "duration_s": duration_s}
        self._append(self._path("scans", _day(ts_ms)), [rec])

    def load_opportunities(self, day: date) -> pd.DataFrame:
        return self._load(self._path("opportunities", day), OPP_COLUMNS)

    def load_scans(self, day: date) -> pd.DataFrame:
        return self._load(self._path("scans", day), SCAN_COLUMNS)
