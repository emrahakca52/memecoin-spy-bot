import asyncio
import time
import httpx

BASE_URL = "https://api.geckoterminal.com/api/v2"
TRENDING_URL = f"{BASE_URL}/networks/solana/trending_pools"
NEW_POOLS_URL = f"{BASE_URL}/networks/solana/new_pools"

REQUEST_TIMEOUT_SECONDS = 15
CACHE_SECONDS = 90
REQUEST_COOLDOWN_SECONDS = 90
PROVIDER_COOLDOWN_SECONDS = 300

MIN_LIQUIDITY_USD = 10000
MIN_VOLUME_24H_USD = 20000

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


def _included_tokens(payload):
    tokens = {}

    for item in payload.get("included") or []:
        if item.get("type") != "token":
            continue

        attrs = item.get("attributes") or {}
        address = attrs.get("address")

        if address:
            tokens[str(address).lower()] = attrs

    return tokens


def _relationship_address(pool, side, tokens):
    relationships = pool.get("relationships") or {}
    relation = relationships.get(side) or {}
    data = relation.get("data") or {}
    token_id = str(data.get("id") or "")

    if "_" in token_id:
        address = token_id.split("_", 1)[1]
        attrs = tokens.get(address.lower(), {})
        return address, attrs

    return "", {}


def _normalize_pools(payload):
    normalized = []
    tokens = _included_tokens(payload)

    for pool in payload.get("data") or []:
        attrs = pool.get("attributes") or {}

        base_address, base = _relationship_address(
            pool, "base_token", tokens
        )
        quote_address, quote = _relationship_address(
            pool, "quote_token", tokens
        )

        base_symbol = str(base.get("symbol") or "").upper()
        quote_symbol = str(quote.get("symbol") or "").upper()

        # SOL/WSOL temel varlık olarak aday olmasın.
        if base_symbol in {"SOL", "WSOL"}:
            address = quote_address
            token = quote
            price = _num(attrs.get("quote_token_price_usd"))
        else:
            address = base_address
            token = base
            price = _num(attrs.get("base_token_price_usd"))

        if not address or not price:
            continue

        symbol = str(token.get("symbol") or "").strip()

        if symbol.upper() in {"SOL", "WSOL"}:
            continue

        liquidity = _num(attrs.get("reserve_in_usd"))
        volume_data = attrs.get("volume_usd") or {}
        volume = _num(volume_data.get("h24"))

        if liquidity < MIN_LIQUIDITY_USD:
            continue

        if volume < MIN_VOLUME_24H_USD:
            continue

        txns = attrs.get("transactions") or {}
        txns_24h = txns.get("h24") or {}

        buys = int(max(0, _num(txns_24h.get("buys"))))
        sells = int(max(0, _num(txns_24h.get("sells"))))

        ratio = buys / max(sells, 1)

        changes = attrs.get("price_change_percentage") or {}

        normalized.append({
            "token_address": address,
            "token_symbol": symbol,
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_24h_usd": volume,
            "buys": buys,
            "sells": sells,
            "buys_to_sells_ratio": round(ratio, 4),
            "price_change_24h_pct": (
                _num(changes.get("h24"))
                if changes.get("h24") is not None
                else None
            ),
            "pair_address": attrs.get("address"),
            "dex_id": None,
            "source": "GeckoTerminal",
            "mode": "paper",
        })

    return normalized


async def _fetch_endpoint(client, url):
    response = await client.get(
        url,
        params={"include": "base_token,quote_token"},
    )

    if response.status_code == 429:
        raise RuntimeError("rate_limited")

    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError("Invalid GeckoTerminal response")

    return _normalize_pools(payload)


