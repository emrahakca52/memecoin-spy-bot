import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)
REAL_TRADING_ENABLED = False
TRADING_MODE = "paper"

DEX_BASE_URL = "https://api.dexscreener.com"
PROFILE_URL = f"{DEX_BASE_URL}/token-profiles/latest/v1"
BOOST_URL = f"{DEX_BASE_URL}/token-boosts/latest/v1"
TOKENS_URL = f"{DEX_BASE_URL}/tokens/v1/solana"
GECKO_DEMO_BASE = "https://api.coingecko.com/api/v3"
GECKO_NEW_POOLS_URL = f"{GECKO_DEMO_BASE}/onchain/networks/solana/new_pools"
GECKO_TRENDING_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", "1.0"))
CACHE_SECONDS = 120
ERROR_COOLDOWN_SECONDS = 60
MAX_ERROR_COOLDOWN_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 20
MAX_DISCOVERED_ADDRESSES = 60
WSOL_MINT = "So11111111111111111111111111111111111111112"
ALLOWED_QUOTES = {"SOL", "WSOL", "USDC", "USDT"}

_cache: dict[str, Any] = {"time": 0.0, "candidates": [], "error": None, "diagnostics": {}}
_next_request_time = 0.0
_consecutive_errors = 0
_lock = asyncio.Lock()


def _number(value, default=0.0):
    try:
        result = float(value)
        if result != result or abs(result) == float("inf"):
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


async def _get_json(client, url, *, headers=None, params=None, provider="provider"):
    global _next_request_time, _consecutive_errors
    if time.time() < _next_request_time:
        raise RuntimeError("provider_cooldown")
    response = await client.get(url, headers=headers, params=params)
    if response.status_code == 429:
        _consecutive_errors += 1
        delay = min(ERROR_COOLDOWN_SECONDS * (2 ** min(_consecutive_errors - 1, 4)), MAX_ERROR_COOLDOWN_SECONDS)
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 3600))
            except ValueError:
                pass
        _next_request_time = time.time() + delay + random.uniform(1, 5)
        raise RuntimeError(f"{provider}_rate_limited:{url}")
    response.raise_for_status()
    return response.json()


def _extract_token_addresses(payload):
    if not isinstance(payload, list):
        return []
    result = []
    for item in payload:
        if not isinstance(item, dict) or str(item.get("chainId", "")).lower() != "solana":
            continue
        address = str(item.get("tokenAddress") or "").strip()
        if address and address != WSOL_MINT:
            result.append(address)
    return list(dict.fromkeys(result))


async def _discover_dex_pairs(client):
    profiles = await _get_json(client, PROFILE_URL, provider="dexscreener")
    # Avoid making the second discovery call if the first feed is already rate-limited.
    boosts = await _get_json(client, BOOST_URL, provider="dexscreener")
    addresses = list(dict.fromkeys(_extract_token_addresses(profiles) + _extract_token_addresses(boosts)))[:MAX_DISCOVERED_ADDRESSES]
    pairs, errors = [], []
    for start in range(0, len(addresses), 30):
        batch = addresses[start:start + 30]
        try:
            payload = await _get_json(client, f"{TOKENS_URL}/" + ",".join(batch), provider="dexscreener")
            if isinstance(payload, list):
                pairs.extend(payload)
        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:100]}")
    if not pairs and errors:
        raise RuntimeError("dexscreener_token_pair_requests_failed")
    return pairs, len(addresses), errors


def _candidate(address, symbol, name, quote_symbol, pool_address, price, liquidity, volume, buys, sells, source, url):
    if not address or address == WSOL_MINT or price <= 0 or buys + sells <= 0:
        return None
    ratio = buys / max(sells, 1.0)
    score = min(volume / max(liquidity, 1.0), 10.0) * 5 + min(ratio, 3.0) * 5
    return {
        "token_address": address, "symbol": symbol or "UNKNOWN", "name": name or "",
        "quote_symbol": quote_symbol or "UNKNOWN", "pool_address": pool_address or "",
        "price_usd": price, "liquidity_usd": liquidity, "volume_24h_usd": volume,
        "buys_24h": int(buys), "sells_24h": int(sells), "buy_sell_ratio": round(ratio, 3),
        "score": round(score, 3), "source": source, "url": url or "",
    }


