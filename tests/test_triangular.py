import pytest

from arb.config import load_settings
from arb.costs import CostModel
from arb.scanners.triangular import scan
from tests.fakes import FakeSource, spot_market

BIG = 1000.0


def build(btc_usdt, eth_usdt, eth_btc, eth_bids_size=BIG):
    """Each arg is (bid, ask). Books are deep unless stated."""
    src = FakeSource()
    for base, quote in [("BTC", "USDT"), ("ETH", "USDT"), ("ETH", "BTC")]:
        src.add_market("binance", spot_market(base, quote))
    for sym, (bid, ask) in {"BTC/USDT": btc_usdt, "ETH/USDT": eth_usdt, "ETH/BTC": eth_btc}.items():
        size = eth_bids_size if sym == "ETH/USDT" else BIG
        src.add_book("binance", sym, [(bid, size)], [(ask, BIG)])
    return src


async def run(src):
    s = load_settings()
    return await scan(src, s, CostModel(s), 7)


async def test_forward_cycle_detected():
    # USDT -> BTC (buy @50000) -> ETH (buy with BTC @0.04) -> USDT (sell @2100)
    src = build((49990, 50000), (2100, 2101), (0.0399, 0.04))
    (o,) = await run(src)
    assert o.key == "binance USDT->BTC->ETH->USDT"
    assert [(l.symbol, l.side) for l in o.legs] == [("BTC/USDT", "buy"), ("ETH/BTC", "buy"), ("ETH/USDT", "sell")]
    final = 300 / 50000 * 0.999 / 0.04 * 0.999 * 2100 * 0.999
    assert o.notional_usd == 300 and o.capacity_usd == 300
    assert o.net_pnl_usd == pytest.approx(final - 300)
    assert o.costs_usd["slippage"] == pytest.approx(0.0)
    assert o.costs_usd["fees"] > 0
    assert o.gross_pnl_usd == pytest.approx(o.net_pnl_usd + o.costs_usd["fees"])
    assert o.net_bps == pytest.approx((final - 300) / 300 * 1e4)
    assert o.net_after_tax_usd == pytest.approx((final - 300) * (1 - 0.312))


async def test_reverse_direction_detected():
    # USDT -> ETH (buy @2000) -> BTC (sell ETH/BTC @0.0425) -> USDT (sell BTC @50000)
    src = build((50000, 50010), (1999, 2000), (0.0425, 0.0426))
    (o,) = await run(src)
    assert o.key == "binance USDT->ETH->BTC->USDT"
    assert [(l.symbol, l.side) for l in o.legs] == [("ETH/USDT", "buy"), ("ETH/BTC", "sell"), ("BTC/USDT", "sell")]
    final = 300 / 2000 * 0.999 * 0.0425 * 0.999 * 50000 * 0.999
    assert o.net_pnl_usd == pytest.approx(final - 300)


async def test_fair_prices_rejected_after_fees():
    # no-fee product is exactly 1; 3 x 0.1% fees make it a loss in both directions
    assert await run(build((50000, 50000), (2000, 2000), (0.04, 0.04))) == []


async def test_depth_limits_notional():
    # 300 USDT needs ~0.1497 ETH of bids; only 0.08 available -> falls back to 150
    src = build((49990, 50000), (2100, 2101), (0.0399, 0.04), eth_bids_size=0.08)
    (o,) = await run(src)
    assert o.notional_usd == 150 and o.capacity_usd == 150


async def test_slippage_counted():
    src = build((49990, 50000), (2100, 2101), (0.0399, 0.04))
    src.add_book("binance", "ETH/USDT", [(2100, 0.05), (2090, BIG)], [(2101, BIG)])
    (o,) = await run(src)
    assert o.costs_usd["slippage"] > 0
    assert o.gross_pnl_usd == pytest.approx(o.net_pnl_usd + o.costs_usd["fees"] + o.costs_usd["slippage"])


async def test_failing_book_and_disabled():
    class Flaky(FakeSource):
        async def order_book(self, exchange, symbol, depth=20):
            raise RuntimeError("boom")

    src = build((49990, 50000), (2100, 2101), (0.0399, 0.04))
    flaky = Flaky()
    flaky.__dict__.update(src.__dict__)
    assert await run(flaky) == []
    s = load_settings()
    s.scanners.triangular.enabled = False
    assert await scan(src, s, CostModel(s), 0) == []
