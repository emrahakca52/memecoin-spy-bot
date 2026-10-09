import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Yalnızca piyasa verisi okunur; gerçek işlem yapılmaz.
REAL_TRADING_ENABLED = False
TRADING_MODE = "paper"

TRENDING_URL = (
    "https://api.geckoterminal.com/api/v2/"
    "networks/solana/trending_pools"
)

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))

CACHE_SECONDS = 90
ERROR_COOLDOWN_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 15

_cache: dict[str, Any] = {
    "time": 0.0,
    "candidates": [],
    "error": None,
}

_next_request_time = 0.0
_last_error_time = 0.0


def _number(value: Any, default: float = 0.0) -> float:
    """API'den gelen sayısal değerleri güvenli şekilde dönüştürür."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _address_from_relationship(pool: dict, name: str) -> str:
    """Havuz ilişkisinden Solana token adresini alır."""
    relationships = pool.get("relationships") or {}
    relation = relationships.get(name) or {}
    data = relation.get("data") or {}
    token_id = str(data.get("id") or "")

    if token_id.startswith("solana_"):
        return token_id[len("solana_"):]

    return token_id


def _included_tokens(payload: dict) -> dict[str, dict]:
    """Varsa included bölümündeki token bilgilerini indeksler."""
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
    """GeckoTerminal havuzlarını aday coin kayıtlarına dönüştürür."""
    data = payload.get("data") or []
    tokens = _included_tokens(payload)
    candidates = []

    for pool in data:
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
        name_parts = [
            part.strip()
            for part in pool_name.split("/")
        ]

        base_symbol = str(
            base_attrs.get("symbol")
            or (name_parts[0] if name_parts else "")
        ).strip()

        quote_symbol = str(
            quote_attrs.get("symbol")
            or (
                name_parts[1]
                if len(name_parts) > 1
                else ""
            )
        ).strip()

        # SOL/WSOL'u coin adayı olarak değerlendirme.
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

        if buys + sells == 0:
            continue

        price = _number(attrs.get("base_token_price_usd"))

        if price <= 0:
            continue

        buy_sell_ratio = buys / max(sells, 1.0)

        # Bu puan yalnızca sıralama amaçlıdır.
        # Kârlılık veya başarı garantisi değildir.
        score = (
            min(volume_24h / max(liquidity, 1.0), 10.0) * 5
            + min(buy_sell_ratio, 3.0) * 5
            + min(buys / max(sells, 1.0), 3.0)
        )

        candidates.append({
            "token_address": base_address,
            "symbol": base_symbol or "UNKNOWN",
            "name": pool_name,
            "quote_symbol": quote_symbol,
            "pool_address": str(attrs.get("address") or ""),
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume_24h,
            "buys_24h": int(buys),
            "sells_24h": int(sells),
            "buy_sell_ratio": round(buy_sell_ratio, 3),
            "score": round(score, 3),
            "source": "GeckoTerminal",
        })

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    # Aynı token birden fazla havuzda bulunabilir.
    unique = {}

    for candidate in candidates:
        address = candidate["token_address"]

        if address not in unique:
            unique[address] = candidate
        elif (
            candidate["liquidity_usd"]
            > unique[address]["liquidity_usd"]
        ):
            unique[address] = candidate

    return sorted(
        unique.values(),
        key=lambda item: item["score"],
        reverse=True,
    )


def _fetch_candidates() -> list[dict]:
    """Tek API isteğiyle trend havuzlarını tarar."""
    global _next_request_time, _last_error_time

    now = time.time()

    if now < _next_request_time:
        raise RuntimeError("provider_cooldown")

    headers = {
        "Accept": "application/json",
        "User-Agent": "MemecoinSpyBot/1.0",
    }

    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        response = client.get(TRENDING_URL)

    if response.status_code == 429:
        _last_error_time = time.time()
        _next_request_time = (
            _last_error_time + ERROR_COOLDOWN_SECONDS
        )
        raise RuntimeError("geckoterminal_rate_limited")

    response.raise_for_status()

    payload = response.json()
    candidates = _normalize_pools(payload)

    _next_request_time = time.time() + CACHE_SECONDS

    return candidates


def get_signal_candidates(
    limit: int = 20,
    **kwargs,
) -> dict:
    """
    En yüksek puanlı coin adaylarını döndürür.

    Paper trading dışında işlem yapmaz.
    """
    now = time.time()

    if now - _cache["time"] < CACHE_SECONDS:
        candidates = _cache["candidates"]
        error = _cache["error"]
    else:
        try:
            candidates = _fetch_candidates()

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

            # Hata durumunda eski veriyi yeni veriymiş gibi sunma.
            _cache.update({
                "time": time.time(),
                "candidates": [],
                "error": error,
            })

            candidates = []

    result = candidates[:max(1, min(int(limit), 20))]

    return {
        "provider": "GeckoTerminal",
        "checked": len(candidates),
        "candidate_count": len(candidates),
        "signals": result,
        "mode": TRADING_MODE,
        "real_trading_enabled": REAL_TRADING_ENABLED,
        "note": (
            "Adaylar puanlarına göre sıralandı; "
            "bu liste alım tavsiyesi veya kâr garantisi değildir."
            if result
            else "Uygun aday bulunamadı veya piyasa verisi alınamadı."
        ),
        "last_error": error,
    }
