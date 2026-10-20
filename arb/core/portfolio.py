"""Positions, cash and PnL accounting.

All instruments are linear and USDT-settled, so spot and perps share one model: buying moves
qty*price out of cash, selling moves it in, and equity = cash + sum(qty * mark). For perps this
gives the same PnL as margin accounting; margin/leverage limits are enforced by the RiskManager.

Positions are tracked per group (one hedged trade = one group), so each closed group is one
trade in the ledger with its fees and funding attributed to it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from arb.core.types import Fill, FundingEvent, Key, MarketSnapshot

EPS = 1e-12
PosKey = tuple[str, str, str, str]  # (group_id, exchange, symbol, market)


@dataclass
class Position:
    group_id: str
    exchange: str
    symbol: str
    market: str
    qty: float  # signed base units, + long / - short
    avg_price: float
    opened_ms: int


@dataclass
class ClosedTrade:
    group_id: str
    tag: str
    open_ms: int
    close_ms: int
    pnl: float  # net of fees, including funding
    fees: float
    funding: float


@dataclass
class _GroupStats:
    tag: str
    open_ms: int
    cash_flow: float = 0.0
    fees: float = 0.0
    funding: float = 0.0


class Portfolio:
    def __init__(self, initial_cash: float):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[PosKey, Position] = {}
        self.last_prices: dict[Key, float] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.fees_paid = 0.0
        self.funding_received = 0.0
        self.traded_notional = 0.0
        self._groups: dict[str, _GroupStats] = {}

    # --- updates -------------------------------------------------------------------------
    def apply_fill(self, f: Fill) -> None:
        if f.qty <= EPS:
            return
        flow = -f.signed_qty * f.price - f.fee
        self.cash += flow
        self.fees_paid += f.fee
        self.traded_notional += f.qty * f.price
        self.last_prices[(f.exchange, f.symbol)] = f.price
        g = self._groups.setdefault(f.group_id, _GroupStats(f.tag, f.ts_ms))
        g.cash_flow += flow
        g.fees += f.fee

        key = (f.group_id, f.exchange, f.symbol, f.market)
        pos = self.positions.get(key)
        if pos is None:
            self.positions[key] = Position(f.group_id, f.exchange, f.symbol, f.market, f.signed_qty, f.price, f.ts_ms)
        else:
            new_qty = pos.qty + f.signed_qty
            if pos.qty * f.signed_qty > 0:  # adding to the position
                pos.avg_price = (pos.avg_price * abs(pos.qty) + f.price * f.qty) / abs(new_qty)
            elif pos.qty * new_qty < 0:  # flipped through zero
                pos.avg_price = f.price
            pos.qty = new_qty
            if abs(pos.qty) <= EPS:
                del self.positions[key]
        self._maybe_close_group(f.group_id, f.ts_ms)

    def apply_funding(self, events: list[FundingEvent], snapshot: MarketSnapshot) -> None:
        """Perp funding: longs pay shorts when the rate is positive."""
        for ev in events:
            for pos in self.positions.values():
                if pos.market != "swap" or pos.exchange != ev.exchange or pos.symbol != ev.symbol:
                    continue
                payment = -pos.qty * self._mark(pos.exchange, pos.symbol, snapshot) * ev.rate
                self.cash += payment
                self.funding_received += payment
                g = self._groups[pos.group_id]
                g.cash_flow += payment
                g.funding += payment

    def record_equity(self, snapshot: MarketSnapshot) -> float:
        eq = self.equity(snapshot)
        self.equity_curve.append((snapshot.ts_ms, eq))
        return eq

    # --- queries -------------------------------------------------------------------------
    def _mark(self, exchange: str, symbol: str, snapshot: MarketSnapshot | None) -> float:
        q = snapshot.quote(exchange, symbol) if snapshot else None
        if q is not None:
            self.last_prices[(exchange, symbol)] = q.mid
            return q.mid
        return self.last_prices.get((exchange, symbol), 0.0)

    def equity(self, snapshot: MarketSnapshot | None = None) -> float:
        return self.cash + sum(p.qty * self._mark(p.exchange, p.symbol, snapshot) for p in self.positions.values())

    def gross_exposure(self, snapshot: MarketSnapshot | None = None) -> float:
        return sum(abs(p.qty) * self._mark(p.exchange, p.symbol, snapshot) for p in self.positions.values())

    def open_groups(self, prefix: str = "") -> list[str]:
        return sorted({p.group_id for p in self.positions.values() if p.group_id.startswith(prefix)})

    def group_positions(self, group_id: str) -> list[Position]:
        return [p for p in self.positions.values() if p.group_id == group_id]

    def net_qty(self, exchange: str, symbol: str, market: str) -> float:
        return sum(p.qty for p in self.positions.values()
                   if p.exchange == exchange and p.symbol == symbol and p.market == market)

    def group_pnl(self, group_id: str, snapshot: MarketSnapshot | None = None) -> float:
        """Unrealized + realized PnL of an open group (cash flows so far + mark value of its positions)."""
        g = self._groups.get(group_id)
        if g is None:
            return 0.0
        return g.cash_flow + sum(p.qty * self._mark(p.exchange, p.symbol, snapshot) for p in self.group_positions(group_id))

    def _maybe_close_group(self, group_id: str, ts_ms: int) -> None:
        if any(p.group_id == group_id for p in self.positions.values()):
            return
        g = self._groups.pop(group_id, None)
        if g is not None:
            self.closed_trades.append(ClosedTrade(group_id, g.tag, g.open_ms, ts_ms, g.cash_flow, g.fees, g.funding))

    # --- persistence ---------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "initial_cash": self.initial_cash, "cash": self.cash,
            "positions": [asdict(p) for p in self.positions.values()],
            "last_prices": [[ex, sym, px] for (ex, sym), px in self.last_prices.items()],
            "closed_trades": [asdict(t) for t in self.closed_trades],
            "equity_curve": self.equity_curve,
            "fees_paid": self.fees_paid, "funding_received": self.funding_received,
            "traded_notional": self.traded_notional,
            "groups": {gid: asdict(g) for gid, g in self._groups.items()},
        }

    @classmethod
    def from_dict(cls, d: dict) -> Portfolio:
        p = cls(d["initial_cash"])
        p.cash = d["cash"]
        for pos in d["positions"]:
            pos = Position(**pos)
            p.positions[(pos.group_id, pos.exchange, pos.symbol, pos.market)] = pos
        p.last_prices = {(ex, sym): px for ex, sym, px in d["last_prices"]}
        p.closed_trades = [ClosedTrade(**t) for t in d["closed_trades"]]
        p.equity_curve = [tuple(x) for x in d["equity_curve"]]
        p.fees_paid, p.funding_received = d["fees_paid"], d["funding_received"]
        p.traded_notional = d["traded_notional"]
        p._groups = {gid: _GroupStats(**g) for gid, g in d["groups"].items()}
        return p
