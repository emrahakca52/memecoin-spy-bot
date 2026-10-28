"""Fail-closed token safety + AI screening orchestration."""
from __future__ import annotations
import logging
import os
import httpx
from grok import evaluate_token
from rugcheck import check_token

logger = logging.getLogger(__name__)
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()


async def check_onchain_mint_authorities(address: str) -> dict:
    """Independently verify mint/freeze authorities from Solana RPC, as in safety.ts."""
    if not address:
        return {"passed": False, "reasons": ["missing_token_address"]}
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getParsedAccountInfo",
        "params": [address, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            response = await client.post(SOLANA_RPC_URL, json=payload)
            response.raise_for_status()
            data = response.json()
        if data.get("error"):
            return {"passed": False, "reasons": ["onchain_rpc_error"]}
        value = ((data.get("result") or {}).get("value"))
        parsed = (((value or {}).get("data") or {}).get("parsed") or {})
        info = parsed.get("info") if isinstance(parsed, dict) else None
        if not isinstance(info, dict):
            return {"passed": False, "reasons": ["onchain_mint_data_missing"]}
        if parsed.get("type") != "mint":
            return {"passed": False, "reasons": ["account_is_not_a_parsed_token_mint"]}
        mint_authority = info.get("mintAuthority")
        freeze_authority = info.get("freezeAuthority")
        reasons = []
        if mint_authority:
            reasons.append("onchain_mint_authority_active")
        if freeze_authority:
            reasons.append("onchain_freeze_authority_active")
        return {
            "passed": not reasons,
            "reasons": reasons,
            "mint_authority_active": bool(mint_authority),
            "freeze_authority_active": bool(freeze_authority),
            "decimals": info.get("decimals"),
            "supply": info.get("supply"),
        }
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        # Fail closed: a failed independent check is not an approval.
        return {"passed": False, "reasons": ["onchain_rpc_check_unavailable"],
                "error": type(exc).__name__}


async def evaluate_candidate(token: dict) -> dict:
    address = str(token.get("token_address") or "")
    symbol = token.get("symbol") or "UNKNOWN"
    rug = await check_token(address, token)
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

    onchain = await check_onchain_mint_authorities(address)
    if not onchain.get("passed"):
        reasons = onchain.get("reasons", ["onchain_safety_rejected"])
        logger.info("Token screening symbol=%s stage=onchain_safety decision=SKIP ai_called=false reasons=%s",
                    symbol, ";".join(str(x) for x in reasons[:5]))
        return {
            "passed": False, "decision": "SKIP", "stage": "onchain_safety",
            "rugcheck": rug,
            "onchain_safety": onchain,
            "grok": {"provider": "not_called", "decision": "NOT_RUN", "passed": False, "api_ok": False},
            "rejection_reasons": reasons,
        }

    ai = await evaluate_token(token, {**rug, "onchain_safety": onchain})
    logger.info("Token screening symbol=%s stage=ai provider=%s api_ok=%s decision=%s confidence=%s passed=%s",
                symbol, ai.get("provider"), ai.get("api_ok"), ai.get("decision"),
                ai.get("confidence"), ai.get("passed"))
    return {
        "passed": bool(ai.get("passed")),
        "decision": ai.get("decision", "SKIP"),
        "stage": "ai" if not ai.get("passed") else "approved_for_paper",
        "rugcheck": rug,
        "onchain_safety": onchain,
        "grok": ai,
        "rejection_reasons": [] if ai.get("passed") else ai.get("reasons", ["ai_rejected"]),
    }
