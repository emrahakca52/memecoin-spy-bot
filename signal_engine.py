"""Jupiter-first Solana token discovery with DexScreener fallback."""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any

import httpx

TRADING_MODE = "paper"
REAL_TRADING_ENABLED = False
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://api.jup.ag").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
JUPITER_MIN_INTERVAL_SECONDS = float(os.getenv("JUPITER_MIN_INTERVAL_SECONDS", "2.1"))

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))
MIN_TRADES_24H = int(os.getenv("MIN_TRADES_24H", "20"))
MAX_PRICE_CHANGE_24H_PCT = float(os.getenv("MAX_PRICE_CHANGE_24H_PCT", "100"))
MAX_NEGATIVE_PRICE_CHANGE_24H_PCT = float(os.getenv("MAX_NEGATIVE_PRICE_CHANGE_24H_PCT", "30"))
CACHE_SECONDS = int(os.getenv("SIGNAL_CACHE_SECONDS", "150"))

_lock = asyncio.Lock()
_jupiter_lock = asyncio.Lock()
_last_jupiter_request = 0.0
_cache = {"timestamp": 0.0, "candidates": [], "diagnostics": {}}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _first_num(*values: Any) -> float:
    for value in values:
        if value is not None:
            return _num(value)
    return 0.0


async def _jupiter_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    """Respect keyless Jupiter's documented 0.5 requests/second rate limit."""
    global _last_jupiter_request
    async with _jupiter_lock:
        wait = JUPITER_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_jupiter_request)
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        response = await client.get(f"{JUPITER_BASE_URL}{path}", params=params, headers=headers)
        _last_jupiter_request = time.monotonic()
        response.raise_for_status()
        return response.json()


def _jupiter_candidates(tokens: Any) -> list[dict]:
    if not isinstance(tokens, list):
        return []
    candidates = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        address = token.get("id") or token.get("mint") or token.get("address")
        if not address:
            continue
        stats = token.get("stats24h") or token.get("stats24H") or token.get("stats") or {}
        if not isinstance(stats, dict):
            stats = {}
        stats = stats.get("24h", stats.get("h24", stats))
        if not isinstance(stats, dict):
            stats = {}
        buys = int(_first_num(stats.get("numBuys"), stats.get("buys"), token.get("numBuys"), token.get("buys24h")))
        sells = int(_first_num(stats.get("numSells"), stats.get("sells"), token.get("numSells"), token.get("sells24h")))
        volume = _first_num(
            stats.get("volume"), stats.get("volumeUsd"), stats.get("totalVolume"),
            stats.get("volumeUSD"), token.get("volume24h"), token.get("volume24H")
        )
        if not volume:
            volume = _first_num(
                stats.get("buyVolume"), stats.get("buyVolumeUsd"), stats.get("buyVolume24h")
            ) + _first_num(
                stats.get("sellVolume"), stats.get("sellVolumeUsd"), stats.get("sellVolume24h")
            )
        # Keep percentage fields as percentages; Jupiter stats24h.priceChange is a ratio.
        # Do not infer units from the magnitude: a genuine +0.5% must remain +0.5%.
        if _has_finite_number(stats.get("priceChangePercent")):
            price_change_pct = _num(stats.get("priceChangePercent"))
            change_present = True
        elif _has_finite_number(token.get("priceChange24h")):
            price_change_pct = _num(token.get("priceChange24h"))
            change_present = True
        elif _has_finite_number(stats.get("priceChange")):
            price_change_pct = _num(stats.get("priceChange")) * 100.0
            change_present = True
        else:
            price_change_pct = 0.0
            change_present = False
        candidates.append({
            "token_address": address,
            "symbol": token.get("symbol") or "UNKNOWN",
            "name": token.get("name") or "",
            "price_usd": _first_num(token.get("usdPrice"), token.get("price")),
            "liquidity_usd": _first_num(token.get("liquidity"), token.get("liquidityUsd")),
            "volume_24h_usd": volume,
            "buys_24h": buys,
            "sells_24h": sells,
            "buy_sell_ratio": buys / sells if sells > 0 else 0.0,
            "trade_count_24h": int(_first_num(stats.get("numTraders"), stats.get("tradeCount"), buys + sells)),
            "price_change_24h_pct": price_change_pct,
            "price_change_24h_present": change_present,
            "market_cap_usd": _first_num(token.get("mcap"), token.get("marketCap")),
            "provider": "Jupiter",
            "url": f"https://jup.ag/tokens/{address}",
        })
    return candidates


async def _discover_jupiter(client: httpx.AsyncClient) -> list[dict]:
    # /recent includes newly listed mints and token statistics in Jupiter's Tokens API V2.
    data = await _jupiter_get(client, "/tokens/v2/recent")
    return _jupiter_candidates(data)


async def _discover_dexscreener(client: httpx.AsyncClient) -> list[dict]:
    response = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
    response.raise_for_status()
    profiles = response.json()
    if not isinstance(profiles, list):
        raise ValueError("Invalid DexScreener profile response")
    addresses = [
        p.get("tokenAddress") for p in profiles
        if isinstance(p, dict) and p.get("chainId") == "solana" and p.get("tokenAddress")
    ][:30]
    if not addresses:
        return []
    response = await client.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addresses))
    response.raise_for_status()
    pairs = response.json()
    best = {}
    if isinstance(pairs, list):
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            token = pair.get("baseToken") or {}
            address = token.get("address")
            if not address:
                continue
            liquidity = _num((pair.get("liquidity") or {}).get("usd"))
            if address not in best or liquidity > best[address][0]:
                best[address] = (liquidity, pair)
    candidates = []
    for address, (_, pair) in best.items():
        token = pair.get("baseToken") or {}
        volume = pair.get("volume") or {}
        txns = (pair.get("txns") or {}).get("h24") or {}
        change = pair.get("priceChange") or {}
        buys, sells = int(_num(txns.get("buys"))), int(_num(txns.get("sells")))
        candidates.append({
            "token_address": address, "symbol": token.get("symbol") or "UNKNOWN",
            "name": token.get("name") or "", "price_usd": _num(pair.get("priceUsd")),
            "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
            "volume_24h_usd": _num(volume.get("h24")), "buys_24h": buys, "sells_24h": sells,
            "buy_sell_ratio": buys / sells if sells > 0 else 0.0, "trade_count_24h": buys + sells,
            "price_change_24h_pct": _num(change.get("h24")),
            "market_cap_usd": _num(pair.get("marketCap")), "provider": "DexScreener",
            "url": pair.get("url"),
        })
    return candidates


