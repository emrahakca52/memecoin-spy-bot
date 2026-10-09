
import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Güvenlik: gerçek işlemler kapalıdır.
REAL_TRADING_ENABLED = False
TRADING_MODE = "paper"

DEX_BASE_URL = "https://api.dexscreener.com"
PROFILE_URL = f"{DEX_BASE_URL}/token-profiles/latest/v1"
BOOST_URL = f"{DEX_BASE_URL}/token-boosts/latest/v1"
TOKENS_URL = f"{DEX_BASE_URL}/tokens/v1/solana"

GECKO_DEMO_BASE = "https://api.coingecko.com/api/v3"
GECKO_NEW_POOLS_URL = (
    f"{GECKO_DEMO_BASE}/onchain/networks/solana/new_pools"
)
GECKO_TRENDING_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
)

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", "1.0"))

CACHE_SECONDS = 120
ERROR_COOLDOWN_SECONDS = 60
MAX_ERROR_COOLDOWN_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 20
MAX_DISCOVERED_ADDRESSES = 60
MAX_CANDIDATE_PREVIEW = 20

WSOL_MINT = "So11111111111111111111111111111111111111112"
ALLOWED_QUOTES = {"SOL", "WSOL", "USDC", "USDT"}

_cache: dict[str, Any] = {
    "time": 0.0,
    "candidates": [],
    "error": None,
    "diagnostics": {},
}

# Her sağlayıcı için bağımsız bekleme süresi.
_provider_cooldowns: dict[str, float] = {}
_provider_errors: dict[str, int] = {}

_lock = asyncio.Lock()


def _number(value, default=0.0):
    try:
        result = float(value)
        if result != result or abs(result) == float("inf"):
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


async def _get_json(
    client,
    url,
    *,
    headers=None,
    params=None,
    provider="provider",
):
    now = time.time()
    cooldown_until = _provider_cooldowns.get(provider, 0.0)

    if now < cooldown_until:
        raise RuntimeError(f"{provider}_cooldown")

    response = await client.get(url, headers=headers, params=params)

    if response.status_code == 429:
        count = _provider_errors.get(provider, 0) + 1
        _provider_errors[provider] = count

        delay = min(
            ERROR_COOLDOWN_SECONDS * (2 ** min(count - 1, 4)),
            MAX_ERROR_COOLDOWN_SECONDS,
        )

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 3600))
            except (TypeError, ValueError):
                pass

        _provider_cooldowns[provider] = (
            time.time() + delay + random.uniform(1, 5)
        )
        raise RuntimeError(f"{provider}_rate_limited")

    response.raise_for_status()
    _provider_errors[provider] = 0
    _provider_cooldowns.pop(provider, None)
    return response.json()


def _dedupe(candidates):
    unique = {}

    for item in candidates:
        address = str(item.get("token_address") or "").strip()
        if not address:
            continue

        old = unique.get(address)

        if old is None or item["liquidity_usd"] > old["liquidity_usd"]:
            unique[address] = item

    return sorted(
        unique.values(),
        key=lambda item: item.get("score", 0),
        reverse=True,
    )


def _extract_token_addresses(payload):
    if not isinstance(payload, list):
        return []

    result = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        if str(item.get("chainId", "")).lower() != "solana":
            continue

        address = str(item.get("tokenAddress") or "").strip()

        if address and address != WSOL_MINT:
            result.append(address)

    return list(dict.fromkeys(result))


def _candidate(
    address,
    symbol,
    name,
    quote_symbol,
    pool_address,
    price,
    liquidity,
    volume,
    buys,
    sells,
    source,
    url,
):
    address = str(address or "").strip()
    price = _number(price)
    liquidity = _number(liquidity)
    volume = _number(volume)
    buys = _number(buys)
    sells = _number(sells)

    if (
        not address
        or address == WSOL_MINT
        or price <= 0
        or liquidity < 0
        or volume < 0
        or buys + sells <= 0
    ):
        return None

    ratio = buys / max(sells, 1.0)
    score = (
        min(volume / max(liquidity, 1.0), 10.0) * 5
        + min(ratio, 3.0) * 5
    )

    return {
        "token_address": address,
        "symbol": symbol or "UNKNOWN",
        "name": name or "",
        "quote_symbol": quote_symbol or "UNKNOWN",
        "pool_address": pool_address or "",
        "price_usd": price,
        "liquidity_usd": liquidity,
        "volume_24h_usd": volume,
        "buys_24h": int(buys),
        "sells_24h": int(sells),
        "buy_sell_ratio": round(ratio, 3),
        "score": round(score, 3),
        "source": source,
        "url": url or "",
    }


