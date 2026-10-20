"""Funding carry: long spot + short perp when trailing realised funding beats costs.

Decisions use only realised funding settlements already delivered in snapshots (never the current
funding quote), so there is no lookahead. What is open is derived from the Portfolio (group ids
prefixed `fc:{exchange}:{base}:`), so a restarted process resumes correctly; the only strategy
state is the rolling funding history (state_dict/load_state). Spot is never shorted.
"""
from __future__ import annotations

from collections import deque

from arb.config import FundingCarryStrategyCfg
from arb.core.portfolio import Portfolio
from arb.core.types import MarketSnapshot, OrderIntent, Requirements
from arb.costs import CostModel
from arb.strategies.base import Strategy

DAY_MS = 86_400_000
DEFAULT_INTERVAL_H = 8.0
BALANCE_TOL = 0.01  # max |spot + perp| / spot qty before a group counts as unbalanced


class FundingCarry(Strategy):
    name = "funding_carry"

    def __init__(self, cfg: FundingCarryStrategyCfg, costs: CostModel):
        self.cfg = cfg
        self.costs = costs
        self.history: dict[tuple[str, str], deque[float]] = {}

    # --- symbols / history ---------------------------------------------------------------
    @staticmethod
    def spot_symbol(base: str) -> str:
        return f"{base}/USDT"

    @staticmethod
    def perp_symbol(base: str) -> str:
        return f"{base}/USDT:USDT"

    def _hist(self, exchange: str, symbol: str) -> deque[float]:
        h = self.history.get((exchange, symbol))
        if h is None:
            h = self.history[(exchange, symbol)] = deque(maxlen=self.cfg.avg_window)
        return h

    def seed_funding_history(self, exchange: str, symbol: str, rates: list[float]) -> None:
        """Warm up with past realised rates (oldest first); the newest avg_window are kept."""
        h = self._hist(exchange, symbol)
        h.extend(rates)

    def requirements(self) -> Requirements:
        ex = self.cfg.exchange
        quotes, funding = [], []
        for b in self.cfg.bases:
            quotes += [(ex, self.spot_symbol(b), "spot"), (ex, self.perp_symbol(b), "swap")]
            funding.append((ex, self.perp_symbol(b)))
        return Requirements(quotes=quotes, funding=funding)

    # --- decisions -----------------------------------------------------------------------
    def on_snapshot(self, snapshot: MarketSnapshot, portfolio: Portfolio) -> list[OrderIntent]:
        ex = self.cfg.exchange
        for ev in snapshot.funding_events:
            if ev.exchange == ex:
                self._hist(ex, ev.symbol).append(ev.rate)
        intents: list[OrderIntent] = []
        for base in self.cfg.bases:
            intents += self._on_base(base, snapshot, portfolio)
        return intents

    def gross_ann_pct(self, snapshot: MarketSnapshot, base: str) -> float | None:
        """Trailing mean realised funding, annualised (percent); None without history."""
        ex, perp = self.cfg.exchange, self.perp_symbol(base)
        h = self.history.get((ex, perp))
        if not h:
            return None
        fq = snapshot.funding.get((ex, perp))
        interval = fq.interval_hours if fq is not None else DEFAULT_INTERVAL_H
        return sum(h) / len(h) * (24 / interval) * 365 * 100

    def net_ann_pct(self, gross_ann_pct: float, snapshot: MarketSnapshot, base: str) -> float | None:
        """Gross minus the round-trip cost amortised over max_hold_days.

        Conservative: assumes the position is held only max_hold_days, so costs are never
        spread over a longer holding period than the strategy would allow.
        """
        ex = self.cfg.exchange
        spot = snapshot.quote(ex, self.spot_symbol(base))
        perp = snapshot.quote(ex, self.perp_symbol(base))
        if spot is None or perp is None:
            return None
        cost_frac = (2 * self.costs.fee_rate(ex, "spot") + 2 * self.costs.fee_rate(ex, "swap")
                     + (spot.ask - spot.bid) / spot.mid + (perp.ask - perp.bid) / perp.mid)
        return gross_ann_pct - cost_frac * 365 / self.cfg.max_hold_days * 100

    def _on_base(self, base: str, snapshot: MarketSnapshot, portfolio: Portfolio) -> list[OrderIntent]:
        cfg, ex = self.cfg, self.cfg.exchange
        spot_sym, perp_sym = self.spot_symbol(base), self.perp_symbol(base)
        gross = self.gross_ann_pct(snapshot, base)
        open_groups = portfolio.open_groups(f"fc:{ex}:{base}:")

        if open_groups:
            out: list[OrderIntent] = []
            for gid in open_groups:
                positions = portfolio.group_positions(gid)
                reason = self._exit_reason(positions, gross, snapshot.ts_ms)
                if reason:
                    out += [OrderIntent(gid, p.exchange, p.symbol, p.market, "sell" if p.qty > 0 else "buy",
                                        abs(p.qty), reduce_only=True, tag="exit") for p in positions]
            return out

        h = self.history.get((ex, perp_sym))
        if gross is None or h is None or len(h) < cfg.avg_window:
            return []
        net = self.net_ann_pct(gross, snapshot, base)
        spot = snapshot.quote(ex, spot_sym)
        perp = snapshot.quote(ex, perp_sym)
        if net is None or net < cfg.entry_annualized_pct or spot is None or perp is None or spot.ask <= 0:
            return []
        qty = cfg.notional_usd / spot.ask
        gid = f"fc:{ex}:{base}:{snapshot.ts_ms}"
        return [OrderIntent(gid, ex, spot_sym, "spot", "buy", qty, tag="entry"),
                OrderIntent(gid, ex, perp_sym, "swap", "sell", qty, tag="entry")]

    def _exit_reason(self, positions: list, gross: float | None, ts_ms: int) -> str | None:
        cfg = self.cfg
        spot_qty = sum(p.qty for p in positions if p.market == "spot")
        perp_qty = sum(p.qty for p in positions if p.market == "swap")
        if spot_qty <= 0 or abs(spot_qty + perp_qty) > BALANCE_TOL * spot_qty:
            return "unbalanced"
        if (ts_ms - min(p.opened_ms for p in positions)) / DAY_MS >= cfg.max_hold_days:
            return "max_hold"
        if gross is not None and gross < cfg.exit_annualized_pct:
            return "funding"
        return None

    # --- persistence ---------------------------------------------------------------------
    def state_dict(self) -> dict:
        return {"funding_history": {f"{ex}|{sym}": list(h) for (ex, sym), h in self.history.items()}}

    def load_state(self, state: dict) -> None:
        self.history = {}
        for key, rates in state.get("funding_history", {}).items():
            ex, sym = key.split("|", 1)
            self._hist(ex, sym).extend(rates)
