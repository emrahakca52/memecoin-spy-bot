"""Fail-closed token safety + AI screening orchestration."""
from __future__ import annotations
import logging
from grok import evaluate_token
from rugcheck import check_token

logger = logging.getLogger(__name__)


async def evaluate_candidate(token: dict) -> dict:
    address = str(token.get("token_address") or "")
    symbol = token.get("symbol") or "UNKNOWN"
    rug = await check_token(address)
    if not rug.get("passed"):
        reasons = rug.get("rejection_reasons", []) + rug.get("missing_data", [])
        if rug.get("error"):
            reasons.append(rug.get("error"))
        logger.info("Token screening symbol=%s stage=rugcheck decision=SKIP ai_called=false reasons=%s",
                    symbol, ";".join(str(x) for x in reasons[:5]))
        return {
            "passed": False, "decision": "SKIP", "stage": "rugcheck",
            "rugcheck": rug,
            "grok": {"provider": "not_called", "decision": "NOT_RUN", "passed": False, "api_ok": False},
            "rejection_reasons": reasons,
        }

    ai = await evaluate_token(token, rug)
    logger.info("Token screening symbol=%s stage=ai provider=%s api_ok=%s decision=%s confidence=%s passed=%s",
                symbol, ai.get("provider"), ai.get("api_ok"), ai.get("decision"),
                ai.get("confidence"), ai.get("passed"))
    return {
        "passed": bool(ai.get("passed")),
        "decision": ai.get("decision", "SKIP"),
        "stage": "ai" if not ai.get("passed") else "approved_for_paper",
        "rugcheck": rug,
        "grok": ai,
        "rejection_reasons": [] if ai.get("passed") else ai.get("reasons", ["ai_rejected"]),
    }
