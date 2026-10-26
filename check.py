"""Fail-closed token safety + Grok screening orchestration."""
from __future__ import annotations
from grok import evaluate_token
from rugcheck import check_token

async def evaluate_candidate(token: dict) -> dict:
    address = str(token.get("token_address") or "")
    rug = await check_token(address)
    if not rug.get("passed"):
        return {"passed": False, "decision": "SKIP", "stage": "rugcheck", "rugcheck": rug,
                "grok": {"decision": "NOT_RUN", "passed": False},
                "rejection_reasons": rug.get("rejection_reasons", []) + rug.get("missing_data", []) + ([rug.get("error")] if rug.get("error") else [])}
    grok = await evaluate_token(token, rug)
    return {"passed": bool(grok.get("passed")), "decision": grok.get("decision", "SKIP"),
            "stage": "grok" if not grok.get("passed") else "approved_for_paper",
            "rugcheck": rug, "grok": grok,
            "rejection_reasons": [] if grok.get("passed") else grok.get("reasons", ["grok_rejected"])}
