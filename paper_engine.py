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

# In-memory paper ledger. It resets when the service restarts.
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
    # Prefer DexScreener for prices because it is already the fallback signal source.
    try:
        payload = await _get_json(client, DEX_TOKEN_URL + token_address)
        pairs = payload.get("pairs") or []
        sol_pairs = [
            pair for pair in pairs
            if isinstance(pair, dict)
            and (pair.get("chainId") or "").lower() == "solana"
            and _num(pair.get("priceUsd")) > 0
        ]
        if sol_pairs:
            sol_pairs.sort(
                key=lambda pair: _num((pair.get("liquidity") or {}).get("usd")),
                reverse=True,
            )
            return _num(sol_pairs[0].get("priceUsd")), "DexScreener"
    except (httpx.HTTPError, ValueError):
        pass

    # Fallback to GeckoTerminal's token pools endpoint.
    try:
        payload = await _get_json(
            client,
            GECKO_TOKEN_POOLS_URL + token_address + "/pools",
        )
        pools = payload.get("data") or []
        candidates = []
        for pool in pools:
            attrs = pool.get("attributes") or {}
            price = _num(attrs.get("base_token_price_usd"))
            if price > 0:
                candidates.append(
                    (_num(attrs.get("reserve_in_usd")), price)
                )
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1], "GeckoTerminal"
    except (httpx.HTTPError, ValueError):
        pass

    return None, "unavailable"


async def get_token_price_usd(token_address):
    if not token_address:
        return None

    now = time.monotonic()
    cached = _price_cache.get(token_address)
    if cached and now - cached["at"] < PRICE_CACHE_SECONDS:
        return cached["price"]

    # Keep repeated requests for the same token from being sent too quickly.
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
            price, provider = await _fetch_price(client, token_address)

        if price is not None and price > 0:
            _price_cache[token_address] = {
                "at": time.monotonic(),
                "price": price,
                "provider": provider,
            }
            return price

    return None


def record_paper_trade(
    token_address,
    token_symbol,
    side,
    amount_usd,
    price_usd,
    reason=None,
):
    global _realized_pnl_usd

    amount_usd = _num(amount_usd)
    price_usd = _num(price_usd)

    trade = {
        "token_address": token_address,
        "token_symbol": token_symbol,
        "side": side,
        "amount_usd": round(amount_usd, 8),
        "price_usd": price_usd,
        "reason": reason,
        "timestamp": time.time(),
        "mode": "paper",
    }
    _paper_trades.append(trade)

    if side == "BUY" and price_usd > 0:
        token = token_address
        position = _open_positions.get(token)
        if position is None:
            _open_positions[token] = {
                "token_address": token_address,
                "token_symbol": token_symbol,
                "entry_price_usd": price_usd,
                "current_price_usd": price_usd,
                "invested_usd": amount_usd,
                "quantity": amount_usd / price_usd,
                "unrealized_pnl_usd": 0.0,
                "opened_at": time.time(),
            }
        else:
            old_quantity = _num(position.get("quantity"))
            new_quantity = amount_usd / price_usd
            total_quantity = old_quantity + new_quantity
            if total_quantity > 0:
                position["entry_price_usd"] = (
                    _num(position.get("entry_price_usd")) * old_quantity
                    + price_usd * new_quantity
                ) / total_quantity
            position["quantity"] = total_quantity
            position["invested_usd"] = _num(position.get("invested_usd")) + amount_usd
            position["current_price_usd"] = price_usd

    elif side == "SELL" and price_usd > 0:
        position = _open_positions.get(token_address)
        if position:
            quantity = _num(position.get("quantity"))
            proceeds = min(amount_usd, quantity * price_usd)
            cost_basis = _num(position.get("invested_usd")) * (
                proceeds / max(quantity * price_usd, 1e-12)
            )
            pnl = proceeds - cost_basis
            _realized_pnl_usd += pnl
            _open_positions.pop(token_address, None)


async def update_paper_prices():
    global _realized_pnl_usd

    updated = 0
    closed = 0
    errors = 0

    for token_address in list(_open_positions.keys()):
        position = _open_positions.get(token_address)
        if not position:
            continue

        price = await get_token_price_usd(token_address)
        if price is None or price <= 0:
            errors += 1
            continue

        position = _open_positions.get(token_address)
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

        change_pct = (price / entry - 1.0) * 100.0

        # These are experimental paper rules, not validated trading signals.
        if change_pct >= 10.0 or change_pct <= -5.0:
            symbol = position.get("token_symbol") or token_address[:8]
            reason = "take_profit_10pct" if change_pct >= 10.0 else "stop_loss_5pct"
            _paper_trades.append({
                "token_address": token_address,
                "token_symbol": symbol,
                "side": "SELL",
                "amount_usd": round(value, 8),
                "price_usd": price,
                "reason": reason,
                "timestamp": time.time(),
                "mode": "paper",
            })
            _realized_pnl_usd += pnl
            _open_positions.pop(token_address, None)
            closed += 1

    return {
        "updated": updated,
        "closed": closed,
        "price_errors": errors,
        "note": (
            "Paper prices updated"
            if updated
            else "No open positions" if not _open_positions
            else "Price provider unavailable or cooling down"
        ),
    }


def get_paper_status():
    positions = list(_open_positions.values())
    invested = sum(_num(p.get("invested_usd")) for p in positions)
    current_value = sum(
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
        "paper_current_value_usd": round(current_value, 8),
        "paper_unrealized_pnl_usd": round(unrealized, 8),
        "paper_realized_pnl_usd": round(_realized_pnl_usd, 8),
        "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 8),
        "note": (
            "Paper simulation only. No wallet is connected and no real "
            "orders are sent. Data resets when the service restarts."
        ),
    }


def get_paper_trades():
    return list(_paper_trades)
