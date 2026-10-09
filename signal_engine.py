import asyncio
import time
from datetime import datetime, timezone

import httpx

GECKO_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/solana/trending_pools"
)

REQUEST_TIMEOUT_SECONDS = 20
CACHE_SECONDS = 300
INITIAL_COOLDOWN_SECONDS = 120
MAX_COOLDOWN_SECONDS = 1800
MAX_POOLS = 20

_cache = {"at": 0.0, "value": None}
_lock = asyncio.Lock()
_blocked_until = 0.0
_consecutive_429 = 0
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

    if token_id.startswith("solana_"):
        return token_id[len("solana_"):]

    return token_id


class RateLimitError(Exception):
    def __init__(self, retry_after=120):
        self.retry_after = retry_after
        super().__init__("GeckoTerminal returned HTTP 429")


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

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                wait_seconds = float(retry_after)
            except (TypeError, ValueError):
                wait_seconds = INITIAL_COOLDOWN_SECONDS

            raise RateLimitError(wait_seconds)

        response.raise_for_status()
        payload = response.json()

    pools = payload.get("data") or []
    results = []

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

        pool_name = attrs.get("name") or "Unknown pool"
        name_parts = pool_name.split(" / ", 1)
        token_name = name_parts[0].strip()

        results.append({
            "chain": "solana",
            "token_name": token_name,
            "token_symbol": token_name,
            "token_address": _token_address_from_pool(item),
            "pair_address": attrs.get("address") or "",
            "dex": attrs.get("dex_id"),
            "price_usd": attrs.get("base_token_price_usd"),
            "liquidity_usd": round(
                _num(attrs.get("reserve_in_usd")), 2
            ),
            "volume_24h_usd": round(
                _num(volume.get("h24")), 2
            ),
            "buys_24h": buys,
            "sells_24h": sells,
            "buys_to_sells_ratio": ratio,
            "price_change_24h_pct": _num(
                price_change.get("h24")
            ),
            "fdv_usd": attrs.get("fdv_usd"),
            "pair_created_at_ms": None,
            "profile": {},
            "source": "GeckoTerminal",
        })

    return results


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _blocked_until, _consecutive_429, _last_error

    async with _lock:
        now = time.monotonic()

        cache_exists = _cache["value"] is not None
        cache_age = now - _cache["at"] if cache_exists else float("inf")
        cache_is_fresh = cache_exists and cache_age < CACHE_SECONDS

        if cache_is_fresh:
            base = _cache["value"]

        elif now < _blocked_until:
            if not cache_exists:
                raise httpx.HTTPError(
                    "GeckoTerminal bekleme sÃ¼resinde; henÃ¼z Ã¶nbellek yok."
                )
            base = _cache["value"]

        else:
            try:
                base = await _fetch_base_data()

                _cache["value"] = base
                _cache["at"] = time.monotonic()
                _blocked_until = 0.0
                _consecutive_429 = 0
                _last_error = None

            except RateLimitError as exc:
                _consecutive_429 += 1
                backoff = min(
                    INITIAL_COOLDOWN_SECONDS
                    * (2 ** (_consecutive_429 - 1)),
                    MAX_COOLDOWN_SECONDS,
                )
                delay = max(
                    backoff,
                    min(exc.retry_after, MAX_COOLDOWN_SECONDS),
                )
                _blocked_until = time.monotonic() + delay
                _last_error = "GeckoTerminal HTTP 429"

                if not cache_exists:
                    raise httpx.HTTPError(
                        f"GeckoTerminal 429; {int(delay)} saniye beklenecek."
                    ) from exc

                base = _cache["value"]

            except (httpx.HTTPError, ValueError) as exc:
                _last_error = f"{type(exc).__name__}: {str(exc)[:160]}"

                if not cache_exists:
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
                "Paper simulation only. Signals are not buy "
                "recommendations and do not prove profitability. "
                "Cached data may be stale."
            ),
            "signals": filtered[:limit],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
