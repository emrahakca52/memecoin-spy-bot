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

BASE_URL = "https://api.dexscreener.com"
PROFILE_URL = f"{BASE_URL}/token-profiles/latest/v1"
BOOST_URL = f"{BASE_URL}/token-boosts/latest/v1"
TOKENS_URL = f"{BASE_URL}/tokens/v1/solana"

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUY_SELL_RATIO = float(os.getenv("MIN_BUY_SELL_RATIO", "1.0"))

CACHE_SECONDS = 120
ERROR_COOLDOWN_SECONDS = 60
MAX_ERROR_COOLDOWN_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 15
MAX_DISCOVERED_ADDRESSES = 60
WSOL_MINT = "So11111111111111111111111111111111111111112"
ALLOWED_QUOTES = {"SOL", "WSOL", "USDC", "USDT"}

_cache: dict[str, Any] = {
    "time": 0.0, "candidates": [], "error": None, "diagnostics": {}
}
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


async def _get_json(client, url):
    global _next_request_time, _consecutive_errors
    if time.time() < _next_request_time:
        raise RuntimeError("provider_cooldown")
    response = await client.get(url)
    if response.status_code == 429:
        _consecutive_errors += 1
        delay = min(
            ERROR_COOLDOWN_SECONDS * (2 ** min(_consecutive_errors - 1, 4)),
            MAX_ERROR_COOLDOWN_SECONDS,
        )
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 3600))
            except ValueError:
                pass
        _next_request_time = time.time() + delay + random.uniform(1, 5)
        raise RuntimeError("dexscreener_rate_limited")
    response.raise_for_status()
    return response.json()


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


async def _discover_pairs(client):
    # These are discovery feeds, not a guarantee that every new token is found.
    profiles = await _get_json(client, PROFILE_URL)
    boosts = await _get_json(client, BOOST_URL)
    addresses = list(dict.fromkeys(
        _extract_token_addresses(profiles) + _extract_token_addresses(boosts)
    ))[:MAX_DISCOVERED_ADDRESSES]

    if not addresses:
        return [], 0, []

    pairs = []
    errors = []
    # DexScreener token endpoint accepts batches; keep request volume bounded.
    for start in range(0, len(addresses), 30):
        batch = addresses[start:start + 30]
        try:
            payload = await _get_json(client, f"{TOKENS_URL}/" + ",".join(batch))
            if isinstance(payload, list):
                pairs.extend(payload)
        except RuntimeError as exc:
            if "rate_limited" in str(exc) or "cooldown" in str(exc):
                raise
            errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")

    if not pairs and errors:
        raise RuntimeError("token_pair_requests_failed")
    return pairs, len(addresses), errors


def _normalize_pairs(pairs):
    candidates = []
    stats = {
        "pairs_received": len(pairs),
        "solana_pairs": 0,
        "missing_address": 0,
        "native_sol_excluded": 0,
        "wrong_quote": 0,
        "invalid_price": 0,
        "no_transactions": 0,
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
            stats["missing_address"] += 1
            continue

        address = str(base.get("address") or "").strip()
        symbol = str(base.get("symbol") or "UNKNOWN").strip()
        quote_symbol = str(quote.get("symbol") or "").strip().upper()

        if not address:
            stats["missing_address"] += 1
            continue
        if address == WSOL_MINT:
            stats["native_sol_excluded"] += 1
            continue
        if quote_symbol not in ALLOWED_QUOTES:
            stats["wrong_quote"] += 1
            continue

        liquidity = _number((pair.get("liquidity") or {}).get("usd"))
        volume = _number((pair.get("volume") or {}).get("h24"))
        h24 = ((pair.get("txns") or {}).get("h24") or {})
        buys = _number(h24.get("buys"))
        sells = _number(h24.get("sells"))
        price = _number(pair.get("priceUsd"))

        if price <= 0:
            stats["invalid_price"] += 1
            continue
        if buys + sells <= 0:
            stats["no_transactions"] += 1
            continue

        ratio = buys / max(sells, 1.0)
        score = min(volume / max(liquidity, 1.0), 10.0) * 5 + min(ratio, 3.0) * 5

        candidates.append({
            "token_address": address,
            "symbol": symbol,
            "name": str(base.get("name") or ""),
            "quote_symbol": quote_symbol,
            "pool_address": str(pair.get("pairAddress") or ""),
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "buys_24h": int(buys),
            "sells_24h": int(sells),
            "buy_sell_ratio": round(ratio, 3),
            "score": round(score, 3),
            "source": "DEX Screener",
            "url": str(pair.get("url") or ""),
        })
        stats["passed_raw_checks"] += 1

    unique = {}
    for item in candidates:
        address = item["token_address"]
        old = unique.get(address)
        if old is None or item["liquidity_usd"] > old["liquidity_usd"]:
            unique[address] = item

    stats["unique_tokens"] = len(unique)
    return sorted(unique.values(), key=lambda x: x["score"], reverse=True), stats


async def _fetch_candidates():
    global _next_request_time, _consecutive_errors
    if time.time() < _next_request_time:
        raise RuntimeError("provider_cooldown")

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.2"},
    ) as client:
        pairs, discovered_count, batch_errors = await _discover_pairs(client)

    candidates, diagnostics = _normalize_pairs(pairs)
    diagnostics["discovered_solana_tokens"] = discovered_count
    diagnostics["batch_errors"] = batch_errors
    _consecutive_errors = 0
    _next_request_time = time.time() + CACHE_SECONDS
    return candidates, diagnostics


