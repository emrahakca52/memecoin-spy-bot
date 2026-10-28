"""Jupiter-first Solana token discovery with DexScreener fallback."""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any

import httpx

TRADING_MODE = "paper"
REAL_TRADING_ENABLED = False
JUPITER_BASE_URL = os.getenv("JUPITER_BASE_URL", "https://api.jup.ag").rstrip("/")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
JUPITER_MIN_INTERVAL_SECONDS = float(os.getenv("JUPITER_MIN_INTERVAL_SECONDS", "2.1"))

MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))
MIN_TRADES_24H = int(os.getenv("MIN_TRADES_24H", "20"))
MAX_PRICE_CHANGE_24H_PCT = float(os.getenv("MAX_PRICE_CHANGE_24H_PCT", "100"))
MAX_NEGATIVE_PRICE_CHANGE_24H_PCT = float(os.getenv("MAX_NEGATIVE_PRICE_CHANGE_24H_PCT", "30"))
CACHE_SECONDS = int(os.getenv("SIGNAL_CACHE_SECONDS", "150"))

_lock = asyncio.Lock()
_jupiter_lock = asyncio.Lock()
_last_jupiter_request = 0.0
_cache = {"timestamp": 0.0, "candidates": [], "diagnostics": {}}
_last_dexscreener_discovery = {}
_last_geckoterminal_discovery = {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _first_num(*values: Any) -> float:
    for value in values:
        if value is not None:
            return _num(value)
    return 0.0


def _candidate_score(candidate: dict, min_liq: float, min_vol: float) -> tuple[float, dict]:
    """Rank candidates using early-runner features; hard safety checks remain separate."""
    liq = max(0.0, _num(candidate.get("liquidity_usd")))
    vol24 = max(0.0, _num(candidate.get("volume_24h_usd")))
    vol1h = max(0.0, _num(candidate.get("volume_1h_usd")))
    vol5m = max(0.0, _num(candidate.get("volume_5m_usd")))
    ratio24 = max(0.0, _num(candidate.get("buy_sell_ratio")))
    ratio1h = max(0.0, _num(candidate.get("buy_sell_ratio_1h")))
    trades24 = max(0.0, _num(candidate.get("trade_count_24h")))
    trades1h = max(0.0, _num(candidate.get("trade_count_1h")))
    change24 = _num(candidate.get("price_change_24h_pct"))
    change1h = _num(candidate.get("price_change_1h_pct"))
    change5m = _num(candidate.get("price_change_5m_pct"))

    # Score relative activity as well as absolute size: high 1h/5m activity
    # against the 24h baseline can indicate acceleration before a larger move.
    liquidity_score = min(1.0, math.log1p(liq) / math.log1p(max(min_liq * 10, 1.0)))
    volume24_score = min(1.0, math.log1p(vol24) / math.log1p(max(min_vol * 10, 1.0)))
    volume_accel = (vol1h / max(vol24, 1.0)) * 24.0 if vol1h > 0 else 0.0
    volume_accel_score = min(1.0, max(0.0, volume_accel) / 2.0)
    if vol5m > 0 and vol1h > 0:
        short_accel = (vol5m * 12.0) / max(vol1h, 1.0)
        short_accel_score = min(1.0, max(0.0, short_accel) / 2.0)
    else:
        short_accel_score = 0.5  # neutral when provider has no short-window data

    ratio = ratio1h if ratio1h > 0 else ratio24
    ratio_score = min(1.0, max(0.0, (ratio - 1.0) / 1.5))
    trades_score = min(1.0, trades1h / 80.0) if trades1h > 0 else min(1.0, trades24 / 200.0)

    # Reward positive but not parabolic momentum; penalize large 24h run-ups
    # because buying after an extreme candle can mean chasing the top.
    momentum = change1h if change1h != 0 else change24
    if momentum < 0:
        momentum_score = max(0.0, 1.0 - abs(momentum) / 20.0) * 0.35
    elif momentum <= 12:
        momentum_score = 0.45 + (momentum / 12.0) * 0.55
    else:
        momentum_score = max(0.0, 1.0 - (momentum - 12.0) / 38.0)
    if change24 > 80:
        momentum_score *= max(0.15, 1.0 - (change24 - 80.0) / 120.0)
    if change5m > 20:
        momentum_score *= 0.75  # very sharp 5m candles are an overheating warning

    components = {
        "liquidity": round(liquidity_score * 20, 1),
        "volume_24h": round(volume24_score * 10, 1),
        "volume_acceleration_1h": round(volume_accel_score * 15, 1),
        "volume_acceleration_5m": round(short_accel_score * 10, 1),
        "buy_sell_balance": round(ratio_score * 20, 1),
        "trade_activity": round(trades_score * 10, 1),
        "price_momentum": round(momentum_score * 15, 1),
    }
    return round(sum(components.values()), 1), components


async def _jupiter_get(client: httpx.AsyncClient, path: str, params: dict | None = None):
    """Respect keyless Jupiter's documented 0.5 requests/second rate limit."""
    global _last_jupiter_request
    async with _jupiter_lock:
        wait = JUPITER_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_jupiter_request)
        if wait > 0:
            await asyncio.sleep(wait)
        headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
        response = await client.get(f"{JUPITER_BASE_URL}{path}", params=params, headers=headers)
        _last_jupiter_request = time.monotonic()
        response.raise_for_status()
        return response.json()


