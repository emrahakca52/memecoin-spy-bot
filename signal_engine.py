import asyncio
import time
from datetime import datetime, timezone

import httpx

PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

# Önbellek 10 dakika geçerli
CACHE_SECONDS = 600

_cache = {"at": 0.0, "value": None}
_lock = asyncio.Lock()

# 429 hatasından sonra yeni istekleri bir süre beklet
_blocked_until = 0.0


def _num(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _blocked_until

    async with _lock:
        now = time.monotonic()

        # Önce mevcut önbelleği kullan
        if (
            _cache["value"] is not None
            and now - _cache["at"] < CACHE_SECONDS
        ):
            base = _cache["value"]

        else:
            # Önceden 429 geldiyse tekrar istek gönderme
            if now < _blocked_until:
                if _cache["value"] is None:
                    raise httpx.HTTPError(
                        "DexScreener rate limit cooldown active"
                    )
                base = _cache["value"]

            else:
                try:
                    async with httpx.AsyncClient(
                        timeout=20,
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
                            if (
                                not isinstance(item, dict)
                                or item.get("chainId") != "solana"
                            ):
                                continue

                            addr = item.get("tokenAddress")

                            if addr and addr not in seen:
                                seen.add(addr)
                                sol_profiles.append(item)

                        addresses = [
                            item["tokenAddress"]
                            for item in sol_profiles[:30]
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
                            if (
                                not isinstance(pair, dict)
                                or pair.get("chainId") != "solana"
                            ):
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
                                "price_usd": pair.get("priceUsd"),
                                "liquidity_usd": round(liquidity, 2),
                                "volume_24h_usd": round(volume24, 2),
                                "buys_24h": buys,
                                "sells_24h": sells,
                                "buys_to_sells_ratio": ratio,
                                "price_change_24h_pct": _num(
                                    (pair.get("priceChange") or {}).get("h24")
                                ),
                                "fdv_usd": pair.get("fdv"),
                                "pair_created_at_ms": pair.get(
                                    "pairCreatedAt"
                                ),
                                "profile": profile_by_address.get(
                                    address, {}
                                ),
                            })

                    _cache["value"] = base
                    _cache["at"] = time.monotonic()
                    _blocked_until = 0.0

                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429:
                        # Rate limit durumunda 10 dakika bekle
                        _blocked_until = time.monotonic() + 600

                    # Eski veri varsa onu kullan
                    if _cache["value"] is None:
                        raise

                    base = _cache["value"]

                except httpx.HTTPError:
                    if _cache["value"] is None:
                        raise

                    base = _cache["value"]

        filtered = [
            item for item in base
            if item["liquidity_usd"] >= min_liquidity_usd
            and item["volume_24h_usd"] >= min_volume_24h_usd
            and item["buys_to_sells_ratio"] >= min_buys_sells_ratio
        ]

        filtered.sort(
            key=lambda item: (
                item["liquidity_usd"],
                item["volume_24h_usd"],
            ),
            reverse=True,
        )

        return {
            "mode": "paper",
            "provider": "DexScreener public API",
            "profiles_and_pairs_checked": len(base),
            "candidates_count": len(filtered[:limit]),
            "filters": {
                "min_liquidity_usd": min_liquidity_usd,
                "min_volume_24h_usd": min_volume_24h_usd,
                "min_buys_sells_ratio": min_buys_sells_ratio,
            },
            "warning": (
                "Rule-based candidates only. Not buy recommendations "
                "or proof of profitability. Data may be incomplete or delayed."
            ),
            "signals": filtered[:limit],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
