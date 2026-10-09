import asyncio
import time
from datetime import datetime, timezone

import httpx

# Primary source: GeckoTerminal. Fallback: DexScreener.
GECKO_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/solana/trending_pools"
)
DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

REQUEST_TIMEOUT_SECONDS = 15
CACHE_SECONDS = 240
GECKO_COOLDOWN_SECONDS = 1800
MAX_POOLS = 20
MAX_FALLBACK_TOKENS = 30

_cache = {"at": 0.0, "value": None, "provider": None}
_lock = asyncio.Lock()
_gecko_blocked_until = 0.0
_gecko_last_error = None
_last_provider = None


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


def _normalise_gecko(payload):
    results = []
    for item in (payload.get("data") or [])[:MAX_POOLS]:
        if not isinstance(item, dict):
            continue

        attrs = item.get("attributes") or {}
        transactions = attrs.get("transactions") or {}
        h24 = transactions.get("h24") or {}
        volume = attrs.get("volume_usd") or {}
        price_change = attrs.get("price_change_percentage") or {}
        buys = int(_num(h24.get("buys")))
        sells = int(_num(h24.get("sells")))
        pool_name = attrs.get("name") or "Unknown pool"
        token_name = pool_name.split(" / ", 1)[0].strip()

        results.append({
            "chain": "solana",
            "token_name": token_name,
            "token_symbol": token_name,
            "token_address": _token_address_from_pool(item),
            "pair_address": attrs.get("address") or "",
            "dex": attrs.get("dex_id"),
            "price_usd": attrs.get("base_token_price_usd"),
            "liquidity_usd": round(_num(attrs.get("reserve_in_usd")), 2),
            "volume_24h_usd": round(_num(volume.get("h24")), 2),
            "buys_24h": buys,
            "sells_24h": sells,
            "buys_to_sells_ratio": round(buys / max(sells, 1), 3),
            "price_change_24h_pct": _num(price_change.get("h24")),
            "fdv_usd": attrs.get("fdv_usd"),
            "pair_created_at_ms": None,
            "profile": {},
            "source": "GeckoTerminal",
        })
    return results


async def _get_json(client, url):
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def _fetch_gecko(client):
    payload = await _get_json(client, GECKO_URL)
    return _normalise_gecko(payload)


async def _fetch_dexscreener(client):
    # Use the top-boosted Solana token list as a fallback discovery source.
    payload = await _get_json(client, DEX_BOOSTS_URL)
    if not isinstance(payload, list):
        return []

    addresses = []
    seen = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        if (item.get("chainId") or "").lower() != "solana":
            continue
        address = item.get("tokenAddress") or ""
        if address and address not in seen:
            seen.add(address)
            addresses.append(address)
        if len(addresses) >= MAX_FALLBACK_TOKENS:
            break

    if not addresses:
        return []

    # DexScreener accepts comma-separated token addresses on this endpoint.
    url = DEX_TOKENS_URL + ",".join(addresses)
    pairs_payload = await _get_json(client, url)
    pairs = pairs_payload.get("pairs") or []
    best_by_token = {}

    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if (pair.get("chainId") or "").lower() != "solana":
            continue

        base_token = pair.get("baseToken") or {}
        address = base_token.get("address") or ""
        if not address:
            continue

        liquidity = _num((pair.get("liquidity") or {}).get("usd"))
        volume_24h = _num((pair.get("volume") or {}).get("h24"))
        txns = (pair.get("txns") or {}).get("h24") or {}
        buys = int(_num(txns.get("buys")))
        sells = int(_num(txns.get("sells")))
        price_change = pair.get("priceChange") or {}

        candidate = {
            "chain": "solana",
            "token_name": base_token.get("name") or "Unknown",
            "token_symbol": base_token.get("symbol") or "UNKNOWN",
            "token_address": address,
            "pair_address": pair.get("pairAddress") or "",
            "dex": pair.get("dexId"),
            "price_usd": pair.get("priceUsd"),
            "liquidity_usd": round(liquidity, 2),
            "volume_24h_usd": round(volume_24h, 2),
            "buys_24h": buys,
            "sells_24h": sells,
            "buys_to_sells_ratio": round(buys / max(sells, 1), 3),
            "price_change_24h_pct": _num(price_change.get("h24")),
            "fdv_usd": pair.get("fdv"),
            "pair_created_at_ms": pair.get("pairCreatedAt"),
            "profile": {},
            "source": "DexScreener fallback",
        }

        # Keep the most liquid pool for each token to avoid duplicate signals.
        previous = best_by_token.get(address)
        if previous is None or liquidity > previous["liquidity_usd"]:
            best_by_token[address] = candidate

    return sorted(
        best_by_token.values(),
        key=lambda item: (item["liquidity_usd"], item["volume_24h_usd"]),
        reverse=True,
    )


