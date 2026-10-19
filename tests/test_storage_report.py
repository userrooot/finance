from datetime import date

from arb.models import Leg, Opportunity
from arb.report import build_report, write_report
from arb.storage import OpportunityStore

TS = 1_767_225_600_000  # 2026-01-01 00:00:00 UTC
DAY = date(2026, 1, 1)


def _opp(strategy="basis", key="BTC", ts=TS, bps=20.0, tax=2.0):
    return Opportunity(strategy, ts, key, [Leg("binance", "BTC/USDT", "spot", "buy", 1.0, 1.0)], 300, 3, {"fees": 1},
                       2, tax, bps, 300, "7d", 12.5)


def test_roundtrip(tmp_path):
    st = OpportunityStore(tmp_path)
    st.append([_opp(), _opp(ts=TS + 1000)])
    st.log_scan(TS, {"basis": 1}, ["boom"], 1.5)
    df = st.load_opportunities(DAY)
    assert len(df) == 2 and df["legs"][0][0]["side"] == "buy"
    sc = st.load_scans(DAY)
    assert sc["errors"][0] == ["boom"]
    assert st.load_opportunities(date(2026, 1, 2)).empty
    assert "strategy" in st.load_opportunities(date(2026, 1, 2)).columns


def test_report(tmp_path):
    st = OpportunityStore(tmp_path)
    for i in range(4):
        st.log_scan(TS + i, {}, ["e"] if i == 0 else [], 1.0)
    st.append([_opp(ts=TS), _opp(ts=TS + 1), _opp("stat_arb", "A/B", TS + 2, 5.0, 0.5)])
    md = build_report(st, DAY)
    assert "Scan cycles: 4" in md and "Errors: 1" in md
    assert "| basis |" in md and "| stat_arb |" in md
    assert md.index("| basis | BTC |") < md.index("| stat_arb | A/B |")
    assert "How to read" in md
    p = write_report(st, DAY, tmp_path / "rep")
    assert p.exists()


def test_empty_report(tmp_path):
    md = build_report(OpportunityStore(tmp_path), DAY)
    assert "No opportunities passed the cost filters" in md
