import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Güvenlik: gerçek işlem kapalı.
REAL_TRADING_ENABLED = False
TRADING_MODE = "paper"

DEXSCREENER_URL = (
    "https://api.dexscreener.com/latest/dex/search"
)

MIN_LIQUIDITY_USD = float(
    os.getenv("MIN_LIQUIDITY_USD", "10000")
)

MIN_VOLUME_24H_USD = float(
    os.getenv("MIN_VOLUME_24H_USD", "20000")
)

CACHE_SECONDS = 600
ERROR_COOLDOWN_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 20

_cache: dict[str, Any] = {
    "time": 0.0,
    "candidates": [],
    "error": None,
}

_next_request_time = 0.0
_lock = asyncio.Lock()


def _number(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_pairs(payload):
    pairs = payload.get("pairs") or []
    candidates = []

    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue

        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}

        base_symbol = str(
            base.get("symbol") or ""
        ).strip()

        quote_symbol = str(
            quote.get("symbol") or ""
        ).strip()

        base_address = str(
            base.get("address") or ""
        ).strip()

        if not base_address:
            continue

        # SOL'un kendisini aday olarak ekleme.
        if base_symbol.upper() in {"SOL", "WSOL"}:
            continue

        # SOL aramasında ilgisiz pariteleri azalt.
        if quote_symbol.upper() not in {
            "SOL", "WSOL", "USDC", "USDT"
        }:
            continue

        liquidity_data = pair.get("liquidity") or {}
        liquidity = _number(liquidity_data.get("usd"))

        volume_data = pair.get("volume") or {}
        volume_24h = _number(volume_data.get("h24"))

        txns = pair.get("txns") or {}
        h24 = txns.get("h24") or {}

        buys = _number(h24.get("buys"))
        sells = _number(h24.get("sells"))

        price = _number(pair.get("priceUsd"))

        if liquidity < MIN_LIQUIDITY_USD:
            continue

        if volume_24h < MIN_VOLUME_24H_USD:
            continue

        if buys + sells <= 0 or price <= 0:
            continue

        ratio = buys / max(sells, 1.0)

        # Sıralama puanıdır; kârlılık tahmini değildir.
        score = (
            min(volume_24h / max(liquidity, 1.0), 10.0) * 5
            + min(ratio, 3.0) * 5
        )

        candidates.append({
            "token_address": base_address,
            "symbol": base_symbol or "UNKNOWN",
            "name": str(base.get("name") or ""),
            "quote_symbol": quote_symbol,
            "pool_address": str(
                pair.get("pairAddress") or ""
            ),
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

    # Aynı token farklı havuzlarda bulunabilir.
    unique = {}

    for item in candidates:
        address = item["token_address"]
        previous = unique.get(address)

        if (
            previous is None
            or item["liquidity_usd"]
            > previous["liquidity_usd"]
        ):
            unique[address] = item

    return sorted(
        unique.values(),
        key=lambda item: item["score"],
        reverse=True,
    )


async def _fetch_candidates():
    global _next_request_time

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
        _next_request_time = (
            time.time() + ERROR_COOLDOWN_SECONDS
        )
        raise RuntimeError("dexscreener_rate_limited")

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("invalid_market_data")

    candidates = _normalize_pairs(payload)

    _next_request_time = time.time() + CACHE_SECONDS

    return candidates


async def get_signal_candidates(
    limit=20,
    **kwargs,
):
    async with _lock:
        now = time.time()

        if now - _cache["time"] < CACHE_SECONDS:
            candidates = _cache["candidates"]
            error = _cache["error"]

        else:
            try:
                candidates = await _fetch_candidates()

                _cache.update({
                    "time": time.time(),
                    "candidates": candidates,
                    "error": None,
                })

                error = None

            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

                logger.warning(
                    "DEX Screener tarama hatası: %s",
                    error,
                )

                _cache.update({
                    "time": time.time(),
                    "candidates": [],
                    "error": error,
                })

                candidates = []

    try:
        safe_limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        safe_limit = 20

    signals = candidates[:safe_limit]

    if signals:
        note = (
            "Adaylar piyasa verilerine göre sıralandı. "
            "Bu sonuçlar yatırım tavsiyesi veya kâr garantisi değildir."
        )
    elif error:
        note = (
            "Piyasa verisi alınamadı. "
            "last_error alanını kontrol edin."
        )
    else:
        note = (
            "Arama sonuçlarında filtrelerden geçen aday bulunamadı. "
            "Bu, piyasada hiç fırsat olmadığı anlamına gelmez."
        )

    return {
        "provider": "DEX Screener",
        "checked": len(candidates),
        "candidate_count": len(candidates),
        "signals": signals,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "note": note,
        "last_error": error,
    }
