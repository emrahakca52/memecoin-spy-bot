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


def evaluate_report(report: dict) -> dict:
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
    if score is None: missing.append("rugcheck_score")
    elif score > MAX_RUGCHECK_SCORE: reasons.append("rugcheck_score_above_limit")
    if top10 is None: missing.append("top10_holder_concentration")
    elif top10 > MAX_TOP10_HOLDER_PCT: reasons.append("top10_holder_concentration_above_limit")
    if lp_locked is None: missing.append("lp_lock_percentage")
    elif lp_locked < MIN_LP_LOCKED_PCT: reasons.append("lp_lock_below_minimum")
    if REQUIRE_MINT_AUTHORITY_REVOKED:
        if mint_active is None: missing.append("mint_authority_status")
        elif mint_active: reasons.append("mint_authority_active")
    if REQUIRE_FREEZE_AUTHORITY_REVOKED:
        if freeze_active is None: missing.append("freeze_authority_status")
        elif freeze_active: reasons.append("freeze_authority_active")
    if REQUIRE_ZERO_INSIDER_NETWORKS:
        if insider_count is None: missing.append("insider_network_data")
        elif insider_count != 0: reasons.append("insider_networks_detected")
    dangerous = []
    for risk in risks:
        if not isinstance(risk, dict):
            continue
        level = str(risk.get("level", "")).lower()
        name = str(risk.get("name", "unknown_risk"))
        if level in {"danger", "critical", "high"}:
            dangerous.append(name)
    if dangerous:
        reasons.append("high_risk_flags:" + ",".join(dangerous[:8]))
    return {
        "provider": "RugCheck", "passed": not reasons and not missing,
        "score": score, "top10_holders_pct": top10, "lp_locked_pct": lp_locked,
        "mint_authority_active": mint_active, "freeze_authority_active": freeze_active,
        "insider_network_count": insider_count, "risk_flags": dangerous,
        "rejection_reasons": reasons, "missing_data": missing,
    }


async def check_token(token_address: str) -> dict:
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
        return evaluate_report(payload)
    except (httpx.HTTPError, ValueError) as exc:
        return {"provider": "RugCheck", "passed": False, "error": type(exc).__name__}
