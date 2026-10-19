import pytest

from arb.config import load_settings
from arb.costs import CostModel, crossing_capacity, walk_book, walk_book_quote

ASKS = [(100.0, 1.0), (101.0, 2.0), (102.0, 5.0)]


def test_walk_book_vwap_across_levels():
    f = walk_book(ASKS, 2.0)
    assert f.qty == 2.0
    assert f.avg_price == pytest.approx((100 + 101) / 2)


def test_walk_book_partial_when_thin():
    f = walk_book(ASKS, 10.0)
    assert f.qty == 8.0


def test_walk_book_quote():
    f = walk_book_quote(ASKS, 302.0)  # 100 + 2*101
    assert f.qty == pytest.approx(3.0)
    assert f.notional == pytest.approx(302.0)


def test_crossing_capacity_stops_when_fees_eat_edge():
    asks = [(100.0, 1.0), (100.5, 1.0)]
    bids = [(101.0, 1.5), (100.6, 5.0)]
    qty, profit = crossing_capacity(asks, bids, 0.001, 0.001)
    # unit 1: 101*.999 - 100*1.001 = 0.799 ; unit 1.5 slice: 0.5 @ (101 vs 100.5) = 100.899-100.6005=0.2985
    # next: 100.6*.999 - 100.5*1.001 = 100.4994-100.6005 < 0 -> stop
    assert qty == pytest.approx(1.5)
    assert profit == pytest.approx(0.799 + 0.5 * 0.2985)


def test_crossing_capacity_none_when_no_edge():
    assert crossing_capacity([(100.0, 1)], [(100.05, 1)], 0.001, 0.001) == (0.0, 0.0)


def test_tax_only_on_gains():
    cm = CostModel(load_settings())
    assert cm.after_tax(-5.0) == -5.0
    assert cm.after_tax(10.0) == pytest.approx(10.0 * (1 - 0.312))


def test_fee_lookup():
    cm = CostModel(load_settings())
    assert cm.fee_rate("binance", "swap") == 0.0005
    assert cm.fee("okx", "spot", 1000.0, taker=False) == pytest.approx(0.8)
