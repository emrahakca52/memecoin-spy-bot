
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

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/search"

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))

CACHE_SECONDS = 120
ERROR_COOLDOWN_SECONDS = 60
MAX_ERROR_COOLDOWN_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 15

_cache: dict[str, Any] = {
    "time": 0.0,
    "candidates": [],
    "error": None,
    "diagnostics": {},
}

_next_request_time = 0.0
_consecutive_errors = 0
_lock = asyncio.Lock()


def _number(value, default=0.0):
    try:
        if value is None:
            return default
        result = float(value)
        if not (float("-inf") < result < float("inf")):
            return default
        return result
    except (TypeError, ValueError, OverflowError):
        return default


def _normalize_pairs(payload):
    pairs = payload.get("pairs") or []
    candidates = []

    stats = {
        "pairs_received": len(pairs),
        "solana_pairs": 0,
        "wrong_chain": 0,
        "missing_address": 0,
        "wrong_quote": 0,
        "low_liquidity": 0,
        "low_volume": 0,
        "no_transactions": 0,
        "invalid_price": 0,
        "passed_filters": 0,
    }

    for pair in pairs:
        if not isinstance(pair, dict):
            continue

        if str(pair.get("chainId", "")).lower() != "solana":
            stats["wrong_chain"] += 1
            continue

        stats["solana_pairs"] += 1

        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}

        if not isinstance(base, dict) or not isinstance(quote, dict):
            stats["missing_address"] += 1
            continue

        symbol = str(base.get("symbol") or "").strip()
        quote_symbol = str(quote.get("symbol") or "").strip()
        address = str(base.get("address") or "").strip()

        if not address:
            stats["missing_address"] += 1
            continue

        if symbol.upper() in {"SOL", "WSOL"}:
            continue

        if quote_symbol.upper() not in {"SOL", "WSOL", "USDC", "USDT"}:
            stats["wrong_quote"] += 1
            continue

        liquidity = _number((pair.get("liquidity") or {}).get("usd"))
        volume_24h = _number((pair.get("volume") or {}).get("h24"))
        h24 = (pair.get("txns") or {}).get("h24") or {}

        buys = _number(h24.get("buys"))
        sells = _number(h24.get("sells"))
        price = _number(pair.get("priceUsd"))

        if liquidity < MIN_LIQUIDITY_USD:
            stats["low_liquidity"] += 1
            continue

        if volume_24h < MIN_VOLUME_24H_USD:
            stats["low_volume"] += 1
            continue

        if buys + sells <= 0:
            stats["no_transactions"] += 1
            continue

        if price <= 0:
            stats["invalid_price"] += 1
            continue

        ratio = buys / max(sells, 1.0)
        score = (
            min(volume_24h / max(liquidity, 1.0), 10.0) * 5
            + min(ratio, 3.0) * 5
        )

        candidates.append({
            "token_address": address,
            "symbol": symbol or "UNKNOWN",
            "name": str(base.get("name") or ""),
            "quote_symbol": quote_symbol,
            "pool_address": str(pair.get("pairAddress") or ""),
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume_24h,
            "buys_24h": int(buys),
            "sells_24h": int(sells),
            "buy_sell_ratio": round(ratio, 3),
            "score": round(score, 3),
            "source": "DEX Screener",
            "url": str(pair.get("url") or ""),
        })

        stats["passed_filters"] += 1

    # Bir tokenin birden fazla havuzu varsa en likit havuzu tut.
    unique = {}

    for item in candidates:
        address = item["token_address"]
        previous = unique.get(address)

        if previous is None or item["liquidity_usd"] > previous["liquidity_usd"]:
            unique[address] = item

    stats["unique_tokens"] = len(unique)

    return (
        sorted(
            unique.values(),
            key=lambda item: item["score"],
            reverse=True,
        ),
        stats,
    )


async def _fetch_candidates():
    global _next_request_time, _consecutive_errors

    if time.time() < _next_request_time:
        raise RuntimeError("provider_cooldown")

    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.0",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        response = await client.get(
            DEXSCREENER_URL,
            params={"q": "SOL"},
        )

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
    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("invalid_market_data")

    candidates, diagnostics = _normalize_pairs(payload)

    _consecutive_errors = 0
    _next_request_time = time.time() + CACHE_SECONDS

    return candidates, diagnostics


async def get_signal_candidates(limit=20, **kwargs):
    async with _lock:
        now = time.time()

        fresh = (
            _cache["error"] is None
            and now - _cache["time"] < CACHE_SECONDS
        )

        if fresh:
            candidates = _cache["candidates"]
            diagnostics = _cache["diagnostics"]
            error = None

        elif now < _next_request_time:
            candidates = _cache["candidates"]
            diagnostics = _cache["diagnostics"]
            error = _cache["error"] or "provider_cooldown"

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
                error = f"{type(exc).__name__}: {exc}"
                logger.warning("DEX Screener tarama hatasi: %s", error)

                _cache["error"] = error
                candidates = []
                diagnostics = _cache["diagnostics"]

    try:
        safe_limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        safe_limit = 20

    signals = candidates[:safe_limit]

    if signals:
        note = (
            "Adaylar piyasa verilerine gore siralandi. "
            "Yatirim tavsiyesi veya kar garantisi degildir."
        )
    elif error:
        note = "Piyasa verisi alinamadi. last_error alanini kontrol edin."
    else:
        note = (
            "Veri alindi ancak filtrelerden gecen aday bulunamadi. "
            "diagnostics alanini inceleyin."
        )

    return {
        "provider": "DEX Screener",
        "checked": diagnostics.get("solana_pairs", 0),
        "candidate_count": len(candidates),
        "signals": signals,
        "diagnostics": diagnostics,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "note": note,
        "last_error": error,
    }
