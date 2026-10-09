import asyncio
import time

import httpx

DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
REQUEST_TIMEOUT_SECONDS = 12
CACHE_SECONDS = 60
REQUEST_COOLDOWN_SECONDS = 180

_cache = {"at": 0.0, "pairs": None}
_last_request_at = 0.0
_lock = asyncio.Lock()
_last_error = None


def _num(value, default=0.0):
    try:
        result = float(value or 0)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


async def _fetch_pairs(client):
    response = await client.get(DEX_SEARCH_URL, params={"q": "SOL"})
    if response.status_code == 429:
        raise RuntimeError("rate_limited")
    response.raise_for_status()
    payload = response.json()

    pairs, seen = [], set()
    for pair in payload.get("pairs") or []:
        if not isinstance(pair, dict):
            continue
        if (pair.get("chainId") or "").lower() != "solana":
            continue

        base = pair.get("baseToken") or {}
        address = base.get("address")
        price = _num(pair.get("priceUsd"))
        symbol = str(base.get("symbol") or "").strip().upper()

        # This scanner targets memecoin candidates, not the SOL asset itself.
        if symbol in {"SOL", "WSOL"}:
            continue
        if not address or price <= 0:
            continue

        pair_address = pair.get("pairAddress")
        key = pair_address or address
        if key in seen:
            continue
        seen.add(key)
        pairs.append(pair)

    return pairs


def _build_signals(
    pairs,
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    signals = []
    for pair in pairs or []:
        base = pair.get("baseToken") or {}
        address = base.get("address")
        symbol = str(base.get("symbol") or "").strip()
        if symbol.upper() in {"SOL", "WSOL"}:
            continue

        price = _num(pair.get("priceUsd"))
        if not address or price <= 0:
            continue

        liquidity = _num((pair.get("liquidity") or {}).get("usd"))
        volume = _num((pair.get("volume") or {}).get("h24"))
        txns = (pair.get("txns") or {}).get("h24") or {}
        buys = int(max(0, _num(txns.get("buys"))))
        sells = int(max(0, _num(txns.get("sells"))))
        ratio = buys / max(sells, 1)

        if liquidity < min_liquidity_usd or volume < min_volume_24h_usd:
            continue
        if ratio < min_buys_sells_ratio:
            continue

        price_change = (pair.get("priceChange") or {}).get("h24")
        signals.append({
            "token_address": address,
            "token_symbol": symbol,
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "buys": buys,
            "sells": sells,
            "buys_to_sells_ratio": round(ratio, 4),
            # Preserve missing price-change data as null, not a misleading 0%.
            "price_change_24h_pct": _num(price_change) if price_change is not None else None,
            "pair_address": pair.get("pairAddress"),
            "dex_id": pair.get("dexId"),
            "source": "DexScreener",
            "mode": "paper",
        })

    signals.sort(
        key=lambda item: (item["liquidity_usd"], item["volume_24h_usd"]),
        reverse=True,
    )
    return signals[:max(0, int(limit))]


def _response(pairs, min_liquidity_usd, min_volume_24h_usd,
              min_buys_sells_ratio, limit, provider, note=None):
    signals = _build_signals(
        pairs,
        min_liquidity_usd=min_liquidity_usd,
        min_volume_24h_usd=min_volume_24h_usd,
        min_buys_sells_ratio=min_buys_sells_ratio,
        limit=limit,
    )
    return {
        "provider": provider,
        "checked": len(pairs or []),
        "candidate_count": len(signals),
        "signals": signals,
        "mode": "paper",
        "real_trading_enabled": False,
        "note": note or "Experimental filters only; signals do not guarantee profits.",
    }


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _last_request_at, _last_error

    now = time.monotonic()
    cached_pairs = _cache["pairs"]
    if cached_pairs is not None and now - _cache["at"] < CACHE_SECONDS:
        return _response(
            cached_pairs, min_liquidity_usd, min_volume_24h_usd,
            min_buys_sells_ratio, limit, "DexScreener cache",
            "Cached data; verify quotes before any simulated entry.",
        )

    async with _lock:
        now = time.monotonic()
        cached_pairs = _cache["pairs"]
        if cached_pairs is not None and now - _cache["at"] < CACHE_SECONDS:
            return _response(
                cached_pairs, min_liquidity_usd, min_volume_24h_usd,
                min_buys_sells_ratio, limit, "DexScreener cache",
                "Cached data; verify quotes before any simulated entry.",
            )

        if now - _last_request_at < REQUEST_COOLDOWN_SECONDS:
            if cached_pairs is not None:
                return _response(
                    cached_pairs, min_liquidity_usd, min_volume_24h_usd,
                    min_buys_sells_ratio, limit, "DexScreener stale cache",
                    "Provider cooldown active; quotes may be stale.",
                )
            return {
                "provider": "DexScreener", "checked": 0, "candidate_count": 0,
                "signals": [], "mode": "paper", "real_trading_enabled": False,
                "note": "Provider cooldown active; no fresh data available.",
            }

        _last_request_at = now
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.7"},
            ) as client:
                pairs = await _fetch_pairs(client)
            _cache["pairs"] = pairs
            _cache["at"] = time.monotonic()
            _last_error = None
            return _response(
                pairs, min_liquidity_usd, min_volume_24h_usd,
                min_buys_sells_ratio, limit, "DexScreener",
            )
                except Exception as exc:
            _last_error = str(exc)[:250]

            print(
                f"DexScreener request failed: "
                f"{type(exc).__name__}: {_last_error}",
                flush=True
            )

            if cached_pairs is not None:
                return _response(
                    cached_pairs, min_liquidity_usd, min_volume_24h_usd,
                    min_buys_sells_ratio, limit, "DexScreener stale cache",
                    "Provider failed; cached quotes may be stale.",
                )

            return {
                "provider": "DexScreener",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Market data unavailable; no fresh signals returned.",
            }
                return _response(
                    cached_pairs, min_liquidity_usd, min_volume_24h_usd,
                    min_buys_sells_ratio, limit, "DexScreener stale cache",
                    "Provider failed; cached quotes may be stale.",
                )
            return {
                "provider": "DexScreener", "checked": 0, "candidate_count": 0,
                "signals": [], "mode": "paper", "real_trading_enabled": False,
                "note": "Market data unavailable; no fresh signals returned.",
            }
