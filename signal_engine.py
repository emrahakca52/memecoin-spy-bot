"""Birdeye-only Solana token discovery for the Memecoin Spy paper bot.
Discovery and market metrics use Birdeye only. Paper mode; no orders are submitted.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

TRADING_MODE = "paper"
REAL_TRADING_ENABLED = False

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_LISTING_URL = f"{BIRDEYE_BASE}/defi/v2/tokens/new_listing"
BIRDEYE_OVERVIEW_URL = f"{BIRDEYE_BASE}/defi/token_overview"

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))
CACHE_SECONDS = max(60, min(150, int(os.getenv("SIGNAL_CACHE_SECONDS", "150"))))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
LISTING_LIMIT = max(1, min(20, int(os.getenv("BIRDEYE_LISTING_LIMIT", "20"))))
MAX_OVERVIEWS_PER_SCAN = max(
    1, min(5, int(os.getenv("BIRDEYE_MAX_OVERVIEWS_PER_SCAN", "5")))
)
REQUEST_INTERVAL_SECONDS = max(3.0, float(os.getenv("BIRDEYE_REQUEST_INTERVAL_SECONDS", "3.0")))
COOLDOWN_DEFAULT_SECONDS = max(300, int(os.getenv("BIRDEYE_429_COOLDOWN_SECONDS", "300")))

_lock = asyncio.Lock()
_request_lock = asyncio.Lock()
_last_request_at = 0.0
_cooldown_until = 0.0
_cooldown_reason: str | None = None
_cache: dict[str, Any] = {
    "timestamp": 0.0, "candidates": [], "diagnostics": {}, "cache_for_seconds": CACHE_SECONDS
}
_listing_cursor = 0

_stats: dict[str, Any] = {
    "requests": 0, "last_http_status": None, "last_error": None,
    "last_endpoint": None, "last_request_utc": None, "listing_items": 0,
    "overview_successes": 0,
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _first(data: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return default


def _api_key() -> str:
    return os.getenv("BIRDEYE_API_KEY", "").strip()


async def _rate_limit() -> None:
    """Serialize requests and apply conservative spacing plus provider cooldown."""
    global _last_request_at
    async with _request_lock:
        now = time.monotonic()
        if now < _cooldown_until:
            remaining = max(1, int(_cooldown_until - now))
            raise RuntimeError(f"Birdeye cooldown active ({remaining}s): {_cooldown_reason or 'rate limit'}")
        wait = REQUEST_INTERVAL_SECONDS - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        if now < _cooldown_until:
            remaining = max(1, int(_cooldown_until - now))
            raise RuntimeError(f"Birdeye cooldown active ({remaining}s): {_cooldown_reason or 'rate limit'}")
        _last_request_at = now


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    global _cooldown_until, _cooldown_reason
    await _rate_limit()
    _stats["requests"] += 1
    _stats["last_endpoint"] = url
    _stats["last_request_utc"] = datetime.now(timezone.utc).isoformat()
    _stats["last_http_status"] = None
    _stats["last_error"] = None
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        _stats["last_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        raise

    _stats["last_http_status"] = response.status_code
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            delay = max(COOLDOWN_DEFAULT_SECONDS, min(1800, int(float(retry_after)))) if retry_after else COOLDOWN_DEFAULT_SECONDS
        except (ValueError, TypeError):
            delay = COOLDOWN_DEFAULT_SECONDS
        _cooldown_until = time.monotonic() + delay
        _cooldown_reason = f"HTTP 429; cooldown {delay}s"
        _stats["last_error"] = _cooldown_reason
        raise RuntimeError(_cooldown_reason)
    if response.status_code in (401, 403):
        _stats["last_error"] = f"HTTP {response.status_code}; verify API key and endpoint access"
        raise RuntimeError(_stats["last_error"])
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise RuntimeError("Birdeye returned an unsuccessful or invalid JSON payload")
    return payload


def _listing_items(payload: dict) -> list[dict]:
    data = payload.get("data", {})
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = next((data.get(k) for k in ("items", "tokens", "list", "result") if isinstance(data.get(k), list)), [])
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _normalize_candidate(listing: dict, overview: dict) -> dict:
    address = str(_first(listing, "address", "token_address", default=_first(overview, "address", default="")))
    symbol = str(_first(overview, "symbol", default=_first(listing, "symbol", default="UNKNOWN")))
    name = str(_first(overview, "name", default=_first(listing, "name", default="")))
    liquidity = _num(_first(overview, "liquidity", "liquidity_usd", default=_first(listing, "liquidity", "liquidity_usd", default=0)))
    volume_raw = _first(overview, "v24hUSD", "volume24h", "volume_24h_usd", default=None)
    price = _num(_first(overview, "price", "priceUsd", "price_usd", default=_first(listing, "price", "priceUsd", default=0)))
    buys_raw = _first(overview, "buy24h", "buys24h", "buys_24h", default=None)
    sells_raw = _first(overview, "sell24h", "sells24h", "sells_24h", default=None)
    volume_present = volume_raw is not None
    buys_present, sells_present = buys_raw is not None, sells_raw is not None
    volume, buys, sells = _num(volume_raw), _num(buys_raw), _num(sells_raw)
    ratio = buys / sells if sells > 0 else (buys if buys > 0 else 0.0)
    return {
        "token_address": address, "symbol": symbol, "token_symbol": symbol, "name": name,
        "price_usd": price, "liquidity_usd": liquidity, "volume_24h_usd": volume,
        "volume_24h_present": volume_present, "buys_24h": int(buys), "sells_24h": int(sells),
        "buy_sell_counts_present": buys_present and sells_present,
        "buy_sell_ratio": ratio, "buys_to_sells_ratio": ratio,
        "pool_address": None, "pair_address": None, "provider": "Birdeye",
        "url": f"https://birdeye.so/token/{address}?chain=solana" if address else None,
        "listed_at": _first(listing, "block_unix_time", "listedAt", "listTime", "blockUnixTime"),
        "market_cap_usd": _num(_first(overview, "mc", "marketCap", "market_cap", default=0)),
        "unique_wallets_24h": int(_num(_first(overview, "uniqueWallet24h", "unique_wallets_24h", default=0))),
    }


async def _fetch_candidates() -> tuple[list[dict], dict]:
    global _listing_cursor
    api_key = _api_key()
    if not api_key:
        message = "BIRDEYE_API_KEY is not configured"
        _stats["last_error"] = message
        return [], {
            "provider": "Birdeye", "source_counts": {"Birdeye_new_listing": 0, "Birdeye_overview": 0},
            "source_errors": [message], "last_error": message,
            "note": "Birdeye-only discovery; paper mode only.",
        }

    if time.monotonic() < _cooldown_until:
        remaining = max(1, int(_cooldown_until - time.monotonic()))
        message = f"Birdeye cooldown active ({remaining}s): {_cooldown_reason or 'rate limit'}"
        return [], {
            "provider": "Birdeye", "source_counts": {"Birdeye_new_listing": 0, "Birdeye_overview": 0},
            "source_errors": [message], "last_error": message, "overview_batch_size": 0,
            "listing_rotation_next_index": _listing_cursor, "mode": TRADING_MODE,
            "real_trading_enabled": REAL_TRADING_ENABLED, "note": "Cooldown active; no request sent.",
        }

    headers = {"accept": "application/json", "X-API-KEY": api_key, "x-chain": "solana"}
    errors: list[str] = []
    candidates: list[dict] = []
    listing_count = liquidity_eligible_count = overview_successes = 0
    eligible: list[dict] = []

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=8.0), headers=headers
        ) as client:
            listing_payload = await _get_json(client, BIRDEYE_LISTING_URL, {
                "limit": LISTING_LIMIT, "meme_platform_enabled": "true"
            })
            listings = _listing_items(listing_payload)
            listing_count = len(listings)
            _stats["listing_items"] = listing_count
            addressed = [item for item in listings if _first(item, "address", "token_address")]
            addressed.sort(
                key=lambda item: _num(_first(item, "liquidity", "liquidity_usd", default=0)),
                reverse=True
            )
            liquidity_eligible_count = sum(
                1 for item in addressed
                if _num(_first(item, "liquidity", "liquidity_usd", default=0)) >= MIN_LIQUIDITY_USD
            )
            if addressed:
                start = _listing_cursor % len(addressed)
                rotated = addressed[start:] + addressed[:start]
                eligible = rotated[:MAX_OVERVIEWS_PER_SCAN]
                _listing_cursor = (start + len(eligible)) % len(addressed)

            for listing in eligible:
                address = str(_first(listing, "address", "token_address", default=""))
                try:
                    payload = await _get_json(client, BIRDEYE_OVERVIEW_URL, {
                        "address": address, "frames": "24h"
                    })
                    data = payload.get("data", {})
                    if not isinstance(data, dict):
                        raise RuntimeError("token overview response has no data object")
                    candidates.append(_normalize_candidate(listing, data))
                    overview_successes += 1
                except Exception as exc:
                    errors.append(f"overview:{address}: {type(exc).__name__}: {str(exc)[:140]}")
                    if "cooldown" in str(exc).lower() or "429" in str(exc):
                        break
    except Exception as exc:
        errors.append(f"birdeye_new_listing: {type(exc).__name__}: {str(exc)[:180]}")

    _stats["overview_successes"] = overview_successes
    diagnostics = {
        "provider": "Birdeye",
        "source_counts": {
            "Birdeye_new_listing": listing_count,
            "Birdeye_liquidity_eligible": liquidity_eligible_count,
            "Birdeye_overview": overview_successes,
        },
        "source_errors": errors[-20:], "last_error": errors[-1] if errors else None,
        "overview_batch_size": len(eligible), "listing_rotation_next_index": _listing_cursor,
        "overview_frames_requested": "24h", "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
        "cooldown_default_seconds": COOLDOWN_DEFAULT_SECONDS,
        "note": "Discovery and market metrics use Birdeye only. Paper mode only.",
        "mode": TRADING_MODE, "real_trading_enabled": REAL_TRADING_ENABLED,
    }
    return candidates, diagnostics


def _score(candidate: dict) -> float:
    return round(
        min(_num(candidate.get("liquidity_usd")) / 10000.0, 5.0)
        + min(_num(candidate.get("volume_24h_usd")) / 20000.0, 5.0)
        + min(_num(candidate.get("buy_sell_ratio")), 5.0), 3
    )


async def get_signal_candidates(
    min_liquidity_usd: float | None = None,
    min_volume_24h_usd: float | None = None,
    min_buys_sells_ratio: float | None = None,
    limit: int = 20,
) -> dict:
    min_liquidity = MIN_LIQUIDITY_USD if min_liquidity_usd is None else float(min_liquidity_usd)
    min_volume = MIN_VOLUME_24H_USD if min_volume_24h_usd is None else float(min_volume_24h_usd)
    min_ratio = MIN_BUYS_SELLS_RATIO if min_buys_sells_ratio is None else float(min_buys_sells_ratio)

    async with _lock:
        now = time.monotonic()
        ttl = int(_cache.get("cache_for_seconds", CACHE_SECONDS))
        fresh = now - float(_cache.get("timestamp", 0)) < ttl and bool(_cache.get("diagnostics"))
        if fresh:
            candidates = list(_cache.get("candidates", []))
            diagnostics = dict(_cache.get("diagnostics", {}))
        else:
            candidates, diagnostics = await _fetch_candidates()
            had_error = bool(diagnostics.get("last_error"))
            ttl = min(CACHE_SECONDS, 60) if had_error else CACHE_SECONDS
            _cache.update({
                "timestamp": time.monotonic(), "candidates": candidates,
                "diagnostics": diagnostics, "cache_for_seconds": ttl
            })

    filtered, rejected = [], []
    for candidate in candidates:
        liquidity = _num(candidate.get("liquidity_usd"))
        volume = _num(candidate.get("volume_24h_usd"))
        ratio = _num(candidate.get("buy_sell_ratio"))
        price = _num(candidate.get("price_usd"))
        reasons = []
        if liquidity < min_liquidity:
            reasons.append("liquidity_below_minimum")
        if volume < min_volume or not candidate.get("volume_24h_present", True):
            reasons.append("volume_24h_below_minimum")
        if ratio < min_ratio or not candidate.get("buy_sell_counts_present", True):
            reasons.append("buy_sell_ratio_below_minimum")
        if price <= 0:
            reasons.append("missing_or_zero_price")
        if reasons:
            rejected.append({
                "symbol": candidate.get("symbol"), "token_address": candidate.get("token_address"),
                "liquidity_usd": liquidity, "volume_24h_usd": volume,
                "buy_sell_ratio": round(ratio, 4), "price_usd": price,
                "metric_presence": {
                    "liquidity": liquidity > 0,
                    "volume_24h": bool(candidate.get("volume_24h_present", volume > 0)),
                    "buy_sell_counts": bool(candidate.get("buy_sell_counts_present", False)),
                    "price": price > 0,
                },
                "rejection_reasons": reasons,
            })
            continue
        item = dict(candidate)
        item["score"] = _score(item)
        filtered.append(item)

    filtered.sort(key=lambda item: item["score"], reverse=True)
    signals = filtered[:max(0, int(limit))]
    diagnostics_out = dict(diagnostics)
    diagnostics_out.update({
        "checked": len(candidates), "candidate_count": len(filtered),
        "filters": {
            "min_liquidity_usd": min_liquidity, "min_volume_24h_usd": min_volume,
            "min_buys_sells_ratio": min_ratio,
        },
        "candidate_preview": [{
            "symbol": item.get("symbol"), "token_address": item.get("token_address"),
            "liquidity_usd": item.get("liquidity_usd"), "volume_24h_usd": item.get("volume_24h_usd"),
            "buy_sell_ratio": item.get("buy_sell_ratio"), "score": item.get("score"),
            "provider": item.get("provider"),
        } for item in signals[:20]],
        "rejected_preview": rejected[:10],
        "rejection_counts": {
            reason: sum(1 for item in rejected if reason in item["rejection_reasons"])
            for reason in ("liquidity_below_minimum", "volume_24h_below_minimum",
                           "buy_sell_ratio_below_minimum", "missing_or_zero_price")
        },
        "cached_for_seconds": int(_cache.get("cache_for_seconds", CACHE_SECONDS)),
        "mode": TRADING_MODE, "real_trading_enabled": REAL_TRADING_ENABLED,
        "provider_diagnostics": get_signal_engine_status(),
    })
    return {
        "provider": "Birdeye", "checked": len(candidates), "candidate_count": len(filtered),
        "signals": signals, "note": diagnostics.get("note"),
        "last_error": diagnostics.get("last_error"),
        "source_errors": diagnostics.get("source_errors", []), "diagnostics": diagnostics_out,
    }


def get_signal_engine_status() -> dict:
    now = time.monotonic()
    return {
        "provider": "Birdeye", "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "cache_age_seconds": round(now - float(_cache.get("timestamp", 0)), 1) if _cache.get("timestamp") else None,
        "cache_ttl_seconds": int(_cache.get("cache_for_seconds", CACHE_SECONDS)),
        "cooldown_seconds_remaining": max(0, int(_cooldown_until - now)),
        "cooldown_reason": _cooldown_reason, "requests": _stats["requests"],
        "last_http_status": _stats["last_http_status"], "last_error": _stats["last_error"],
        "last_endpoint": _stats["last_endpoint"], "last_request_utc": _stats["last_request_utc"],
        "listing_items": _stats["listing_items"], "overview_successes": _stats["overview_successes"],
    }
