"""Checks that stand between the bot and real money.

- check_live_gate: live needs live_trading: true, ARB_LIVE_CONFIRM set to the exact phrase,
  and a positive live.max_order_usd cap.
- check_key_permissions: refuse to trade with an API key that can withdraw funds, and refuse
  when the exchange's key permissions cannot be verified.
- reconcile: compare the exchange's balances/positions with the bot's portfolio. It only reports;
  the caller pauses new entries and alerts a human. It never trades to "fix" a mismatch.
- liquidation_risks: short perps whose liquidation price is too close to the mark price.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Mapping

from arb.config import Settings
from arb.core.portfolio import Portfolio

log = logging.getLogger(__name__)

LIVE_CONFIRM_ENV = "ARB_LIVE_CONFIRM"
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"
MIN_QTY = 1e-9


class LiveSafetyError(RuntimeError):
    pass


def check_live_gate(settings: Settings, env: Mapping[str, str]) -> None:
    problems = []
    if not settings.live_trading:
        problems.append("live_trading is false in settings.yaml")
    if env.get(LIVE_CONFIRM_ENV) != LIVE_CONFIRM_PHRASE:
        problems.append(f"environment variable {LIVE_CONFIRM_ENV} is not set to {LIVE_CONFIRM_PHRASE}")
    if not settings.live.max_order_usd or settings.live.max_order_usd <= 0:
        problems.append("live.max_order_usd must be a positive cap")
    if problems:
        raise LiveSafetyError("live trading refused: " + "; ".join(problems))


def check_key_permissions(exchange: str, client: Any) -> None:
    """Refuse keys that can withdraw. Unknown exchanges are refused: we cannot verify them."""
    try:
        if exchange == "binance":
            r = client.sapi_get_account_apirestrictions()
            if str(r.get("enableWithdrawals")).lower() == "true":
                raise LiveSafetyError("binance API key has withdrawals enabled; create a trade-only key")
            return
        if exchange == "okx":
            data = client.private_get_account_config().get("data") or [{}]
            perms = str(data[0].get("perm", "")).lower()
            if "withdraw" in perms:
                raise LiveSafetyError("okx API key has the withdraw permission; create a trade-only key")
            if "trade" not in perms:
                raise LiveSafetyError("okx API key has no trade permission")
            return
    except LiveSafetyError:
        raise
    except Exception as e:
        raise LiveSafetyError(f"could not verify {exchange} API key permissions: {type(e).__name__}: {e}") from e
    raise LiveSafetyError(f"cannot verify API key permissions on {exchange}; live trading supports binance and okx")


def _expected(portfolio: Portfolio) -> tuple[dict, dict]:
    spot: dict[tuple[str, str], float] = defaultdict(float)  # (exchange, base asset) -> qty
    perps: dict[tuple[str, str], float] = defaultdict(float)  # (exchange, symbol) -> signed base qty
    for p in portfolio.positions.values():
        if p.market == "spot":
            spot[(p.exchange, p.symbol.split("/")[0])] += p.qty
        else:
            perps[(p.exchange, p.symbol)] += p.qty
    return spot, perps


def reconcile(clients: Mapping[str, Any], portfolio: Portfolio, tolerance_pct: float) -> list[str]:
    """Human-readable mismatches between the exchange accounts and the bot's portfolio."""
    spot, perps = _expected(portfolio)
    tol = tolerance_pct / 100
    issues = []
    for ex, client in clients.items():
        try:
            totals = client.fetch_balance().get("total") or {}
            positions = client.fetch_positions()
        except Exception as e:
            issues.append(f"{ex}: could not read account ({type(e).__name__})")
            continue
        for (e2, asset), want in spot.items():
            if e2 != ex:
                continue
            have = float(totals.get(asset) or 0.0)
            # Extra coins the user holds are fine; the bot's own coins must be there.
            if have < want * (1 - tol) - MIN_QTY:
                issues.append(f"{ex} spot {asset}: exchange {have:.8f} < bot {want:.8f}")
        actual: dict[str, float] = defaultdict(float)
        for pos in positions:
            m = client.market(pos["symbol"])
            cs = float(m.get("contractSize") or 1.0) if m.get("contract") else 1.0
            qty = float(pos.get("contracts") or 0.0) * cs
            actual[pos["symbol"]] += -qty if pos.get("side") == "short" else qty
        symbols = {s for (e2, s) in perps if e2 == ex} | set(actual)
        for sym in sorted(symbols):
            want, have = perps.get((ex, sym), 0.0), actual.get(sym, 0.0)
            if abs(have - want) > max(abs(want) * tol, MIN_QTY):
                issues.append(f"{ex} perp {sym}: exchange {have:+.8f} vs bot {want:+.8f}")
    return issues


def liquidation_risks(clients: Mapping[str, Any], min_distance_pct: float) -> list[tuple[str, str, float]]:
    """(exchange, symbol, distance %) for perp positions whose liquidation is closer than the limit."""
    out = []
    for ex, client in clients.items():
        try:
            positions = client.fetch_positions()
        except Exception as e:
            log.warning("fetch_positions failed on %s: %s", ex, e)
            continue
        for pos in positions:
            liq, mark = pos.get("liquidationPrice"), pos.get("markPrice")
            if not liq or not mark or not float(pos.get("contracts") or 0.0):
                continue
            distance = abs(float(liq) - float(mark)) / float(mark) * 100
            if distance < min_distance_pct:
                out.append((ex, pos["symbol"], distance))
    return out
