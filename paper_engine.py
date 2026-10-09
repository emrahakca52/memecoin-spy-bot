import asyncio
import time

import httpx

GECKO_TOKEN_POOLS_URL = (
    "https://api.geckoterminal.com/api/v2/networks/solana/tokens/"
)
DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"
PRICE_TIMEOUT_SECONDS = 12
PRICE_CACHE_SECONDS = 90
PRICE_COOLDOWN_SECONDS = 60

_price_cache = {}
_price_last_request = {}
_price_lock = asyncio.Lock()

_open_positions = {}
_paper_trades = []
_realized_pnl_usd = 0.0


def _num(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


async def _get_json(client, url):
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def _fetch_price(client, token_address):
    try:
        payload = await _get_json(client, DEX_TOKEN_URL + token_address)
        pairs = payload.get("pairs") or []
        sol_pairs = [
            p for p in pairs
            if isinstance(p, dict)
            and (p.get("chainId") or "").lower() == "solana"
            and _num(p.get("priceUsd")) > 0
        ]
        if sol_pairs:
            sol_pairs.sort(
                key=lambda p: _num((p.get("liquidity") or {}).get("usd")),
                reverse=True,
            )
            return _num(sol_pairs[0].get("priceUsd"))

    except (httpx.HTTPError, ValueError):
        pass

    try:
        payload = await _get_json(
            client, GECKO_TOKEN_POOLS_URL + token_address + "/pools"
        )
        candidates = []
        for pool in payload.get("data") or []:
            attrs = pool.get("attributes") or {}
            price = _num(attrs.get("base_token_price_usd"))
            if price > 0:
                candidates.append(
                    (_num(attrs.get("reserve_in_usd")), price)
                )
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
    except (httpx.HTTPError, ValueError):
        pass

    return None


async def get_token_price_usd(token_address):
    if not token_address:
        return None

    now = time.monotonic()
    cached = _price_cache.get(token_address)
    if cached and now - cached["at"] < PRICE_CACHE_SECONDS:
        return cached["price"]

    last = _price_last_request.get(token_address, 0.0)
    if now - last < PRICE_COOLDOWN_SECONDS:
        return cached["price"] if cached else None

    async with _price_lock:
        now = time.monotonic()
        cached = _price_cache.get(token_address)
        if cached and now - cached["at"] < PRICE_CACHE_SECONDS:
            return cached["price"]

        _price_last_request[token_address] = now
        headers = {
            "Accept": "application/json",
            "User-Agent": "MemecoinSpyBot/1.1",
        }
        async with httpx.AsyncClient(
            timeout=PRICE_TIMEOUT_SECONDS,
            headers=headers,
        ) as client:
            price = await _fetch_price(client, token_address)

        if price is not None and price > 0:
            _price_cache[token_address] = {
                "at": time.monotonic(),
                "price": price,
            }
            return price

    return None


def record_paper_trade(
    token_address,
    side,
    amount_usd,
    price_usd,
    token_symbol="",
    source="manual",
    reason=None,
):
    global _realized_pnl_usd

    side = str(side).upper()
    amount_usd = _num(amount_usd)
    price_usd = _num(price_usd)

    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not token_address or amount_usd <= 0 or price_usd <= 0:
        raise ValueError("token_address, amount_usd and price_usd must be valid")

    trade = {
        "token_address": token_address,
        "token_symbol": token_symbol or token_address[:8],
        "side": side,
        "amount_usd": round(amount_usd, 8),
        "price_usd": price_usd,
        "source": source,
        "reason": reason,
        "timestamp": time.time(),
        "mode": "paper",
    }
    _paper_trades.append(trade)

    if side == "BUY":
        quantity = amount_usd / price_usd
        position = _open_positions.get(token_address)
        if position is None:
            _open_positions[token_address] = {
                "token_address": token_address,
                "token_symbol": trade["token_symbol"],
                "entry_price_usd": price_usd,
                "current_price_usd": price_usd,
                "invested_usd": amount_usd,
                "quantity": quantity,
                "unrealized_pnl_usd": 0.0,
                "opened_at": time.time(),
            }
        else:
            old_qty = _num(position.get("quantity"))
            new_qty = quantity
            total_qty = old_qty + new_qty
            position["entry_price_usd"] = (
                _num(position.get("entry_price_usd")) * old_qty
                + price_usd * new_qty
            ) / total_qty
            position["quantity"] = total_qty
            position["invested_usd"] = _num(position.get("invested_usd")) + amount_usd
            position["current_price_usd"] = price_usd

    elif side == "SELL":
        position = _open_positions.get(token_address)
        if position:
            qty = _num(position.get("quantity"))
            sell_qty = min(qty, amount_usd / price_usd)
            cost_per_unit = _num(position.get("invested_usd")) / max(qty, 1e-12)
            proceeds = sell_qty * price_usd
            cost = sell_qty * cost_per_unit
            _realized_pnl_usd += proceeds - cost
            remaining_qty = qty - sell_qty
            if remaining_qty <= 1e-12:
                _open_positions.pop(token_address, None)
            else:
                position["quantity"] = remaining_qty
                position["invested_usd"] = max(
                    0.0, _num(position.get("invested_usd")) - cost
                )

    return {"ok": True, "mode": "paper", "trade": trade}


def open_paper_position(
    token_address,
    token_symbol="",
    amount_usd=10.0,
    price_usd=None,
    source="auto",
    max_open_positions=None,
    metadata=None,
):
    """Open a simulated position; accepts auto_paper_bot compatibility args."""
    if not token_address:
        raise ValueError("token_address is required")
    if price_usd is None or _num(price_usd) <= 0:
        raise ValueError("price_usd is required and must be positive")

    # Do not repeatedly buy the same token on every polling cycle.
    if token_address in _open_positions:
        return {"ok": True, "mode": "paper", "skipped": True,
                "reason": "position_already_open", "token_address": token_address}

    if max_open_positions is not None and len(_open_positions) >= int(max_open_positions):
        return {"ok": True, "mode": "paper", "skipped": True,
                "reason": "max_open_positions_reached"}

    result = record_paper_trade(
        token_address=token_address,
        token_symbol=token_symbol,
        side="BUY",
        amount_usd=amount_usd,
        price_usd=price_usd,
        source=source,
    )
    position = _open_positions.get(token_address)
    if position is not None and metadata:
        position["metadata"] = dict(metadata)
    return result


async def update_paper_prices():
    global _realized_pnl_usd

    updated = 0
    closed = 0
    errors = 0

    for address in list(_open_positions):
        position = _open_positions.get(address)
        if not position:
            continue

        price = await get_token_price_usd(address)
        if price is None or price <= 0:
            errors += 1
            continue

        position = _open_positions.get(address)
        if not position:
            continue

        position["current_price_usd"] = price
        quantity = _num(position.get("quantity"))
        invested = _num(position.get("invested_usd"))
        value = quantity * price
        pnl = value - invested
        position["unrealized_pnl_usd"] = round(pnl, 8)
        updated += 1

        entry = _num(position.get("entry_price_usd"))
        if entry <= 0:
            continue
        change_pct = (price / entry - 1) * 100

        if change_pct >= 10 or change_pct <= -5:
            reason = "take_profit_10pct" if change_pct >= 10 else "stop_loss_5pct"
            _paper_trades.append({
                "token_address": address,
                "token_symbol": position.get("token_symbol", address[:8]),
                "side": "SELL",
                "amount_usd": round(value, 8),
                "price_usd": price,
                "source": "auto",
                "reason": reason,
                "timestamp": time.time(),
                "mode": "paper",
            })
            _realized_pnl_usd += pnl
            _open_positions.pop(address, None)
            closed += 1

    return {
        "updated": updated,
        "closed": closed,
        "price_errors": errors,
        "note": (
            "Paper prices updated" if updated
            else "No open positions" if not _open_positions
            else "Price provider unavailable or cooling down"
        ),
    }


def get_paper_status():
    positions = list(_open_positions.values())
    invested = sum(_num(p.get("invested_usd")) for p in positions)
    value = sum(
        _num(p.get("quantity")) * _num(p.get("current_price_usd"))
        for p in positions
    )
    unrealized = sum(_num(p.get("unrealized_pnl_usd")) for p in positions)
    return {
        "mode": "paper",
        "real_trading_enabled": False,
        "simulated_trade_count": len(_paper_trades),
        "open_positions": positions,
        "realized_pnl_usd": round(_realized_pnl_usd, 8),
        "take_profit_pct": 10.0,
        "stop_loss_pct": -5.0,
        "paper_invested_usd": round(invested, 8),
        "paper_current_value_usd": round(value, 8),
        "paper_unrealized_pnl_usd": round(unrealized, 8),
        "paper_realized_pnl_usd": round(_realized_pnl_usd, 8),
        "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 8),
        "note": "Paper simulation only. No wallet is connected and no real orders are sent.",
    }


def portfolio_status():
    status = get_paper_status()
    return {
        "paper_invested_usd": status["paper_invested_usd"],
        "paper_current_value_usd": status["paper_current_value_usd"],
        "paper_unrealized_pnl_usd": status["paper_unrealized_pnl_usd"],
        "paper_realized_pnl_usd": status["paper_realized_pnl_usd"],
        "paper_total_pnl_usd": status["paper_total_pnl_usd"],
    }


def paper_status():
    return get_paper_status()


def get_paper_trades():
    return list(_paper_trades)


def list_paper_trades():
    return get_paper_trades()
