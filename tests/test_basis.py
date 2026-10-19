import pytest

from arb.config import load_settings
from arb.costs import CostModel
from arb.scanners import basis
from tests.fakes import FakeSource, future_market, spot_market

NOW = 1_000_000_000_000
DAY = 86_400_000
SETTINGS = load_settings()
COSTS = CostModel(SETTINGS)
SPOT = "BTC/USDT"


def source(fut_bid: float, days: float = 30.0, fut_ask: float | None = None) -> FakeSource:
    s = FakeSource()
    fm = future_market("BTC", int(NOW + days * DAY), "261231")
    s.add_market("binance", spot_market("BTC"))
    s.add_market("binance", fm)
    s.add_book("binance", SPOT, [(99.9, 100)], [(100.0, 100)])
    s.add_book("binance", fm["symbol"], [(fut_bid, 100)], [(fut_ask or fut_bid + 0.1, 100)])
    return s


async def test_contango_found_with_math():
    opps = await basis.scan(source(102.0), SETTINGS, COSTS, NOW)
    assert len(opps) == 1
    o = opps[0]
    assert o.strategy == "basis" and o.key == "binance BTC/USDT:USDT-261231"
    # qty = 3; gross = 3*(102-100)
    assert o.gross_pnl_usd == pytest.approx(6.0)
    # fees: spot buy 0.3 + fut sell 306*0.0005 + spot sell 299.7*0.001 + settlement 306*0.0005
    assert o.costs_usd["fees"] == pytest.approx(0.3 + 0.153 + 0.2997 + 0.153)
    assert o.costs_usd["exit_spread"] == pytest.approx(0.15)
    assert o.net_pnl_usd == pytest.approx(6.0 - 0.9057 - 0.15)
    assert o.net_after_tax_usd == pytest.approx(o.net_pnl_usd * (1 - 0.312))
    assert o.annualized_pct == pytest.approx(o.net_pnl_usd / 450 * 365 / 30 * 100)
    assert o.holding == "30d to expiry"
    assert [(l.market, l.side) for l in o.legs] == [("spot", "buy"), ("future", "sell")]
    assert o.capacity_usd == 300.0 * 8


async def test_thin_contango_rejected_after_costs():
    assert await basis.scan(source(100.3), SETTINGS, COSTS, NOW) == []


async def test_backwardation_skipped():
    assert await basis.scan(source(99.0), SETTINGS, COSTS, NOW) == []


async def test_expiring_within_two_days_skipped():
    assert await basis.scan(source(102.0, days=1.5), SETTINGS, COSTS, NOW) == []


async def test_source_error_does_not_crash():
    s = source(102.0)
    s.add_market("okx", spot_market("BTC"))
    s.add_market("okx", future_market("BTC", NOW + 30 * DAY, "261231"))  # no books -> KeyError
    opps = await basis.scan(s, SETTINGS, COSTS, NOW)
    assert [o.key for o in opps] == ["binance BTC/USDT:USDT-261231"]


async def test_disabled():
    st = load_settings()
    st.scanners.basis.enabled = False
    assert await basis.scan(source(102.0), st, CostModel(st), NOW) == []