async def _discover_dex_pairs(client):
    profiles = await _get_json(
        client, PROFILE_URL, provider="dexscreener"
    )

    # İlk çağrı başarılıysa boost akışını da dene.
    boosts = await _get_json(
        client, BOOST_URL, provider="dexscreener"
    )

    addresses = list(
        dict.fromkeys(
            _extract_token_addresses(profiles)
            + _extract_token_addresses(boosts)
        )
    )[:MAX_DISCOVERED_ADDRESSES]

    pairs = []
    errors = []

    for start in range(0, len(addresses), 30):
        batch = addresses[start:start + 30]

        try:
            payload = await _get_json(
                client,
                f"{TOKENS_URL}/" + ",".join(batch),
                provider="dexscreener",
            )

            if isinstance(payload, list):
                pairs.extend(payload)

        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(
                f"{type(exc).__name__}: {str(exc)[:100]}"
            )

    if not pairs and errors:
        raise RuntimeError("dexscreener_token_pair_requests_failed")

    return pairs, len(addresses), errors


def _normalize_dex_pairs(pairs):
    candidates = []
    stats = {
        "pairs_received": len(pairs),
        "solana_pairs": 0,
        "passed_raw_checks": 0,
    }

    for pair in pairs:
        if not isinstance(pair, dict):
            continue

        if str(pair.get("chainId", "")).lower() != "solana":
            continue

        stats["solana_pairs"] += 1

        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}

        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue

        quote_symbol = str(quote.get("symbol") or "").strip().upper()

        if quote_symbol not in ALLOWED_QUOTES:
            continue

        txns = pair.get("txns") or {}
        h24 = txns.get("h24") or {}

        item = _candidate(
            str(base.get("address") or "").strip(),
            str(base.get("symbol") or "UNKNOWN"),
            str(base.get("name") or ""),
            quote_symbol,
            str(pair.get("pairAddress") or ""),
            _number(pair.get("priceUsd")),
            _number((pair.get("liquidity") or {}).get("usd")),
            _number((pair.get("volume") or {}).get("h24")),
            _number(h24.get("buys")),
            _number(h24.get("sells")),
            "DEX Screener",
            str(pair.get("url") or ""),
        )

        if item:
            candidates.append(item)
            stats["passed_raw_checks"] += 1

    return _dedupe(candidates), stats


def _included_index(payload):
    included = payload.get("included", []) if isinstance(payload, dict) else []

    return {
        str(item.get("id")): item
        for item in included
        if isinstance(item, dict) and item.get("id")
    }


def _normalize_coingecko_pools(payload):
    data = payload.get("data", []) if isinstance(payload, dict) else []
    included = _included_index(payload)
    candidates = []

    for pool in data:
        if not isinstance(pool, dict):
            continue

        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}

        base_id = str(
            (((rels.get("base_token") or {}).get("data") or {}).get("id"))
            or ""
        )
        quote_id = str(
            (((rels.get("quote_token") or {}).get("data") or {}).get("id"))
            or ""
        )

        base_attrs = (included.get(base_id) or {}).get("attributes") or {}
        quote_attrs = (included.get(quote_id) or {}).get("attributes") or {}

        address = str(
            base_attrs.get("address")
            or (base_id.split("_", 1)[1] if "_" in base_id else "")
        )
        quote_symbol = str(
            quote_attrs.get("symbol") or "UNKNOWN"
        ).upper()

        txns = attrs.get("transactions") or {}
        h24 = txns.get("h24") or {}

        pool_address = str(attrs.get("address") or "")
        item = _candidate(
            address,
            str(base_attrs.get("symbol") or "UNKNOWN"),
            str(base_attrs.get("name") or attrs.get("name") or ""),
            quote_symbol,
            pool_address or str(pool.get("id") or ""),
            _number(attrs.get("base_token_price_usd")),
            _number(attrs.get("reserve_in_usd")),
            _number((attrs.get("volume_usd") or {}).get("h24")),
            _number(h24.get("buys")),
            _number(h24.get("sells")),
            "CoinGecko",
            f"https://www.geckoterminal.com/solana/pools/{pool_address}",
        )

        if item:
            candidates.append(item)

    return _dedupe(candidates), {
        "provider": "CoinGecko",
        "coingecko_pools_received": len(data),
        "coingecko_candidates": len(candidates),
    }


