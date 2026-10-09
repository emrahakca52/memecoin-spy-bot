import asyncio
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

DEX_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/"
GECKO_TOKEN_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/"
PRICE_TIMEOUT_SECONDS = 12
PRICE_CACHE_SECONDS = 45
PRICE_COOLDOWN_SECONDS = 60
MAX_PROVIDER_COOLDOWN_SECONDS = 300
MIN_HOLD_SECONDS = 30
# If a fresh quote differs from the last accepted quote by more than this,
# require another consistent quote before using it for exits.
MAX_SINGLE_QUOTE_MOVE_PCT = 35.0
CONFIRM_QUOTE_TOLERANCE_PCT = 15.0

_price_cache = {}
_price_last_request = {}
_price_lock = asyncio.Lock()
_provider_cooldown_until = 0.0
_provider_cooldown_reason = None
_open_positions = {}
_paper_trades = []
_realized_pnl_usd = 0.0
_pending_quotes = {}


def _num(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _set_provider_cooldown(response):
    global _provider_cooldown_until, _provider_cooldown_reason
    wait_seconds = PRICE_COOLDOWN_SECONDS
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            wait_seconds = max(1, int(float(retry_after)))
        except (TypeError, ValueError):
            try:
                retry_date = parsedate_to_datetime(retry_after)
                if retry_date.tzinfo is None:
                    retry_date = retry_date.replace(tzinfo=timezone.utc)
                wait_seconds = max(1, int((retry_date - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                wait_seconds = PRICE_COOLDOWN_SECONDS
    wait_seconds = min(wait_seconds, MAX_PROVIDER_COOLDOWN_SECONDS)
    _provider_cooldown_until = max(_provider_cooldown_until, time.monotonic() + wait_seconds)
    _provider_cooldown_reason = f"HTTP 429; pausing provider requests for {wait_seconds}s"


async def _get_json(client, url):
    response = await client.get(url)
    if response.status_code == 429:
        _set_provider_cooldown(response)
    response.raise_for_status()
    return response.json()


async def _fetch_prices_batch(client, token_addresses):
    addresses = [a for a in dict.fromkeys(token_addresses) if a]
    results = {}
    if not addresses:
        return results

    for offset in range(0, len(addresses), 30):
        chunk = addresses[offset:offset + 30]
        try:
            payload = await _get_json(client, DEX_TOKEN_URL + ",".join(chunk))
            for pair in payload.get("pairs") or []:
                if not isinstance(pair, dict) or (pair.get("chainId") or "").lower() != "solana":
                    continue
                # priceUsd belongs to the pair's BASE token, never its quote token.
                base = (pair.get("baseToken") or {}).get("address")
                price = _num(pair.get("priceUsd"))
                if base in chunk and price > 0:
                    liquidity = _num((pair.get("liquidity") or {}).get("usd"))
                    previous = results.get(base)
                    if previous is None or liquidity > previous[0]:
                        results[base] = (liquidity, price)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                return {}
        except (httpx.HTTPError, ValueError):
            pass

    missing = [a for a in addresses if a not in results]
    # One Gecko fallback only per batch to avoid amplifying provider throttling.
    if missing and time.monotonic() >= _provider_cooldown_until:
        address = missing[0]
        try:
            payload = await _get_json(client, GECKO_TOKEN_POOLS_URL + address + "/pools")
            candidates = []
            for pool in payload.get("data") or []:
                attrs = pool.get("attributes") or {}
                # Only use pools whose base token matches the requested address.
                relationships = pool.get("relationships") or {}
                base = ((relationships.get("base_token") or {}).get("data") or {}).get("id") or ""
                base_address = base[len("solana_"):] if base.startswith("solana_") else base
                if base_address != address:
                    continue
                price = _num(attrs.get("base_token_price_usd"))
                if price > 0:
                    candidates.append((_num(attrs.get("reserve_in_usd")), price))
            if candidates:
                candidates.sort(reverse=True)
                results[address] = candidates[0][1]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                _set_provider_cooldown(exc.response)
        except (httpx.HTTPError, ValueError):
            pass

    return {address: (value[1] if isinstance(value, tuple) else value)
            for address, value in results.items()}


async def get_token_prices_usd(token_addresses):
    addresses = [a for a in dict.fromkeys(token_addresses) if a]
    if not addresses:
        return {}
    now = time.monotonic()
    prices = {}
    missing = []
    for address in addresses:
        cached = _price_cache.get(address)
        if cached and now - cached["at"] < PRICE_CACHE_SECONDS:
            prices[address] = cached["price"]
        else:
            prices[address] = None
            missing.append(address)
    if not missing or now < _provider_cooldown_until:
        return prices

    async with _price_lock:
        now = time.monotonic()
        still_missing = []
        for address in missing:
            cached = _price_cache.get(address)
            if cached and now - cached["at"] < PRICE_CACHE_SECONDS:
                prices[address] = cached["price"]
            elif now - _price_last_request.get(address, 0.0) >= PRICE_COOLDOWN_SECONDS:
                still_missing.append(address)
        if not still_missing or now < _provider_cooldown_until:
            return prices
        for address in still_missing:
            _price_last_request[address] = now
        try:
            async with httpx.AsyncClient(
                timeout=PRICE_TIMEOUT_SECONDS,
                headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/1.5"},
            ) as client:
                fetched = await _fetch_prices_batch(client, still_missing)
        except (httpx.HTTPError, ValueError):
            fetched = {}
        for address in still_missing:
            price = _num(fetched.get(address))
            if price > 0:
                _price_cache[address] = {"at": time.monotonic(), "price": price}
                prices[address] = price
    return prices


async def get_token_price_usd(token_address):
    if not token_address:
        return None
    return (await get_token_prices_usd([token_address])).get(token_address)


def record_paper_trade(token_address, side, amount_usd, price_usd,
                       token_symbol="", source="manual", reason=None):
    global _realized_pnl_usd
    side = str(side).upper()
    amount_usd, price_usd = _num(amount_usd), _num(price_usd)
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not token_address or amount_usd <= 0 or price_usd <= 0:
        raise ValueError("token_address, amount_usd and price_usd must be valid")

    trade = {
        "token_address": token_address, "token_symbol": token_symbol or token_address[:8],
        "side": side, "amount_usd": round(amount_usd, 8), "price_usd": price_usd,
        "source": source, "reason": reason, "timestamp": time.time(), "mode": "paper",
    }
    _paper_trades.append(trade)
    if side == "BUY":
        quantity = amount_usd / price_usd
        position = _open_positions.get(token_address)
        if position is None:
            _open_positions[token_address] = {
                "token_address": token_address, "token_symbol": trade["token_symbol"],
                "entry_price_usd": price_usd, "current_price_usd": price_usd,
                "invested_usd": amount_usd, "quantity": quantity,
                "unrealized_pnl_usd": 0.0, "opened_at": time.time(),
            }
            _pending_quotes.pop(token_address, None)
        else:
            old_qty = _num(position.get("quantity"))
            total_qty = old_qty + quantity
            position["entry_price_usd"] = (
                _num(position.get("entry_price_usd")) * old_qty + price_usd * quantity
            ) / max(total_qty, 1e-12)
            position["quantity"] = total_qty
            position["invested_usd"] = _num(position.get("invested_usd")) + amount_usd
            position["current_price_usd"] = price_usd
    else:
        position = _open_positions.get(token_address)
        if position:
            qty = _num(position.get("quantity"))
            sell_qty = min(qty, amount_usd / price_usd)
            cost_per_unit = _num(position.get("invested_usd")) / max(qty, 1e-12)
            proceeds, cost = sell_qty * price_usd, sell_qty * cost_per_unit
            realized = proceeds - cost
            _realized_pnl_usd += realized
            trade["realized_pnl_usd"] = round(realized, 8)
            remaining_qty = qty - sell_qty
            if remaining_qty <= 1e-12:
                _open_positions.pop(token_address, None)
                _pending_quotes.pop(token_address, None)
            else:
                position["quantity"] = remaining_qty
                position["invested_usd"] = max(0.0, _num(position.get("invested_usd")) - cost)
    return {"ok": True, "mode": "paper", "trade": trade}


def open_paper_position(token_address, token_symbol="", amount_usd=10.0, price_usd=None,
                        source="auto", max_open_positions=None, metadata=None):
    if not token_address:
        raise ValueError("token_address is required")
    if price_usd is None or _num(price_usd) <= 0:
        raise ValueError("price_usd is required and must be positive")
    if token_address in _open_positions:
        return {"ok": True, "mode": "paper", "skipped": True, "reason": "position_already_open"}
    if max_open_positions is not None and len(_open_positions) >= int(max_open_positions):
        return {"ok": True, "mode": "paper", "skipped": True, "reason": "max_open_positions_reached"}
    result = record_paper_trade(token_address, "BUY", amount_usd, price_usd, token_symbol, source)
    position = _open_positions.get(token_address)
    if position is not None and metadata:
        position["metadata"] = dict(metadata)
    return result


async def update_paper_prices():
    global _realized_pnl_usd
    updated = closed = errors = rejected = 0
    addresses = list(_open_positions)
    if not addresses:
        return {"updated": 0, "closed": 0, "price_errors": 0, "rejected_quotes": 0, "note": "No open positions"}
    try:
        prices = await get_token_prices_usd(addresses)
    except Exception:
        prices = {}

    for address in addresses:
        position = _open_positions.get(address)
        if not position:
            continue
        price = prices.get(address)
        if price is None or price <= 0:
            errors += 1
            continue

        previous_price = _num(position.get("current_price_usd"))
        pending = _pending_quotes.get(address)
        if previous_price > 0:
            move_pct = abs(price / previous_price - 1) * 100
            if move_pct > MAX_SINGLE_QUOTE_MOVE_PCT:
                # First extreme quote is not trusted; require a second quote
                # close to it before accepting it.
                if pending is None or abs(price / pending["price"] - 1) * 100 > CONFIRM_QUOTE_TOLERANCE_PCT:
                    _pending_quotes[address] = {"price": price, "at": time.time()}
                    rejected += 1
                    continue
                _pending_quotes.pop(address, None)
        _pending_quotes.pop(address, None)
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
        opened_at = _num(position.get("opened_at"))
        if opened_at and time.time() - opened_at < MIN_HOLD_SECONDS:
            continue
        change_pct = (price / entry - 1) * 100
        if change_pct >= 10 or change_pct <= -5:
            reason = "take_profit_10pct" if change_pct >= 10 else "stop_loss_5pct"
            _paper_trades.append({
                "token_address": address, "token_symbol": position.get("token_symbol", address[:8]),
                "side": "SELL", "amount_usd": round(value, 8), "price_usd": price,
                "source": "auto", "reason": reason, "realized_pnl_usd": round(pnl, 8),
                "entry_price_usd": entry, "change_pct": round(change_pct, 4),
                "timestamp": time.time(), "mode": "paper",
            })
            _realized_pnl_usd += pnl
            _open_positions.pop(address, None)
            _pending_quotes.pop(address, None)
            closed += 1

    if not _open_positions:
        note = "No open positions"
    elif rejected:
        note = "Extreme price quote rejected pending confirmation"
    elif updated:
        note = "Paper prices updated" if not errors else "Some prices updated; some unavailable"
    elif time.monotonic() < _provider_cooldown_until:
        remaining = max(1, int(_provider_cooldown_until - time.monotonic()))
        note = f"Provider rate-limited; retry in about {remaining}s"
    else:
        note = "Price provider unavailable or cooling down"
    return {"updated": updated, "closed": closed, "price_errors": errors,
            "rejected_quotes": rejected, "note": note}


def get_paper_status():
    positions = list(_open_positions.values())
    invested = sum(_num(p.get("invested_usd")) for p in positions)
    value = sum(_num(p.get("quantity")) * _num(p.get("current_price_usd")) for p in positions)
    unrealized = sum(_num(p.get("unrealized_pnl_usd")) for p in positions)
    return {
        "mode": "paper", "real_trading_enabled": False,
        "simulated_trade_count": len(_paper_trades), "open_positions": positions,
        "realized_pnl_usd": round(_realized_pnl_usd, 8),
        "take_profit_pct": 10.0, "stop_loss_pct": -5.0,
        "paper_invested_usd": round(invested, 8), "paper_current_value_usd": round(value, 8),
        "paper_unrealized_pnl_usd": round(unrealized, 8),
        "paper_realized_pnl_usd": round(_realized_pnl_usd, 8),
        "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 8),
        "note": "Paper simulation only. No wallet is connected and no real orders are sent. "
                "Extreme quotes may be rejected; stale prices are possible.",
    }


def portfolio_status():
    status = get_paper_status()
    return {key: status[key] for key in (
        "paper_invested_usd", "paper_current_value_usd", "paper_unrealized_pnl_usd",
        "paper_realized_pnl_usd", "paper_total_pnl_usd"
    )}


def paper_status():
    return get_paper_status()


def get_paper_trades():
    return list(_paper_trades)


def list_paper_trades():
    return get_paper_trades()
