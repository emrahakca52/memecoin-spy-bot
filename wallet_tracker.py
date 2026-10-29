import os
from datetime import datetime, timezone

import httpx

DEFAULT_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]


def _rpc_urls():
    configured = os.getenv("SOLANA_RPC_URL", "").strip()
    return list(dict.fromkeys(([configured] if configured else []) + DEFAULT_RPC_URLS))


async def get_wallet_stats(wallet_address: str, limit: int = 20):
    """Read public Solana transaction activity; does not calculate profitability."""
    address = wallet_address.strip()
    if not address or not 32 <= len(address) <= 44:
        raise ValueError("Wallet address length looks invalid. Check the public Solana address.")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100.")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    timeout = httpx.Timeout(12.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for rpc_url in _rpc_urls():
            try:
                response = await client.post(
                    rpc_url, json=payload,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("error"):
                    continue
                signatures = body.get("result")
                if not isinstance(signatures, list):
                    continue

                timestamps = [
                    item["blockTime"] for item in signatures
                    if isinstance(item, dict) and item.get("blockTime") is not None
                ]
                latest = max(timestamps) if timestamps else None
                return {
                    "wallet": address,
                    "transactions_found": len(signatures),
                    "successful_transactions": sum(
                        1 for item in signatures
                        if isinstance(item, dict) and item.get("err") is None
                    ),
                    "failed_transactions": sum(
                        1 for item in signatures
                        if isinstance(item, dict) and item.get("err") is not None
                    ),
                    "latest_transaction_utc": (
                        datetime.fromtimestamp(latest, timezone.utc).isoformat()
                        if latest is not None else None
                    ),
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "rpc_provider": rpc_url,
                    "limitation": (
                        "Public transaction counts only; this does not calculate wallet "
                        "profit/loss or prove that the wallet is profitable."
                    ),
                }
            except (httpx.HTTPError, ValueError):
                continue

    raise httpx.HTTPError(
        "All configured Solana RPC endpoints failed. Set SOLANA_RPC_URL to a valid provider."
    )


async def get_wallet_token_activity(wallet_address: str, limit: int = 10):
    """Return recent token-balance changes for a public Solana wallet.

    This is observational only: balance deltas do not prove a swap or profitability.
    """
    import re

    address = wallet_address.strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
        raise ValueError("Invalid Solana wallet address.")
    if limit < 1 or limit > 20:
        raise ValueError("limit must be between 1 and 20.")

    timeout = httpx.Timeout(15.0, connect=6.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        last_error = None
        for rpc_url in _rpc_urls():
            try:
                sig_response = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [address, {"limit": limit}],
                })
                sig_response.raise_for_status()
                sig_body = sig_response.json()
                if sig_body.get("error") or not isinstance(sig_body.get("result"), list):
                    last_error = "Signature lookup returned an invalid RPC response."
                    continue

                activity = []
                for signature in sig_body["result"]:
                    if not isinstance(signature, dict) or signature.get("err") is not None:
                        continue
                    tx_response = await client.post(rpc_url, json={
                        "jsonrpc": "2.0", "id": 2,
                        "method": "getTransaction",
                        "params": [signature.get("signature"), {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                        }],
                    })
                    tx_response.raise_for_status()
                    tx_body = tx_response.json()
                    tx = tx_body.get("result") if isinstance(tx_body, dict) else None
                    if not isinstance(tx, dict):
                        continue
                    meta = tx.get("meta") or {}
                    pre = meta.get("preTokenBalances") or []
                    post = meta.get("postTokenBalances") or []
                    before = {}
                    after = {}
                    for item in pre:
                        if item.get("owner") == address:
                            mint = item.get("mint")
                            amount = (item.get("uiTokenAmount") or {}).get("uiAmountString")
                            try:
                                before[mint] = float(amount or 0)
                            except (TypeError, ValueError):
                                pass
                    for item in post:
                        if item.get("owner") == address:
                            mint = item.get("mint")
                            amount = (item.get("uiTokenAmount") or {}).get("uiAmountString")
                            try:
                                after[mint] = float(amount or 0)
                            except (TypeError, ValueError):
                                pass
                    changes = []
                    for mint in sorted(set(before) | set(after)):
                        delta = after.get(mint, 0.0) - before.get(mint, 0.0)
                        if abs(delta) > 0:
                            changes.append({
                                "mint": mint,
                                "delta_ui": delta,
                                "direction": "increased" if delta > 0 else "decreased",
                            })
                    if changes:
                        activity.append({
                            "signature": signature.get("signature"),
                            "block_time_utc": (
                                datetime.fromtimestamp(signature["blockTime"], timezone.utc).isoformat()
                                if signature.get("blockTime") is not None else None
                            ),
                            "token_balance_changes": changes,
                            "fee_lamports": meta.get("fee"),
                            "note": "Balance changes only; not a confirmed buy/sell classification or PnL.",
                        })
                return {
                    "wallet": address,
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "transactions_scanned": len(sig_body["result"]),
                    "token_activity_count": len(activity),
                    "activity": activity,
                    "mode": "read_only",
                    "real_trading_enabled": False,
                    "rpc_provider": rpc_url,
                }
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                last_error = type(exc).__name__
                continue
    raise httpx.HTTPError(f"Solana RPC providers unavailable: {last_error or 'unknown error'}")
