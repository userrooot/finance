"""Typed settings loaded from config/settings.yaml."""
from __future__ import annotations

from pathlib import Path

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


def load_settings(path: str | Path = DEFAULT_PATH) -> Settings:
    with open(path) as f:
        return Settings.model_validate(yaml.safe_load(f))