async def get_signal_candidates(
    min_liquidity_usd: float | None = None,
    min_volume_24h_usd: float | None = None,
    min_buys_sells_ratio: float | None = None,
    limit: int = 20,
) -> dict:
    min_liq = MIN_LIQUIDITY_USD if min_liquidity_usd is None else float(min_liquidity_usd)
    min_vol = MIN_VOLUME_24H_USD if min_volume_24h_usd is None else float(min_volume_24h_usd)
    min_ratio = MIN_BUYS_SELLS_RATIO if min_buys_sells_ratio is None else float(min_buys_sells_ratio)

    async with _lock:
        now = time.monotonic()
        if now - _cache["timestamp"] < CACHE_SECONDS and _cache["candidates"]:
            candidates = list(_cache["candidates"])
            diagnostics = dict(_cache["diagnostics"])
        else:
            candidates, provider, jupiter_error, fallback_error = [], "Jupiter", None, None
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MemecoinSpy/1.0"}) as client:
                try:
                    candidates = await _discover_jupiter(client)
                    # If the endpoint returns no usable market statistics, use the fallback
                    # rather than silently treating missing fields as real zero-volume data.
                    if not candidates or not any(c["volume_24h_usd"] > 0 and c["liquidity_usd"] > 0 and c.get("price_change_24h_present") for c in candidates):
                        raise ValueError("Jupiter recent-token response lacked usable 24h volume")
                except Exception as exc:
                    jupiter_error = f"{type(exc).__name__}: {exc}"
                    provider = "DexScreener fallback"
                    try:
                        candidates = await _discover_dexscreener(client)
                    except Exception as fallback_exc:
                        fallback_error = f"{type(fallback_exc).__name__}: {fallback_exc}"
                        candidates = []
            diagnostics = {
                "provider": provider,
                "jupiter_error": jupiter_error,
                "fallback_error": fallback_error,
                "candidate_count": len(candidates),
                "mode": TRADING_MODE,
            }
            _cache.update({"timestamp": now, "candidates": candidates, "diagnostics": diagnostics})

    filtered = []
    for candidate in candidates:
        liq, vol = _num(candidate.get("liquidity_usd")), _num(candidate.get("volume_24h_usd"))
        ratio, price = _num(candidate.get("buy_sell_ratio")), _num(candidate.get("price_usd"))
        trades = int(_num(candidate.get("trade_count_24h")))
        change = _num(candidate.get("price_change_24h_pct"))
        if candidate.get("price_change_24h_present") is False:
            continue
        if liq < min_liq or vol < min_vol or ratio < min_ratio or trades < MIN_TRADES_24H:
            continue
        if abs(change) > MAX_PRICE_CHANGE_24H_PCT or change < -MAX_NEGATIVE_PRICE_CHANGE_24H_PCT or price <= 0:
            continue
        item = dict(candidate)
        item["score"] = round(min(liq / 10000, 5) + min(vol / 20000, 5) + min(ratio, 4), 2)
        filtered.append(item)
    filtered.sort(key=lambda item: item.get("score", 0), reverse=True)
    return {
        "provider": diagnostics.get("provider", "Jupiter"),
        "checked": len(candidates),
        "candidate_count": len(filtered),
        "signals": filtered[:max(0, int(limit))],
        "mode": TRADING_MODE,
        "diagnostics": diagnostics,
    }


def get_signal_engine_status() -> dict:
    return {
        "provider": _cache.get("diagnostics", {}).get("provider", "Jupiter"),
        "mode": TRADING_MODE,
        "cache_age_seconds": round(time.monotonic() - _cache.get("timestamp", 0), 1),
        "last_candidate_count": len(_cache.get("candidates", [])),
        "diagnostics": _cache.get("diagnostics", {}),
    }



def _has_finite_number(value: Any) -> bool:
    """Return True only for finite numeric values (booleans are not metrics)."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _normalize(token: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider token metrics without treating missing/NaN changes as valid."""
    price_change = token.get("price_change_24h_percent", token.get("price_change_24h_pct"))
    change_present = _has_finite_number(price_change)
    return {
        "token_address": token.get("address") or token.get("token_address") or token.get("mint"),
        "symbol": token.get("symbol") or "UNKNOWN",
        "name": token.get("name") or "",
        "price_usd": _num(token.get("price", token.get("price_usd"))),
        "liquidity_usd": _num(token.get("liquidity", token.get("liquidity_usd"))),
        "volume_24h_usd": _num(token.get("volume_24h_usd", token.get("volume24h"))),
        "buys_24h": int(_num(token.get("buy_24h", token.get("buys_24h")))),
        "sells_24h": int(_num(token.get("sell_24h", token.get("sells_24h")))),
        "trade_count_24h": int(_num(token.get("trade_24h_count", token.get("trade_count_24h")))),
        "price_change_24h_pct": _num(price_change),
        "price_change_24h_present": change_present,
    }
