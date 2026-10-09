import asyncio
import time

import httpx

DEX_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEX_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"

REQUEST_TIMEOUT = 15
CACHE_SECONDS = 30
COOLDOWN_SECONDS = 60

_cache = {"time": 0.0, "data": None}
_last_request = 0.0
_lock = asyncio.Lock()


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


async def _fetch_json(client, url, params=None):
    response = await client.get(url, params=params)

    if response.status_code == 429:
        raise httpx.HTTPStatusError(
            "Market data provider rate limited",
            request=response.request,
            response=response,
        )

    response.raise_for_status()
    return response.json()


async def _collect_pairs(client):
    pairs_by_address = {}

    # Önce Solana token listesine ulaşmayı dene.
    try:
        boosts = await _fetch_json(client, DEX_BOOSTS_URL)

        addresses = list(dict.fromkeys(
            item.get("tokenAddress")
            for item in boosts
            if isinstance(item, dict)
            and item.get("chainId") == "solana"
            and item.get("tokenAddress")
        ))[:30]

        for offset in range(0, len(addresses), 10):
            batch = addresses[offset:offset + 10]

            if not batch:
                continue

            payload = await _fetch_json(
                client,
                "https://api.dexscreener.com/latest/dex/tokens/"
                + ",".join(batch),
            )

            for pair in payload.get("pairs") or []:
                if not isinstance(pair, dict):
                    continue

                if (pair.get("chainId") or "").lower() != "solana":
                    continue

                address = (pair.get("baseToken") or {}).get("address")

                if address and address in batch:
                    pairs_by_address[address] = pair

    except httpx.HTTPStatusError:
        raise
    except (httpx.HTTPError, ValueError, TypeError):
        pass

    # İlk kaynak sonuç vermezse arama kaynağını dene.
    if not pairs_by_address:
        payload = await _fetch_json(
            client,
            DEX_SEARCH_URL,
            params={"q": "SOL"},
        )

        for pair in payload.get("pairs") or []:
            if not isinstance(pair, dict):
                continue

            if (pair.get("chainId") or "").lower() != "solana":
                continue

            address = (pair.get("baseToken") or {}).get("address")

            if address:
                previous = pairs_by_address.get(address)

                if (
                    previous is None
                    or _number(
                        (pair.get("liquidity") or {}).get("usd")
                    ) > _number(
                        (previous.get("liquidity") or {}).get("usd")
                    )
                ):
                    pairs_by_address[address] = pair

    return list(pairs_by_address.values())


def _build_signals(pairs, min_liquidity_usd,
                   min_volume_24h_usd, min_buys_sells_ratio, limit):
    signals = []

    for pair in pairs:
        base = pair.get("baseToken") or {}
        address = base.get("address")

        if not address:
            continue

        liquidity = _number(
            (pair.get("liquidity") or {}).get("usd")
        )

        volume = _number(
            (pair.get("volume") or {}).get("h24")
        )

        txns = (pair.get("txns") or {}).get("h24") or {}
        buys = _number((txns.get("buys") or 0))
        sells = _number((txns.get("sells") or 0))

        ratio = buys / max(sells, 1)

        # Yetersiz piyasa verilerini ele.
        if liquidity < min_liquidity_usd:
            continue

        if volume < min_volume_24h_usd:
            continue

        if ratio < min_buys_sells_ratio:
            continue

        # DexScreener fiyatı yalnızca BASE token içindir.
        price = _number(pair.get("priceUsd"))

        if price <= 0:
            continue

        signals.append({
            "token_address": address,
            "token_symbol": base.get("symbol") or "",
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "buys": int(buys),
            "sells": int(sells),
            "buys_to_sells_ratio": round(ratio, 4),
            "price_change_24h_pct": _number(
                (pair.get("priceChange") or {}).get("h24")
            ),
            "pair_address": pair.get("pairAddress"),
            "dex_id": pair.get("dexId"),
            "source": "DexScreener",
            "mode": "paper",
        })

    signals.sort(
        key=lambda item: (
            item["liquidity_usd"],
            item["volume_24h_usd"],
        ),
        reverse=True,
    )

    return signals[:limit]


async def get_signal_candidates(
    min_liquidity_usd=10000,
    min_volume_24h_usd=20000,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _last_request

    now = time.monotonic()

    if (
        _cache["data"] is not None
        and now - _cache["time"] < CACHE_SECONDS
    ):
        return _build_response(
            _cache["data"],
            min_liquidity_usd,
            min_volume_24h_usd,
            min_buys_sells_ratio,
            limit,
            "cache",
        )

    async with _lock:
        now = time.monotonic()

        if (
            _cache["data"] is not None
            and now - _cache["time"] < CACHE_SECONDS
        ):
            return _build_response(
                _cache["data"],
                min_liquidity_usd,
                min_volume_24h_usd,
                min_buys_sells_ratio,
                limit,
                "cache",
            )

        if now - _last_request < COOLDOWN_SECONDS:
            return {
                "provider": "DexScreener",
                "checked": 0,
                "signals": [],
                "note": "Provider cooldown active; retry later.",
            }

        _last_request = now

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "MemecoinSpyBot/1.5",
            },
        ) as client:
            pairs = await _collect_pairs(client)

        _cache["data"] = pairs
        _cache["time"] = time.monotonic()

        return _build_response(
            pairs,
            min_liquidity_usd,
            min_volume_24h_usd,
            min_buys_sells_ratio,
            limit,
            "DexScreener",
        )


def _build_response(
    pairs,
    min_liquidity_usd,
    min_volume_24h_usd,
    min_buys_sells_ratio,
    limit,
    provider,
):
    signals = _build_signals(
        pairs,
        min_liquidity_usd,
        min_volume_24h_usd,
        min_buys_sells_ratio,
        limit,
    )

    return {
        "provider": provider,
        "checked": len(pairs),
        "candidate_count": len(signals),
        "signals": signals,
        "mode": "paper",
        "real_trading_enabled": False,
        "note": (
            "Market filters only; signals are experimental and "
            "do not guarantee profitable trades."
        ),
    }