def _normalize_dex_pairs(pairs):
    candidates, stats = [], {"pairs_received": len(pairs), "solana_pairs": 0, "passed_raw_checks": 0}
    for pair in pairs:
        if not isinstance(pair, dict) or str(pair.get("chainId", "")).lower() != "solana":
            continue
        stats["solana_pairs"] += 1
        base, quote = pair.get("baseToken") or {}, pair.get("quoteToken") or {}
        if not isinstance(base, dict) or not isinstance(quote, dict):
            continue
        quote_symbol = str(quote.get("symbol") or "").strip().upper()
        if quote_symbol not in ALLOWED_QUOTES:
            continue
        h24 = ((pair.get("txns") or {}).get("h24") or {})
        item = _candidate(
            str(base.get("address") or "").strip(), str(base.get("symbol") or "UNKNOWN"),
            str(base.get("name") or ""), quote_symbol, str(pair.get("pairAddress") or ""),
            _number(pair.get("priceUsd")), _number((pair.get("liquidity") or {}).get("usd")),
            _number((pair.get("volume") or {}).get("h24")), _number(h24.get("buys")), _number(h24.get("sells")),
            "DEX Screener", str(pair.get("url") or ""))
        if item:
            candidates.append(item)
            stats["passed_raw_checks"] += 1
    return _dedupe(candidates), stats


def _included_index(payload):
    return {str(x.get("id")): x for x in payload.get("included", []) if isinstance(x, dict) and x.get("id")}


def _normalize_coingecko_pools(payload):
    data = payload.get("data", []) if isinstance(payload, dict) else []
    included = _included_index(payload if isinstance(payload, dict) else {})
    candidates = []
    for pool in data:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}
        base_id = str((((rels.get("base_token") or {}).get("data") or {}).get("id")) or "")
        quote_id = str((((rels.get("quote_token") or {}).get("data") or {}).get("id")) or "")
        base_attrs = (included.get(base_id) or {}).get("attributes") or {}
        quote_attrs = (included.get(quote_id) or {}).get("attributes") or {}
        address = str(base_attrs.get("address") or (base_id.split("_", 1)[1] if "_" in base_id else ""))
        quote_symbol = str(quote_attrs.get("symbol") or "UNKNOWN").upper()
        txns = attrs.get("transactions") or {}
        h24 = txns.get("h24") or {}
        item = _candidate(
            address, str(base_attrs.get("symbol") or "UNKNOWN"), str(base_attrs.get("name") or attrs.get("name") or ""),
            quote_symbol, str(attrs.get("address") or pool.get("id") or ""),
            _number(attrs.get("base_token_price_usd")), _number(attrs.get("reserve_in_usd")),
            _number((attrs.get("volume_usd") or {}).get("h24")), _number(h24.get("buys")), _number(h24.get("sells")),
            "CoinGecko", f"https://www.geckoterminal.com/solana/pools/{attrs.get('address', '')}")
        if item:
            candidates.append(item)
    return _dedupe(candidates), {"provider": "CoinGecko", "coingecko_pools_received": len(data), "coingecko_candidates": len(candidates)}


def _normalize_gecko_pools(payload):
    data = payload.get("data", []) if isinstance(payload, dict) else []
    candidates = []
    for pool in data:
        if not isinstance(pool, dict):
            continue
        attrs = pool.get("attributes") or {}
        rels = pool.get("relationships") or {}
        base_id = str((((rels.get("base_token") or {}).get("data") or {}).get("id")) or "")
        address = base_id.split("_", 1)[1] if base_id.startswith("solana_") else str(attrs.get("base_token_address") or "")
        txns = attrs.get("transactions") or {}
        h24 = txns.get("h24") or {}
        name = str(attrs.get("name") or "")
        item = _candidate(address, name.split(" / ")[0].strip() if name else "UNKNOWN", name, "UNKNOWN",
            str(attrs.get("address") or ""), _number(attrs.get("base_token_price_usd")),
            _number(attrs.get("reserve_in_usd")), _number((attrs.get("volume_usd") or {}).get("h24")),
            _number(h24.get("buys")), _number(h24.get("sells")), "GeckoTerminal",
            f"https://www.geckoterminal.com/solana/pools/{attrs.get('address', '')}")
        if item:
            candidates.append(item)
    return _dedupe(candidates), {"provider": "GeckoTerminal", "gecko_pools_received": len(data), "gecko_candidates": len(candidates)}