def _normalize_gecko_pools(payload):
    data = payload.get("data", []) if isinstance(payload, dict) else []
    candidates = []

    for pool in data:
        if not isinstance(pool, dict):
            continue

        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}

        base_id = str(
            (((rels.get("base_token") or {}).get("data") or {}).get("id"))
            or ""
        )

        address = (
            base_id.split("_", 1)[1]
            if base_id.startswith("solana_")
            else str(attrs.get("base_token_address") or "")
        )

        txns = attrs.get("transactions") or {}
        h24 = txns.get("h24") or {}
        name = str(attrs.get("name") or "")
        pool_address = str(attrs.get("address") or "")

        item = _candidate(
            address,
            name.split(" / ")[0].strip() if name else "UNKNOWN",
            name,
            "UNKNOWN",
            pool_address,
            _number(attrs.get("base_token_price_usd")),
            _number(attrs.get("reserve_in_usd")),
            _number((attrs.get("volume_usd") or {}).get("h24")),
            _number(h24.get("buys")),
            _number(h24.get("sells")),
            "GeckoTerminal",
            f"https://www.geckoterminal.com/solana/pools/{pool_address}",
        )

        if item:
            candidates.append(item)

    return _dedupe(candidates), {
        "provider": "GeckoTerminal",
        "gecko_pools_received": len(data),
        "gecko_candidates": len(candidates),
    }


async def _fetch_coingecko():
    api_key = os.getenv("COINGECKO_API_KEY", "").strip()

    if not api_key:
        raise RuntimeError("coingecko_api_key_missing")

    all_pools = []
    all_included = []
    page_errors = []
    pages_scanned = 0

    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.6",
        "x-cg-demo-api-key": api_key,
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        for page in (1, 2, 3):
            try:
                payload = await _get_json(
                    client,
                    GECKO_NEW_POOLS_URL,
                    params={
                        "include": "base_token,quote_token",
                        "page": page,
                    },
                    provider="coingecko",
                )

                data = payload.get("data", []) if isinstance(payload, dict) else []

                if not data:
                    break

                all_pools.extend(data)

                included = payload.get("included", [])
                if isinstance(included, list):
                    all_included.extend(included)

                pages_scanned += 1

            except Exception as exc:
                page_errors.append(
                    f"page_{page}:{type(exc).__name__}:{str(exc)[:100]}"
                )
                if not all_pools:
                    raise
                break

            if page < 3:
                await asyncio.sleep(0.35)

    combined_payload = {
        "data": all_pools,
        "included": all_included,
    }

    candidates, diagnostics = _normalize_coingecko_pools(combined_payload)

    diagnostics.update({
        "coingecko_pages_scanned": pages_scanned,
        "coingecko_page_errors": page_errors,
    })

    return candidates, diagnostics


async def _fetch_gecko_fallback():
    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.6",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        payload = await _get_json(
            client,
            GECKO_TRENDING_URL,
            provider="geckoterminal",
        )

    return _normalize_gecko_pools(payload)


async def _fetch_dexscreener():
    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.6",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        pairs, discovered, batch_errors = await _discover_dex_pairs(client)

    candidates, diagnostics = _normalize_dex_pairs(pairs)

    diagnostics.update({
        "provider": "DEX Screener",
        "discovered_solana_tokens": discovered,
        "batch_errors": batch_errors,
    })

    return candidates, diagnostics


async def _fetch_candidates():
    """
    Kaynakları birleştirir.
    Tek bir kaynağın hatası diğer kaynakları durdurmaz.
    """
    combined = []
    source_diagnostics = []
    errors = []

    providers = (
        ("CoinGecko", _fetch_coingecko),
        ("DEX Screener", _fetch_dexscreener),
        ("GeckoTerminal", _fetch_gecko_fallback),
    )

    for provider_name, fetcher in providers:
        try:
            candidates, diagnostics = await fetcher()
            combined.extend(candidates)

            source_diagnostics.append({
                "source": provider_name,
                "status": "ok",
                **diagnostics,
            })

        except Exception as exc:
            message = (
                f"{provider_name}:{type(exc).__name__}:"
                f"{str(exc)[:120]}"
            )
            errors.append(message)

            source_diagnostics.append({
                "source": provider_name,
                "status": "error",
                "error": message,
            })

            logger.warning("Market source failed: %s", message)

    combined = _dedupe(combined)

    if not combined:
        raise RuntimeError(
            "; ".join(errors) if errors else "no_market_data"
        )

    return combined, {
        "provider": "Combined",
        "sources": source_diagnostics,
        "source_errors": errors,
        "unique_candidates": len(combined),
    }


