import asyncio
import time
from datetime import datetime, timezone

import httpx

# GeckoTerminal public API; Solana trending pools.
GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
REQUEST_TIMEOUT_SECONDS = 20
CACHE_SECONDS = 180
RATE_LIMIT_COOLDOWN_SECONDS = 60
MAX_POOLS = 20

_cache = {"at": 0.0, "value": None}
_lock = asyncio.Lock()
_blocked_until = 0.0
_last_error = None


def _num(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _token_address_from_pool(pool):
    relationships = pool.get("relationships") or {}
    base = (relationships.get("base_token") or {}).get("data") or {}
    token_id = base.get("id") or ""
    # GeckoTerminal IDs commonly look like "solana_<mint address>".
    if token_id.startswith("solana_"):
        return token_id[len("solana_"):]
    return token_id


async def _fetch_base_data():
    headers = {
        "Accept": "application/json;version=20230302",
        "User-Agent": "MemecoinSpyPro/1.0",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        response = await client.get(GECKO_URL)
        response.raise_for_status()
        payload = response.json()

    pools = payload.get("data") or []
    base = []

    for item in pools[:MAX_POOLS]:
        if not isinstance(item, dict):
            continue

        attrs = item.get("attributes") or {}
        transactions = attrs.get("transactions") or {}
        h24 = transactions.get("h24") or {}
        volume = attrs.get("volume_usd") or {}
        price_change = attrs.get("price_change_percentage") or {}

        buys = int(_num(h24.get("buys")))
        sells = int(_num(h24.get("sells")))
        ratio = round(buys / max(sells, 1), 3)

        pool_address = attrs.get("address") or ""
        token_address = _token_address_from_pool(item)

        # Pool name is a fallback label if token metadata is not included.
        pool_name = attrs.get("name") or "Unknown pool"
        name_parts = pool_name.split(" / ", 1)
        token_name = name_parts[0].strip() if name_parts else pool_name
        token_symbol = token_name

        base.append({
            "chain": "solana",
            "token_name": token_name,
            "token_symbol": token_symbol,
            "token_address": token_address,
            "pair_address": pool_address,
            "dex": attrs.get("dex_id"),
            "price_usd": attrs.get("base_token_price_usd"),
            "liquidity_usd": round(_num(attrs.get("reserve_in_usd")), 2),
            "volume_24h_usd": round(_num(volume.get("h24")), 2),
            "buys_24h": buys,
            "sells_24h": sells,
            "buys_to_sells_ratio": ratio,
            "price_change_24h_pct": _num(price_change.get("h24")),
            "fdv_usd": attrs.get("fdv_usd"),
            "pair_created_at_ms": None,
            "profile": {},
            "source": "GeckoTerminal",
        })

    return base


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _blocked_until, _last_error

    async with _lock:
        now = time.monotonic()
        cache_is_fresh = (
            _cache["value"] is not None
            and now - _cache["at"] < CACHE_SECONDS
        )

        if cache_is_fresh:
            base = _cache["value"]
        elif now < _blocked_until:
            if _cache["value"] is None:
                raise httpx.HTTPError(
                    "GeckoTerminal rate-limit cooldown active"
                )
            base = _cache["value"]
        else:
            try:
                base = await _fetch_base_data()
                _cache["value"] = base
                _cache["at"] = time.monotonic()
                _blocked_until = 0.0
                _last_error = None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _blocked_until = (
                        time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                    )
                    _last_error = "GeckoTerminal rate limit (429)"
                else:
                    _last_error = f"HTTP {exc.response.status_code}"

                if _cache["value"] is None:
                    raise
                base = _cache["value"]
            except (httpx.HTTPError, ValueError) as exc:
                _last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
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
            "provider": "GeckoTerminal public API",
            "profiles_and_pairs_checked": len(base),
            "candidates_count": len(filtered[:limit]),
            "filters": {
                "min_liquidity_usd": min_liquidity_usd,
                "min_volume_24h_usd": min_volume_24h_usd,
                "min_buys_sells_ratio": min_buys_sells_ratio,
            },
            "warning": (
                "Rule-based candidates only. Not buy recommendations "
                "or proof of profitability. GeckoTerminal public API "
                "is rate-limited and pool data may be incomplete."
            ),
            "signals": filtered[:limit],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
