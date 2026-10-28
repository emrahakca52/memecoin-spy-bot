"""Fail-closed token safety + AI screening orchestration."""
from __future__ import annotations
import asyncio
import logging
import os
import httpx
from config import JUPITER_QUOTE_URL, JUPITER_API_KEY, JUPITER_SLIPPAGE_BPS, USDC_MINT
from grok import evaluate_token
from rugcheck import check_token

logger = logging.getLogger(__name__)

def _solana_rpc_urls() -> list[str]:
    """Use configured RPC endpoints first, with public fallbacks for transient outages."""
    configured = os.getenv("SOLANA_RPC_URLS", "").strip()
    raw_urls = [item.strip() for item in configured.split(",") if item.strip()]
    if not raw_urls:
        primary = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
        raw_urls = [primary, "https://solana-rpc.publicnode.com"]
    # Preserve order while removing duplicate URLs.
    return list(dict.fromkeys(raw_urls))


async def check_onchain_mint_authorities(address: str) -> dict:
    """Independently verify mint/freeze authorities; RPC outages never count as approval."""
    if not address:
        return {"passed": False, "reasons": ["missing_token_address"]}
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getParsedAccountInfo",
        "params": [address, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    }
    errors = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
        for rpc_url in _solana_rpc_urls():
            try:
                response = await client.post(rpc_url, json=payload)
                response.raise_for_status()
                data = response.json()
                if data.get("error"):
                    errors.append("jsonrpc_error")
                    continue
                value = (data.get("result") or {}).get("value")
                if value is None:
                    return {"passed": False, "reasons": ["onchain_account_not_found"], "rpc_endpoint": rpc_url}
                parsed = ((value.get("data") or {}).get("parsed") or {})
                info = parsed.get("info") if isinstance(parsed, dict) else None
                if not isinstance(info, dict):
                    return {"passed": False, "reasons": ["onchain_mint_data_missing"], "rpc_endpoint": rpc_url}
                if parsed.get("type") != "mint":
                    return {"passed": False, "reasons": ["account_is_not_a_parsed_token_mint"], "rpc_endpoint": rpc_url}
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
                    "rpc_endpoint": rpc_url,
                }
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                errors.append(type(exc).__name__)
                continue
    # Fail closed after every configured endpoint fails.
    return {
        "passed": False,
        "reasons": ["onchain_rpc_check_unavailable"],
        "error": ",".join(errors[-3:]) or "all_rpc_endpoints_failed",
        "rpc_endpoint_count": len(_solana_rpc_urls()),
    }


async def check_jupiter_round_trip(token_address: str) -> dict:
    """Require a small SOL->token->USDC quote round trip before paper entry.

    This is a route/sellability sanity check, not a guarantee that execution will
    succeed. It deliberately fails closed if either quote is unavailable.
    """
    if not token_address:
        return {"passed": False, "reasons": ["missing_token_address"]}
    sol_mint = "So11111111111111111111111111111111111111112"
    buy_params = {
        "inputMint": sol_mint,
        "outputMint": token_address,
        "amount": "10000000",  # 0.01 SOL in lamports
        "slippageBps": str(JUPITER_SLIPPAGE_BPS),
    }
    try:
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(7.0, connect=4.0), headers=headers) as client:
            # Current Jupiter Swap API permits keyless prototyping at a low rate;
            # serialize the two quote calls with a small pause to avoid burst 429s.
            buy_response = await client.get(JUPITER_QUOTE_URL, params=buy_params)
            if buy_response.status_code != 200:
                return {"passed": False, "reasons": ["jupiter_buy_quote_unavailable"],
                        "http_status": buy_response.status_code}
            buy_quote = buy_response.json()
            out_amount = str(buy_quote.get("outAmount") or "")
            if not out_amount.isdigit() or int(out_amount) <= 0:
                return {"passed": False, "reasons": ["jupiter_buy_quote_zero_output"]}
            sell_params = {
                "inputMint": token_address,
                "outputMint": USDC_MINT,
                "amount": out_amount,
                "slippageBps": str(JUPITER_SLIPPAGE_BPS),
            }
            await asyncio.sleep(2.1)
            sell_response = await client.get(JUPITER_QUOTE_URL, params=sell_params)
            if sell_response.status_code != 200:
                return {"passed": False, "reasons": ["jupiter_sell_quote_unavailable"],
                        "http_status": sell_response.status_code}
            sell_quote = sell_response.json()
            sell_out = str(sell_quote.get("outAmount") or "")
            if not sell_out.isdigit() or int(sell_out) <= 0:
                return {"passed": False, "reasons": ["jupiter_sell_quote_zero_output"]}
            impact = sell_quote.get("priceImpactPct")
            try:
                impact_pct = abs(float(impact)) * 100.0 if impact is not None else None
            except (TypeError, ValueError):
                impact_pct = None
            if impact_pct is not None and impact_pct > 15.0:
                return {"passed": False, "reasons": ["jupiter_sell_price_impact_above_15pct"],
                        "sell_price_impact_pct": round(impact_pct, 3)}
            return {
                "passed": True, "reasons": [],
                "buy_out_amount_raw": out_amount,
                "sell_out_amount_usdc_raw": sell_out,
                "sell_price_impact_pct": round(impact_pct, 3) if impact_pct is not None else None,
                "provider": "Jupiter quote API",
            }
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        return {"passed": False, "reasons": ["jupiter_round_trip_check_unavailable"],
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

    round_trip = await check_jupiter_round_trip(address)
    if not round_trip.get("passed"):
        reasons = round_trip.get("reasons", ["jupiter_round_trip_rejected"])
        logger.info("Token screening symbol=%s stage=round_trip decision=SKIP ai_called=false reasons=%s",
                    symbol, ";".join(str(x) for x in reasons[:5]))
        return {
            "passed": False, "decision": "SKIP", "stage": "round_trip",
            "rugcheck": rug,
            "onchain_safety": onchain,
            "round_trip": round_trip,
            "grok": {"provider": "not_called", "decision": "NOT_RUN", "passed": False, "api_ok": False},
            "rejection_reasons": reasons,
        }

    ai = await evaluate_token(token, {**rug, "onchain_safety": onchain, "round_trip": round_trip})
    logger.info("Token screening symbol=%s stage=ai provider=%s api_ok=%s decision=%s confidence=%s passed=%s",
                symbol, ai.get("provider"), ai.get("api_ok"), ai.get("decision"),
                ai.get("confidence"), ai.get("passed"))
    return {
        "passed": bool(ai.get("passed")),
        "decision": ai.get("decision", "SKIP"),
        "stage": "ai" if not ai.get("passed") else "approved_for_paper",
        "rugcheck": rug,
        "onchain_safety": onchain,
        "round_trip": round_trip,
        "grok": ai,
        "rejection_reasons": [] if ai.get("passed") else ai.get("reasons", ["ai_rejected"]),
    }