async def _fetch_candidates():
    headers = {
        "Accept": "application/json;version=20230302",
        "User-Agent": "MemecoinSpyBot/2.0",
    }

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    ) as client:
        # İlk olarak popüler havuzları tara.
        trending = await _fetch_endpoint(
            client, TRENDING_URL
        )

        # Ardından yeni havuzları tara.
        new_pools = await _fetch_endpoint(
            client, NEW_POOLS_URL
        )

    # Aynı token farklı havuzlarda görünüyorsa
    # likiditesi yüksek olan kaydı tut.
    best_by_token = {}

    for item in trending + new_pools:
        address = item["token_address"].lower()
        existing = best_by_token.get(address)

        if (
            existing is None
            or item["liquidity_usd"] > existing["liquidity_usd"]
        ):
            best_by_token[address] = item

    return list(best_by_token.values())


def _build_signals(
    pairs,
    min_liquidity_usd=MIN_LIQUIDITY_USD,
    min_volume_24h_usd=MIN_VOLUME_24H_USD,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    signals = []

    for item in pairs or []:
        if item["liquidity_usd"] < min_liquidity_usd:
            continue

        if item["volume_24h_usd"] < min_volume_24h_usd:
            continue

        if item["buys_to_sells_ratio"] < min_buys_sells_ratio:
            continue

        signals.append(item)

    signals.sort(
        key=lambda item: (
            item["liquidity_usd"],
            item["volume_24h_usd"],
            item["buys_to_sells_ratio"],
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
        "note": note or (
            "Experimental filters only; signals do not guarantee profits."
        ),
    }


async def get_signal_candidates(
    min_liquidity_usd=MIN_LIQUIDITY_USD,
    min_volume_24h_usd=MIN_VOLUME_24H_USD,
    min_buys_sells_ratio=1.0,
    limit=20,
):
    global _last_request_at
    global _provider_cooldown_until
    global _last_error

    now = time.monotonic()
    cached = _cache["pairs"]

    if cached is not None and now - _cache["at"] < CACHE_SECONDS:
        return _response(
            cached,
            min_liquidity_usd,
            min_volume_24h_usd,
            min_buys_sells_ratio,
            limit,
            "GeckoTerminal cache",
            "Cached data; prices may have changed.",
        )

    async with _lock:
        now = time.monotonic()
        cached = _cache["pairs"]

        if cached is not None and now - _cache["at"] < CACHE_SECONDS:
            return _response(
                cached,
                min_liquidity_usd,
                min_volume_24h_usd,
                min_buys_sells_ratio,
                limit,
                "GeckoTerminal cache",
            )

        if now < _provider_cooldown_until:
            if cached is not None:
                return _response(
                    cached,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "GeckoTerminal stale cache",
                    "Provider cooldown active; data may be stale.",
                )

            return {
                "provider": "GeckoTerminal",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Provider cooldown active.",
                "last_error": _last_error,
            }

        if now - _last_request_at < REQUEST_COOLDOWN_SECONDS:
            if cached is not None:
                return _response(
                    cached,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "GeckoTerminal stale cache",
                )

            return {
                "provider": "GeckoTerminal",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Request cooldown active.",
            }

        _last_request_at = time.monotonic()

        try:
            pairs = await _fetch_candidates()

            _cache["pairs"] = pairs
            _cache["at"] = time.monotonic()
            _last_error = None

            return _response(
                pairs,
                min_liquidity_usd,
                min_volume_24h_usd,
                min_buys_sells_ratio,
                limit,
                "GeckoTerminal",
            )

        except Exception as exc:
            _last_error = (
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )

            print(
                f"GeckoTerminal request failed: {_last_error}",
                flush=True,
            )

            if "rate_limited" in str(exc):
                _provider_cooldown_until = (
                    time.monotonic()
                    + PROVIDER_COOLDOWN_SECONDS
                )

            if cached is not None:
                return _response(
                    cached,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "GeckoTerminal stale cache",
                    "Provider failed; cached data may be stale.",
                )

            return {
                "provider": "GeckoTerminal",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Market data unavailable; no fresh signals returned.",
                "last_error": _last_error,
            }
