import pytest

from arb.config import load_settings
from arb.costs import CostModel
from arb.scanners.cross_exchange import scan
from tests.fakes import FakeSource, spot_market


def make(books: dict[str, tuple], symbol="ETH/USDT", base="ETH", ts=None):
    src = FakeSource()
    for ex, (bids, asks) in books.items():
        src.add_market(ex, spot_market(base))
        src.add_book(ex, symbol, bids, asks, (ts or {}).get(ex, 0))
    return src


async def run(src):
    s = load_settings()
    return await scan(src, s, CostModel(s), 123)


async def test_profitable_hand_checked():
    src = make({"binance": ([(1999.0, 5)], [(2000.0, 0.1), (2001.0, 1.0)]),
                "okx": ([(2100.0, 1.0)], [(2101.0, 1.0)])})
    opps = await run(src)
    assert [o.key for o in opps] == ["ETH/USDT binance->okx"]
    o = opps[0]
    qty = 0.15  # 300 / 2000
    assert o.legs[0].side == "buy" and o.legs[1].side == "sell"
    assert o.legs[0].qty == pytest.approx(qty)
    assert o.gross_pnl_usd == pytest.approx(qty * 100)  # 15
    assert o.costs_usd["slippage"] == pytest.approx(0.05)  # 0.05 ETH filled at 2001
    assert o.costs_usd["fees"] == pytest.approx(0.001 * 300.05 + 0.001 * 315.0)
    assert o.costs_usd["rebalance"] == pytest.approx(0.0008 * 2000 + 1.0)
    assert o.net_pnl_usd == pytest.approx(15 - 0.05 - 0.61505 - 2.6)
    assert o.details["net_before_rebalance_usd"] == pytest.approx(15 - 0.05 - 0.61505)
    assert o.net_after_tax_usd == pytest.approx(o.net_pnl_usd * (1 - 0.312))
    assert o.notional_usd == pytest.approx(300.05)
    assert o.net_bps == pytest.approx(o.net_pnl_usd / 300.05 * 1e4)
    assert o.capacity_usd == pytest.approx(1.0 * 2000)  # limited by 1 ETH of okx bids
    assert o.holding == "instant"


async def test_fees_kill_positive_spread():
    # 10 bps spread < 20 bps of taker fees
    src = make({"binance": ([(1999.0, 5)], [(2000.0, 5)]), "okx": ([(2002.0, 5)], [(2003.0, 5)])})
    assert await run(src) == []


async def test_partial_depth_limits_capacity():
    src = make({"binance": ([(1999.0, 5)], [(2000.0, 0.1), (2100.0, 5)]),
                "okx": ([(2050.0, 10)], [(2060.0, 10)])})
    (o,) = await run(src)
    assert o.capacity_usd == pytest.approx(0.1 * 2000)
    assert o.legs[0].qty == pytest.approx(0.1)
    assert o.notional_usd == pytest.approx(200.0)


async def test_missing_symbol_skipped():
    src = make({"binance": ([(1999.0, 5)], [(2000.0, 5)]), "okx": ([(2100.0, 5)], [(2101.0, 5)])})
    src.add_market("bybit", spot_market("BTC"))  # bybit has no ETH/USDT
    opps = await run(src)
    assert {o.key for o in opps} == {"ETH/USDT binance->okx"}


async def test_failing_order_book_does_not_crash():
    class Flaky(FakeSource):
        async def order_book(self, exchange, symbol, depth=20):
            if exchange == "bybit":
                raise RuntimeError("boom")
            return await super().order_book(exchange, symbol, depth)

    src = Flaky()
    for ex, (bids, asks) in {"binance": ([(1999.0, 5)], [(2000.0, 5)]),
                             "okx": ([(2100.0, 5)], [(2101.0, 5)]),
                             "bybit": ([(1.0, 1)], [(1.0, 1)])}.items():
        src.add_market(ex, spot_market("ETH"))
        src.add_book(ex, "ETH/USDT", bids, asks)
    opps = await run(src)
    assert [o.key for o in opps] == ["ETH/USDT binance->okx"]


async def test_stale_note_and_disabled():
    src = make({"binance": ([(1999.0, 5)], [(2000.0, 5)]), "okx": ([(2100.0, 5)], [(2101.0, 5)])},
               ts={"binance": 0, "okx": 5000})
    (o,) = await run(src)
    assert any("stale" in n for n in o.risk_notes)
    s = load_settings()
    s.scanners.cross_exchange.enabled = False
    assert await scan(src, s, CostModel(s), 0) == []
