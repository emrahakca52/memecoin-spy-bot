"""Fail-closed pre-trade market-data gates."""
from __future__ import annotations
from datetime import datetime, timezone
from .models import TokenMarket, RiskResult
from .settings import Settings

def evaluate_market(m: TokenMarket, settings: Settings, now: datetime | None = None) -> RiskResult:
    reasons: list[str] = []
    missing: list[str] = []
    if m.chain.lower() != "solana": reasons.append("wrong_chain")
    if not m.address: missing.append("token_address")
    if m.price_usd is None or m.price_usd <= 0: missing.append("price")
    if m.liquidity_usd is None: missing.append("liquidity")
    elif m.liquidity_usd < settings.min_liquidity_usd: reasons.append("liquidity_below_minimum")
    if m.volume_24h_usd is None: missing.append("volume_24h")
    elif m.volume_24h_usd < settings.min_volume_24h_usd: reasons.append("volume_below_minimum")
    if m.txns_24h is None: missing.append("txns_24h")
    elif m.txns_24h < settings.min_txns_24h: reasons.append("transactions_below_minimum")
    if m.pair_created_at_ms is None:
        missing.append("pair_age")
    else:
        current = now or datetime.now(timezone.utc)
        age_hours = max(0.0, current.timestamp()*1000 - m.pair_created_at_ms) / 3_600_000
        if age_hours > settings.max_pair_age_hours: reasons.append("pair_too_old")

    chain = m.onchain or {}
    if not chain.get("authority_check"):
        missing.append("mint_freeze_authorities")
    else:
        if chain.get("mint_authority_present") is True: reasons.append("mint_authority_enabled")
        if chain.get("freeze_authority_present") is True: reasons.append("freeze_authority_enabled")
    # Largest-account RPC data is only a top-20 sample, not a full wallet index.
    # It is useful context but never clears the holder-concentration gate.
    missing.append("holder_concentration")
    missing.extend(["lp_lock_status", "bundle_sniper_activity"])
    score = max(0.0, 100.0 - 18.0*len(reasons) - 8.0*len(missing))
    return RiskResult(eligible=not reasons and not missing, score=round(score, 2),
                      reasons=tuple(reasons), missing=tuple(missing))