async def get_signal_candidates(
    limit=20, min_liquidity_usd=None, min_volume_24h_usd=None,
    min_buys_sells_ratio=None, **kwargs
):
    global _next_request_time
    liquidity_floor = max(0.0, _number(
        MIN_LIQUIDITY_USD if min_liquidity_usd is None else min_liquidity_usd,
        MIN_LIQUIDITY_USD,
    ))
    volume_floor = max(0.0, _number(
        MIN_VOLUME_24H_USD if min_volume_24h_usd is None else min_volume_24h_usd,
        MIN_VOLUME_24H_USD,
    ))
    ratio_floor = max(0.0, _number(
        MIN_BUY_SELL_RATIO if min_buys_sells_ratio is None else min_buys_sells_ratio,
        MIN_BUY_SELL_RATIO,
    ))

    async with _lock:
        now = time.time()
        fresh = _cache["time"] > 0 and now - _cache["time"] < CACHE_SECONDS
        if fresh:
            candidates, diagnostics, error = (
                _cache["candidates"], _cache["diagnostics"], None
            )
        elif now < _next_request_time:
            candidates, diagnostics = _cache["candidates"], _cache["diagnostics"]
            error = _cache["error"] or "provider_cooldown"
        else:
            try:
                candidates, diagnostics = await _fetch_candidates()
                _cache.update({
                    "time": time.time(), "candidates": candidates,
                    "diagnostics": diagnostics, "error": None,
                })
                error = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:180]}"
                logger.warning("DEX Screener scan failed: %s", error)
                _cache["error"] = error
                candidates, diagnostics = _cache["candidates"], _cache["diagnostics"]

        filtered = [
            item for item in candidates
            if item["liquidity_usd"] >= liquidity_floor
            and item["volume_24h_usd"] >= volume_floor
            and item["buy_sell_ratio"] >= ratio_floor
        ]
        diag = {
            **diagnostics,
            "active_min_liquidity_usd": liquidity_floor,
            "active_min_volume_24h_usd": volume_floor,
            "active_min_buy_sell_ratio": ratio_floor,
            "below_liquidity_filter": sum(x["liquidity_usd"] < liquidity_floor for x in candidates),
            "below_volume_filter": sum(x["volume_24h_usd"] < volume_floor for x in candidates),
            "below_ratio_filter": sum(x["buy_sell_ratio"] < ratio_floor for x in candidates),
        }

    try:
        safe_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        safe_limit = 20
    signals = filtered[:safe_limit]

    if signals:
        note = "Filtered market candidates only; not investment advice or a profit guarantee."
    elif error:
        note = "Market data unavailable; inspect last_error and diagnostics."
    else:
        note = "Data received, but no candidates passed the active filters."

    return {
        "provider": "DEX Screener",
        "checked": diag.get("solana_pairs", 0),
        "candidate_count": len(filtered),
        "signals": signals,
        "diagnostics": diag,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "note": note,
        "last_error": error,
    }
