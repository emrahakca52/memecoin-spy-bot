import asyncio
import random
import time

import httpx

DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"

REQUEST_TIMEOUT_SECONDS = 12
CACHE_SECONDS = 90
REQUEST_COOLDOWN_SECONDS = 180
MAX_PROVIDER_COOLDOWN_SECONDS = 900

_cache = {"at": 0.0, "pairs": None}
_last_request_at = 0.0
_provider_cooldown_until = 0.0
_last_error = None
_lock = asyncio.Lock()


def _num(value, default=0.0):
    try:
        result = float(value or 0)
        if result != result or abs(result) == float("inf"):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _retry_after_seconds(response):
    value = response.headers.get("Retry-After")

    if value:
        try:
            return min(
                MAX_PROVIDER_COOLDOWN_SECONDS,
                max(1, int(float(value))),
            )
        except (TypeError, ValueError):
            pass

    return 300


def _filter_solana_pairs(payload):
    pairs = []
    seen = set()

    for pair in payload.get("pairs") or []:
        if not isinstance(pair, dict):
            continue

        # Yalnızca Solana ağındaki çiftleri kullan.
        if str(pair.get("chainId") or "").lower() != "solana":
            continue

        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}

        address = base.get("address")
        symbol = str(base.get("symbol") or "").strip().upper()
        name = str(base.get("name") or "").strip().upper()
        price = _num(pair.get("priceUsd"))

        if not address or price <= 0:
            continue

        # SOL ve WSOL gibi temel varlıkları aday listesinden çıkar.
        if symbol in {"SOL", "WSOL"}:
            continue

        if name in {"SOL", "WSOL", "WRAPPED SOL", "SOLANA"}:
            continue

        liquidity = _num((pair.get("liquidity") or {}).get("usd"))
        volume = _num((pair.get("volume") or {}).get("h24"))

        # Düşük likiditeli ve düşük hacimli çiftleri ele.
        if liquidity < 10000 or volume < 20000:
            continue

        pair_address = pair.get("pairAddress")
        key = str(pair_address or address).lower()

        if key in seen:
            continue

        seen.add(key)
        pairs.append(pair)

    return pairs


async def _fetch_pairs(client):
    response = await client.get(
        DEX_SEARCH_URL,
        params={"q": "SOL"},
    )

    if response.status_code == 429:
        raise RuntimeError(
            f"rate_limited:{_retry_after_seconds(response)}"
        )

    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Invalid DexScreener JSON response")

    return _filter_solana_pairs(payload)


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

        if not address:
            continue

        price = _num(pair.get("priceUsd"))
        liquidity = _num(
            (pair.get("liquidity") or {}).get("usd")
        )
        volume = _num(
            (pair.get("volume") or {}).get("h24")
        )

        if price <= 0:
            continue

        if liquidity < min_liquidity_usd:
            continue

        if volume < min_volume_24h_usd:
            continue

        txns = (
            (pair.get("txns") or {}).get("h24") or {}
        )

        buys = int(max(0, _num(txns.get("buys"))))
        sells = int(max(0, _num(txns.get("sells"))))

        ratio = buys / max(sells, 1)

        if ratio < min_buys_sells_ratio:
            continue

        price_change = (
            (pair.get("priceChange") or {}).get("h24")
        )

        signals.append({
            "token_address": address,
            "token_symbol": symbol,
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "buys": buys,
            "sells": sells,
            "buys_to_sells_ratio": round(ratio, 4),
            "price_change_24h_pct": (
                _num(price_change)
                if price_change is not None
                else None
            ),
            "pair_address": pair.get("pairAddress"),
            "dex_id": pair.get("dexId"),
            "source": "DexScreener",
            "mode": "paper",
        })

    # Önce likidite, ardından hacim yüksek olanları sırala.
    signals.sort(
        key=lambda item: (
            item["liquidity_usd"],
            item["volume_24h_usd"],
        ),
        reverse=True,
    )

    return signals[:max(0, int(limit))]


def _response(
    pairs,
    min_liquidity_usd,
    min_volume_24h_usd,
    min_buys_sells_ratio,
    limit,
    provider,
    note=None,
):
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
        "note": (
            note
            or "Experimental filters only; signals do not guarantee profits."
        ),
    }


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _last_request_at
    global _provider_cooldown_until, _last_error

    now = time.monotonic()
    cached_pairs = _cache["pairs"]

    # Taze önbellek varsa API'ye yeniden gitme.
    if (
        cached_pairs is not None
        and now - _cache["at"] < CACHE_SECONDS
    ):
        return _response(
            cached_pairs,
            min_liquidity_usd,
            min_volume_24h_usd,
            min_buys_sells_ratio,
            limit,
            "DexScreener cache",
            "Cached data; verify prices before simulated entries.",
        )

    async with _lock:
        now = time.monotonic()
        cached_pairs = _cache["pairs"]

        if (
            cached_pairs is not None
            and now - _cache["at"] < CACHE_SECONDS
        ):
            return _response(
                cached_pairs,
                min_liquidity_usd,
                min_volume_24h_usd,
                min_buys_sells_ratio,
                limit,
                "DexScreener cache",
                "Cached data; verify prices before simulated entries.",
            )

        # Sağlayıcı bekleme süresindeyse yeni istek gönderme.
        if now < _provider_cooldown_until:
            if cached_pairs is not None:
                return _response(
                    cached_pairs,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "DexScreener stale cache",
                    "Provider cooldown active; cached prices may be stale.",
                )

            return {
                "provider": "DexScreener",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": (
                    "Provider rate-limited; waiting before retry."
                ),
                "last_error": _last_error,
                "retry_in_seconds": max(
                    1, int(_provider_cooldown_until - now)
                ),
            }

        # İstekler arasında minimum süre bırak.
        elapsed = now - _last_request_at

        if elapsed < REQUEST_COOLDOWN_SECONDS:
            if cached_pairs is not None:
                return _response(
                    cached_pairs,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "DexScreener stale cache",
                    "Request cooldown active; cached data may be stale.",
                )

            return {
                "provider": "DexScreener",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Request cooldown active; no fresh data available.",
            }

        _last_request_at = time.monotonic()

        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "MemecoinSpyBot/1.7",
                },
            ) as client:
                pairs = await _fetch_pairs(client)

            _cache["pairs"] = pairs
            _cache["at"] = time.monotonic()
            _last_error = None

            return _response(
                pairs,
                min_liquidity_usd,
                min_volume_24h_usd,
                min_buys_sells_ratio,
                limit,
                "DexScreener",
            )

        except Exception as exc:
            _last_error = (
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

            print(
                f"DexScreener request failed: {_last_error}",
                flush=True,
            )

            if str(exc).startswith("rate_limited:"):
                try:
                    wait_seconds = int(str(exc).split(":")[1])
                except (ValueError, IndexError):
                    wait_seconds = 300

                _provider_cooldown_until = (
                    time.monotonic()
                    + min(
                        MAX_PROVIDER_COOLDOWN_SECONDS,
                        max(1, wait_seconds),
                    )
                    + random.uniform(1, 5)
                )

            if cached_pairs is not None:
                return _response(
                    cached_pairs,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "DexScreener stale cache",
                    "Provider failed; cached data may be stale.",
                )

            return {
                "provider": "DexScreener",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Market data unavailable; no fresh signals returned.",
                "last_error": _last_error,
            }
