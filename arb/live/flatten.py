"""Emergency flatten: close every perp position on an exchange and sell the spot the bot holds.

Perp positions are read from the exchange (whatever opened them). Spot is limited to what the
bot's own state says it bought, so coins the user holds for other reasons are left alone.
Orders are reduce-only taker orders sent through LiveBroker, split so each stays under the
live.max_order_usd cap.
"""
from __future__ import annotations

import time
from typing import Any

from arb.core.portfolio import Portfolio
from arb.core.types import MarketSnapshot, OrderIntent, Quote
from arb.costs import CostModel
from arb.execution.live_broker import LiveBroker

MAX_CHUNKS = 200


def _chunks(qty: float, price: float, max_usd: float) -> list[float]:
    per = max_usd * 0.98 / price
    n = min(MAX_CHUNKS, max(1, int(qty / per) + (1 if qty % per > 1e-12 else 0)))
    return [qty / n] * n


def flatten_all(exchange: str, client: Any, broker: LiveBroker, portfolio: Portfolio | None) -> list[str]:
    now = int(time.time() * 1000)
    log: list[str] = []
    todo: list[tuple[str, str, str, float]] = []  # (symbol, market, side, base qty)
    for pos in client.fetch_positions():
        contracts = float(pos.get("contracts") or 0.0)
        if not contracts:
            continue
        m = client.market(pos["symbol"])
        qty = contracts * (float(m.get("contractSize") or 1.0) if m.get("contract") else 1.0)
        todo.append((pos["symbol"], "swap", "buy" if pos.get("side") == "short" else "sell", qty))
    if portfolio is not None:
        for p in portfolio.positions.values():
            if p.exchange == exchange and p.market == "spot" and p.qty > 0:
                todo.append((p.symbol, "spot", "sell", p.qty))
    for symbol, market, side, qty in todo:
        last = float(client.fetch_ticker(symbol)["last"])
        snap = MarketSnapshot(now, {(exchange, symbol): Quote(last, last, now)})
        done = 0.0
        for part in _chunks(qty, last, broker.max_order_usd):
            [f] = broker.execute([OrderIntent("flatten", exchange, symbol, market, side, part,
                                              reduce_only=True, tag="flatten_all")], snap)
            done += f.qty
        log.append(f"{exchange} {market} {symbol}: {side} {done:.8f} of {qty:.8f}")
    log += [f"note: {e}" for e in broker.events]
    return log


def make_broker(clients: dict, settings) -> LiveBroker:
    return LiveBroker(clients, CostModel(settings), settings.live.max_order_usd, settings.live.order_timeout_s)
