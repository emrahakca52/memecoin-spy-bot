"""Transparent no-key rule screening; this is not an AI model."""
def evaluate_local(token: dict, rug: dict) -> dict:
    reasons = []
    liquidity = token.get("liquidity_usd")
    volume = token.get("volume_24h_usd")
    txns = token.get("txns_24h")
    change = token.get("price_change_24h_pct")
    if not isinstance(liquidity,(int,float)) or liquidity < 20000: reasons.append("liquidity_below_20000")
    if not isinstance(volume,(int,float)) or volume < 30000: reasons.append("volume_below_30000")
    if not isinstance(txns,(int,float)) or txns < 30: reasons.append("transactions_below_30")
    if not isinstance(change,(int,float)) or abs(change) > 150: reasons.append("missing_or_extreme_24h_change")
    if not isinstance(rug,dict) or not rug.get("available"): reasons.append("rugcheck_unavailable")
    else:
        score = rug.get("score_normalised")
        holders = rug.get("top10_holders_pct")
        lp = rug.get("lp_locked_pct")
        if not isinstance(score,(int,float)) or score > 55: reasons.append("rugcheck_score_missing_or_high")
        if not isinstance(holders,(int,float)) or holders > 25: reasons.append("holder_concentration_missing_or_high")
        if not isinstance(lp,(int,float)) or lp < 50: reasons.append("lp_lock_missing_or_low")
        if rug.get("dangerous_risks"): reasons.append("rugcheck_dangerous_risks")
    passed = not reasons
    return {"provider":"local_rules","api_ok":True,"passed":passed,
            "decision":"PASS" if passed else "REJECT","reasons":reasons,
            "disclaimer":"Deterministic rules only; not AI advice or an independent model"}