def _dedupe(candidates):
    unique = {}
    for item in candidates:
        old = unique.get(item["token_address"])
        if old is None or item["liquidity_usd"] > old["liquidity_usd"]:
            unique[item["token_address"]] = item
    return sorted(unique.values(), key=lambda x: x["score"], reverse=True)


async def _fetch_coingecko():
    """Discover a broader sample of new Solana pools without relaxing trade filters.

    CoinGecko's on-chain new-pools endpoint is paginated. Read only a few pages per
    scan to stay conservative with the Demo API rate limit; any partial data is kept
    if a later page fails.
    """
    api_key = os.getenv("COINGECKO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("coingecko_api_key_missing")

    all_pools = []
    all_included = []
    page_errors = []
    pages_scanned = 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers={
        "Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.5", "x-cg-demo-api-key": api_key,
    }) as client:
        for page in (1, 2, 3):
            try:
                payload = await _get_json(
                    client,
                    GECKO_NEW_POOLS_URL,
                    params={"include": "base_token,quote_token", "page": page},
                    provider="coingecko",
                )
                data = payload.get("data", []) if isinstance(payload, dict) else []
                if not data:
                    break
                all_pools.extend(data)
                included = payload.get("included", []) if isinstance(payload, dict) else []
                if isinstance(included, list):
                    all_included.extend(included)
                pages_scanned += 1
            except Exception as exc:
                page_errors.append(f"page_{page}:{type(exc).__name__}:{str(exc)[:100]}")
                if not all_pools:
                    raise
                break
            # Small pause between paginated requests to avoid unnecessary bursts.
            if page < 3:
                await asyncio.sleep(0.35)

    combined_payload = {"data": all_pools, "included": all_included}
    candidates, diagnostics = _normalize_coingecko_pools(combined_payload)
    diagnostics.update({
        "coingecko_pages_scanned": pages_scanned,
        "coingecko_page_errors": page_errors,
        "fallback_used": True,
    })
    return candidates, diagnostics


async def _fetch_gecko_fallback():
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.4"}) as client:
        payload = await _get_json(client, GECKO_TRENDING_URL, provider="geckoterminal")
    candidates, diagnostics = _normalize_gecko_pools(payload)
    diagnostics["fallback_used"] = True
    return candidates, diagnostics


async def _fetch_candidates():
    global _next_request_time, _consecutive_errors
    errors = []
    # Try CoinGecko's authenticated Solana new-pools endpoint first; it is separate from the rate-limited feeds.
    try:
        candidates, diagnostics = await _fetch_coingecko()
        _consecutive_errors = 0
        _next_request_time = time.time() + CACHE_SECONDS
        return candidates, diagnostics
    except Exception as exc:
        errors.append(f"coingecko_failed:{type(exc).__name__}:{str(exc)[:120]}")
        logger.warning("CoinGecko scan failed: %s", errors[-1])

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.4"}) as client:
            pairs, discovered, batch_errors = await _discover_dex_pairs(client)
        candidates, diagnostics = _normalize_dex_pairs(pairs)
        diagnostics.update({"provider": "DEX Screener", "discovered_solana_tokens": discovered, "batch_errors": batch_errors})
        _consecutive_errors = 0
        _next_request_time = time.time() + CACHE_SECONDS
        return candidates, diagnostics
    except Exception as exc:
        errors.append(f"dexscreener_failed:{type(exc).__name__}:{str(exc)[:120]}")
        logger.warning("DEX Screener scan failed: %s", errors[-1])

    try:
        candidates, diagnostics = await _fetch_gecko_fallback()
        diagnostics["provider_errors"] = errors
        _consecutive_errors = 0
        _next_request_time = time.time() + CACHE_SECONDS
        return candidates, diagnostics
    except Exception as exc:
        errors.append(f"geckoterminal_failed:{type(exc).__name__}:{str(exc)[:120]}")
        _consecutive_errors += 1
        _next_request_time = time.time() + min(ERROR_COOLDOWN_SECONDS * (2 ** min(_consecutive_errors - 1, 4)), MAX_ERROR_COOLDOWN_SECONDS)
        raise RuntimeError("; ".join(errors)) from exc


