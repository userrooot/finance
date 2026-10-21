"""Paper trading: the shared TradingEngine driven by live public market data and simulated fills."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from arb.config import Settings
from arb.connectors.tracking import TrackingSource
from arb.core.engine import StepResult, TradingEngine
from arb.core.portfolio import Portfolio
from arb.core.risk import RiskManager
from arb.core.types import Fill, FundingEvent, FundingQuote, MarketSnapshot, Quote
from arb.costs import CostModel
from arb.execution.broker import SimBroker
from arb.strategies.base import Strategy

log = logging.getLogger(__name__)

WARMUP_FUNDING_DAYS = 14


class PaperTrader:
    LIVE = False  # LiveTrader sets this; paper must never be mistaken for live

    def __init__(self, settings: Settings, strategy: Strategy, source, state_dir: str | Path,
                 notifier=None, clock: Callable[[], float] = time.time, sleep=asyncio.sleep, broker=None):
        if settings.live_trading and not self.LIVE:
            raise RuntimeError("PaperTrader refuses to run with live_trading=True")
        self.settings = settings
        self.strategy = strategy
        self.source = TrackingSource(source)
        self.notifier = notifier
        self.clock = clock
        self._sleep = sleep
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"

        self.fresh = not self.state_path.exists()
        self.started_ms = int(clock() * 1000)
        self.last_ts: int | None = None
        # (exchange, symbol) -> [next_funding_ms, rate]
        self.tracker: dict[tuple[str, str], tuple[int, float]] = {}
        saved = None if self.fresh else json.loads(self.state_path.read_text())
        if saved:
            self.portfolio = Portfolio.from_dict(saved["portfolio"])
            self.risk = RiskManager(settings, self.portfolio.initial_cash, state=saved["risk"])
            strategy.load_state(saved.get("strategy") or {})
            self.tracker = {(ex, sym): (int(n), float(r)) for ex, sym, n, r in saved.get("funding_tracker", [])}
            self.last_ts = saved.get("last_ts")
            self.started_ms = saved.get("started_ms", self.started_ms)
        else:
            self.portfolio = Portfolio(settings.capital.total_usd)
            self.risk = RiskManager(settings, settings.capital.total_usd)
        self._pending_events: list[str] = []
        broker = broker or SimBroker(CostModel(settings), slippage_bps=0.0, through_bps=settings.execution.through_bps)
        self.engine = TradingEngine(strategy, broker, self.portfolio, self.risk, delay_execution=False,
                                    on_event=self._pending_events.append, execution=settings.execution)

    # --- warm-up -------------------------------------------------------------------------
    async def warm_up(self) -> None:
        # Also retry after a restart if an earlier warm-up failed and the strategy has no history yet.
        if not self.fresh and any(v for v in self.strategy.state_dict().values()):
            return
        req = self.strategy.requirements()
        now_ms = int(self.clock() * 1000)
        if hasattr(self.strategy, "seed_funding_history"):
            since = now_ms - WARMUP_FUNDING_DAYS * 86_400_000
            for ex, perp in req.funding:
                try:
                    hist = await self.source.funding_history(ex, perp, since)
                    self.strategy.seed_funding_history(ex, perp, [r for _, r in sorted(hist)])
                except Exception as e:
                    log.warning("warm-up funding history failed for %s %s: %s", ex, perp, e)
        cfg = getattr(self.strategy, "cfg", None)
        bars = getattr(cfg, "lookback_bars", None)
        if hasattr(self.strategy, "seed_prices") and bars and hasattr(self.strategy, "bar_ms"):
            for ex, sym, _ in req.quotes:
                try:
                    rows = await self.source.ohlcv(ex, sym, "1h", bars)
                    self.strategy.seed_prices(sym, [float(r[4]) for r in rows])
                except Exception as e:
                    log.warning("warm-up prices failed for %s %s: %s", ex, sym, e)

    # --- data ----------------------------------------------------------------------------
    async def build_snapshot(self) -> MarketSnapshot | None:
        req = self.strategy.requirements()
        now_ms = int(self.clock() * 1000)
        depth = self.settings.paper.book_depth
        self.source.failures.clear()
        books = await asyncio.gather(*(self.source.order_book(ex, sym, depth) for ex, sym, _ in req.quotes),
                                     return_exceptions=True)
        quotes: dict = {}
        for (ex, sym, _), b in zip(req.quotes, books):
            if isinstance(b, BaseException):
                log.warning("order book failed %s %s: %s", ex, sym, b)
                return None
            if not b.bids or not b.asks:
                log.warning("empty order book %s %s", ex, sym)
                self.source.failures.append(f"empty order book {ex} {sym}")
                return None
            quotes[(ex, sym)] = Quote(b.bids[0][0], b.asks[0][0], b.timestamp_ms, b.bids[0][1], b.asks[0][1], book=b)

        by_ex: dict[str, list[str]] = {}
        for ex, sym in req.funding:
            by_ex.setdefault(ex, []).append(sym)
        results = await asyncio.gather(*(self.source.funding(ex, syms) for ex, syms in by_ex.items()),
                                       return_exceptions=True)
        funding: dict = {}
        for (ex, syms), res in zip(by_ex.items(), results):
            if isinstance(res, BaseException):
                log.warning("funding failed %s: %s", ex, res)
                return None
            for info in res:
                funding[(ex, info.symbol)] = FundingQuote(info.rate, info.interval_hours, info.next_funding_ms)
        missing = [k for k in req.funding if k not in funding]
        if missing:
            log.warning("missing funding for %s", missing)
            self.source.failures.append(f"missing funding for {missing}")
            return None

        events: list[FundingEvent] = []
        for key, fq in funding.items():
            if fq.next_funding_ms is None:
                continue
            prev = self.tracker.get(key)
            nxt = int(fq.next_funding_ms)
            if prev is not None and now_ms >= prev[0]:
                events.append(FundingEvent(prev[0], key[0], key[1], prev[1]))  # exactly once per settlement
                if nxt <= prev[0]:  # exchange still reports the settled time: roll forward ourselves
                    nxt = prev[0] + int(fq.interval_hours * 3_600_000)
            self.tracker[key] = (nxt, fq.rate)
        return MarketSnapshot(now_ms, quotes, funding, events)

    # --- loop ----------------------------------------------------------------------------
    async def run_once(self) -> StepResult | None:
        snap = await self.build_snapshot()
        if snap is None:
            self._append("events.jsonl", {"ts": int(self.clock() * 1000), "type": "skipped_cycle",
                                          "errors": list(self.source.failures)})
            self._persist()
            return None
        self._pending_events.clear()
        res = await self._step(snap)
        self.last_ts = snap.ts_ms
        for f in res.fills:
            self._append("fills.jsonl", asdict(f))
        self._append("equity.jsonl", {"ts": snap.ts_ms, "equity": res.equity})
        for msg in self._pending_events:
            self._append("events.jsonl", {"ts": snap.ts_ms, "type": "event", "msg": msg})
        for intent, reason in res.rejected:
            self._append("events.jsonl", {"ts": snap.ts_ms, "type": "rejected", "group": intent.group_id,
                                          "symbol": intent.symbol, "side": intent.side, "reason": reason})
        self._persist()
        self._notify(res)
        return res

    async def _step(self, snap: MarketSnapshot) -> StepResult:
        return self.engine.step(snap)

    async def run(self, cycles: int | None = None) -> None:
        try:
            await self.warm_up()
            n = 0
            while cycles is None or n < cycles:
                try:
                    await self.run_once()
                except Exception:
                    log.exception("paper cycle failed")
                n += 1
                if cycles is not None and n >= cycles:
                    break
                await self._sleep(self.settings.paper.interval_s)
        finally:
            await self.source.close()

    # --- persistence / notifications -----------------------------------------------------
    def _append(self, name: str, rec: dict) -> None:
        with open(self.dir / name, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _persist(self) -> None:
        state = {
            "portfolio": self.portfolio.to_dict(), "risk": self.risk.to_dict(),
            "strategy": self.strategy.state_dict(),
            "funding_tracker": [[ex, sym, n, r] for (ex, sym), (n, r) in self.tracker.items()],
            "last_ts": self.last_ts, "started_ms": self.started_ms,
        }
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_path)

    def _notify(self, res: StepResult) -> None:
        if self.notifier is None:
            return
        msgs: list[str] = []
        groups: dict[str, list[Fill]] = {}
        for f in res.fills:
            groups.setdefault(f.group_id, []).append(f)
        for gid, fs in groups.items():
            legs = ", ".join(f"{f.side} {f.qty:.6g} {f.symbol}@{f.exchange} {f.price:.2f}" for f in fs)
            msgs.append(f"FILL {gid} [{fs[0].tag}]: {legs}")
        msgs += [f"RISK/EVENT: {m}" for m in self._pending_events]
        for m in msgs:
            try:
                self.notifier.send(m)
            except Exception as e:
                log.warning("notifier failed: %s", type(e).__name__)
