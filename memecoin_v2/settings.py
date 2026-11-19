"""Typed environment settings for the v2 scanner."""
from __future__ import annotations
import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if value == value and abs(value) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"
    real_trading_enabled: bool = False
    min_liquidity_usd: float = 20_000.0
    min_volume_24h_usd: float = 30_000.0
    min_txns_24h: int = 30
    max_pair_age_hours: float = 72.0
    max_top_holder_pct: float = 5.0
    max_top10_holder_pct: float = 25.0
    max_daily_loss_usd: float = 25.0
    paper_starting_balance_usd: float = 500.0
    request_timeout_seconds: float = 12.0

    @classmethod
    def from_env(cls) -> "Settings":
        # Real trading is deliberately hard-disabled in v2. A future live-trading
        # implementation must be separately reviewed and explicitly designed.
        return cls(
            mode="paper", real_trading_enabled=False,
            min_liquidity_usd=max(0.0, _float("V2_MIN_LIQUIDITY_USD", 20_000)),
            min_volume_24h_usd=max(0.0, _float("V2_MIN_VOLUME_24H_USD", 30_000)),
            min_txns_24h=max(0, _int("V2_MIN_TXNS_24H", 30)),
            max_pair_age_hours=max(1.0, _float("V2_MAX_PAIR_AGE_HOURS", 72)),
            max_top_holder_pct=max(0.1, _float("V2_MAX_TOP_HOLDER_PCT", 5)),
            max_top10_holder_pct=max(0.1, _float("V2_MAX_TOP10_HOLDER_PCT", 25)),
            max_daily_loss_usd=max(0.0, _float("V2_MAX_DAILY_LOSS_USD", 25)),
            paper_starting_balance_usd=max(1.0, _float("V2_PAPER_STARTING_BALANCE_USD", 500)),
            request_timeout_seconds=max(3.0, _float("V2_REQUEST_TIMEOUT_SECONDS", 12)),
        )