def _jupiter_candidates(tokens: Any) -> list[dict]:
    if not isinstance(tokens, list):
        return []
    candidates = []
    for token in tokens:
        if not isinstance(token, dict):
            continue
        address = token.get("id") or token.get("mint") or token.get("address")
        if not address:
            continue
        raw_stats = token.get("stats24h") or token.get("stats24H") or token.get("stats") or {}
        if not isinstance(raw_stats, dict):
            raw_stats = {}
        stats = raw_stats.get("24h", raw_stats.get("h24", raw_stats))
        if not isinstance(stats, dict):
            stats = {}
        stats1h = raw_stats.get("1h", raw_stats.get("h1", {}))
        stats5m = raw_stats.get("5m", raw_stats.get("m5", {}))
        if not isinstance(stats1h, dict):
            stats1h = {}
        if not isinstance(stats5m, dict):
            stats5m = {}
        buys = int(_first_num(stats.get("numBuys"), stats.get("buys"), token.get("numBuys"), token.get("buys24h")))
        sells = int(_first_num(stats.get("numSells"), stats.get("sells"), token.get("numSells"), token.get("sells24h")))
        volume = _first_num(
            stats.get("volume"), stats.get("volumeUsd"), stats.get("totalVolume"),
            stats.get("volumeUSD"), token.get("volume24h"), token.get("volume24H")
        )
        if not volume:
            volume = _first_num(
                stats.get("buyVolume"), stats.get("buyVolumeUsd"), stats.get("buyVolume24h")
            ) + _first_num(
                stats.get("sellVolume"), stats.get("sellVolumeUsd"), stats.get("sellVolume24h")
            )
        # Keep percentage fields as percentages; Jupiter stats24h.priceChange is a ratio.
        # Do not infer units from the magnitude: a genuine +0.5% must remain +0.5%.
        if _has_finite_number(stats.get("priceChangePercent")):
            price_change_pct = _num(stats.get("priceChangePercent"))
            change_present = True
        elif _has_finite_number(token.get("priceChange24h")):
            price_change_pct = _num(token.get("priceChange24h"))
            change_present = True
        elif _has_finite_number(stats.get("priceChange")):
            price_change_pct = _num(stats.get("priceChange")) * 100.0
            change_present = True
        else:
            price_change_pct = 0.0
            change_present = False
        buys1h = int(_first_num(stats1h.get("numBuys"), stats1h.get("buys")))
        sells1h = int(_first_num(stats1h.get("numSells"), stats1h.get("sells")))
        volume1h = _first_num(stats1h.get("volume"), stats1h.get("volumeUsd"), stats1h.get("totalVolume"))
        volume5m = _first_num(stats5m.get("volume"), stats5m.get("volumeUsd"), stats5m.get("totalVolume"))
        candidates.append({
            "token_address": address,
            "symbol": token.get("symbol") or "UNKNOWN",
            "name": token.get("name") or "",
            "price_usd": _first_num(token.get("usdPrice"), token.get("price")),
            "liquidity_usd": _first_num(token.get("liquidity"), token.get("liquidityUsd")),
            "volume_24h_usd": volume,
            "volume_1h_usd": volume1h,
            "volume_5m_usd": volume5m,
            "buys_24h": buys,
            "sells_24h": sells,
            "buy_sell_ratio": buys / sells if sells > 0 else 0.0,
            "buy_sell_ratio_1h": buys1h / sells1h if sells1h > 0 else 0.0,
            "trade_count_24h": int(_first_num(stats.get("numTraders"), stats.get("tradeCount"), buys + sells)),
            "trade_count_1h": buys1h + sells1h,
            "price_change_24h_pct": price_change_pct,
            "price_change_1h_pct": _num(stats1h.get("priceChangePercent", stats1h.get("priceChange", 0))),
            "price_change_5m_pct": _num(stats5m.get("priceChangePercent", stats5m.get("priceChange", 0))),
            "price_change_24h_present": change_present,
            "market_cap_usd": _first_num(token.get("mcap"), token.get("marketCap")),
            "provider": "Jupiter",
            "url": f"https://jup.ag/tokens/{address}",
        })
    return candidates