async def get_signal_candidates(limit=20, min_liquidity_usd=None, min_volume_24h_usd=None, min_buys_sells_ratio=None, **kwargs):
    global _next_request_time
    liquidity_floor = max(0.0, _number(MIN_LIQUIDITY_USD if min_liquidity_usd is None else min_liquidity_usd, MIN_LIQUIDITY_USD))
    volume_floor = max(0.0, _number(MIN_VOLUME_24H_USD if min_volume_24h_usd is None else min_volume_24h_usd, MIN_VOLUME_24H_USD))
    ratio_floor = max(0.0, _number(MIN_BUY_SELL_RATIO if min_buys_sells_ratio is None else min_buys_sells_ratio, MIN_BUY_SELL_RATIO))

    async with _lock:
        now = time.time()
        fresh = _cache["time"] > 0 and now - _cache["time"] < CACHE_SECONDS
        if fresh:
            candidates, diagnostics, error = _cache["candidates"], _cache["diagnostics"], None
        elif now < _next_request_time:
            candidates, diagnostics = _cache["candidates"], _cache["diagnostics"]
            error = _cache["error"] or "provider_cooldown"
        else:
            try:
                candidates, diagnostics = await _fetch_candidates()
                _cache.update({"time": time.time(), "candidates": candidates, "diagnostics": diagnostics, "error": None})
                error = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:300]}"
                logger.warning("Market scan failed: %s", error)
                _cache["error"] = error
                candidates, diagnostics = _cache["candidates"], _cache["diagnostics"]

        filtered = [x for x in candidates if x["liquidity_usd"] >= liquidity_floor and x["volume_24h_usd"] >= volume_floor and x["buy_sell_ratio"] >= ratio_floor]
        # Read-only preview helps explain why candidates fail filters; it never changes trade decisions.
        candidate_preview = [
            {
                "symbol": str(x.get("symbol") or "UNKNOWN"),
                "name": str(x.get("name") or ""),
                "liquidity_usd": round(_number(x.get("liquidity_usd")), 2),
                "volume_24h_usd": round(_number(x.get("volume_24h_usd")), 2),
                "buy_sell_ratio": round(_number(x.get("buy_sell_ratio")), 3),
                "score": round(_number(x.get("score")), 3),
                "source": str(x.get("source") or ""),
                "url": str(x.get("url") or ""),
            }
            for x in candidates[:20]
        ]
        diag = {**diagnostics, "active_min_liquidity_usd": liquidity_floor, "active_min_volume_24h_usd": volume_floor,
            "active_min_buy_sell_ratio": ratio_floor, "below_liquidity_filter": sum(x["liquidity_usd"] < liquidity_floor for x in candidates),
            "below_volume_filter": sum(x["volume_24h_usd"] < volume_floor for x in candidates),
            "below_ratio_filter": sum(x["buy_sell_ratio"] < ratio_floor for x in candidates),
            "candidate_preview": candidate_preview}

    try:
        safe_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        safe_limit = 20
    signals = filtered[:safe_limit]
    note = ("Filtered market candidates only; not investment advice or a profit guarantee." if signals else
            "Market data unavailable; inspect last_error and diagnostics." if error else
            "Data received, but no candidates passed the active filters.")
    provider = diag.get("provider", "Market data providers")
    return {"provider": provider, "checked": diag.get("solana_pairs", diag.get("coingecko_pools_received", diag.get("gecko_pools_received", 0))),
        "candidate_count": len(filtered), "signals": signals, "diagnostics": diag, "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED, "note": note, "last_error": error}
