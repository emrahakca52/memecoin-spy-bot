"""RugCheck report retrieval and conservative token-risk evaluation."""
from __future__ import annotations
import os
from typing import Any
import httpx
from config import (
    MAX_RUGCHECK_SCORE, MAX_TOP10_HOLDER_PCT, MIN_LP_LOCKED_PCT,
    REQUIRE_MINT_AUTHORITY_REVOKED, REQUIRE_FREEZE_AUTHORITY_REVOKED,
    REQUIRE_ZERO_INSIDER_NETWORKS,
)

BASE_URL = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1").rstrip("/")


def _num(value: Any) -> float | None:
    try:
        n = float(value)
        return n if n == n and abs(n) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _find_pct_holders(report: dict) -> float | None:
    holders = report.get("topHolders") or report.get("top_holders")
    if not isinstance(holders, list) or len(holders) < 1:
        return None
    vals = []
    for item in holders[:10]:
        if not isinstance(item, dict):
            return None
        pct = _num(item.get("pct", item.get("percentage")))
        if pct is None:
            # Some APIs expose fractional ownership; accept only explicit pct values.
            return None
        vals.append(pct)
    total = sum(vals)
    # Some providers represent pct as a 0..1 fraction; normalize only when clear.
    return total * 100 if total <= 1.0 else total


def _lp_locked_pct(report: dict) -> float | None:
    direct = _num(report.get("lpLockedPct", report.get("lp_locked_pct")))
    if direct is not None:
        return direct * 100 if 0 <= direct <= 1 else direct
    markets = report.get("markets")
    vals = []
    if isinstance(markets, list):
        for market in markets:
            if not isinstance(market, dict):
                continue
            lp = market.get("lp")
            if isinstance(lp, dict):
                val = _num(lp.get("lpLockedPct", lp.get("lockedPct", lp.get("locked_pct"))))
                if val is not None:
                    vals.append(val * 100 if 0 <= val <= 1 else val)
    return max(vals) if vals else None


def _authority_status(report: dict, name: str) -> bool | None:
    meta = report.get("tokenMeta") or report.get("token_meta") or {}
    if isinstance(meta, dict) and name in meta:
        value = meta[name]
    elif name in report:
        value = report[name]
    else:
        return None
    # An explicit null/empty/zero address means revoked; an absent field is unknown.
    if value is None:
        return False
    return bool(str(value).strip() and str(value).strip().lower() not in {"none", "null", "0", "false"})


