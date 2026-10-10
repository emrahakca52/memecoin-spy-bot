import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Safety: this module only discovers and scores candidates.
# It never sends orders or enables real trading.
REAL_TRADING_ENABLED = False
TRADING_MODE = "paper"

DEX_BASE = "https://api.dexscreener.com"
DEX_PROFILES_URL = f"{DEX_BASE}/token-profiles/latest/v1"
DEX_BOOSTS_URL = f"{DEX_BASE}/token-boosts/latest/v1"
DEX_TOKENS_URL = f"{DEX_BASE}/tokens/v1/solana"

COINGECKO_NEW_POOLS_URL = (
    "https://api.coingecko.com/api/v3/onchain/networks/solana/new_pools"
)

COINGECKO_DEMO_API_KEY = (
    os.getenv("COINGECKO_DEMO_API_KEY")
    or os.getenv("COINGECKO_API_KEY")
    or ""
).strip()

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))

CACHE_SECONDS = int(os.getenv("SIGNAL_CACHE_SECONDS", "180"))
ERROR_COOLDOWN_SECONDS = int(os.getenv("ERROR_COOLDOWN_SECONDS", "60"))
MAX_ERROR_COOLDOWN_SECONDS = int(os.getenv("MAX_ERROR_COOLDOWN_SECONDS", "600"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
MAX_DISCOVERED_ADDRESSES = int(os.getenv("MAX_DISCOVERED_ADDRESSES", "60"))
PREVIEW_LIMIT = int(os.getenv("SIGNAL_PREVIEW_LIMIT", "20"))

# Per-source cooldowns reduce repeated requests when a provider rate-limits us.
_provider_cooldowns: dict[str, float] = {}
_provider_errors: dict[str, int] = {}
_provider_last_status: dict[str, int | None] = {}
_provider_last_error: dict[str, str | None] = {}
_provider_requests: dict[str, int] = {}
_lock = asyncio.Lock()
_cache: dict[str, Any] = {
    "timestamp": 0.0,
    "candidates": [],
    "diagnostics": {},
}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _first(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _cooldown_remaining(provider: str) -> float:
    return max(0.0, _provider_cooldowns.get(provider, 0.0) - time.monotonic())


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    provider: str,
    params: dict | None = None,
    headers: dict | None = None,
) -> Any:
    remaining = _cooldown_remaining(provider)
    if remaining > 0:
        message = f"{provider} cooldown active ({int(remaining)}s remaining)"
        _provider_last_error[provider] = message
        raise RuntimeError(message)

    _provider_requests[provider] = _provider_requests.get(provider, 0) + 1
    _provider_last_status[provider] = None
    _provider_last_error[provider] = None
    try:
        response = await client.get(url, params=params, headers=headers)
        _provider_last_status[provider] = response.status_code
        if response.status_code == 429:
            _provider_errors[provider] = _provider_errors.get(provider, 0) + 1
            exponent = min(_provider_errors[provider] - 1, 5)
            delay = min(
                MAX_ERROR_COOLDOWN_SECONDS,
                ERROR_COOLDOWN_SECONDS * (2 ** exponent),
            )
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, min(float(retry_after), 3600.0))
                except ValueError:
                    pass
            delay += random.uniform(0, min(10.0, delay * 0.1))
            _provider_cooldowns[provider] = time.monotonic() + delay
            message = f"{provider} returned HTTP 429; cooldown {int(delay)}s"
            _provider_last_error[provider] = message
            raise RuntimeError(message)

        response.raise_for_status()
        _provider_errors[provider] = 0
        _provider_cooldowns.pop(provider, None)
        return response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        _provider_last_status[provider] = status
        _provider_last_error[provider] = f"{provider} HTTP {status}"
        if status in (403, 408, 425, 500, 502, 503, 504):
            _provider_errors[provider] = _provider_errors.get(provider, 0) + 1
            exponent = min(_provider_errors[provider] - 1, 4)
            delay = min(
                MAX_ERROR_COOLDOWN_SECONDS,
                ERROR_COOLDOWN_SECONDS * (2 ** exponent),
            )
            _provider_cooldowns[provider] = time.monotonic() + delay
        raise
    except httpx.HTTPError as exc:
        _provider_last_error[provider] = f"{provider} network error: {type(exc).__name__}"
        raise


def _dex_token_address(item: dict) -> str | None:
    chain = str(item.get("chainId") or "").lower()
    address = item.get("tokenAddress")
    if chain == "solana" and address:
        return str(address)
    return None


async def _discover_dex_pairs(client: httpx.AsyncClient) -> tuple[list[dict], list[str]]:
    """Fetch profiles and boosts independently; keep partial results if one fails."""
    addresses: list[str] = []
    errors: list[str] = []

    for label, url in (
        ("dex_profiles", DEX_PROFILES_URL),
        ("dex_boosts", DEX_BOOSTS_URL),
    ):
        try:
            payload = await _get_json(client, url, provider="dexscreener")
            if isinstance(payload, dict):
                items = payload.get("data", [])
            else:
                items = payload if isinstance(payload, list) else []
            for item in items:
                if isinstance(item, dict):
                    address = _dex_token_address(item)
                    if address and address not in addresses:
                        addresses.append(address)
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
            if _cooldown_remaining("dexscreener") > 0:
                break

    addresses = addresses[:MAX_DISCOVERED_ADDRESSES]
    pairs: list[dict] = []
    for start in range(0, len(addresses), 30):
        batch = addresses[start : start + 30]
        try:
            payload = await _get_json(
                client,
                f"{DEX_TOKENS_URL}/{','.join(batch)}",
                provider="dexscreener",
            )
            if isinstance(payload, list):
                pairs.extend(item for item in payload if isinstance(item, dict))
            elif isinstance(payload, dict):
                items = payload.get("pairs", payload.get("data", []))
                if isinstance(items, list):
                    pairs.extend(item for item in items if isinstance(item, dict))
        except Exception as exc:
            errors.append(f"dex_token_lookup: {type(exc).__name__}: {str(exc)[:160]}")
            break

    return pairs, errors


def _normalize_dex_pair(pair: dict) -> dict | None:
    if str(pair.get("chainId") or "").lower() != "solana":
        return None
    base = pair.get("baseToken") or {}
    address = base.get("address")
    if not address:
        return None

    txns = pair.get("txns") or {}
    h24 = txns.get("h24") or {}
    buys = _as_float(h24.get("buys"))
    sells = _as_float(h24.get("sells"))
    ratio = buys / max(sells, 1.0)
    volume = pair.get("volume") or {}
    liquidity = pair.get("liquidity") or {}

    return {
        "token_address": str(address),
        "symbol": str(base.get("symbol") or "UNKNOWN"),
        "token_symbol": str(base.get("symbol") or "UNKNOWN"),
        "name": str(base.get("name") or ""),
        "price_usd": _as_float(pair.get("priceUsd")),
        "liquidity_usd": _as_float(liquidity.get("usd")),
        "volume_24h_usd": _as_float(volume.get("h24")),
        "buys_24h": int(buys),
        "sells_24h": int(sells),
        "buy_sell_ratio": ratio,
        "buys_to_sells_ratio": ratio,
        "pool_address": pair.get("pairAddress"),
        "pair_address": pair.get("pairAddress"),
        "provider": "DexScreener",
        "url": pair.get("url"),
        "created_at": pair.get("pairCreatedAt"),
    }


async def _fetch_coingecko(client: httpx.AsyncClient) -> tuple[list[dict], list[str]]:
    pools: list[dict] = []
    errors: list[str] = []
    raw_pool_count = 0
    headers = {"accept": "application/json"}
    if COINGECKO_DEMO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_DEMO_API_KEY

    for page in (1, 2, 3):
        try:
            payload = await _get_json(
                client,
                COINGECKO_NEW_POOLS_URL,
                provider="coingecko",
                params={"page": page, "include": "base_token"},
                headers=headers,
            )
            data = payload.get("data", []) if isinstance(payload, dict) else []
            raw_pool_count += len(data) if isinstance(data, list) else 0
            included = payload.get("included", []) if isinstance(payload, dict) else []
            included_by_id = {
                obj.get("id"): obj
                for obj in included
                if isinstance(obj, dict) and obj.get("id")
            }

            for pool in data:
                if not isinstance(pool, dict):
                    continue
                attrs = pool.get("attributes") or {}
                rels = pool.get("relationships") or {}
                base_rel = (rels.get("base_token") or {}).get("data") or {}
                base_obj = included_by_id.get(base_rel.get("id"), {})
                base_attrs = base_obj.get("attributes") or {}
                address = base_attrs.get("address") or attrs.get("base_token_address")
                if not address:
                    continue

                volume = attrs.get("volume_usd") or {}
                txns = attrs.get("transactions") or {}
                h24 = txns.get("h24") or {}
                buys = _as_float(h24.get("buys"))
                sells = _as_float(h24.get("sells"))
                ratio = buys / max(sells, 1.0)
                liquidity = _as_float(attrs.get("reserve_in_usd"))
                price = _as_float(attrs.get("base_token_price_usd"))
                pool_id = pool.get("id", "")
                pool_address = attrs.get("address") or (
                    pool_id.split("_", 1)[1] if "_" in pool_id else pool_id
                )

                pools.append({
                    "token_address": str(address),
                    "symbol": str(base_attrs.get("symbol") or "UNKNOWN"),
                    "token_symbol": str(base_attrs.get("symbol") or "UNKNOWN"),
                    "name": str(base_attrs.get("name") or ""),
                    "price_usd": price,
                    "liquidity_usd": liquidity,
                    "volume_24h_usd": _as_float(volume.get("h24")),
                    "buys_24h": int(buys),
                    "sells_24h": int(sells),
                    "buy_sell_ratio": ratio,
                    "buys_to_sells_ratio": ratio,
                    "pool_address": pool_address,
                    "pair_address": pool_address,
                    "provider": "CoinGecko",
                    "url": None,
                    "created_at": attrs.get("pool_created_at"),
                })
        except Exception as exc:
            errors.append(f"coingecko_page_{page}: {type(exc).__name__}: {str(exc)[:160]}")
            if _cooldown_remaining("coingecko") > 0:
                break
        if page != 3:
            await asyncio.sleep(0.35)

    _fetch_coingecko.last_raw_pool_count = raw_pool_count
    return pools, errors


_fetch_coingecko.last_raw_pool_count = 0


async def _fetch_candidates() -> tuple[list[dict], dict]:
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=8.0)
    limits = httpx.Limits(max_connections=5, max_keepalive_connections=3)
    all_candidates: list[dict] = []
    source_errors: list[str] = []
    source_counts: dict[str, int] = {}

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        dex_pairs, dex_errors = await _discover_dex_pairs(client)
        source_errors.extend(dex_errors)
        normalized_dex = [
            item for pair in dex_pairs
            if (item := _normalize_dex_pair(pair)) is not None
        ]
        source_counts["DexScreener"] = len(normalized_dex)
        all_candidates.extend(normalized_dex)

        cg_pools, cg_errors = await _fetch_coingecko(client)
        source_errors.extend(cg_errors)
        source_counts["CoinGecko"] = len(cg_pools)
        source_counts["CoinGecko_raw_pools"] = getattr(
            _fetch_coingecko, "last_raw_pool_count", len(cg_pools)
        )
        all_candidates.extend(cg_pools)

    # GeckoTerminal removed from discovery because its endpoint was returning 429.
    # DexScreener and CoinGecko continue independently as discovery sources.
    by_address: dict[str, dict] = {}
    for candidate in all_candidates:
        address = candidate.get("token_address")
        if not address:
            continue
        old = by_address.get(address)
        if old is None:
            by_address[address] = candidate
            continue
        old_quality = (
            int(_as_float(old.get("liquidity_usd")) > 0)
            + int(_as_float(old.get("volume_24h_usd")) > 0)
            + int(_as_float(old.get("price_usd")) > 0)
        )
        new_quality = (
            int(_as_float(candidate.get("liquidity_usd")) > 0)
            + int(_as_float(candidate.get("volume_24h_usd")) > 0)
            + int(_as_float(candidate.get("price_usd")) > 0)
        )
        if new_quality > old_quality:
            by_address[address] = candidate

    candidates = list(by_address.values())
    diagnostics = {
        "provider": "Combined",
        "checked": len(candidates),
        "source_counts": source_counts,
        "source_errors": source_errors[-20:],
        "candidate_count": len(candidates),
        "note": (
            "Discovery uses DexScreener and CoinGecko only. "
            "GeckoTerminal discovery is disabled due to repeated HTTP 429 responses. "
            "Candidates are deduplicated across sources; paper mode only."
        ),
        "last_error": source_errors[-1] if source_errors else None,
    }
    return candidates, diagnostics