_last_jupiter_discovery = {}


async def _discover_jupiter(client: httpx.AsyncClient) -> list[dict]:
    """Aggregate Jupiter recent, trending, and top-traded token feeds."""
    global _last_jupiter_discovery
    endpoints = (
        ("/tokens/v2/recent", "recent"),
        ("/tokens/v2/toptrending/1h?limit=100", "toptrending_1h"),
        ("/tokens/v2/toptraded/24h?limit=100", "toptraded_24h"),
    )
    all_candidates = {}
    feed_diagnostics = []
    for path, label in endpoints:
        feed = {"feed": label, "count": 0, "error": None}
        try:
            data = await _jupiter_get(client, path)
            parsed = _jupiter_candidates(data)
            feed["count"] = len(parsed)
            for candidate in parsed:
                address = candidate.get("token_address")
                if not address:
                    continue
                # Merge duplicate feed records field-by-field. A token can have
                # liquidity in one feed and transaction/price-change stats in another;
                # replacing the whole record can discard useful metrics and cause false
                # rejections. Never synthesize missing price-change data as a real value.
                previous = all_candidates.get(address)
                if previous is None:
                    all_candidates[address] = candidate
                else:
                    merged = dict(previous)
                    for key in ("liquidity_usd", "volume_24h_usd", "buys_24h",
                                "sells_24h", "trade_count_24h", "market_cap_usd"):
                        merged[key] = max(_num(previous.get(key)), _num(candidate.get(key)))
                    # Prefer a valid positive price and ratio from either feed.
                    if _num(candidate.get("price_usd")) > 0 and _num(previous.get("price_usd")) <= 0:
                        merged["price_usd"] = candidate["price_usd"]
                    # Keep a ratio actually reported by one feed; do not calculate a
                    # synthetic ratio from maxima taken from different feed snapshots.
                    merged["buy_sell_ratio"] = max(
                        _num(previous.get("buy_sell_ratio")), _num(candidate.get("buy_sell_ratio"))
                    )
                    if candidate.get("price_change_24h_present") and not previous.get("price_change_24h_present"):
                        merged["price_change_24h_pct"] = candidate.get("price_change_24h_pct", 0.0)
                        merged["price_change_24h_present"] = True
                    if not merged.get("name") and candidate.get("name"):
                        merged["name"] = candidate["name"]
                    if not merged.get("symbol") or merged.get("symbol") == "UNKNOWN":
                        merged["symbol"] = candidate.get("symbol") or merged.get("symbol")
                    all_candidates[address] = merged
        except Exception as exc:
            feed["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        feed_diagnostics.append(feed)
    _last_jupiter_discovery = {
        "feeds": feed_diagnostics,
        "unique_candidates": len(all_candidates),
    }
    if not all_candidates and all(feed["error"] for feed in feed_diagnostics):
        raise RuntimeError("All Jupiter discovery feeds failed")
    return list(all_candidates.values())


async def _discover_dexscreener(client: httpx.AsyncClient) -> list[dict]:
    """Build a broader Solana candidate pool from DexScreener profiles and boosts."""
    source_urls = (
        "https://api.dexscreener.com/token-profiles/latest/v1",
        "https://api.dexscreener.com/token-boosts/latest/v1",
        "https://api.dexscreener.com/token-boosts/top/v1",
    )
    global _last_dexscreener_discovery
    addresses = []
    seen = set()
    feed_diagnostics = []
    # Each discovery feed is optional: one unavailable feed must not disable fallback.
    for url in source_urls:
        feed = {"feed": url.rsplit("/", 1)[-1], "item_count": 0, "solana_tokens": 0, "error": None}
        try:
            response = await client.get(url)
            response.raise_for_status()
            items = response.json()
        except Exception as exc:
            feed["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            feed_diagnostics.append(feed)
            continue
        if not isinstance(items, list):
            feed["error"] = "Unexpected response type: " + type(items).__name__
            feed_diagnostics.append(feed)
            continue
        feed["item_count"] = len(items)
        for item in items:
            if not isinstance(item, dict) or item.get("chainId") != "solana":
                continue
            address = item.get("tokenAddress")
            if address:
                feed["solana_tokens"] += 1
                if address not in seen:
                    seen.add(address)
                    addresses.append(address)
            if len(addresses) >= 90:
                break
        feed_diagnostics.append(feed)
        if len(addresses) >= 90:
            break
    _last_dexscreener_discovery = {"feeds": feed_diagnostics, "unique_addresses": len(addresses)}
    if not addresses:
        return []
    candidates_by_address = {}
    # DexScreener's token endpoint accepts comma-separated addresses in batches.
    for offset in range(0, len(addresses), 30):
        batch = addresses[offset:offset + 30]
        response = await client.get(
            "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch)
        )
        response.raise_for_status()
        pairs = response.json()
        if not isinstance(pairs, list):
            continue
        for pair in pairs:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            token = pair.get("baseToken") or {}
            address = token.get("address")
            if not address:
                continue
            liquidity = _num((pair.get("liquidity") or {}).get("usd"))
            if address not in candidates_by_address or liquidity > candidates_by_address[address][0]:
                candidates_by_address[address] = (liquidity, pair)
    _last_dexscreener_discovery["pairs_returned"] = len(candidates_by_address)
    candidates = []
    for address, (_, pair) in candidates_by_address.items():
        token = pair.get("baseToken") or {}
        volume = pair.get("volume") or {}
        txns_all = pair.get("txns") or {}
        txns = txns_all.get("h24") or {}
        txns1h = txns_all.get("h1") or {}
        txns5m = txns_all.get("m5") or {}
        change = pair.get("priceChange") or {}
        buys, sells = int(_num(txns.get("buys"))), int(_num(txns.get("sells")))
        buys1h, sells1h = int(_num(txns1h.get("buys"))), int(_num(txns1h.get("sells")))
        candidates.append({
            "token_address": address, "symbol": token.get("symbol") or "UNKNOWN",
            "name": token.get("name") or "", "price_usd": _num(pair.get("priceUsd")),
            "liquidity_usd": _num((pair.get("liquidity") or {}).get("usd")),
            "volume_24h_usd": _num(volume.get("h24")),
            "volume_1h_usd": _num(volume.get("h1")),
            "volume_5m_usd": _num(volume.get("m5")),
            "buys_24h": buys, "sells_24h": sells,
            "buy_sell_ratio": buys / sells if sells > 0 else 0.0,
            "buy_sell_ratio_1h": buys1h / sells1h if sells1h > 0 else 0.0,
            "trade_count_24h": buys + sells, "trade_count_1h": buys1h + sells1h,
            "price_change_24h_pct": _num(change.get("h24")),
            "price_change_1h_pct": _num(change.get("h1")),
            "price_change_5m_pct": _num(change.get("m5")),
            "price_change_24h_present": _has_finite_number(change.get("h24")),
            "market_cap_usd": _num(pair.get("marketCap")), "provider": "DexScreener",
            "url": pair.get("url"),
        })
    return candidates


async def _discover_geckoterminal(client: httpx.AsyncClient) -> list[dict]:
    """Fallback discovery from GeckoTerminal when DexScreener is rate-limited."""
    global _last_geckoterminal_discovery
    results = []
    diagnostics = {"feeds": [], "candidate_count": 0}
    seen = set()
    for endpoint in ("trending_pools", "new_pools"):
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/{endpoint}"
        feed = {"feed": endpoint, "pool_count": 0, "error": None}
        try:
            response = await client.get(
                url,
                params={"include": "base_token"},
                headers={"Accept": "application/json;version=20230203"},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            feed["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            diagnostics["feeds"].append(feed)
            continue
        pools = payload.get("data", []) if isinstance(payload, dict) else []
        included = payload.get("included", []) if isinstance(payload, dict) else []
        token_by_id = {
            item.get("id"): item.get("attributes", {})
            for item in included
            if isinstance(item, dict) and item.get("type") == "token"
        }
        feed["pool_count"] = len(pools) if isinstance(pools, list) else 0
        for pool in pools if isinstance(pools, list) else []:
            if not isinstance(pool, dict):
                continue
            attrs = pool.get("attributes") or {}
            rel = pool.get("relationships") or {}
            base_rel = (rel.get("base_token") or {}).get("data") or {}
            token_id = base_rel.get("id") or ""
            address = token_id.split("solana_", 1)[-1] if token_id.startswith("solana_") else ""
            if not address or address in seen:
                continue
            seen.add(address)
            token_attrs = token_by_id.get(token_id, {})
            txns = (attrs.get("transactions") or {}).get("h24") or {}
            volume = attrs.get("volume_usd") or {}
            changes = attrs.get("price_change_percentage") or {}
            buys = int(_num(txns.get("buys")))
            sells = int(_num(txns.get("sells")))
            results.append({
                "token_address": address,
                "symbol": token_attrs.get("symbol") or (attrs.get("name", "").split(" / ")[0] if attrs.get("name") else "UNKNOWN"),
                "name": token_attrs.get("name") or attrs.get("name") or "",
                "price_usd": _num(attrs.get("base_token_price_usd")),
                "liquidity_usd": _num(attrs.get("reserve_in_usd")),
                "volume_24h_usd": _num(volume.get("h24")),
                "buys_24h": buys,
                "sells_24h": sells,
                "buy_sell_ratio": buys / sells if sells > 0 else 0.0,
                "trade_count_24h": buys + sells,
                "price_change_24h_pct": _num(changes.get("h24")),
                "price_change_24h_present": _has_finite_number(changes.get("h24")),
                "market_cap_usd": _num(attrs.get("market_cap_usd") or attrs.get("fdv_usd")),
                "provider": "GeckoTerminal",
                "url": f"https://www.geckoterminal.com/solana/pools/{attrs.get('address', '')}",
            })
        diagnostics["feeds"].append(feed)
    diagnostics["candidate_count"] = len(results)
    _last_geckoterminal_discovery = diagnostics
    return results


async def get_signal_candidates(
    min_liquidity_usd: float | None = None,
    min_volume_24h_usd: float | None = None,
    min_buys_sells_ratio: float | None = None,
    limit: int = 20,
) -> dict:
    global _last_geckoterminal_discovery
    min_liq = MIN_LIQUIDITY_USD if min_liquidity_usd is None else float(min_liquidity_usd)
    min_vol = MIN_VOLUME_24H_USD if min_volume_24h_usd is None else float(min_volume_24h_usd)
    min_ratio = MIN_BUYS_SELLS_RATIO if min_buys_sells_ratio is None else float(min_buys_sells_ratio)

    async with _lock:
        now = time.monotonic()
        if now - _cache["timestamp"] < CACHE_SECONDS and _cache["candidates"]:
            candidates = list(_cache["candidates"])
            diagnostics = dict(_cache["diagnostics"])
        else:
            candidates, provider, jupiter_error, fallback_error = [], "Jupiter", None, None
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "MemecoinSpy/1.0"}) as client:
                try:
                    candidates = await _discover_jupiter(client)
                    # If the endpoint returns no usable market statistics, use the fallback
                    # rather than silently treating missing fields as real zero-volume data.
                    if not candidates or not any(
                        c["volume_24h_usd"] >= min_vol
                        and c["liquidity_usd"] >= min_liq
                        and c.get("price_change_24h_present")
                        for c in candidates
                    ):
                        raise ValueError(
                            "Jupiter recent-token response had no candidates meeting configured "
                            "liquidity/volume thresholds; trying DexScreener fallback"
                        )
                except Exception as exc:
                    jupiter_error = f"{type(exc).__name__}: {exc}"
                    provider = "DexScreener fallback"
                    try:
                        candidates = await _discover_dexscreener(client)
                    except Exception as fallback_exc:
                        fallback_error = f"{type(fallback_exc).__name__}: {fallback_exc}"
                        candidates = []
                    if not candidates:
                        try:
                            candidates = await _discover_geckoterminal(client)
                            if candidates:
                                provider = "GeckoTerminal fallback"
                        except Exception as gecko_exc:
                            globals()["_last_geckoterminal_discovery"] = {
                                "error": f"{type(gecko_exc).__name__}: {str(gecko_exc)[:160]}"
                            }
            diagnostics = {
                "provider": provider,
                "jupiter_error": jupiter_error,
                "fallback_error": fallback_error,
                "candidate_count": len(candidates),
                "jupiter_discovery": dict(_last_jupiter_discovery),
                "fallback_discovery": dict(_last_dexscreener_discovery),
                "geckoterminal_discovery": dict(globals().get("_last_geckoterminal_discovery", {})),
                "mode": TRADING_MODE,
            }
            _cache.update({"timestamp": now, "candidates": candidates, "diagnostics": diagnostics})

    # Bounded market-filter adjustment: ease activity thresholds slightly while
    # keeping liquidity, trade-count, price-momentum and all RugCheck gates unchanged.
    requested_min_vol = min_vol
    requested_min_ratio = min_ratio
    min_vol = max(20000.0, min_vol * (2.0 / 3.0))
    min_ratio = max(1.0, min_ratio - 0.1)

    filtered = []
    rejected = {
        "missing_price_change": 0,
        "low_liquidity": 0,
        "low_volume": 0,
        "low_buy_sell_ratio": 0,
        "too_few_trades": 0,
        "price_change_out_of_range": 0,
        "price_change_above_positive_limit": 0,
        "price_change_below_negative_limit": 0,
        "invalid_price": 0,
    }
    for candidate in candidates:
        liq, vol = _num(candidate.get("liquidity_usd")), _num(candidate.get("volume_24h_usd"))
        ratio, price = _num(candidate.get("buy_sell_ratio")), _num(candidate.get("price_usd"))
        trades = int(_num(candidate.get("trade_count_24h")))
        change = _num(candidate.get("price_change_24h_pct"))

        # Count every failed criterion independently so diagnostics show the actual bottlenecks.
        if candidate.get("price_change_24h_present") is False:
            rejected["missing_price_change"] += 1
        if liq < min_liq:
            rejected["low_liquidity"] += 1
        if vol < min_vol:
            rejected["low_volume"] += 1
        if ratio < min_ratio:
            rejected["low_buy_sell_ratio"] += 1
        if trades < MIN_TRADES_24H:
            rejected["too_few_trades"] += 1
        if change > MAX_PRICE_CHANGE_24H_PCT:
            rejected["price_change_above_positive_limit"] += 1
            rejected["price_change_out_of_range"] += 1
        elif change < -MAX_NEGATIVE_PRICE_CHANGE_24H_PCT:
            rejected["price_change_below_negative_limit"] += 1
            rejected["price_change_out_of_range"] += 1
        if price <= 0:
            rejected["invalid_price"] += 1

        if candidate.get("price_change_24h_present") is False:
            continue
        if liq < min_liq or vol < min_vol or ratio < min_ratio or trades < MIN_TRADES_24H:
            continue
        if abs(change) > MAX_PRICE_CHANGE_24H_PCT or change < -MAX_NEGATIVE_PRICE_CHANGE_24H_PCT or price <= 0:
            continue
        item = dict(candidate)
        score, score_components = _candidate_score(item, min_liq, min_vol)
        item["score"] = score
        item["score_components"] = score_components
        filtered.append(item)
    filtered.sort(key=lambda item: item.get("score", 0), reverse=True)
    return {
        "provider": diagnostics.get("provider", "Jupiter"),
        "checked": len(candidates),
        "candidate_count": len(filtered),
        "signals": filtered[:max(0, int(limit))],
        "filter_diagnostics": {
            "thresholds": {
                "min_liquidity_usd": min_liq,
                "min_volume_24h_usd": min_vol,
                "min_buys_sells_ratio": min_ratio,
                "min_trades_24h": MIN_TRADES_24H,
                "max_abs_price_change_24h_pct": MAX_PRICE_CHANGE_24H_PCT,
                "max_negative_price_change_24h_pct": MAX_NEGATIVE_PRICE_CHANGE_24H_PCT,
            },
            "rejected_by_criterion": rejected,
            "requested_min_volume_24h_usd": requested_min_vol,
            "requested_min_buys_sells_ratio": requested_min_ratio,
            "note": "Activity thresholds are modestly relaxed within bounded floors; liquidity, trade-count, price limits and RugCheck safety gates remain unchanged. Counts overlap.",
        },
        "mode": TRADING_MODE,
        "diagnostics": diagnostics,
    }


def get_signal_engine_status() -> dict:
    return {
        "provider": _cache.get("diagnostics", {}).get("provider", "Jupiter"),
        "mode": TRADING_MODE,
        "cache_age_seconds": round(time.monotonic() - _cache.get("timestamp", 0), 1),
        "last_candidate_count": len(_cache.get("candidates", [])),
        "diagnostics": _cache.get("diagnostics", {}),
    }



def _has_finite_number(value: Any) -> bool:
    """Return True only for finite numeric values (booleans are not metrics)."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _normalize(token: dict[str, Any]) -> dict[str, Any]:
    """Normalize provider token metrics without treating missing/NaN changes as valid."""
    price_change = token.get("price_change_24h_percent", token.get("price_change_24h_pct"))
    change_present = _has_finite_number(price_change)
    return {
        "token_address": token.get("address") or token.get("token_address") or token.get("mint"),
        "symbol": token.get("symbol") or "UNKNOWN",
        "name": token.get("name") or "",
        "price_usd": _num(token.get("price", token.get("price_usd"))),
        "liquidity_usd": _num(token.get("liquidity", token.get("liquidity_usd"))),
        "volume_24h_usd": _num(token.get("volume_24h_usd", token.get("volume24h"))),
        "buys_24h": int(_num(token.get("buy_24h", token.get("buys_24h")))),
        "sells_24h": int(_num(token.get("sell_24h", token.get("sells_24h")))),
        "trade_count_24h": int(_num(token.get("trade_24h_count", token.get("trade_count_24h")))),
        "price_change_24h_pct": _num(price_change),
        "price_change_24h_present": change_present,
    }