async def _fetch_candidates():
    global _gecko_blocked_until, _gecko_last_error, _last_provider

    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.1",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        now = time.monotonic()

        # Do not hammer GeckoTerminal while its cooldown is active.
        if now >= _gecko_blocked_until:
            try:
                data = await _fetch_gecko(client)
                if data:
                    _gecko_blocked_until = 0.0
                    _gecko_last_error = None
                    _last_provider = "GeckoTerminal public API"
                    return data, _last_provider
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _gecko_blocked_until = (
                        time.monotonic() + GECKO_COOLDOWN_SECONDS
                    )
                    _gecko_last_error = "GeckoTerminal HTTP 429"
                else:
                    _gecko_last_error = f"GeckoTerminal HTTP {exc.response.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                _gecko_last_error = (
                    f"GeckoTerminal {type(exc).__name__}: {str(exc)[:120]}"
                )

        # Fallback runs when GeckoTerminal is rate-limited or unavailable.
        try:
            data = await _fetch_dexscreener(client)
            if data:
                _last_provider = "DexScreener fallback"
                return data, _last_provider
            raise RuntimeError("DexScreener fallback returned no Solana pairs")
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            raise httpx.HTTPError(
                f"GeckoTerminal unavailable ({_gecko_last_error}); "
                f"DexScreener fallback failed: {type(exc).__name__}: "
                f"{str(exc)[:140]}"
            ) from exc


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _last_provider

    async with _lock:
        now = time.monotonic()
        cache_exists = _cache["value"] is not None
        cache_fresh = cache_exists and now - _cache["at"] < CACHE_SECONDS

        if cache_fresh:
            base = _cache["value"]
            provider = _cache["provider"] or "Cached data"
        else:
            try:
                base, provider = await _fetch_candidates()
                _cache["value"] = base
                _cache["at"] = time.monotonic()
                _cache["provider"] = provider
                _last_provider = provider
            except httpx.HTTPError:
                if not cache_exists:
                    raise
                base = _cache["value"]
                provider = f"Cached fallback ({_cache['provider'] or 'unknown source'})"

        filtered = [
            item for item in base
            if item["liquidity_usd"] >= min_liquidity_usd
            and item["volume_24h_usd"] >= min_volume_24h_usd
            and item["buys_to_sells_ratio"] >= min_buys_sells_ratio
        ]
        filtered.sort(
            key=lambda item: (item["liquidity_usd"], item["volume_24h_usd"]),
            reverse=True,
        )

        return {
            "mode": "paper",
            "provider": provider,
            "profiles_and_pairs_checked": len(base),
            "candidates_count": len(filtered[:limit]),
            "filters": {
                "min_liquidity_usd": min_liquidity_usd,
                "min_volume_24h_usd": min_volume_24h_usd,
                "min_buys_sells_ratio": min_buys_sells_ratio,
            },
            "warning": (
                "Paper simulation only. Signals are not buy recommendations "
                "and do not prove profitability. Fallback or cached data may "
                "be incomplete or stale."
            ),
            "signals": filtered[:limit],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