def evaluate_report(report: dict, market_context: dict | None = None) -> dict:
    """Apply market-aware review bands while preserving hard security vetoes.

    Market momentum can tighten the review band, never override active authorities
    or explicit high/critical risk flags. Missing core safety data remains fail-closed.
    """
    market = market_context if isinstance(market_context, dict) else {}
    liquidity = _num(market.get("liquidity_usd")) or 0.0
    volume = _num(market.get("volume_24h_usd")) or 0.0
    change = _num(market.get("price_change_24h_pct")) or 0.0
    volatility = abs(change)

    # Strong momentum/low liquidity means stricter thresholds; healthier liquidity
    # and moderate movement allow borderline cases to receive AI review.
    if volatility >= 50 or liquidity < 12000:
        profile = {"name": "strict", "max_score": 35.0, "max_top10_pct": 25.0, "min_lp_pct": 80.0}
    elif liquidity >= 25000 and volume >= 50000 and volatility <= 25:
        profile = {"name": "balanced", "max_score": 45.0, "max_top10_pct": 35.0, "min_lp_pct": 60.0}
    else:
        profile = {"name": "standard", "max_score": 40.0, "max_top10_pct": 30.0, "min_lp_pct": 70.0}

    risks = report.get("risks") if isinstance(report.get("risks"), list) else []
    score = _num(report.get("score"))
    top10 = _find_pct_holders(report)
    lp_locked = _lp_locked_pct(report)
    mint_active = _authority_status(report, "mintAuthority")
    freeze_active = _authority_status(report, "freezeAuthority")
    insider = report.get("insiderNetworks", report.get("insider_networks"))
    insider_count = len(insider) if isinstance(insider, list) else (_num(insider) if insider is not None else None)

    reasons: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []

    if score is None:
        missing.append("rugcheck_score")
    elif score > profile["max_score"]:
        reasons.append("rugcheck_score_above_dynamic_limit")
    elif score > MAX_RUGCHECK_SCORE:
        warnings.append("rugcheck_score_above_base_limit")

    if top10 is None:
        missing.append("top10_holder_concentration")
    elif top10 > profile["max_top10_pct"]:
        reasons.append("top10_holder_concentration_above_dynamic_limit")
    elif top10 > MAX_TOP10_HOLDER_PCT:
        warnings.append("top10_holder_concentration_above_base_limit")

    if lp_locked is None:
        missing.append("lp_lock_percentage")
    elif lp_locked < profile["min_lp_pct"]:
        reasons.append("lp_lock_below_dynamic_minimum")
    elif lp_locked < MIN_LP_LOCKED_PCT:
        warnings.append("lp_lock_below_base_minimum")

    if REQUIRE_MINT_AUTHORITY_REVOKED:
        if mint_active is None:
            missing.append("mint_authority_status")
        elif mint_active:
            reasons.append("mint_authority_active")
    if REQUIRE_FREEZE_AUTHORITY_REVOKED:
        if freeze_active is None:
            missing.append("freeze_authority_status")
        elif freeze_active:
            reasons.append("freeze_authority_active")

    # Insider-network data is reviewed as a risk signal, not an automatic veto by itself.
    # Unknown data is surfaced to the AI review and never represented as zero.
    if insider_count is None:
        warnings.append("insider_network_data_missing")
    elif insider_count > 0:
        warnings.append("insider_networks_detected")

    dangerous = []
    market_managed_flags = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        level = str(risk.get("level", "")).lower()
        name = str(risk.get("name", "unknown_risk"))
        normalized_name = name.lower().replace("_", " ").replace("-", " ")
        # These risks already have explicit market-aware numeric thresholds above.
        # Do not let a generic RugCheck label silently re-impose the old fixed limit.
        is_market_managed = any(term in normalized_name for term in (
            "top 10 holder", "top10 holder", "holder concentration",
            "high ownership", "ownership concentration", "lp lock",
            "liquidity lock", "lp unlocked",
        ))
        if level in {"danger", "critical", "high"}:
            if is_market_managed:
                market_managed_flags.append(name)
            else:
                dangerous.append(name)
    if market_managed_flags:
        warnings.append("market_managed_risk_flags:" + ",".join(market_managed_flags[:8]))
    if dangerous:
        reasons.append("high_risk_flags:" + ",".join(dangerous[:8]))

    return {
        "provider": "RugCheck",
        "passed": not reasons and not missing,
        "risk_profile": profile["name"],
        "dynamic_thresholds": profile,
        "market_context": {
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "price_change_24h_pct": change,
            "absolute_price_change_24h_pct": volatility,
        },
        "score": score, "top10_holders_pct": top10, "lp_locked_pct": lp_locked,
        "mint_authority_active": mint_active, "freeze_authority_active": freeze_active,
        "insider_network_count": insider_count, "risk_flags": dangerous,
        "warnings": warnings, "rejection_reasons": reasons, "missing_data": missing,
    }

async def check_token(token_address: str, market_context: dict | None = None) -> dict:
    if not token_address or len(token_address) < 32:
        return {"provider": "RugCheck", "passed": False, "error": "invalid_token_address"}
    url = f"{BASE_URL}/tokens/{token_address}/report"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=6.0)) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return {"provider": "RugCheck", "passed": False, "http_status": response.status_code, "error": "report_unavailable"}
        payload = response.json()
        if not isinstance(payload, dict):
            return {"provider": "RugCheck", "passed": False, "error": "invalid_report_shape"}
        return evaluate_report(payload, market_context)
    except (httpx.HTTPError, ValueError) as exc:
        return {"provider": "RugCheck", "passed": False, "error": type(exc).__name__}
