import asyncio
import time
from datetime import datetime, timezone

import httpx

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

CACHE_SECONDS = 900
RATE_LIMIT_COOLDOWN_SECONDS = 900
REQUEST_TIMEOUT_SECONDS = 20
MAX_TOKEN_ADDRESSES = 30

_cache = {
    "at": 0.0,
    "value": None,
}
_lock = asyncio.Lock()
_blocked_until = 0.0
_last_error = None


def _num(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


async def _fetch_base_data():
    global _blocked_until, _last_error

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "MemecoinSpyPro/1.0"},
    ) as client:
        profiles_response = await client.get(PROFILES_URL)
        profiles_response.raise_for_status()

        profiles = profiles_response.json()

        if not isinstance(profiles, list):
            raise httpx.HTTPError(
                "Unexpected token profile response"
            )

        sol_profiles = []
        seen = set()

        for item in profiles:
            if not isinstance(item, dict):
                continue

            if item.get("chainId") != "solana":
                continue

            address = item.get("tokenAddress")

            if address and address not in seen:
                seen.add(address)
                sol_profiles.append(item)

        addresses = [
            item["tokenAddress"]
            for item in sol_profiles[:MAX_TOKEN_ADDRESSES]
        ]

        pairs = []

        if addresses:
            pairs_response = await client.get(
                TOKENS_URL + ",".join(addresses)
            )
            pairs_response.raise_for_status()

            payload = pairs_response.json()

            if isinstance(payload, dict):
                pairs = payload.get("pairs") or []

        profile_by_address = {
            item.get("tokenAddress"): item
            for item in sol_profiles
        }

        base = []

        for pair in pairs:
            if not isinstance(pair, dict):
                continue

            if pair.get("chainId") != "solana":
                continue

            token = pair.get("baseToken") or {}
            address = token.get("address")

            if not address:
                continue

            liquidity = _num(
                (pair.get("liquidity") or {}).get("usd")
            )

            volume24 = _num(
                (pair.get("volume") or {}).get("h24")
            )

            h24 = (
                (pair.get("txns") or {}).get("h24") or {}
            )

            buys = int(_num(h24.get("buys")))
            sells = int(_num(h24.get("sells")))
            ratio = round(buys / max(sells, 1), 3)

            base.append({
                "chain": "solana",
                "token_name": token.get("name"),
                "token_symbol": token.get("symbol"),
                "token_address": address,
                "pair_address": pair.get("pairAddress"),
                "dex": pair.get("dexId"),
                "price_usd":
