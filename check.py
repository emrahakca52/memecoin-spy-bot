"""Fail-closed token safety + AI screening orchestration."""
from __future__ import annotations
import asyncio
import logging
import os
import httpx
from config import (
    JUPITER_QUOTE_URL, JUPITER_API_KEY, JUPITER_SLIPPAGE_BPS, USDC_MINT,
    ONCHAIN_MAX_TOP_WALLET_PCT, ONCHAIN_MAX_TOP10_WALLET_PCT,
    ONCHAIN_HOLDER_ACCOUNTS_CHECKED,
)
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
        "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
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


SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"


def _positive_amount(row: dict) -> float | None:
    """Read a token-account UI amount without silently converting missing data to zero."""
    if not isinstance(row, dict):
        return None
    for key in ("uiAmountString", "uiAmount"):
        value = row.get(key)
        if value is not None:
            try:
                amount = float(value)
                if amount >= 0 and amount < float("inf"):
                    return amount
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def _calculate_real_wallet_concentration(
    largest_accounts: list,
    token_account_values: list,
    owner_account_values: list,
    supply: float,
) -> dict:
    """Aggregate top token accounts by wallet and exclude program-owned curve/pool vaults."""
    if not isinstance(largest_accounts, list) or not largest_accounts:
        return {"passed": False, "reasons": ["holder_largest_accounts_missing"]}
    if not isinstance(token_account_values, list) or len(token_account_values) != len(largest_accounts):
        return {"passed": False, "reasons": ["holder_token_account_data_incomplete"]}
    if not isinstance(owner_account_values, list):
        return {"passed": False, "reasons": ["holder_owner_account_data_incomplete"]}
    if not isinstance(supply, (int, float)) or supply <= 0 or supply == float("inf"):
        return {"passed": False, "reasons": ["holder_supply_missing_or_invalid"]}

    owners, amounts = [], []
    for row, account in zip(largest_accounts, token_account_values):
        info = (((account or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        owner = info.get("owner") if isinstance(info, dict) else None
        amount = _positive_amount(row)
        if not owner or amount is None:
            return {"passed": False, "reasons": ["holder_owner_or_balance_missing"]}
        owners.append(owner)
        amounts.append(amount)

    unique_owners = list(dict.fromkeys(owners))
    if len(owner_account_values) != len(unique_owners):
        return {"passed": False, "reasons": ["holder_owner_program_data_incomplete"]}
    owner_programs = {}
    for owner, account in zip(unique_owners, owner_account_values):
        if account is None:
            owner_programs[owner] = None
        elif isinstance(account, dict) and account.get("owner"):
            owner_programs[owner] = account["owner"]
        else:
            return {"passed": False, "reasons": ["holder_owner_program_data_incomplete"]}

    wallets, program_held = {}, 0.0
    for owner, amount in zip(owners, amounts):
        program_id = owner_programs.get(owner)
        if program_id not in (None, SYSTEM_PROGRAM_ID):
            program_held += amount
        else:
            wallets[owner] = wallets.get(owner, 0.0) + amount

    ranked = sorted(wallets.values(), reverse=True)
    top_wallet_pct = ranked[0] / supply * 100.0 if ranked else 0.0
    top_10_pct = sum(ranked[:10]) / supply * 100.0
    program_held_pct = program_held / supply * 100.0
    reasons = []
    if top_wallet_pct > ONCHAIN_MAX_TOP_WALLET_PCT:
        reasons.append("onchain_top_real_wallet_above_limit")
    if top_10_pct > ONCHAIN_MAX_TOP10_WALLET_PCT:
        reasons.append("onchain_top10_real_wallets_above_limit")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "top_wallet_pct": round(top_wallet_pct, 4),
        "top_10_pct": round(top_10_pct, 4),
        "program_held_pct": round(program_held_pct, 4),
        "wallets_counted": len(ranked),
        "accounts_checked": len(largest_accounts),
        "limits": {"top_wallet_pct": ONCHAIN_MAX_TOP_WALLET_PCT,
                   "top_10_pct": ONCHAIN_MAX_TOP10_WALLET_PCT},
    }


async def check_onchain_holder_concentration(address: str) -> dict:
    """Independently verify real-wallet concentration; RPC uncertainty is a rejection."""
    if not address:
        return {"passed": False, "reasons": ["missing_token_address"]}
    errors = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        for rpc_url in _solana_rpc_urls():
            try:
                async def call(method, params):
                    response = await client.post(
                        rpc_url,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("error") or "result" not in payload:
                        raise RuntimeError("solana_rpc_invalid_response")
                    return payload["result"]

                largest_result = await call("getTokenLargestAccounts",
                    [address, {"commitment": "confirmed"}])
                largest = (largest_result or {}).get("value")
                if not isinstance(largest, list) or not largest:
                    raise RuntimeError("holder_largest_accounts_missing")
                largest = largest[:ONCHAIN_HOLDER_ACCOUNTS_CHECKED]
                token_addresses = [row.get("address") for row in largest if isinstance(row, dict)]
                if len(token_addresses) != len(largest) or not token_addresses or any(not x for x in token_addresses):
                    raise RuntimeError("holder_token_account_addresses_missing")

                token_result = await call("getMultipleAccounts",
                    [token_addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}])
                token_values = (token_result or {}).get("value")
                if not isinstance(token_values, list):
                    raise RuntimeError("holder_token_account_data_missing")
                owners = []
                for account in token_values:
                    info = (((account or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                    owner = info.get("owner") if isinstance(info, dict) else None
                    if not owner:
                        raise RuntimeError("holder_owner_missing")
                    if owner not in owners:
                        owners.append(owner)

                owner_result = await call("getMultipleAccounts",
                    [owners, {"encoding": "base64", "commitment": "confirmed"}])
                owner_values = (owner_result or {}).get("value")
                if not isinstance(owner_values, list):
                    raise RuntimeError("holder_owner_program_data_missing")

                supply_result = await call("getTokenSupply",
                    [address, {"commitment": "confirmed"}])
                supply_info = (supply_result or {}).get("value") or {}
                supply = _positive_amount(supply_info)
                if supply is None or supply <= 0:
                    raise RuntimeError("holder_supply_missing_or_invalid")

                result = _calculate_real_wallet_concentration(
                    largest, token_values, owner_values, supply
                )
                result["rpc_endpoint"] = rpc_url
                return result
            except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, RuntimeError) as exc:
                errors.append(type(exc).__name__ + ":" + str(exc)[:100])
                continue
    return {"passed": False, "reasons": ["onchain_holder_check_unavailable"],
            "error": ",".join(errors[-3:]) or "all_rpc_endpoints_failed",
            "rpc_endpoint_count": len(_solana_rpc_urls())}


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

    holder_check = await check_onchain_holder_concentration(address)
    if not holder_check.get("passed"):
        reasons = holder_check.get("reasons", ["onchain_holder_check_rejected"])
        logger.info("Token screening symbol=%s stage=onchain_holders decision=SKIP ai_called=false reasons=%s",
                    symbol, ";".join(str(x) for x in reasons[:5]))
        return {
            "passed": False, "decision": "SKIP", "stage": "onchain_holders",
            "rugcheck": rug, "onchain_safety": onchain,
            "onchain_holder_concentration": holder_check,
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

    ai = await evaluate_token(token, {
        **rug, "onchain_safety": onchain,
        "onchain_holder_concentration": holder_check, "round_trip": round_trip,
    })
    logger.info("Token screening symbol=%s stage=ai provider=%s api_ok=%s decision=%s confidence=%s passed=%s",
                symbol, ai.get("provider"), ai.get("api_ok"), ai.get("decision"),
                ai.get("confidence"), ai.get("passed"))
    return {
        "passed": bool(ai.get("passed")),
        "decision": ai.get("decision", "SKIP"),
        "stage": "ai" if not ai.get("passed") else "approved_for_paper",
        "rugcheck": rug,
        "onchain_safety": onchain,
        "onchain_holder_concentration": holder_check,
        "round_trip": round_trip,
        "grok": ai,
        "rejection_reasons": [] if ai.get("passed") else ai.get("reasons", ["ai_rejected"]),
    }
