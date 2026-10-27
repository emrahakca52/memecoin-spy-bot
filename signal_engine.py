"""DexScreener signal engine with required constants."""
from __future__ import annotations

import asyncio
import math
import os
import time
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

_lock = asyncio.Lock()
_cache = {"timestamp": 0.0, "candidates": [], "diagnostics": {}}

def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default

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
            candidates = []
            try:
                async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MemecoinSpy/1.0"}) as client:
                    r = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    profiles = r.json()
                    if not isinstance(profiles, list):
                        raise ValueError("Invalid response")

                    addresses = [
                        p.get("tokenAddress")
                        for p in profiles
                        if isinstance(p, dict) and p.get("chainId") == "solana" and p.get("tokenAddress")
                    ][:30]

                    if addresses:
                        token_url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addresses)
                        r = await client.get(token_url)
                        if r.status_code == 200:
                            pairs = r.json()
                            if isinstance(pairs, list):
                                best = {}
                                for pair in pairs:
                                    if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                                        continue
                                    base = pair.get("baseToken") or {}
                                    addr = base.get("address")
                                    if not addr:
                                        continue
                                    liq = _num((pair.get("liquidity") or {}).get("usd"))
                                    if addr not in best or liq > best[addr][0]:
                                        best[addr] = (liq, pair)

                                for addr, (_, pair) in best.items():
                                    token = pair.get("baseToken") or {}
                                    volume = pair.get("volume") or {}
                                    txns = (pair.get("txns") or {}).get("h24") or {}
                                    change = pair.get("priceChange") or {}
                                    liq_dict = pair.get("liquidity") or {}

                                    buys = int(_num(txns.get("buys")))
                                    sells = int(_num(txns.get("sells")))
                                    candidates.append({
                                        "token_address": addr,
                                        "symbol": token.get("symbol") or "UNKNOWN",
                                        "name": token.get("name") or "",
                                        "price_usd": _num(pair.get("priceUsd")),
                                        "liquidity_usd": _num(liq_dict.get("usd")),
                                        "volume_24h_usd": _num(volume.get("h24")),
                                        "buys_24h": buys,
                                        "sells_24h": sells,
                                        "buy_sell_ratio": buys / sells if sells > 0 else 0.0,
                                        "trade_count_24h": buys + sells,
                                        "price_change_24h_pct": _num(change.get("h24")),
                                        "market_cap_usd": _num(pair.get("marketCap")),
                                        "provider": "DexScreener",
                                        "url": pair.get("url"),
                                    })
            except Exception as e:
                candidates = []

            diagnostics = {
                "provider": "DexScreener",
                "candidate_count": len(candidates),
                "mode": TRADING_MODE,
            }
            _cache.update({"timestamp": now, "candidates": candidates, "diagnostics": diagnostics})

    filtered = []
    for c in candidates:
        liq = _num(c.get("liquidity_usd"))
        vol = _num(c.get("volume_24h_usd"))
        ratio = _num(c.get("buy_sell_ratio"))
        price = _num(c.get("price_usd"))
        trades = int(_num(c.get("trade_count_24h")))
        change = _num(c.get("price_change_24h_pct"))

        if liq < min_liq or vol < min_vol or ratio < min_ratio:
            continue
        if trades < MIN_TRADES_24H:
            continue
        if abs(change) > MAX_PRICE_CHANGE_24H_PCT or change < -MAX_NEGATIVE_PRICE_CHANGE_24H_PCT:
            continue
        if price <= 0:
            continue

        item = dict(c)
        item["score"] = round(
            min(liq / 10000, 5) + min(vol / 20000, 5) + min(ratio, 4), 2
        )
        filtered.append(item)

    filtered.sort(key=lambda x: x.get("score", 0), reverse=True)
    signals = filtered[:max(0, int(limit))]

    return {
        "provider": "DexScreener",
        "checked": len(candidates),
        "candidate_count": len(filtered),
        "signals": signals,
        "mode": TRADING_MODE,
        "diagnostics": diagnostics if "diagnostics" in dir() else {},
    }

def get_signal_engine_status() -> dict:
    return {
        "provider": "DexScreener",
        "mode": TRADING_MODE,
        "cache_age_seconds": round(time.monotonic() - _cache.get("timestamp", 0), 1),
        "last_candidate_count": len(_cache.get("candidates", [])),
    }
