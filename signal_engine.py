"""Birdeye V3 token-list discovery for the Memecoin Spy paper bot.
Discovery and market metrics use Birdeye only. Paper mode; no orders are submitted.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

TRADING_MODE = "paper"
REAL_TRADING_ENABLED = False

BIRDEYE_BASE = "https://public-api.birdeye.so"
BIRDEYE_LIST_URL = f"{BIRDEYE_BASE}/defi/v3/token/list"

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))
MIN_TRADES_24H = max(0, int(os.getenv("MIN_TRADES_24H", "20")))
MAX_PRICE_CHANGE_24H_PCT = max(0.0, float(os.getenv("MAX_PRICE_CHANGE_24H_PCT", "100")))
MAX_NEGATIVE_PRICE_CHANGE_24H_PCT = max(0.0, float(os.getenv("MAX_NEGATIVE_PRICE_CHANGE_24H_PCT", "30")))
CACHE_SECONDS = max(60, int(os.getenv("SIGNAL_CACHE_SECONDS", "150")))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
LISTING_LIMIT = max(1, min(100, int(os.getenv("BIRDEYE_LISTING_LIMIT", "100"))))
REQUEST_INTERVAL_SECONDS = max(3.0, float(os.getenv("BIRDEYE_REQUEST_INTERVAL_SECONDS", "3.0")))
COOLDOWN_DEFAULT_SECONDS = max(300, int(os.getenv("BIRDEYE_429_COOLDOWN_SECONDS", "300")))

_lock = asyncio.Lock()
_request_lock = asyncio.Lock()
_last_request_at = 0.0
_cooldown_until = 0.0
_cooldown_reason: str | None = None
_cache: dict[str, Any] = {"timestamp": 0.0, "candidates": [], "diagnostics": {}, "cache_for_seconds": CACHE_SECONDS}
_stats: dict[str, Any] = {
    "requests": 0, "last_http_status": None, "last_error": None,
    "last_endpoint": None, "last_request_utc": None, "listing_items": 0,
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _has_finite_number(value: Any) -> bool:
    """Return whether a provider metric is present and finite."""
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _api_key() -> str:
    return os.getenv("BIRDEYE_API_KEY", "").strip()


async def _rate_limit() -> None:
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


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict:
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
        _stats["last_error"] = f"HTTP {response.status_code}; verify API key and endpoint access/plan"
        raise RuntimeError(_stats["last_error"])
    if response.status_code >= 400:
        detail = " ".join(response.text.split())[:240]
        _stats["last_error"] = f"HTTP {response.status_code}: {detail}"
        # Birdeye can return HTTP 400 when the account's monthly CU allowance
        # is exhausted. Repeating the same request cannot fix that and can
        # waste more quota, so pause discovery for six hours and serve cached
        # results (if any) until a later retry.
        if "compute units usage limit exceeded" in detail.lower():
            delay = max(21600, COOLDOWN_DEFAULT_SECONDS)
            _cooldown_until = time.monotonic() + delay
            _cooldown_reason = f"Birdeye CU limit exceeded; retry after cooldown ({delay}s)"
            _stats["last_error"] = f"HTTP {response.status_code}: {detail}; {_cooldown_reason}"
        raise RuntimeError(_stats["last_error"])
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("success") is False:
        _stats["last_error"] = "Birdeye returned an unsuccessful or invalid JSON payload"
        raise RuntimeError(_stats["last_error"])
    return payload


def _items(payload: dict) -> list[dict]:
    data = payload.get("data", {})
    if isinstance(data, list):
        values = data
    elif isinstance(data, dict):
        values = data.get("items", [])
    else:
        values = []
    return [item for item in values if isinstance(item, dict)]


def _normalize(item: dict) -> dict:
    address = str(item.get("address") or "")
    buys_raw = item.get("buy_24h")
    sells_raw = item.get("sell_24h")
    buys = _num(buys_raw)
    sells = _num(sells_raw)
    ratio = buys / sells if sells > 0 else 0.0
    volume_raw = item.get("volume_24h_usd")
    liquidity_raw = item.get("liquidity")
    price_raw = item.get("price")
    return {
        "token_address": address,
        "symbol": str(item.get("symbol") or "UNKNOWN").strip() or "UNKNOWN",
        "token_symbol": str(item.get("symbol") or "UNKNOWN").strip() or "UNKNOWN",
        "name": str(item.get("name") or ""),
        "price_usd": _num(price_raw),
        "liquidity_usd": _num(liquidity_raw),
        "volume_24h_usd": _num(volume_raw),
        "volume_24h_present": volume_raw is not None,
        "buys_24h": int(buys),
        "sells_24h": int(sells),
        "buy_sell_counts_present": buys_raw is not None and sells_raw is not None,
        "buy_sell_ratio": ratio,
        "buys_to_sells_ratio": ratio,
        "pool_address": None,
        "pair_address": None,
        "provider": "Birdeye",
        "url": f"https://birdeye.so/token/{address}?chain=solana" if address else None,
        "listed_at": item.get("recent_listing_time"),
        "market_cap_usd": _num(item.get("market_cap")),
        "fdv_usd": _num(item.get("fdv")),
        "unique_wallets_24h": int(_num(item.get("unique_wallet_24h"))),
        "trade_count_24h": int(_num(item.get("trade_24h_count"))),
        "price_change_24h_pct": _num(item.get("price_change_24h_percent")),
        "price_change_24h_present": _has_finite_number(item.get("price_change_24h_percent")),
    }


async def _fetch_candidates() -> tuple[list[dict], dict]:
    """Discover Solana tokens using DexScreener's public, keyless API."""
    errors: list[str] = []
    candidates: list[dict] = []
    profile_url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=8.0),
                                     headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/2.3"}) as client:
            # This endpoint is limited to 60 requests/minute; one request per scan.
            response = await client.get(profile_url)
            _stats["requests"] += 1
            _stats["last_endpoint"] = profile_url
            _stats["last_request_utc"] = datetime.now(timezone.utc).isoformat()
            _stats["last_http_status"] = response.status_code
            if response.status_code == 429:
                delay = max(21600, COOLDOWN_DEFAULT_SECONDS)
                global _cooldown_until, _cooldown_reason
                _cooldown_until = time.monotonic() + delay
                _cooldown_reason = f"DexScreener rate limit; retry after cooldown ({delay}s)"
                raise RuntimeError(_cooldown_reason)
            response.raise_for_status()
            profiles = response.json()
            if not isinstance(profiles, list):
                raise ValueError("DexScreener returned an invalid token-profile list")
            # Add one complementary discovery feed without polling every endpoint.
            # One extra public request per cache refresh, then one batched pair lookup.
            boost_url = "https://api.dexscreener.com/token-boosts/latest/v1"
            boost_addresses: list[str] = []
            try:
                boost_response = await client.get(boost_url)
                _stats["requests"] += 1
                _stats["last_endpoint"] = boost_url
                _stats["last_request_utc"] = datetime.now(timezone.utc).isoformat()
                _stats["last_http_status"] = boost_response.status_code
                if boost_response.status_code == 429:
                    raise RuntimeError("DexScreener boost feed rate limited")
                boost_response.raise_for_status()
                boost_items = boost_response.json()
                if isinstance(boost_items, list):
                    boost_addresses = [
                        str(item.get("tokenAddress") or "")
                        for item in boost_items
                        if isinstance(item, dict)
                        and item.get("chainId") == "solana"
                        and item.get("tokenAddress")
                    ]
            except Exception as boost_exc:
                # Keep profile discovery usable if the supplemental feed is unavailable.
                errors.append(f"dexscreener_boosts: {type(boost_exc).__name__}: {str(boost_exc)[:120]}")

            profile_addresses = [
                str(item.get("tokenAddress") or "")
                for item in profiles
                if isinstance(item, dict) and item.get("chainId") == "solana" and item.get("tokenAddress")
            ]
            # Prioritize newly boosted addresses, then fill from latest token profiles.
            addresses = list(dict.fromkeys(boost_addresses + profile_addresses))[:30]
            if not addresses:
                raise ValueError("DexScreener returned no Solana token addresses")
            # Batch up to 30 addresses in one call; no API key is required.
            token_url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addresses)
            response = await client.get(token_url)
            _stats["requests"] += 1
            _stats["last_endpoint"] = token_url
            _stats["last_request_utc"] = datetime.now(timezone.utc).isoformat()
            _stats["last_http_status"] = response.status_code
            response.raise_for_status()
            pairs = response.json()
            if not isinstance(pairs, list):
                raise ValueError("DexScreener returned an invalid token-pair list")
            best_by_address: dict[str, dict] = {}
            for pair in pairs:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
                address = str(token.get("address") or "")
                if not address:
                    continue
                old = best_by_address.get(address)
                liquidity = _num((pair.get("liquidity") or {}).get("usd"))
                old_liquidity = _num(((old or {}).get("liquidity") or {}).get("usd"))
                if old is None or liquidity > old_liquidity:
                    best_by_address[address] = pair
            for address, pair in best_by_address.items():
                token = pair.get("baseToken") or {}
                volume = pair.get("volume") or {}
                txns = pair.get("txns") or {}
                h24 = txns.get("h24") or {}
                change = pair.get("priceChange") or {}
                liquidity = (pair.get("liquidity") or {})
                buys = int(_num(h24.get("buys")))
                sells = int(_num(h24.get("sells")))
                volume24 = _num(volume.get("h24"))
                price = _num(pair.get("priceUsd"))
                change24 = change.get("h24")
                candidates.append({
                    "token_address": address,
                    "symbol": str(token.get("symbol") or "UNKNOWN").strip() or "UNKNOWN",
                    "token_symbol": str(token.get("symbol") or "UNKNOWN").strip() or "UNKNOWN",
                    "name": str(token.get("name") or ""),
                    "price_usd": price,
                    "liquidity_usd": _num(liquidity.get("usd")),
                    "volume_24h_usd": volume24,
                    "volume_24h_present": volume.get("h24") is not None,
                    "buys_24h": buys, "sells_24h": sells,
                    "buy_sell_counts_present": h24.get("buys") is not None and h24.get("sells") is not None,
                    "buy_sell_ratio": buys / sells if sells > 0 else 0.0,
                    "buys_to_sells_ratio": buys / sells if sells > 0 else 0.0,
                    "pool_address": pair.get("pairAddress"), "pair_address": pair.get("pairAddress"),
                    "provider": "DexScreener",
                    "url": pair.get("url") or f"https://dexscreener.com/solana/{address}",
                    "listed_at": pair.get("pairCreatedAt"),
                    "market_cap_usd": _num(pair.get("marketCap")),
                    "fdv_usd": _num(pair.get("fdv")),
                    "unique_wallets_24h": 0,
                    "trade_count_24h": buys + sells,
                    "price_change_24h_pct": _num(change24),
                    "price_change_24h_present": _has_finite_number(change24),
                })
            _stats["listing_items"] = len(candidates)
            _stats["last_error"] = None
    except Exception as exc:
        errors.append(f"dexscreener_discovery: {type(exc).__name__}: {str(exc)[:180]}")
        _stats["last_error"] = errors[-1]

    diagnostics = {
        "provider": "DexScreener",
        "source_counts": {"DexScreener_token_profiles_and_pairs": len(candidates)},
        "source_errors": errors[-10:],
        "last_error": errors[-1] if errors else None,
        "overview_batch_size": len(candidates),
        "endpoint": "https://api.dexscreener.com/tokens/v1/solana/{up-to-30-token-addresses}",
        "local_min_liquidity_usd": MIN_LIQUIDITY_USD,
        "local_min_volume_24h_usd": MIN_VOLUME_24H_USD,
        "request_interval_seconds": REQUEST_INTERVAL_SECONDS,
        "note": "Keyless DexScreener public API; public rate limits apply. Paper mode only.",
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
            _cache.update({"timestamp": time.monotonic(), "candidates": candidates,
                           "diagnostics": diagnostics, "cache_for_seconds": ttl})

    filtered: list[dict] = []
    rejected: list[dict] = []
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
        buys = int(_num(candidate.get("buys_24h")))
        sells = int(_num(candidate.get("sells_24h")))
        trades = int(_num(candidate.get("trade_count_24h")))
        change_24h = _num(candidate.get("price_change_24h_pct"))
        if (not candidate.get("buy_sell_counts_present", False)
                or buys <= 0 or sells <= 0 or ratio < min_ratio):
            reasons.append("buy_sell_activity_insufficient")
        if trades < MIN_TRADES_24H:
            reasons.append("trade_count_24h_below_minimum")
        if not candidate.get("price_change_24h_present", False):
            reasons.append("missing_or_invalid_price_change_24h")
        elif abs(change_24h) > MAX_PRICE_CHANGE_24H_PCT:
            reasons.append("extreme_price_change_24h")
        elif change_24h < -MAX_NEGATIVE_PRICE_CHANGE_24H_PCT:
            reasons.append("negative_price_change_24h_exceeded")
        if not candidate.get("token_address"):
            reasons.append("missing_token_address")
        if price <= 0:
            reasons.append("missing_or_zero_price")
        if reasons:
            rejected.append({
                "symbol": candidate.get("symbol"), "token_address": candidate.get("token_address"),
                "liquidity_usd": liquidity, "volume_24h_usd": volume,
                "buy_sell_ratio": round(ratio, 4), "price_usd": price,
                "buys_24h": buys, "sells_24h": sells, "trade_count_24h": trades,
                "price_change_24h_pct": change_24h,
                "metric_presence": {"liquidity": liquidity > 0,
                                    "volume_24h": bool(candidate.get("volume_24h_present", volume > 0)),
                                    "buy_sell_counts": bool(candidate.get("buy_sell_counts_present", False)),
                                    "price": price > 0},
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
        "filters": {"min_liquidity_usd": min_liquidity,
                    "min_volume_24h_usd": min_volume,
                    "min_buys_sells_ratio": min_ratio,
                    "max_absolute_price_change_24h_pct": MAX_PRICE_CHANGE_24H_PCT,
                    "max_negative_price_change_24h_pct": MAX_NEGATIVE_PRICE_CHANGE_24H_PCT,
                    "require_price_change_24h": True},
        "candidate_preview": [{"symbol": item.get("symbol"),
                                "token_address": item.get("token_address"),
                                "liquidity_usd": item.get("liquidity_usd"),
                                "volume_24h_usd": item.get("volume_24h_usd"),
                                "buy_sell_ratio": item.get("buy_sell_ratio"),
                                "score": item.get("score"), "provider": item.get("provider")}
                               for item in signals[:20]],
        "rejected_preview": rejected[:10],
        "rejection_counts": {reason: sum(1 for item in rejected if reason in item["rejection_reasons"])
                             for reason in ("liquidity_below_minimum", "volume_24h_below_minimum",
                                            "buy_sell_activity_insufficient", "trade_count_24h_below_minimum",
                                            "extreme_price_change_24h", "negative_price_change_24h_exceeded",
                                            "missing_or_invalid_price_change_24h", "missing_token_address",
                                            "missing_or_zero_price")},
        "additional_filters": {
            "min_trades_24h": MIN_TRADES_24H,
            "max_absolute_price_change_24h_pct": MAX_PRICE_CHANGE_24H_PCT,
            "max_negative_price_change_24h_pct": MAX_NEGATIVE_PRICE_CHANGE_24H_PCT,
            "require_price_change_24h": True,
            "require_nonzero_buys_and_sells": True,
        },
        "cached_for_seconds": int(_cache.get("cache_for_seconds", CACHE_SECONDS)),
        "mode": TRADING_MODE, "real_trading_enabled": REAL_TRADING_ENABLED,
        "provider_diagnostics": get_signal_engine_status(),
    })
    return {"provider": "DexScreener", "checked": len(candidates),
            "candidate_count": len(filtered), "signals": signals,
            "note": diagnostics.get("note"), "last_error": diagnostics.get("last_error"),
            "source_errors": diagnostics.get("source_errors", []), "diagnostics": diagnostics_out}


def get_signal_engine_status() -> dict:
    now = time.monotonic()
    return {
        "provider": "DexScreener", "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "cache_age_seconds": round(now - float(_cache.get("timestamp", 0)), 1) if _cache.get("timestamp") else None,
        "cache_ttl_seconds": int(_cache.get("cache_for_seconds", CACHE_SECONDS)),
        "cooldown_seconds_remaining": max(0, int(_cooldown_until - now)),
        "cooldown_reason": _cooldown_reason,
        "requests": _stats["requests"], "last_http_status": _stats["last_http_status"],
        "last_error": _stats["last_error"], "last_endpoint": _stats["last_endpoint"],
        "last_request_utc": _stats["last_request_utc"], "listing_items": _stats["listing_items"],
    }
