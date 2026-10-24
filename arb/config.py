"""Typed settings loaded from config/settings.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


class FeeRate(BaseModel):
    maker: float
    taker: float


class ExchangeConfig(BaseModel):
    fees: dict[str, FeeRate]  # keyed by market type: spot / swap / future


class Capital(BaseModel):
    total_usd: float
    max_per_trade_usd: float


class Risk(BaseModel):
    max_daily_loss_pct: float
    max_total_loss_pct: float
    max_gross_exposure_x: float = 2.0  # sum of |position notional| / total capital
    max_quote_age_s: float = 15.0


class Tax(BaseModel):
    rate_on_gains: float = 0.0


class Universe(BaseModel):
    quote: str = "USDT"
    base_assets: list[str]


class CrossExchangeCfg(BaseModel):
    enabled: bool = True
    min_net_bps: float = 5.0


class TriangularCfg(BaseModel):
    enabled: bool = True
    exchanges: list[str] = Field(default_factory=lambda: ["binance"])
    min_net_bps: float = 1.0


class FundingCfg(BaseModel):
    enabled: bool = True
    holding_days: float = 7.0
    min_annualized_net_pct: float = 10.0


class BasisCfg(BaseModel):
    enabled: bool = True
    min_annualized_net_pct: float = 8.0


class StatArbCfg(BaseModel):
    enabled: bool = True
    exchange: str = "binance"
    timeframe: str = "1h"
    lookback_bars: int = 720
    max_coint_pvalue: float = 0.05
    entry_z: float = 2.0


class Scanners(BaseModel):
    cross_exchange: CrossExchangeCfg = CrossExchangeCfg()
    triangular: TriangularCfg = TriangularCfg()
    funding: FundingCfg = FundingCfg()
    basis: BasisCfg = BasisCfg()
    stat_arb: StatArbCfg = StatArbCfg()


class Storage(BaseModel):
    dir: str = "data"


class FundingCarryStrategyCfg(BaseModel):
    exchange: str = "binance"
    bases: list[str] = Field(default_factory=lambda: ["BTC", "ETH"])
    notional_usd: float = 300.0
    entry_annualized_pct: float = 15.0
    exit_annualized_pct: float = 3.0
    avg_window: int = 21
    max_hold_days: float = 30.0
    execution: Literal["taker", "maker"] = "taker"
    maker_leg: Literal["perp", "spot"] = "perp"


class StatArbStrategyCfg(BaseModel):
    exchange: str = "binance"
    pairs: list[tuple[str, str]] = Field(default_factory=lambda: [("ETH", "BTC")])
    lookback_bars: int = 720
    entry_z: float = 2.0
    exit_z: float = 0.5
    stop_z: float = 4.0
    max_hold_bars: int = 240
    notional_usd: float = 300.0


class Strategies(BaseModel):
    funding_carry: FundingCarryStrategyCfg = FundingCarryStrategyCfg()
    stat_arb: StatArbStrategyCfg = StatArbStrategyCfg()


class BacktestCfg(BaseModel):
    timeframe: str = "1h"
    spread_bps: float = 2.0
    slippage_bps: float = 2.0


class ExecutionCfg(BaseModel):
    """Maker (post-only limit) order handling, used by strategies with execution: maker."""

    requote_bps: float = 2.0        # re-post when the touch moves this far from our price
    through_bps: float = 1.0        # a resting order fills only when price trades this far through it
    entry_timeout_s: float = 3600.0  # unfilled entry orders are cancelled
    exit_timeout_s: float = 3600.0   # unfilled exit orders fall back to taker


class LiveGate(BaseModel):
    """Conditions paper trading must meet before going live (checked by `arb validate`)."""

    min_paper_days: float = 14.0
    max_pnl_gap_pct: float = 30.0  # paper vs backtest PnL difference, % of the larger absolute PnL
    min_after_tax_pnl_usd: float = 0.0


class LiveCfg(BaseModel):
    capital_usd: float = 0.0             # USDT actually funded for live; loss limits use this. Required (> 0).
    max_order_usd: float = 50.0          # hard cap on every live order's notional
    min_liq_distance_pct: float = 25.0   # close a perp group when liquidation is closer than this
    reconcile_every_s: float = 300.0
    reconcile_tolerance_pct: float = 2.0  # allowed exchange vs portfolio qty difference per symbol
    order_timeout_s: float = 20.0         # taker orders still open after this are cancelled
    gate: LiveGate = LiveGate()


class PaperCfg(BaseModel):
    interval_s: float = 30.0
    state_dir: str = "data/paper"
    book_depth: int = 20


class DefiGasCfg(BaseModel):
    base_units: int = 120_000
    v3_hop_units: int = 120_000
    v2_hop_units: int = 75_000


class DefiChainCfg(BaseModel):
    rpc_url_env: str                     # ARBITRUM_RPC / BASE_RPC (same names as contracts/hardhat.config.js)
    rpc_url_default: str
    max_rps: float = 3.0
    burst: int = 6
    tip_gwei: float = 0.0
    tokens: list[str]
    venues: list[str]
    lateness_blocks: list[int]
    sample_every_s: float = 10.0


class DefiCfg(BaseModel):
    """Flash-loan arbitrage measurement (simulation only; nothing is ever sent)."""

    enabled: bool = False
    addresses_file: str = "defi/addresses.json"
    storage_dir: str = "data/defi"
    loan_tokens: list[str] = ["WETH", "USDC"]
    min_pool_tvl_usd: float = 10_000.0
    min_profit_usd: float = 0.05         # net, after gas
    max_loan_usd: float = 50_000.0
    max_hops: int = 3
    tri_top_k: int = 3
    premium_bps: int = 5                 # checked against addresses.json at startup
    sim_gas: int = 25_000_000            # gas limit of the harness creation eth_call (Hardhat forks cap at 16_777_216)
    gas: DefiGasCfg = DefiGasCfg()
    chains: dict[str, DefiChainCfg]


class Settings(BaseModel):
    live_trading: bool = False
    capital: Capital
    risk: Risk
    tax: Tax = Tax()
    exchanges: dict[str, ExchangeConfig]
    withdrawal_fees: dict[str, float] = Field(default_factory=dict)
    universe: Universe
    scanners: Scanners = Scanners()
    storage: Storage = Storage()
    strategies: Strategies = Strategies()
    backtest: BacktestCfg = BacktestCfg()
    paper: PaperCfg = PaperCfg()
    execution: ExecutionCfg = ExecutionCfg()
    live: LiveCfg = LiveCfg()
    defi: DefiCfg | None = None


def load_settings(path: str | Path = DEFAULT_PATH) -> Settings:
    with open(path) as f:
        return Settings.model_validate(yaml.safe_load(f))
