import asyncio
import time
from datetime import datetime, timezone
from threading import RLock

import httpx

_lock = RLock()
_trades = []
_positions = {}
_next_id = 1

PRICE_CACHE_SECONDS = 300
RATE_LIMIT_COOLDOWN_SECONDS = 600

_price_cache = {}
_blocked_until = 0.0
_last_price_error = None


def _now():
    return datetime.now(timezone.utc).isoformat()


def paper_status():
    with _lock:
        return {
            "mode": "paper",
            "real_trading_enabled": False,
            "simulated_trade_count": len(_trades),
            "open_positions": len(_positions),
            "note": (
                "Paper simulation only. No wallet is connected "
                "and no real orders are sent."
            ),
        }


def list_paper_trades():
    with _lock:
        return [dict(trade) for trade in _trades]


def portfolio_status():
    with _lock:
        positions = [dict(pos) for pos in _positions.values()]
        invested = sum(p["amount_usd"] for p in positions)
        current_value = sum(
            p.get("current_value_usd", p["amount_usd"])
            for p in positions
        )

        return {
            "open_positions": positions,
            "paper_invested_usd": round(invested, 2),
            "paper_current_value_usd": round(current_value, 2),
            "paper_unrealized_pnl_usd": round(
                current_value - invested, 2
            ),
        }


def bot_snapshot():
    return {
        **paper_status(),
        **portfolio_status(),
    }


def record_paper_trade(
    token_address,
    side,
    amount_usd,
    price_usd,
    token_symbol="",
    source="manual",
):
    global _next_id

    side = side.strip().lower()

    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'.")

    if not token_address or not token_address.strip():
        raise ValueError("token_address cannot be empty.")

    if amount_usd <= 0 or price_usd <= 0:
        raise ValueError(
            "amount_usd and price_usd must be greater than zero."
        )

    with _lock:
        trade = {
            "id": _next_id,
            "token_address": token_address.strip(),
            "token_symbol": token_symbol,
            "side": side,
            "amount_usd": round(amount_usd, 2),
            "price_usd": price_usd,
            "simulated_token_quantity": round(
                amount_usd / price_usd, 10
            ),
            "timestamp": _now(),
            "mode": "paper",
            "source": source,
        }

        _next_id += 1
        _trades.append(trade)

        return dict(trade)


def open_paper_position(
    token_address,
    token_symbol,
    amount_usd,
    price_usd,
    max_open_positions=5,
    metadata=None,
):
    if not token_address or amount_usd <= 0 or price_usd <= 0:
        return {"opened": False, "reason": "invalid input"}

    with _lock:
        if token_address in _positions:
            return {"opened": False, "reason": "already open"}

        if len(_positions) >= max_open_positions:
            return {
                "opened": False,
                "reason": "position limit reached",
            }

        quantity = amount_usd / price_usd

        pos = {
            "token_address": token_address,
            "token_symbol": token_symbol,
            "amount_usd": round(amount_usd, 2),
            "entry_price_usd": price_usd,
            "current_price_usd": price_usd,
            "quantity": quantity,
            "current_value_usd": round(amount_usd, 2),
            "unrealized_pnl_usd": 0.0,
            "opened_at": _now(),
            "metadata": metadata or {},
        }

        _positions[token_address] = pos

        record_paper_trade(
            token_address=token_address,
            side="buy",
            amount_usd=amount_usd,
            price_usd=price_usd,
            token_symbol=token_symbol,
            source="auto_paper",
        )

        return {
            "opened": True,
            "position": dict(pos),
        }


async def update_paper_prices():
    global _blocked_until, _last_price_error

    now = time.monotonic()

    with _lock:
        addresses = list(_positions.keys())

        if not addresses:
            return {"updated": 0, "note": "No open positions"}

        # Önce önbellekteki fiyatları kullan.
        cached_addresses = {
            address
            for address in addresses
            if address in _price_cache
            and now - _price_cache[address]["at"] < PRICE_CACHE_SECONDS
        }

        addresses_to_fetch = [
            address for address in addresses
            if address not in cached_addresses
        ]

        # API sınırına takıldıysak yeni istek gönderme.
        if now < _blocked_until:
            return {
                "updated": 0,
                "rate_limited": True,
                "note": "DexScreener cooldown active; cached prices retained.",
                "last_error": _last_price_error,
            }

    if not addresses_to_fetch:
        return {"updated": 0, "cached": True}

    url = (
        "https://api.dexscreener.com/latest/dex/tokens/"
        + ",".join(addresses_to_fetch[:30])
    )

    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "MemecoinSpyPro/1.0"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            _blocked_until = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
            _last_price_error = "DexScreener rate limit (429)"
            return {
                "updated": 0,
                "rate_limited": True,
                "note": "Prices were not refreshed; previous values retained.",
            }

        _last_price_error = (
            f"HTTP {exc.response.status_code} from DexScreener"
        )
        return {
            "updated": 0,
            "error": _last_price_error,
        }

    except httpx.HTTPError as exc:
        _last_price_error = f"Market data request failed: {type(exc).__name__}"
        return {
            "updated": 0,
            "error": _last_price_error,
        }

    pairs = data.get("pairs") or []
    prices = {}

    for pair in pairs:
        token = pair.get("baseToken") or {}
        address = token.get("address")

        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue

        if address and price > 0:
            prices.setdefault(address, price)

    updated = 0
    current_time = time.monotonic()

    with _lock:
        for address, price in prices.items():
            _price_cache[address] = {
                "price": price,
                "at": current_time,
            }

        for address, pos in _positions.items():
            cached = _price_cache.get(address)

            if not cached:
                continue

            price = cached["price"]
            pos["current_price_usd"] = price
            pos["current_value_usd"] = round(
                pos["quantity"] * price, 4
            )
            pos["unrealized_pnl_usd"] = round(
                pos["current_value_usd"] - pos["amount_usd"], 4
            )
            pos["last_updated"] = _now()
            updated += 1

        _last_price_error = None
        _blocked_until = 0.0

    return {
        "updated": updated,
        "cached": False,
        "rate_limited": False,
    }
