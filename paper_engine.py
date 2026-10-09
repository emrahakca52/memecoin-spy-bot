import asyncio
import time
from datetime import datetime, timezone
from threading import RLock

import httpx

# Paper simulation only. No wallet or real orders are used.
_lock = RLock()
_trades = []
_positions = {}
_next_id = 1
_realized_pnl_usd = 0.0

PRICE_CACHE_SECONDS = 120
RATE_LIMIT_COOLDOWN_SECONDS = 60
TAKE_PROFIT_PCT = 0.10   # Close simulated position at +10%
STOP_LOSS_PCT = -0.05    # Close simulated position at -5%
GECKO_BASE_URL = "https://api.geckoterminal.com/api/v2"
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
            "realized_pnl_usd": round(_realized_pnl_usd, 4),
            "take_profit_pct": TAKE_PROFIT_PCT * 100,
            "stop_loss_pct": STOP_LOSS_PCT * 100,
            "note": "Paper simulation only. No wallet is connected and no real orders are sent.",
        }


def list_paper_trades():
    with _lock:
        return [dict(trade) for trade in _trades]


def portfolio_status():
    with _lock:
        positions = [dict(pos) for pos in _positions.values()]
        invested = sum(p["amount_usd"] for p in positions)
        current_value = sum(p.get("current_value_usd", p["amount_usd"]) for p in positions)
        unrealized = current_value - invested
        return {
            "open_positions": positions,
            "paper_invested_usd": round(invested, 2),
            "paper_current_value_usd": round(current_value, 2),
            "paper_unrealized_pnl_usd": round(unrealized, 4),
            "paper_realized_pnl_usd": round(_realized_pnl_usd, 4),
            "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 4),
        }


def bot_snapshot():
    return {**paper_status(), **portfolio_status()}


def record_paper_trade(token_address, side, amount_usd, price_usd, token_symbol="", source="manual"):
    global _next_id
    side = side.strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'.")
    if not token_address or not token_address.strip():
        raise ValueError("token_address cannot be empty.")
    if amount_usd <= 0 or price_usd <= 0:
        raise ValueError("amount_usd and price_usd must be greater than zero.")

    with _lock:
        trade = {
            "id": _next_id,
            "token_address": token_address.strip(),
            "token_symbol": token_symbol,
            "side": side,
            "amount_usd": round(float(amount_usd), 4),
            "price_usd": float(price_usd),
            "simulated_token_quantity": round(float(amount_usd) / float(price_usd), 10),
            "timestamp": _now(),
            "mode": "paper",
            "source": source,
        }
        _next_id += 1
        _trades.append(trade)
        return dict(trade)


def open_paper_position(token_address, token_symbol, amount_usd, price_usd, max_open_positions=5, metadata=None):
    if not token_address or amount_usd <= 0 or price_usd <= 0:
        return {"opened": False, "reason": "invalid input"}
    with _lock:
        if token_address in _positions:
            return {"opened": False, "reason": "already open"}
        if len(_positions) >= max_open_positions:
            return {"opened": False, "reason": "position limit reached"}

        quantity = float(amount_usd) / float(price_usd)
        pos = {
            "token_address": token_address,
            "token_symbol": token_symbol,
            "amount_usd": round(float(amount_usd), 2),
            "entry_price_usd": float(price_usd),
            "current_price_usd": float(price_usd),
            "quantity": quantity,
            "current_value_usd": round(float(amount_usd), 4),
            "unrealized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0,
            "opened_at": _now(),
            "metadata": metadata or {},
        }
        _positions[token_address] = pos
        record_paper_trade(token_address, "buy", amount_usd, price_usd, token_symbol, "auto_paper")
        return {"opened": True, "position": dict(pos)}


