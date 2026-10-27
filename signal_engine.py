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
            # Latest token profiles
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

            # Fetch pair data
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
                liq = pair.get