async def get_signal_candidates(
    limit=20,
    min_liquidity_usd=None,
    min_volume_24h_usd=None,
    min_buys_sells_ratio=None,
    **kwargs,
):
    liquidity_floor = max(
        0.0,
        _number(
            MIN_LIQUIDITY_USD
            if min_liquidity_usd is None
            else min_liquidity_usd,
            MIN_LIQUIDITY_USD,
        ),
    )

    volume_floor = max(
        0.0,
        _number(
            MIN_VOLUME_24H_USD
            if min_volume_24h_usd is None
            else min_volume_24h_usd,
            MIN_VOLUME_24H_USD,
        ),
    )

    ratio_floor = max(
        0.0,
        _number(
            MIN_BUY_SELL_RATIO
            if min_buys_sells_ratio is None
            else min_buys_sells_ratio,
            MIN_BUY_SELL_RATIO,
        ),
    )

    async with _lock:
        now = time.time()
        fresh = (
            _cache["time"] > 0
            and now - _cache["time"] < CACHE_SECONDS
        )

        if fresh:
            candidates = _cache["candidates"]
            diagnostics = _cache["diagnostics"]
            error = _cache["error"]

        else:
            try:
                candidates, diagnostics = await _fetch_candidates()

                _cache.update({
                    "time": time.time(),
                    "candidates": candidates,
                    "diagnostics": diagnostics,
                    "error": None,
                })

                error = None

            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
                logger.warning("Market scan failed: %s", error)

                _cache["error"] = error
                candidates = _cache["candidates"]
                diagnostics = _cache["diagnostics"]

        filtered = [
            item for item in candidates
            if item["liquidity_usd"] >= liquidity_floor
            and item["volume_24h_usd"] >= volume_floor
            and item["buy_sell_ratio"] >= ratio_floor
        ]

        preview = [
            {
                "symbol": str(item.get("symbol") or "UNKNOWN"),
                "name": str(item.get("name") or ""),
                "liquidity_usd": round(
                    _number(item.get("liquidity_usd")), 2
                ),
                "volume_24h_usd": round(
                    _number(item.get("volume_24h_usd")), 2
                ),
                "buy_sell_ratio": round(
                    _number(item.get("buy_sell_ratio")), 3
                ),
                "score": round(_number(item.get("score")), 3),
                "source": str(item.get("source") or ""),
                "url": str(item.get("url") or ""),
            }
            for item in candidates[:MAX_CANDIDATE_PREVIEW]
        ]

        diag = {
            **diagnostics,
            "active_min_liquidity_usd": liquidity_floor,
            "active_min_volume_24h_usd": volume_floor,
            "active_min_buy_sell_ratio": ratio_floor,
            "unique_candidate_count": len(candidates),
            "below_liquidity_filter": sum(
                item["liquidity_usd"] < liquidity_floor
                for item in candidates
            ),
            "below_volume_filter": sum(
                item["volume_24h_usd"] < volume_floor
                for item in candidates
            ),
            "below_ratio_filter": sum(
                item["buy_sell_ratio"] < ratio_floor
                for item in candidates
            ),
            "candidate_preview": preview,
        }

    try:
        safe_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        safe_limit = 20

    signals = filtered[:safe_limit]

    if signals:
        note = (
            "Filtered market candidates only; "
            "not investment advice or a profit guarantee."
        )
    elif error:
        note = (
            "Market scan failed or cached data is being used; "
            "inspect last_error and diagnostics."
        )
    else:
        note = (
            "Market data received, but no candidates passed "
            "the active filters."
        )

    return {
        "provider": diag.get("provider", "Combined"),
        "checked": diag.get(
            "unique_candidate_count",
            len(candidates),
        ),
        "candidate_count": len(filtered),
        "signals": signals,
        "diagnostics": diag,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "note": note,
        "last_error": error,
    }
