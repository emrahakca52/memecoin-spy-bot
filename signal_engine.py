import asyncio
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

# Public market-data sources. No API keys required.
GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

REQUEST_TIMEOUT_SECONDS = 20
# Keep a successful result longer to reduce repeated discovery requests.
CACHE_SECONDS = 600
GECKO_COOLDOWN_SECONDS = 1800
# When DexScreener returns 429, pause discovery calls to it too.
DEX_COOLDOWN_SECONDS = 300
MAX_COOLDOWN_SECONDS = 1800
MAX_POOLS = 20
MAX_FALLBACK_TOKENS = 30

_cache = {"at": 0.0, "value": None, "provider": None}
_lock = asyncio.Lock()
_gecko_blocked_until = 0.0
_dex_blocked_until = 0.0
_gecko_last_error = None
_dex_last_error = None
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
    return token_id[len("solana_"):] if token_id.startswith("solana_") else token_id


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
        buys, sells = int(_num(h24.get("buys"))), int(_num(h24.get("sells")))
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


def _retry_after_seconds(response, default_seconds):
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return max(1, min(int(float(raw)), MAX_COOLDOWN_SECONDS))
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                seconds = (target - datetime.now(timezone.utc)).total_seconds()
                return max(1, min(int(seconds), MAX_COOLDOWN_SECONDS))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(max(1, int(default_seconds)), MAX_COOLDOWN_SECONDS)


async def _get_json(client, url, provider):
    global _gecko_blocked_until, _dex_blocked_until
    response = await client.get(url)
    if response.status_code == 429:
        wait = _retry_after_seconds(
            response,
            GECKO_COOLDOWN_SECONDS if provider == "gecko" else DEX_COOLDOWN_SECONDS,
        )
        until = time.monotonic() + wait
        if provider == "gecko":
            _gecko_blocked_until = max(_gecko_blocked_until, until)
        else:
            _dex_blocked_until = max(_dex_blocked_until, until)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {response.text[:120]}") from exc


async def _fetch_gecko(client):
    return _normalise_gecko(await _get_json(client, GECKO_URL, "gecko"))


async def _fetch_dexscreener(client):
    # Avoid calling DexScreener while its cooldown is active.
    if time.monotonic() < _dex_blocked_until:
        raise RuntimeError("DexScreener discovery is in cooldown after rate limiting")

    discovery_errors = []
    addresses = []
    seen = set()

    for source_name, url in (
        ("token boosts", DEX_BOOSTS_URL),
        ("token profiles", DEX_PROFILES_URL),
    ):
        if time.monotonic() < _dex_blocked_until:
            break
        try:
            payload = await _get_json(client, url, "dex")
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict) or (item.get("chainId") or "").lower() != "solana":
                    continue
                address = item.get("tokenAddress") or ""
                if address and address not in seen:
                    seen.add(address)
                    addresses.append(address)
                if len(addresses) >= MAX_FALLBACK_TOKENS:
                    break
            if addresses:
                break
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            discovery_errors.append(f"{source_name}: {type(exc).__name__} {str(exc)[:100]}")
            if time.monotonic() < _dex_blocked_until:
                break

    if not addresses:
        detail = "; ".join(discovery_errors) or "no Solana token addresses returned"
        raise RuntimeError(f"DexScreener discovery failed: {detail}")

    # Pair lookup is also rate-limited; route through the same 429 handler.
    pairs_payload = await _get_json(
        client, DEX_TOKENS_URL + ",".join(addresses), "dex"
    )
    pairs = pairs_payload.get("pairs") or []
    best_by_token = {}

    for pair in pairs:
        if not isinstance(pair, dict) or (pair.get("chainId") or "").lower() != "solana":
            continue
        base_token = pair.get("baseToken") or {}
        address = base_token.get("address") or ""
        if not address:
            continue
        liquidity = _num((pair.get("liquidity") or {}).get("usd"))
        volume_24h = _num((pair.get("volume") or {}).get("h24"))
        txns = (pair.get("txns") or {}).get("h24") or {}
        buys, sells = int(_num(txns.get("buys"))), int(_num(txns.get("sells")))
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
        previous = best_by_token.get(address)
        if previous is None or liquidity > previous["liquidity_usd"]:
            best_by_token[address] = candidate

    if not best_by_token:
        raise RuntimeError("DexScreener returned no Solana trading pairs for discovered tokens")

    return sorted(
        best_by_token.values(),
        key=lambda item: (item["liquidity_usd"], item["volume_24h_usd"]),
        reverse=True,
    )


async def _fetch_candidates():
    global _gecko_blocked_until, _gecko_last_error, _dex_last_error, _last_provider
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 MemecoinSpyBot/1.3",
    }
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
        follow_redirects=True,
    ) as client:
        now = time.monotonic()
        if now < _gecko_blocked_until:
            _gecko_last_error = "GeckoTerminal cooldown active after HTTP 429"
        else:
            try:
                data = await _fetch_gecko(client)
                if data:
                    _gecko_blocked_until = 0.0
                    _gecko_last_error = None
                    _last_provider = "GeckoTerminal public API"
                    return data, _last_provider
                _gecko_last_error = "GeckoTerminal returned no pools"
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                _gecko_last_error = f"GeckoTerminal HTTP {code}"
                if code == 429:
                    _gecko_blocked_until = max(
                        _gecko_blocked_until,
                        time.monotonic() + GECKO_COOLDOWN_SECONDS,
                    )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                _gecko_last_error = f"GeckoTerminal {type(exc).__name__}: {str(exc)[:160]}"

        try:
            data = await _fetch_dexscreener(client)
            _dex_last_error = None
            _last_provider = "DexScreener fallback"
            return data, _last_provider
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            _dex_last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            detail = (
                f"GeckoTerminal failed ({_gecko_last_error or 'unavailable'}); "
                f"DexScreener failed ({_dex_last_error})"
            )
            raise RuntimeError(detail) from exc


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
                _cache.update({
                    "value": base,
                    "at": time.monotonic(),
                    "provider": provider,
                })
                _last_provider = provider
            except (httpx.HTTPError, RuntimeError):
                # Keep using last known data if providers are rate-limited.
                # It is explicitly labeled as cached and can be stale.
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
                "Paper simulation only. Signals are not buy recommendations and do not "
                "prove profitability. Fallback or cached data may be incomplete or stale."
            ),
            "signals": filtered[:limit],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
