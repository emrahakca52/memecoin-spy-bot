"""Transparent market-momentum ranking; never overrides hard risk checks."""
from __future__ import annotations
import math
from .models import TokenMarket


def _bounded(value: float | None, scale: float) -> float:
    if value is None or not math.isfinite(value) or value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(scale))


def score_market(m: TokenMarket) -> tuple[float, tuple[str, ...]]:
    liquidity = _bounded(m.liquidity_usd, 250_000)
    volume = _bounded(m.volume_24h_usd, 1_000_000)
    txns = _bounded(float(m.txns_24h or 0), 5_000)
    change_1h = m.price_change_1h_pct
    change_24h = m.price_change_24h_pct
    # Reward positive short-term acceleration, but penalize extreme vertical moves.
    momentum = 0.0 if change_1h is None else max(0.0, min(1.0, (change_1h + 5.0) / 25.0))
    trend = 0.0 if change_24h is None else max(0.0, min(1.0, (change_24h + 10.0) / 60.0))
    score = round(25*liquidity + 25*volume + 15*txns + 20*momentum + 15*trend, 2)
    signals = []
    if m.liquidity_usd is not None and m.liquidity_usd >= 50_000: signals.append("liquidity_support")
    if m.volume_24h_usd is not None and m.volume_24h_usd >= 100_000: signals.append("active_volume")
    if change_1h is not None and 0 < change_1h <= 25: signals.append("positive_short_momentum")
    if change_1h is not None and change_1h > 50: signals.append("overheated_short_move")
    return score, tuple(signals)
