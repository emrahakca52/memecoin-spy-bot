import os
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

SOLANA_RPC_URL = os.getenv(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com"
)

REQUEST_TIMEOUT = 15


def get_signatures(wallet_address, limit=20):
    """Cüzdanın son işlemlerini getirir."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [
            wallet_address,
            {"limit": min(max(int(limit), 1), 100)}
        ]
    }

    response = requests.post(
        SOLANA_RPC_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()

    data = response.json()

    if "error" in data:
        raise RuntimeError(str(data["error"]))

    return data.get("result", [])


def get_wallet_stats(wallet_address, limit=20):
    """İşlem sayısı ve son işlem zamanını özetler."""
    signatures = get_signatures(wallet_address, limit)

    timestamps = [
        item["blockTime"]
        for item in signatures
        if item.get("blockTime") is not None
    ]

    latest = max(timestamps) if timestamps else None

    return {
        "wallet": wallet_address,
        "transactions_found": len(signatures),
        "successful_transactions": sum(
            1 for item in signatures
            if item.get("err") is None
        ),
        "failed_transactions": sum(
            1 for item in signatures
            if item.get("err") is not None
        ),
        "latest_transaction_utc": (
            datetime.fromtimestamp(
                latest, timezone.utc
            ).isoformat()
            if latest is not None
            else None
        ),
        "checked_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }


def rank_wallets(wallet_addresses, limit=20):
    """Verilen cüzdanları gözlenen işlem sayısına göre sıralar."""
    results = []

    for address in wallet_addresses:
        try:
            stats = get_wallet_stats(address, limit)
            results.append(stats)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            results.append({
                "wallet": address,
                "error": str(exc)
            })

        time.sleep(0.3)

    return sorted(
        results,
        key=lambda item: item.get("successful_transactions", 0),
        reverse=True
    )


if __name__ == "__main__":
    print(
        "Wallet tracker hazır. "
        "Bu modül yalnızca herkese açık işlem verilerini inceler."
    )
