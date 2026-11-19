"""Provider-independent data models."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TokenMarket:
    chain: str
    address: str
    symbol: str
    name: str
    price_usd: float | None
    liquidity_usd: float | None
    volume_24h_usd: float | None
    txns_24h: int | None
    price_change_1h_pct: float | None
    price_change_24h_pct: float | None
    pair_created_at_ms: int | None
    pair_address: str | None = None
    source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class RiskResult:
    eligible: bool
    score: float
    reasons: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class RankedCandidate:
    market: TokenMarket
    score: float
    risk: RiskResult
    signals: tuple[str, ...]
