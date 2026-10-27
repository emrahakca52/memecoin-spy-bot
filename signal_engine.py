"""DexScreener-based signal discovery for paper trading."""
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

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))
MIN_TRADES_24H = int(os.getenv("MIN_TRADES_24H", "20"))
MAX_PRICE_CHANGE_24H_PCT = float(os.getenv("MAX_PRICE_CHANGE_24H_PCT", "100"))
MAX_NEGATIVE_PRICE_CHANGE_24H_PCT = float(os.getenv("MAX_NEGATIVE_PRICE_CHANGE_24H_PCT", "30"))
CACHE_SECONDS = int(os.getenv("SIGNAL_CACHE_SECONDS", "150"))
REQUEST_TIMEOUT = 15

_lock = asyncio.Lock()
_cache = {"timestamp": 0.0, "candidates": [], "diagnostics": {}}
_cooldown_until = 0.0
_cooldown_reason = None

def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default

async def _fetch_candidates() -> tuple[list[dict], dict]:
    candidates = []
    errors = []
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers={"User-Agent": "MemecoinSpy/1.0"}) as client:
            r = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
            if r.status_code == 429:
                raise RuntimeError("DexScreener rate limited")
            r.raise_for_status()
            profiles = r.json()
            if not isinstance(profiles, list):
                raise ValueError("Invalid profile response")

            addresses = []
            for item in profiles:
                if isinstance(item, dict) and item.get("chainId") == "solana":
                    addr = item.get("tokenAddress")
                    if addr:
                        addresses.append(addr)
            addresses = list(dict.fromkeys(addresses))[:30]

            if not addresses:
                return [], {"error": "No Solana tokens found"}

            token_url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addresses)
            r = await client.get(token_url)
            if r.status_code == 429:
                raise RuntimeError("DexScreener rate limited")
            r.raise_for_status()
            pairs = r.json()
            if not isinstance(pairs, list):
                raise ValueError("Invalid pairs response")

            best = {}
            for pair in pairs:
                if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                    continue
                base = pair.get("baseToken", {})
                addr = base.get("address")
                if not addr:
                    continue
                liq = _num((pair.get("liquidity") or {}).get("usd"))
                if addr not in best or liq > best[addr][0]:
                    best[addr] = (liq, pair)

            for addr, (_, pair) in best.items():
                token = pair.get("baseToken", {})
                volume = pair.get("volume", {})
                txns = pair.get("txns", {}).get("h24", {})
                change = pair.get("priceChange", {})
                liq = pair.get("liquidity", {})

                buys = int(_num(txns.get("buys")))
                sells = int(_num(txns.get("sells")))
                candidates.append({
                    "token_address": addr,
                    "symbol": token.get("symbol") or "UNKNOWN",
                    "name": token.get("name") or "",
                    "price_usd": _num(pair.get("priceUsd")),
                    "liquidity_usd": _num(liq.get("usd")),
                    "volume_24h_usd": _num(volume.get("h24")),
                    "buys_24h": buys,
                    "sells_24h": sells,
                    "buy_sell_ratio": buys / sells if sells > 0 else 0,
                    "trade_count_24h": buys + sells,
                    "price_change_24h_pct": _num(change.get("h24")),
                    "market_cap_usd": _num(pair.get("marketCap")),
                    "provider": "DexScreener",
                    "url": pair.get("url"),
                })
    except Exception as e:
        errors.append(str(e)[:200])

    diagnostics = {
        "provider": "DexScreener",
        "candidate_count": len(candidates),
        "errors": errors,
        "mode": TRADING_MODE,
    }
    return candidates, diagnostics

def _score(c: dict) -> float:
    return (
        min(_num(c.get("liquidity_usd")) / 10000, 5) +
        min(_num(c.get("volume_24h_usd")) / 20000, 5) +
        min(_num(c.get("buy_sell_ratio")), 4)
    )

async def get_signal_candidates(
    min_liquidity_usd: float | None = None,
    min_volume_24h_usd: float | None = None,
    min_buys_sells_ratio: float | None = None,
    limit: int = 20,
) -> dict:
    min_liq = MIN_LIQUIDITY_USD if min_liquidity_usd is None else min_liquidity_usd
    min_vol = MIN_VOLUME_24H_USD if min_volume_24h_usd is None else min_volume_24h_usd
    min_ratio = MIN_BUYS_SELLS_RATIO if min_buys_sells_ratio is None else min_buys_sells_ratio

    async with _lock:
        now = time.monotonic()
        if now - _cache["timestamp"] < CACHE_SECONDS and _cache["candidates"]:
            candidates = _cache["candidates"]
            diagnostics = _cache["diagnostics"]
        else:
            candidates, diagnostics = await _fetch_candidates()
            _cache.update({"timestamp": now, "candidates": candidates, "diagnostics": diagnostics})

    filtered = []
    rejected = []
    for c in candidates:
        reasons = []
        liq = _num(c.get("liquidity_usd"))
        vol = _num(c.get("volume_24h_usd"))
        ratio = _num(c.get("buy_sell_ratio"))
        price = _num(c.get("price_usd"))
        trades = int(_num(c.get("trade_count_24h")))
        change = _num(c.get("price_change_24h_pct"))

        if liq < min_liq:
            reasons.append("low_liquidity")
        if vol < min_vol:
            reasons.append("low_volume")
        if ratio < min_ratio or c.get("buys_24h", 0) == 0 or c.get("sells_24h", 0) == 0:
            reasons.append("weak_buy_sell")
        if trades < MIN_TRADES_24H:
            reasons.append("low_trades")
        if abs(change) > MAX_PRICE_CHANGE_24H_PCT:
            reasons.append("extreme_price_change")
        if change < -MAX_NEGATIVE_PRICE_CHANGE_24H_PCT:
            reasons.append("too_negative")
        if price <= 0:
            reasons.append("bad_price")

        if reasons:
            rejected.append({"symbol": c.get("symbol"), "reasons": reasons})
            continue

        item = dict(c)
        item["score"] = round(_score(item), 2)
        filtered.append(item)

    filtered.sort(key=lambda x: x["score"], reverse=True)
    signals = filtered[:limit]

    return {
        "provider": "DexScreener",
        "checked": len(candidates),
        "candidate_count": len(filtered),
        "signals": signals,
        "diagnostics": {
            **diagnostics,
            "rejected_count": len(rejected),
            "filters": {
                "min_liquidity_usd": min_liq,
                "min_volume_24h_usd": min_vol,
                "min_buys_sells_ratio": min_ratio,
            },
        },
        "mode": TRADING_MODE,
    }

def get_signal_engine_status() -> dict:
    return {
        "provider": "DexScreener",
        "mode": TRADING_MODE,
        "cache_age_seconds": round(time.monotonic() - _cache.get("timestamp", 0), 1),
        "last_candidate_count": len(_cache.get("candidates", [])),
    }
