"""Fail-closed market and token-security gates."""
from __future__ import annotations
from datetime import datetime,timezone
from .models import TokenMarket,RiskResult
from .settings import Settings

def evaluate_market(m:TokenMarket,settings:Settings,now:datetime|None=None)->RiskResult:
    reasons=[];missing=[]
    if m.chain.lower()!="solana": reasons.append("wrong_chain")
    if not m.address: missing.append("token_address")
    if m.price_usd is None or m.price_usd<=0: missing.append("price")
    if m.liquidity_usd is None: missing.append("liquidity")
    elif m.liquidity_usd<settings.min_liquidity_usd: reasons.append("liquidity_below_minimum")
    if m.volume_24h_usd is None: missing.append("volume_24h")
    elif m.volume_24h_usd<settings.min_volume_24h_usd: reasons.append("volume_below_minimum")
    if m.txns_24h is None: missing.append("txns_24h")
    elif m.txns_24h<settings.min_txns_24h: reasons.append("transactions_below_minimum")
    if m.pair_created_at_ms is None: missing.append("pair_age")
    else:
        current=now or datetime.now(timezone.utc)
        age=max(0.0,current.timestamp()*1000-m.pair_created_at_ms)/3_600_000
        if age>settings.max_pair_age_hours: reasons.append("pair_too_old")

    security=m.onchain or {}
    rpc=security.get("rpc",security)
    rug=security.get("rugcheck",{})
    if not isinstance(rpc,dict): rpc={}
    if not isinstance(rug,dict): rug={}

    mint_active=rpc.get("mint_authority_present")
    freeze_active=rpc.get("freeze_authority_present")
    if mint_active is None: mint_active=rug.get("mint_authority_active")
    if freeze_active is None: freeze_active=rug.get("freeze_authority_active")
    if mint_active is None: missing.append("mint_authority_status")
    elif mint_active: reasons.append("mint_authority_enabled")
    if freeze_active is None: missing.append("freeze_authority_status")
    elif freeze_active: reasons.append("freeze_authority_enabled")

    if not rug.get("available"):
        missing.append("rugcheck_report")
    else:
        score=rug.get("score_normalised")
        if score is None: missing.append("rugcheck_normalized_score")
        elif score>settings.max_rugcheck_normalized_score: reasons.append("rugcheck_score_above_limit")
        top10=rug.get("top10_holders_pct")
        if top10 is None: missing.append("top10_holder_concentration")
        elif top10>settings.max_top10_holder_pct: reasons.append("top10_holder_concentration_above_limit")
        lp=rug.get("lp_locked_pct")
        if lp is None: missing.append("lp_lock_status")
        elif lp<settings.min_lp_locked_pct: reasons.append("lp_lock_below_minimum")
        for flag in rug.get("dangerous_risks",[]):
            reasons.append("rugcheck_high_risk:"+str(flag)[:80])

    # Partial RPC largest-account samples are diagnostic only, never a complete holder index.
    if not rug.get("available") or rug.get("top10_holders_pct") is None:
        if "top10_holder_concentration" not in missing: missing.append("top10_holder_concentration")
    helius=security.get("helius",{})
    if not isinstance(helius,dict) or not helius.get("available") or helius.get("sniper_cluster_detected") is None:
        missing.append("bundle_sniper_activity")
    elif helius.get("sniper_cluster_detected"):
        reasons.append("bundle_sniper_cluster_detected")
    score=max(0.0,100.0-18.0*len(reasons)-8.0*len(missing))
    return RiskResult(eligible=not reasons and not missing,score=round(score,2),
                      reasons=tuple(reasons),missing=tuple(dict.fromkeys(missing)))
