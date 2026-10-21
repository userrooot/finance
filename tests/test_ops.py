"""Ops tooling: tax export, state backups, daily summary."""
import csv
import json
from datetime import datetime, timezone

from arb.paper import runner
from arb.tax_export import export_tax
from tests.test_paper import Notes, make


def ms(y, mo, d, h=0, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def fill(ts, gid="g1", side="buy"):
    return {"group_id": gid, "exchange": "binance", "symbol": "BTC/USDT", "market": "spot", "side": side, "qty": 2.0,
            "price": 100.0, "fee": 0.2, "ts_ms": ts, "requested_qty": 2.0, "reduce_only": False, "tag": "t",
            "maker": True}


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_tax_export(tmp_path):
    sd = tmp_path / "s"
    sd.mkdir()
    fills = [
        fill(ms(2025, 12, 31, 18, 29)),   # 23:59 IST on 31 Dec 2025 -> 2025
        fill(ms(2025, 12, 31, 18, 30)),   # 00:00 IST on 1 Jan 2026 -> 2026
        fill(ms(2026, 12, 31, 18, 30)),   # 1 Jan 2027 IST -> excluded
    ]
    (sd / "fills.jsonl").write_text("\n".join(json.dumps(f) for f in fills) + "\n")
    trades = [
        {"group_id": "a", "tag": "t", "open_ms": ms(2025, 12, 31, 10), "close_ms": ms(2025, 12, 31, 18, 30),
         "pnl": 10.0, "fees": 1.0, "funding": 3.0},
        {"group_id": "b", "tag": "t", "open_ms": ms(2026, 3, 1), "close_ms": ms(2026, 3, 2), "pnl": -4.0,
         "fees": 1.0, "funding": 0.5},
        {"group_id": "c", "tag": "t", "open_ms": ms(2026, 3, 1), "close_ms": ms(2027, 3, 2), "pnl": 5.0,
         "fees": 1.0, "funding": 0.5},
    ]
    (sd / "state.json").write_text(json.dumps({"portfolio": {"closed_trades": trades}}))
    out = export_tax(sd, 2026, tmp_path / "r" / "tax")
    f = rows(f"{out}_fills.csv")
    assert [r["date_ist"] for r in f] == ["2026-01-01"]
    assert float(f[0]["notional_usdt"]) == 200.0 and f[0]["maker"] == "True"
    t = rows(f"{out}_trades.csv")
    assert [r["group_id"] for r in t] == ["a", "b"]  # a closes 00:00 IST 1 Jan 2026
    assert t[0]["open_date_ist"] == "2025-12-31" and t[0]["close_date_ist"] == "2026-01-01"
    assert float(t[0]["taxable_gain_usdt"]) == 10.0 and float(t[0]["loss_not_set_off_usdt"]) == 0.0
    assert float(t[1]["taxable_gain_usdt"]) == 0.0 and float(t[1]["loss_not_set_off_usdt"]) == -4.0
    assert "115BBH" in (tmp_path / "r" / "tax_README.txt").read_text()
    assert [r["date_ist"] for r in rows(f"{export_tax(sd, 2025, tmp_path / 'r' / 'y25')}_fills.csv")] == ["2025-12-31"]


async def test_backup_once_per_day_and_retention(tmp_path):
    t, _, _, clock = make(tmp_path)
    t._persist()
    t._persist()
    b = tmp_path / "backups"
    assert len(list(b.glob("state-*.json"))) == 1
    first = next(b.glob("state-*.json"))
    first.write_text("marker")
    t._persist()
    assert first.read_text() == "marker"  # not overwritten the same day
    for _ in range(40):
        clock.t += 86_400
        t._persist()
    files = sorted(p.name for p in b.glob("state-*.json"))
    assert len(files) == runner.BACKUP_KEEP
    assert files[-1] == datetime.fromtimestamp(clock.t, tz=timezone.utc).strftime("state-%Y-%m-%d.json")
    assert "marker" not in (b / files[0]).read_text()


async def test_backup_failure_swallowed(tmp_path, monkeypatch):
    t, _, _, _ = make(tmp_path)
    monkeypatch.setattr(runner.shutil, "copyfile", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    t._persist()
    assert t.state_path.exists()


async def test_daily_summary_once_per_day(tmp_path):
    notes = Notes()
    t, _, _, clock = make(tmp_path, notifier=notes)
    await t.run_once()
    summaries = [m for m in notes.msgs if m.startswith("DAILY SUMMARY")]
    assert len(summaries) == 1 and "equity" in summaries[0] and "risk ok" in summaries[0]
    assert json.loads(t.state_path.read_text())["last_summary_day"]
    clock.t += 600
    await t.run_once()
    assert len([m for m in notes.msgs if m.startswith("DAILY SUMMARY")]) == 1
    clock.t += 86_400
    await t.run_once()
    assert len([m for m in notes.msgs if m.startswith("DAILY SUMMARY")]) == 2


async def test_daily_summary_persisted_across_restart(tmp_path):
    notes = Notes()
    t, _, _, clock = make(tmp_path, notifier=notes)
    await t.run_once()
    t2, _, _, _ = make(tmp_path, clock=clock, notifier=notes)
    await t2.run_once()
    assert len([m for m in notes.msgs if m.startswith("DAILY SUMMARY")]) == 1


async def test_daily_summary_errors_swallowed(tmp_path):
    t, _, _, _ = make(tmp_path, notifier=Notes(fail=True))
    res = await t.run_once()
    assert res is not None
    assert t.state_path.exists()
