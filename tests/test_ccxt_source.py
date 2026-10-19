from arb.connectors.ccxt_source import CcxtSource


class _StubClient:
    async def fetch_order_book(self, symbol, limit=None):
        return {"bids": [[100.0, 5.0]], "asks": [[101.0, 3.0]], "timestamp": 1}

    async def close(self):
        pass


async def test_contract_books_converted_to_base_units():
    src = CcxtSource(["okx"])
    await src.close()
    src._clients["okx"] = _StubClient()
    src._markets["okx"] = {
        "BTC/USDT:USDT": {"contract": True, "contractSize": 0.01},
        "BTC/USDT": {"contract": False, "contractSize": None},
    }
    perp = await src.order_book("okx", "BTC/USDT:USDT")
    spot = await src.order_book("okx", "BTC/USDT")
    assert perp.bids == [(100.0, 0.05)] and perp.asks == [(101.0, 0.03)]
    assert spot.bids == [(100.0, 5.0)]