async def _fetch_token_price(client, token_address):
    # GeckoTerminal token pools endpoint; select the deepest available pool.
    url = f"{GECKO_BASE_URL}/networks/solana/tokens/{token_address}/pools"
    response = await client.get(url, params={"page": 1})
    response.raise_for_status()
    payload = response.json()
    pools = payload.get("data") or []
    best = None
    best_liquidity = -1.0

    for pool in pools:
        attrs = pool.get("attributes") or {}
        price = attrs.get("base_token_price_usd")
        try:
            price = float(price or 0)
            liquidity = float(attrs.get("reserve_in_usd") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and liquidity > best_liquidity:
            best = (price, liquidity)
            best_liquidity = liquidity

    return best[0] if best else None


async def update_paper_prices():
    global _blocked_until, _last_price_error

    now = time.monotonic()
    with _lock:
        addresses = list(_positions.keys())
        if not addresses:
            return {"updated": 0, "closed": 0, "note": "No open positions"}

        cached = {
            address: _price_cache[address]["price"]
            for address in addresses
            if address in _price_cache and now - _price_cache[address]["at"] < PRICE_CACHE_SECONDS
        }
        to_fetch = [address for address in addresses if address not in cached]

        if now < _blocked_until:
            return {
                "updated": 0,
                "closed": 0,
                "rate_limited": True,
                "note": "GeckoTerminal cooldown active; previous prices retained.",
                "last_error": _last_price_error,
            }

    prices = dict(cached)
    if to_fetch:
        headers = {
            "Accept": "application/json;version=20230302",
            "User-Agent": "MemecoinSpyPro/1.1",
        }
        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                # At most five positions are normally open; sequential requests reduce rate-limit risk.
                for address in to_fetch:
                    price = await _fetch_token_price(client, address)
                    if price and price > 0:
                        prices[address] = price
                        _price_cache[address] = {"price": price, "at": time.monotonic()}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                _blocked_until = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                _last_price_error = "GeckoTerminal rate limit (429)"
            else:
                _last_price_error = f"GeckoTerminal HTTP {exc.response.status_code}"
            return {"updated": 0, "closed": 0, "error": _last_price_error, "rate_limited": exc.response.status_code == 429}
        except (httpx.HTTPError, ValueError) as exc:
            _last_price_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return {"updated": 0, "closed": 0, "error": _last_price_error}

    updated = 0
    closed = []
    with _lock:
        for address, pos in list(_positions.items()):
            price = prices.get(address)
            if not price or price <= 0:
                continue

            pos["current_price_usd"] = float(price)
            pos["current_value_usd"] = round(pos["quantity"] * float(price), 4)
            pnl = pos["current_value_usd"] - pos["amount_usd"]
            pnl_pct = (float(price) / pos["entry_price_usd"] - 1.0) * 100.0
            pos["unrealized_pnl_usd"] = round(pnl, 4)
            pos["unrealized_pnl_pct"] = round(pnl_pct, 3)
            pos["last_updated"] = _now()
            updated += 1

            reason = None
            if pnl_pct >= TAKE_PROFIT_PCT * 100:
                reason = "take_profit"
            elif pnl_pct <= STOP_LOSS_PCT * 100:
                reason = "stop_loss"

            if reason:
                sale_value = max(pos["current_value_usd"], 0.0001)
                record_paper_trade(
                    token_address=address,
                    side="sell",
                    amount_usd=sale_value,
                    price_usd=float(price),
                    token_symbol=pos["token_symbol"],
                    source=f"auto_paper_{reason}",
                )
                closed_pnl = sale_value - pos["amount_usd"]
                _realized_pnl_usd += closed_pnl
                closed.append({
                    "token_symbol": pos["token_symbol"],
                    "token_address": address,
                    "exit_price_usd": float(price),
                    "pnl_usd": round(closed_pnl, 4),
                    "pnl_pct": round(pnl_pct, 3),
                    "reason": reason,
                })
                del _positions[address]

        _last_price_error = None if prices else _last_price_error
        _blocked_until = 0.0 if prices else _blocked_until

    return {
        "updated": updated,
        "closed": len(closed),
        "closed_positions": closed,
        "cached": not bool(to_fetch),
        "rate_limited": False,
        "last_error": _last_price_error,
    }
