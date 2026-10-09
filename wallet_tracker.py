import os
from datetime import datetime, timezone

import httpx

# You may set SOLANA_RPC_URL in Render. If it is unavailable, try public fallback RPCs.
DEFAULT_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]

def _rpc_urls():
    configured = os.getenv("SOLANA_RPC_URL", "").strip()
    urls = []
    if configured:
        urls.append(configured)
    urls.extend(DEFAULT_RPC_URLS)
    # Keep order and remove duplicates
    return list(dict.fromkeys(urls))


async def get_wallet_stats(wallet_address: str, limit: int = 20):
    """Read public Solana transaction activity. Does not calculate profitability."""
    address = wallet_address.strip()

    if not address or len(address) < 32 or len(address) > 44:
        raise ValueError("Wallet address length looks invalid. Check the public Solana address.")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100.")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }

    errors = []
    timeout = httpx.Timeout(12.0, connect=6.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for rpc_url in _rpc_urls():
            try:
                response = await client.post(
                    rpc_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                body = response.json()

                if body.get("error"):
                    errors.append(f"{rpc_url}: RPC returned an error")
                    continue

                signatures = body.get("result")
                if not isinstance(signatures, list):
                    errors.append(f"{rpc_url}: unexpected response")
                    continue

                timestamps = [
                    item["blockTime"]
                    for item in signatures
                    if item.get("blockTime") is not None
                ]
                latest = max(timestamps) if timestamps else None

                return {
                    "wallet": address,
                    "transactions_found": len(signatures),
                    "successful_transactions": sum(
                        1 for item in signatures if item.get("err") is None
                    ),
                    "failed_transactions": sum(
                        1 for item in signatures if item.get("err") is not None
                    ),
                    "latest_transaction_utc": (
                        datetime.fromtimestamp(latest, timezone.utc).isoformat()
                        if latest is not None
                        else None
                    ),
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                    "rpc_provider": rpc_url,
                    "limitation": (
                        "This endpoint counts public transactions only. It does not "
                        "calculate wallet profit/loss or establish that the wallet is profitable."
                    ),
                }

            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{rpc_url}: {type(exc).__name__}")

    raise httpx.HTTPError(
        "All configured Solana RPC endpoints failed. Check provider availability "
        "or set SOLANA_RPC_URL in Render to a valid RPC endpoint."
    )
