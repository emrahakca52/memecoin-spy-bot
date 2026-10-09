import os
from datetime import datetime, timezone
import httpx

RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

async def get_wallet_stats(wallet_address: str, limit: int = 20):
    address = wallet_address.strip()
    if not address or len(address) < 32 or len(address) > 44:
        raise ValueError("Wallet address length looks invalid. Check the public Solana address.")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(RPC_URL, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [address, {"limit": limit}]
        })
        response.raise_for_status()
        body = response.json()
    if body.get("error"):
        raise ValueError("Solana RPC rejected the request or address.")
    signatures = body.get("result") or []
    timestamps = [x["blockTime"] for x in signatures if x.get("blockTime") is not None]
    latest = max(timestamps) if timestamps else None
    return {
        "wallet": address,
        "transactions_found": len(signatures),
        "successful_transactions": sum(1 for x in signatures if x.get("err") is None),
        "failed_transactions": sum(1 for x in signatures if x.get("err") is not None),
        "latest_transaction_utc": datetime.fromtimestamp(latest, timezone.utc).isoformat() if latest else None,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "limitation": "This endpoint counts public transactions only. It does not calculate wallet profit/loss or establish that the wallet is profitable."
    }
