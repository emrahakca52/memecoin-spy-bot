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
