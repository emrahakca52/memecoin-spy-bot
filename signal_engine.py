import logging
import os
import time
import asyncio
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Güvenlik: gerçek işlem kesinlikle kapalı.
REAL_TRADING_ENABLED = False
TRADING_MODE = "paper"

TRENDING_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/solana/trending_pools"
)

MIN_LIQUIDITY_USD = float(
    os.getenv("MIN_LIQUIDITY_USD", "10000")
)
MIN_VOLUME_24H_USD = float(
    os.getenv("MIN_VOLUME_24H_USD", "20000")
)

CACHE_SECONDS = 90
ERROR_COOLDOWN_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15

_cache: dict[str, Any] = {
    "time": 0.0,
    "candidates": [],
    "error": None,
}

_next_request_time = 0.0
_lock = asyncio.Lock()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _address_from_relationship(
    pool: dict,
    name: str,
) -> str:
    relationships = pool.get("relationships") or {}
    relation = relationships.get(name) or {}
    data = relation.get("data") or {}
    token_id = str(data.get("id") or "")

    if token_id.startswith("solana_"):
        return token_id[len("solana_"):]

    return token_id


def _included_tokens(payload: dict) -> dict[str, dict]:
    result = {}

    for item in payload.get("included") or []:
        if item.get("type") != "token":
            continue

        item_id = str(item.get("id") or "")
        attrs = item.get("attributes") or {}

        if item_id.startswith("solana_"):
            address = item_id[len("solana_"):]
            result[address] = attrs

    return result


def _normalize_pools(payload: dict) -> list[dict]:
    pools = payload.get("data") or []
    tokens = _included_tokens(payload)
    candidates = []

    for pool in pools:
        attrs = pool.get("attributes") or {}

        base_address = _address_from_relationship(
            pool, "base_token"
        )
        quote_address = _address_from_relationship(
            pool, "quote_token"
        )

        if not base_address:
            continue

        base_attrs = tokens.get(base_address, {})
        quote_attrs = tokens.get(quote_address, {})

        pool_name = str(attrs.get("name") or "")
        parts = [
            item.strip()
            for item in pool_name.split("/")
        ]

        base_symbol = str(
            base_attrs.get("symbol")
            or (parts[0] if parts else "")
        ).strip()

        quote_symbol = str(
            quote_attrs.get("symbol")
            or (parts[1] if len(parts) > 1 else "")
        ).strip()

        # SOL havuzunu coin adayı olarak ekleme.
        if base_symbol.upper() in {"SOL", "WSOL"}:
            continue

        liquidity = _number(attrs.get("reserve_in_usd"))

        volume_data = attrs.get("volume_usd") or {}
        volume_24h = _number(volume_data.get("h24"))

        if liquidity < MIN_LIQUIDITY_USD:
            continue

        if volume_24h < MIN_VOLUME_24H_USD:
            continue

        transactions = attrs.get("transactions") or {}
        h24 = transactions.get("h24") or {}

        buys = _number(h24.get("buys"))
        sells = _number(h24.get("sells"))

        if buys + sells <= 0:
            continue

        price = _number(
            attrs.get("base_token_price_usd")
        )

        if price <= 0:
            continue

        ratio = buys / max(sells, 1.0)

        # Sıralama puanı; kâr olasılığı değildir.
        score = (
            min(volume_24h / max(liquidity, 1.0), 10.0) * 5
            + min(ratio, 3.0) * 5
        )

        candidates.append({
            "token_address": base_address,
            "symbol": base_symbol or "UNKNOWN",
            "name": pool_name,
            "quote_symbol": quote_symbol,
            "pool_address": str(
                attrs.get("address") or ""
            ),
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume_24h,
            "buys_24h": int(buys),
            "sells_24h": int(sells),
            "buy_sell_ratio": round(ratio, 3),
            "score": round(score, 3),
            "source": "GeckoTerminal",
        })

    # Aynı tokenin birden fazla havuzunu tek kayda indir.
    unique = {}

    for candidate in candidates:
        address = candidate["token_address"]

        previous = unique.get(address)

        if (
            previous is None
            or candidate["liquidity_usd"]
            > previous["liquidity_usd"]
        ):
            unique[address] = candidate

    return sorted(
        unique.values(),
        key=lambda item: item["score"],
        reverse=True,
    )


async def _fetch_candidates() -> list[dict]:
    global _next_request_time

    now = time.time()

    if now < _next_request_time:
        raise RuntimeError("provider_cooldown")

    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.0",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        response = await client.get(TRENDING_URL)

    if response.status_code == 429:
        _next_request_time = (
            time.time() + ERROR_COOLDOWN_SECONDS
        )
        raise RuntimeError("geckoterminal_rate_limited")

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("invalid_market_data")

    candidates = _normalize_pools(payload)

    _next_request_time = time.time() + CACHE_SECONDS

    return candidates


async def get_signal_candidates(
    limit: int = 20,
    **kwargs,
) -> dict:
    """
    Piyasa adaylarını asenkron olarak getirir.
    Gerçek emir göndermez.
    """
    async with _lock:
        now = time.time()

        # Başarılı sonuçları önbellekten kullan.
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
                    "GeckoTerminal tarama hatası: %s",
                    error,
                )

                # Hata varsa eski veriyi güncel gibi gösterme.
                _cache.update({
                    "time": time.time(),
                    "candidates": [],
                    "error": error,
                })

                candidates = []

    safe_limit = max(1, min(int(limit), 20))
    signals = candidates[:safe_limit]

    if signals:
        note = (
            "Piyasa adayları puanlarına göre sıralandı. "
            "Bu sonuçlar alım tavsiyesi veya kâr garantisi değildir."
        )
    elif error:
        note = (
            "Piyasa verisi alınamadı. "
            "Veri sağlayıcısı ve istek sınırları kontrol edilmeli."
        )
    else:
        note = "Belirlenen koşullarda uygun aday bulunamadı."

    return {
        "provider": "GeckoTerminal",
        "checked": len(candidates),
        "candidate_count": len(candidates),
        "signals": signals,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "note": note,
        "last_error": error,
    }
