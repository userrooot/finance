import pytest

from arb.config import load_settings
from arb.costs import CostModel
from arb.models import FundingInfo
from arb.scanners import funding
from tests.fakes import FakeSource, perp_market, spot_market

NOW = 1_000_000_000_000
SETTINGS = load_settings()
COSTS = CostModel(SETTINGS)
SPOT, PERP = "BTC/USDT", "BTC/USDT:USDT"


def info(ex: str, rate: float, interval: float = 8.0, avg: float | None = None) -> FundingInfo:
    return FundingInfo(ex, PERP, rate, interval, 100.0, 100.0, None, avg)


def carry_source(rate: float, avg: float | None = None, ex: str = "binance") -> FakeSource:
    s = FakeSource()
    s.add_market(ex, spot_market("BTC"))
    s.add_market(ex, perp_market("BTC"))
    s.add_book(ex, SPOT, [(99.9, 100)], [(100.0, 100)])
    s.add_book(ex, PERP, [(100.1, 100)], [(100.2, 100)])
    s.add_funding(info(ex, rate, avg=avg))
    return s


async def test_positive_carry_math():
    opps = await funding.scan(carry_source(0.0005), SETTINGS, COSTS, NOW)
    assert [o.strategy for o in opps] == ["funding_carry"]
    o = opps[0]
    # N=300, qty=3, 21 periods: gross = 300*0.0005*21
    assert o.gross_pnl_usd == pytest.approx(3.15)
    # fees: 0.3 + 3*100.1*0.0005 + 3*99.9*0.001 + 3*100.2*0.0005
    assert o.costs_usd["fees"] == pytest.approx(0.3 + 0.15015 + 0.2997 + 0.1503)
    assert o.costs_usd["spread"] == pytest.approx(0.6)
    assert o.net_pnl_usd == pytest.approx(3.15 - 0.90015 - 0.6)
    assert o.net_after_tax_usd == pytest.approx(o.net_pnl_usd * (1 - 0.312))
    assert o.annualized_pct == pytest.approx(o.net_pnl_usd / 450 * 365 / 7 * 100)
    assert o.key == "binance BTC" and o.holding == "7d"
    assert [(l.market, l.side) for l in o.legs] == [("spot", "buy"), ("swap", "sell")]
    assert o.capacity_usd == 2400.0 or o.capacity_usd == 300.0 * 8  # books hold 100 BTC
    assert o.details["breakeven_days"] == pytest.approx(1.50015 / (300 * 0.0005 * 3))


async def test_negative_funding_skipped():
    assert await funding.scan(carry_source(-0.0005), SETTINGS, COSTS, NOW) == []


async def test_tiny_funding_rejected_after_costs():
    assert await funding.scan(carry_source(0.00001), SETTINGS, COSTS, NOW) == []
    # passes the gross-only annualized check (~11%) but not once costs are subtracted
    assert await funding.scan(carry_source(0.0000211), SETTINGS, COSTS, NOW) == []


async def test_avg_rate_preferred_over_current():
    assert await funding.scan(carry_source(0.0005, avg=0.00001), SETTINGS, COSTS, NOW) == []
    opps = await funding.scan(carry_source(0.00001, avg=0.0005), SETTINGS, COSTS, NOW)
    assert len(opps) == 1
    assert opps[0].details["rate_used"] == 0.0005 and opps[0].details["rate"] == 0.00001


async def test_funding_spread_normalizes_intervals():
    s = FakeSource()
    for ex in ("binance", "bybit"):
        s.add_market(ex, perp_market("BTC"))
        s.add_book(ex, PERP, [(100.0, 100)], [(100.0, 100)])
    s.add_funding(info("binance", 0.0004, 8.0))  # 0.00005 / h
    s.add_funding(info("bybit", 0.0004, 4.0))  # 0.0001 / h
    opps = await funding.scan(s, SETTINGS, COSTS, NOW)
    assert [o.strategy for o in opps] == ["funding_spread"]
    o = opps[0]
    assert o.key == "BTC long binance short bybit"
    assert o.gross_pnl_usd == pytest.approx(300 * 0.00005 * 168)
    assert o.costs_usd["fees"] == pytest.approx(2 * 300 * 0.0005 + 2 * 300 * 0.00055)
    assert o.costs_usd["spread"] == pytest.approx(0.0)
    assert o.net_pnl_usd == pytest.approx(2.52 - 0.63)
    assert o.annualized_pct == pytest.approx(o.net_pnl_usd / 300 * 365 / 7 * 100)
    assert (o.legs[0].exchange, o.legs[0].side) == ("binance", "buy")
    assert (o.legs[1].exchange, o.legs[1].side) == ("bybit", "sell")


async def test_funding_spread_equal_hourly_rates_none():
    s = FakeSource()
    for ex in ("binance", "bybit"):
        s.add_market(ex, perp_market("BTC"))
        s.add_book(ex, PERP, [(100.0, 100)], [(100.0, 100)])
    s.add_funding(info("binance", 0.0008, 8.0))
    s.add_funding(info("bybit", 0.0004, 4.0))
    assert await funding.scan(s, SETTINGS, COSTS, NOW) == []


async def test_source_errors_do_not_crash():
    s = carry_source(0.0005)  # binance fine

    class Boom(FakeSource):
        async def funding(self, exchange, symbols):
            if exchange == "okx":
                raise RuntimeError("boom")
            return await super().funding(exchange, symbols)

    b = Boom()
    b._markets, b._books, b._funding, b.exchanges = s._markets, s._books, s._funding, ["okx", "binance"]
    b.add_market("okx", spot_market("BTC"))
    b.add_market("okx", perp_market("BTC"))
    opps = await funding.scan(b, SETTINGS, COSTS, NOW)
    assert [o.key for o in opps] == ["binance BTC"]

    # missing order book for a candidate is skipped too
    del s._books[("binance", PERP)]
    assert await funding.scan(s, SETTINGS, COSTS, NOW) == []


async def test_disabled():
    st = load_settings()
    st.scanners.funding.enabled = False
    assert await funding.scan(carry_source(0.0005), st, CostModel(st), NOW) == []
