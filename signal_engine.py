import asyncio
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"

REQUEST_TIMEOUT_SECONDS = 15
CACHE_SECONDS = 600
GECKO_COOLDOWN_SECONDS = 1800
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
_next_provider = "gecko"


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
        address = _token_address_from_pool(item)
        if not address:
            continue
        results.append({
            "chain": "solana", "token_name": token_name, "token_symbol": token_name,
            "token_address": address, "pair_address": attrs.get("address") or "",
            "dex": attrs.get("dex_id"), "price_usd": attrs.get("base_token_price_usd"),
            "liquidity_usd": round(_num(attrs.get("reserve_in_usd")), 2),
            "volume_24h_usd": round(_num(volume.get("h24")), 2),
            "buys_24h": buys, "sells_24h": sells,
            "buys_to_sells_ratio": round(buys / max(sells, 1), 3),
            "price_change_24h_pct": _num(price_change.get("h24")),
            "fdv_usd": attrs.get("fdv_usd"), "pair_created_at_ms": None,
            "profile": {}, "source": "GeckoTerminal",
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
        default = GECKO_COOLDOWN_SECONDS if provider == "gecko" else DEX_COOLDOWN_SECONDS
        wait = _retry_after_seconds(response, default)
        until = time.monotonic() + wait
        if provider == "gecko":
            _gecko_blocked_until = max(_gecko_blocked_until, until)
        else:
            _dex_blocked_until = max(_dex_blocked_until, until)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {response.text[:100]}") from exc


async def _fetch_gecko(client):
    return _normalise_gecko(await _get_json(client, GECKO_URL, "gecko"))


async def _fetch_dexscreener(client):
    global _dex_blocked_until
    if time.monotonic() < _dex_blocked_until:
        raise RuntimeError("DexScreener cooldown active after rate limiting")
    addresses, seen = [], set()
    errors = []
    # Stop after the first discovery endpoint that gives addresses.
    for label, url in (("token boosts", DEX_BOOSTS_URL), ("token profiles", DEX_PROFILES_URL)):
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
            errors.append(f"{label}: {type(exc).__name__} {str(exc)[:100]}")
            if time.monotonic() < _dex_blocked_until:
                break
    if not addresses:
        raise RuntimeError("; ".join(errors) or "no Solana token addresses returned")

    payload = await _get_json(client, DEX_TOKENS_URL + ",".join(addresses), "dex")
    best = {}
    for pair in payload.get("pairs") or []:
        if not isinstance(pair, dict) or (pair.get("chainId") or "").lower() != "solana":
            continue
        base = pair.get("baseToken") or {}
        address = base.get("address") or ""
        if not address:
            continue
        liquidity = _num((pair.get("liquidity") or {}).get("usd"))
        volume = _num((pair.get("volume") or {}).get("h24"))
        txns = (pair.get("txns") or {}).get("h24") or {}
        buys, sells = int(_num(txns.get("buys"))), int(_num(txns.get("sells")))
        candidate = {
            "chain": "solana", "token_name": base.get("name") or "Unknown",
            "token_symbol": base.get("symbol") or "UNKNOWN", "token_address": address,
            "pair_address": pair.get("pairAddress") or "", "dex": pair.get("dexId"),
            "price_usd": pair.get("priceUsd"), "liquidity_usd": round(liquidity, 2),
            "volume_24h_usd": round(volume, 2), "buys_24h": buys, "sells_24h": sells,
            "buys_to_sells_ratio": round(buys / max(sells, 1), 3),
            "price_change_24h_pct": _num((pair.get("priceChange") or {}).get("h24")),
            "fdv_usd": pair.get("fdv"), "pair_created_at_ms": pair.get("pairCreatedAt"),
            "profile": {}, "source": "DexScreener fallback",
        }
        previous = best.get(address)
        if previous is None or liquidity > previous["liquidity_usd"]:
            best[address] = candidate
    if not best:
        raise RuntimeError("DexScreener returned no Solana trading pairs")
    return sorted(best.values(), key=lambda x: (x["liquidity_usd"], x["volume_24h_usd"]), reverse=True)


async def _fetch_one_provider(provider):
    global _gecko_last_error, _dex_last_error, _last_provider, _gecko_blocked_until, _dex_blocked_until
    headers = {"Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.4"}
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers, follow_redirects=True) as client:
        if provider == "gecko":
            if time.monotonic() < _gecko_blocked_until:
                raise RuntimeError("GeckoTerminal cooldown active after rate limiting")
            try:
                data = await _fetch_gecko(client)
                if not data:
                    raise RuntimeError("GeckoTerminal returned no pools")
                _gecko_last_error = None
                _last_provider = "GeckoTerminal public API"
                return data, _last_provider
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    _gecko_blocked_until = max(_gecko_blocked_until, time.monotonic() + GECKO_COOLDOWN_SECONDS)
                _gecko_last_error = f"GeckoTerminal HTTP {exc.response.status_code}"
                raise
            except Exception as exc:
                _gecko_last_error = f"GeckoTerminal {type(exc).__name__}: {str(exc)[:120]}"
                raise
        if time.monotonic() < _dex_blocked_until:
            raise RuntimeError("DexScreener cooldown active after rate limiting")
        try:
            data = await _fetch_dexscreener(client)
            _dex_last_error = None
            _last_provider = "DexScreener fallback"
            return data, _last_provider
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                _dex_blocked_until = max(_dex_blocked_until, time.monotonic() + DEX_COOLDOWN_SECONDS)
            _dex_last_error = f"DexScreener HTTP {exc.response.status_code}"
            raise
        except Exception as exc:
            _dex_last_error = f"DexScreener {type(exc).__name__}: {str(exc)[:120]}"
            raise


async def get_signal_candidates(min_liquidity_usd=10000, min_volume_24h_usd=20000, min_buys_sells_ratio=1.0, limit=20):
    global _last_provider, _next_provider
    async with _lock:
        now = time.monotonic()
        cache_exists = _cache["value"] is not None
        if cache_exists and now - _cache["at"] < CACHE_SECONDS:
            base = _cache["value"]
            provider = f"Cached data ({_cache['provider'] or 'unknown source'})"
        else:
            # Try only one provider per scan. Alternate on later scans to reduce bursts.
            providers = [_next_provider, "dex" if _next_provider == "gecko" else "gecko"]
            selected = next((p for p in providers if time.monotonic() >= (
                _gecko_blocked_until if p == "gecko" else _dex_blocked_until
            )), None)
            if selected is None:
                if cache_exists:
                    base = _cache["value"]
                    provider = f"Stale cached fallback ({_cache['provider'] or 'unknown source'}); providers cooling down"
                else:
                    base, provider = [], "No data (providers cooling down)"
            else:
                try:
                    base, provider = await _fetch_one_provider(selected)
                    _cache.update({"value": base, "at": time.monotonic(), "provider": provider})
                    _last_provider = provider
                    _next_provider = "dex" if selected == "gecko" else "gecko"
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    if cache_exists:
                        base = _cache["value"]
                        provider = f"Stale cached fallback ({_cache['provider'] or 'unknown source'})"
                    else:
                        base, provider = [], f"No data: {type(exc).__name__}: {str(exc)[:150]}"
                    _next_provider = "dex" if selected == "gecko" else "gecko"

        filtered = [x for x in base if
                    _num(x.get("liquidity_usd")) >= min_liquidity_usd and
                    _num(x.get("volume_24h_usd")) >= min_volume_24h_usd and
                    _num(x.get("buys_to_sells_ratio")) >= min_buys_sells_ratio]
        filtered.sort(key=lambda x: (_num(x.get("liquidity_usd")), _num(x.get("volume_24h_usd"))), reverse=True)
        return {
            "mode": "paper", "provider": provider, "profiles_and_pairs_checked": len(base),
            "candidates_count": len(filtered[:limit]),
            "filters": {"min_liquidity_usd": min_liquidity_usd, "min_volume_24h_usd": min_volume_24h_usd,
                        "min_buys_sells_ratio": min_buys_sells_ratio},
            "warning": "Paper simulation only. Signals are not recommendations. Cached data may be stale; no profitability is implied.",
            "signals": filtered[:limit], "timestamp": datetime.now(timezone.utc).isoformat(),
        }