def _score(candidate: dict) -> float:
    liquidity = _as_float(candidate.get("liquidity_usd"))
    volume = _as_float(candidate.get("volume_24h_usd"))
    ratio = _as_float(
        _first(candidate, "buy_sell_ratio", "buys_to_sells_ratio", default=0)
    )
    return round(
        min(liquidity / 10000.0, 5.0)
        + min(volume / 20000.0, 5.0)
        + min(ratio, 5.0),
        3,
    )


async def get_signal_candidates(
    min_liquidity_usd: float | None = None,
    min_volume_24h_usd: float | None = None,
    min_buys_sells_ratio: float | None = None,
    limit: int = 20,
) -> dict:
    min_liquidity = (
        MIN_LIQUIDITY_USD if min_liquidity_usd is None else float(min_liquidity_usd)
    )
    min_volume = (
        MIN_VOLUME_24H_USD if min_volume_24h_usd is None else float(min_volume_24h_usd)
    )
    min_ratio = (
        MIN_BUYS_SELLS_RATIO
        if min_buys_sells_ratio is None
        else float(min_buys_sells_ratio)
    )

    now = time.monotonic()
    async with _lock:
        cache_is_fresh = (
            now - float(_cache.get("timestamp", 0.0)) < CACHE_SECONDS
            and isinstance(_cache.get("candidates"), list)
            and bool(_cache.get("diagnostics"))
        )
        if cache_is_fresh:
            candidates = list(_cache["candidates"])
            diagnostics = dict(_cache["diagnostics"])
        else:
            candidates, diagnostics = await _fetch_candidates()
            _cache.update({
                "timestamp": time.monotonic(),
                "candidates": candidates,
                "diagnostics": diagnostics,
            })

    filtered: list[dict] = []
    for candidate in candidates:
        liquidity = _as_float(candidate.get("liquidity_usd"))
        volume = _as_float(candidate.get("volume_24h_usd"))
        ratio = _as_float(
            _first(candidate, "buy_sell_ratio", "buys_to_sells_ratio", default=0)
        )
        price = _as_float(candidate.get("price_usd"))
        if (
            liquidity < min_liquidity
            or volume < min_volume
            or ratio < min_ratio
            or price <= 0
        ):
            continue
        item = dict(candidate)
        item["score"] = _score(item)
        filtered.append(item)

    filtered.sort(key=lambda item: item.get("score", 0), reverse=True)
    result_signals = filtered[: max(0, int(limit))]
    result_diagnostics = dict(diagnostics)
    result_diagnostics.update({
        "checked": len(candidates),
        "candidate_count": len(filtered),
        "filters": {
            "min_liquidity_usd": min_liquidity,
            "min_volume_24h_usd": min_volume,
            "min_buys_sells_ratio": min_ratio,
        },
        "candidate_preview": [
            {
                "symbol": item.get("symbol"),
                "token_address": item.get("token_address"),
                "liquidity_usd": item.get("liquidity_usd"),
                "volume_24h_usd": item.get("volume_24h_usd"),
                "buy_sell_ratio": item.get("buy_sell_ratio"),
                "score": item.get("score"),
                "provider": item.get("provider"),
            }
            for item in result_signals[:PREVIEW_LIMIT]
        ],
        "cached_for_seconds": CACHE_SECONDS,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
    })

    return {
        "provider": diagnostics.get("provider", "Combined"),
        "checked": len(candidates),
        "candidate_count": len(filtered),
        "signals": result_signals,
        "note": diagnostics.get("note"),
        "last_error": diagnostics.get("last_error"),
        "source_errors": diagnostics.get("source_errors", []),
        "diagnostics": result_diagnostics,
    }


def get_signal_engine_status() -> dict:
    now = time.monotonic()
    providers = set(_provider_requests) | set(_provider_errors) | set(_provider_last_status)
    return {
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "cache_age_seconds": (
            round(now - float(_cache.get("timestamp", 0.0)), 1)
            if _cache.get("timestamp")
            else None
        ),
        "cache_ttl_seconds": CACHE_SECONDS,
        "provider_cooldowns_seconds": {
            provider: round(max(0.0, expiry - now), 1)
            for provider, expiry in _provider_cooldowns.items()
            if expiry > now
        },
        "provider_error_counts": dict(_provider_errors),
        "provider_diagnostics": {
            provider: {
                "requests": _provider_requests.get(provider, 0),
                "last_http_status": _provider_last_status.get(provider),
                "last_error": _provider_last_error.get(provider),
            }
            for provider in sorted(providers)
        },
    }
