"""Typed configuration loaded from YAML + environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class ExchangeConfig(BaseModel):
    id: str = "binance"
    market_type: Literal["spot", "future", "margin"] = "spot"
    testnet: bool = True
    rate_limit_ms: int = 50
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None


class WhaleDetectorConfig(BaseModel):
    min_notional_usd: float = 250_000
    cluster_window_s: float = 5.0
    min_cluster_prints: int = 1


class WhaleTrackerConfig(BaseModel):
    evaluation_horizon_h: float = 4.0
    win_threshold_bps: float = 40.0
    loss_threshold_bps: float = 40.0
    min_events_for_scoring: int = 20


class SignalsConfig(BaseModel):
    min_win_rate: float = 0.60
    min_expectancy_bps: float = 5.0
    signal_ttl_s: float = 60.0

    @field_validator("min_win_rate")
    @classmethod
    def _win_rate_bounds(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("min_win_rate must be in [0, 1]")
        return v


class RiskConfig(BaseModel):
    max_position_pct: float = 0.05
    max_gross_exposure_pct: float = 0.40
    kelly_fraction: float = 0.25
    stop_loss_bps: float = 80.0
    take_profit_bps: float = 120.0
    daily_drawdown_halt_pct: float = 0.03
    max_leverage: float = 3.0


class ExecutionConfig(BaseModel):
    order_type: Literal["market", "limit_postonly"] = "limit_postonly"
    max_slippage_bps: float = 15.0
    retries: int = 3
    dry_run: bool = True


class StorageConfig(BaseModel):
    path: str = "data/whales.sqlite"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = "logs/bot.log"


class Config(BaseModel):
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    symbols: List[str] = Field(default_factory=lambda: ["BTC/USDT"])
    whale_detector: WhaleDetectorConfig = Field(default_factory=WhaleDetectorConfig)
    whale_tracker: WhaleTrackerConfig = Field(default_factory=WhaleTrackerConfig)
    signals: SignalsConfig = Field(default_factory=SignalsConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        path = Path(path)
        raw: dict = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        cfg = cls.model_validate(raw)
        cfg.exchange.api_key = os.getenv("EXCHANGE_API_KEY") or cfg.exchange.api_key
        cfg.exchange.api_secret = os.getenv("EXCHANGE_API_SECRET") or cfg.exchange.api_secret
        cfg.exchange.api_passphrase = (
            os.getenv("EXCHANGE_API_PASSPHRASE") or cfg.exchange.api_passphrase
        )
        return cfg
