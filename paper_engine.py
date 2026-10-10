import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
import psycopg

logger = logging.getLogger("memecoin_spy.paper_engine")
BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
PRICE_TIMEOUT_SECONDS = float(os.getenv("PRICE_TIMEOUT_SECONDS", "12"))
PRICE_CACHE_SECONDS = max(5, int(os.getenv("PRICE_CACHE_SECONDS", "90")))
PRICE_COOLDOWN_SECONDS = max(1, int(os.getenv("PRICE_COOLDOWN_SECONDS", "60")))
MIN_HOLD_SECONDS = max(0, int(os.getenv("MIN_HOLD_SECONDS", "30")))
MAX_ACCEPTED_PRICE_JUMP_PCT = max(1.0, float(os.getenv("MAX_ACCEPTED_PRICE_JUMP_PCT", "35")))
BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS = max(1.1, float(os.getenv("BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS", "3")))
PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS = max(PRICE_CACHE_SECONDS, int(os.getenv("PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS", "180")))
PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT = max(1.0, float(os.getenv("PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT", "15")))
PRICE_JUMP_PENDING_MAX_AGE_SECONDS = max(30, int(os.getenv("PRICE_JUMP_PENDING_MAX_AGE_SECONDS", "600")))
TAKE_PROFIT_PCT = 10.0
STOP_LOSS_PCT = -5.0

_price_cache = {}
_price_last_request = {}
_pending_price_jumps = {}
_provider_cooldown_until = {}
_provider_cooldown_reason = {}
_open_positions = {}
_paper_trades = []
_realized_pnl_usd = 0.0
_price_lock = asyncio.Lock()
_birdeye_last_request_at = 0.0
_database_status = {"configured": bool(os.getenv("DATABASE_URL", "").strip()), "connected": False, "last_error": None, "last_saved_utc": None, "last_loaded_utc": None}
_price_diagnostics = {"birdeye": {"requests": 0, "last_attempt_utc": None, "last_endpoint": None, "last_http_status": None, "last_error": None, "last_prices_found": 0, "last_price_jump_warning": None, "cooldown_seconds_remaining": 0}}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    return "postgresql://" + url[len("postgres://"):] if url.startswith("postgres://") else url


def get_persistence_status():
    return dict(_database_status)


def _connect_database():
    url = _database_url()
    if not url:
        _database_status.update(configured=False, connected=False, last_error="DATABASE_URL is not configured")
        return None
    return psycopg.connect(url, connect_timeout=8, autocommit=True)


def _ensure_state_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS memecoin_paper_state (
        id SMALLINT PRIMARY KEY CHECK (id = 1), state JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def _state_snapshot():
    return {"open_positions": _open_positions, "paper_trades": _paper_trades,
            "realized_pnl_usd": _realized_pnl_usd, "saved_at_utc": _utc_now(), "mode": "paper"}


def _save_paper_state():
    if not _database_url():
        _database_status.update(configured=False, connected=False, last_error="DATABASE_URL is not configured")
        return False
    try:
        snapshot = json.dumps(_state_snapshot(), default=str, allow_nan=False)
        with _connect_database() as conn:
            _ensure_state_table(conn)
            conn.execute("""INSERT INTO memecoin_paper_state (id, state, updated_at)
                VALUES (1, %s::jsonb, NOW()) ON CONFLICT (id) DO UPDATE
                SET state = EXCLUDED.state, updated_at = NOW()""", (snapshot,))
        _database_status.update(configured=True, connected=True, last_error=None, last_saved_utc=_utc_now())
        return True
    except Exception as exc:
        _database_status.update(configured=True, connected=False, last_error=f"{type(exc).__name__}: {str(exc)[:240]}")
        logger.exception("Could not save paper state to database.")
        return False


def _num(value, default=0.0):
    try:
        result = float(value or 0)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def load_paper_state():
    global _open_positions, _paper_trades, _realized_pnl_usd
    if not _database_url():
        _database_status.update(configured=False, connected=False, last_error="DATABASE_URL is not configured")
        return {"ok": False, "loaded": False, "reason": "database_url_missing"}
    try:
        with _connect_database() as conn:
            _ensure_state_table(conn)
            row = conn.execute("SELECT state FROM memecoin_paper_state WHERE id = 1").fetchone()
        _database_status.update(configured=True, connected=True, last_error=None, last_loaded_utc=_utc_now())
        if not row:
            return {"ok": True, "loaded": False, "reason": "no_saved_state"}
        state = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if not isinstance(state, dict) or not isinstance(state.get("open_positions", {}), dict) or not isinstance(state.get("paper_trades", []), list):
            raise ValueError("Saved paper state has an invalid format")
        _open_positions = state.get("open_positions", {})
        _paper_trades = state.get("paper_trades", [])
        _realized_pnl_usd = _num(state.get("realized_pnl_usd"))
        logger.info("Loaded paper state: %s open positions, %s trades.", len(_open_positions), len(_paper_trades))
        return {"ok": True, "loaded": True, "open_positions": len(_open_positions), "trade_count": len(_paper_trades)}
    except Exception as exc:
        _database_status.update(configured=True, connected=False, last_error=f"{type(exc).__name__}: {str(exc)[:240]}")
        logger.exception("Could not load paper state from database.")
        raise


def _record_provider(provider, **updates):
    _price_diagnostics.setdefault(provider, {}).update(updates)


def get_price_diagnostics():
    result, now = {}, time.monotonic()
    for provider, details in _price_diagnostics.items():
        item = dict(details)
        item["cooldown_seconds_remaining"] = max(0, int(_provider_cooldown_until.get(provider, 0.0) - now))
        item["cooldown_reason"] = _provider_cooldown_reason.get(provider)
        result[provider] = item
    return result


def _provider_is_cooling(provider="birdeye"):
    return time.monotonic() < _provider_cooldown_until.get(provider, 0.0)


def _set_provider_cooldown(provider, seconds, reason):
    _provider_cooldown_until[provider] = max(_provider_cooldown_until.get(provider, 0.0), time.monotonic() + max(1, int(seconds)))
    _provider_cooldown_reason[provider] = reason


async def _respect_birdeye_rate_limit():
    global _birdeye_last_request_at
    wait = BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _birdeye_last_request_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _birdeye_last_request_at = time.monotonic()


async def _fetch_birdeye_price(client, address):
    api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
    if not api_key:
        _record_provider("birdeye", last_error="BIRDEYE_API_KEY is not configured")
        return None
    if _provider_is_cooling():
        remain = max(1, int(_provider_cooldown_until.get("birdeye", 0) - time.monotonic()))
        _record_provider("birdeye", last_error=f"provider cooldown ({remain}s)")
        return None
    await _respect_birdeye_rate_limit()
    _record_provider("birdeye", requests=int(_price_diagnostics["birdeye"].get("requests", 0)) + 1,
                     last_attempt_utc=_utc_now(), last_endpoint=BIRDEYE_PRICE_URL,
                     last_http_status=None, last_error=None, last_prices_found=0)
    try:
        response = await client.get(BIRDEYE_PRICE_URL, params={"address": address}, headers={
            "Accept": "application/json", "X-API-KEY": api_key, "x-chain": "solana"})
        _record_provider("birdeye", last_http_status=response.status_code)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            try:
                cooldown = min(900, max(60, int(float(retry_after))))
            except (TypeError, ValueError, OverflowError):
                cooldown = 300
            _set_provider_cooldown("birdeye", cooldown, f"HTTP 429; cooldown {cooldown}s")
            _record_provider("birdeye", last_error=f"HTTP 429; cooldown {cooldown}s")
            return None
        if response.status_code in (401, 403):
            _record_provider("birdeye", last_error=f"HTTP {response.status_code}; check API key and package access")
            return None
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        price = _num(data.get("value")) if isinstance(data, dict) else 0.0
        if price <= 0:
            _record_provider("birdeye", last_error="HTTP 200 but no usable positive data.value price")
            return None
        _record_provider("birdeye", last_prices_found=1, last_error=None)
        return price
    except httpx.HTTPStatusError as exc:
        _record_provider("birdeye", last_http_status=exc.response.status_code, last_error=f"HTTP {exc.response.status_code}")
    except httpx.TimeoutException:
        _record_provider("birdeye", last_error="request timed out")
    except httpx.HTTPError as exc:
        _record_provider("birdeye", last_error=f"network error: {type(exc).__name__}")
    except (ValueError, AttributeError, TypeError) as exc:
        _record_provider("birdeye", last_error=f"invalid response: {type(exc).__name__}")
    return None


async def _fetch_prices_batch(client, token_addresses):
    results = {}
    for address in token_addresses:
        results[address] = await _fetch_birdeye_price(client, address)
        if _provider_is_cooling():
            break
    return results


def _validate_price_jump(address, price, now):
    """Require two nearby quotes before accepting a large move from a recent quote."""
    previous = _price_cache.get(address)
    if not previous:
        _pending_price_jumps.pop(address, None)
        return True
    old_price = _num(previous.get("price"))
    age = max(0.0, now - _num(previous.get("at")))
    if old_price <= 0 or age > PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS:
        _pending_price_jumps.pop(address, None)
        return True
    jump = abs(price / old_price - 1.0) * 100.0
    if jump <= MAX_ACCEPTED_PRICE_JUMP_PCT:
        _pending_price_jumps.pop(address, None)
        return True
    pending = _pending_price_jumps.get(address)
    if pending and now - _num(pending.get("at")) <= PRICE_JUMP_PENDING_MAX_AGE_SECONDS:
        pending_price = _num(pending.get("price"))
        if pending_price > 0 and abs(price / pending_price - 1.0) * 100.0 <= PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT:
            _pending_price_jumps.pop(address, None)
            msg = f"{address[:10]} large move corroborated by consecutive quotes ({jump:.2f}%)"
            _record_provider("birdeye", last_price_jump_warning=msg)
            logger.warning(msg)
            return True
    _pending_price_jumps[address] = {"price": price, "at": now}
    msg = f"{address[:10]} quote moved {jump:.2f}%; awaiting confirming quote; ignored this cycle"
    _record_provider("birdeye", last_price_jump_warning=msg)
    logger.warning(msg)
    return False


async def get_token_prices_usd(token_addresses):
    """Return cached/fetched prices; one unconfirmed large jump is withheld from trading."""
    addresses = list(dict.fromkeys(str(a) for a in token_addresses if a))
    if not addresses:
        return {}
    now = time.monotonic()
    prices, missing = {}, []
    for address in addresses:
        cached = _price_cache.get(address)
        if cached and now - cached["at"] < PRICE_CACHE_SECONDS:
            prices[address] = cached["price"]
        else:
            prices[address] = None
            missing.append(address)
    if not missing:
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
        if not still_missing:
            return prices
        try:
            async with httpx.AsyncClient(timeout=PRICE_TIMEOUT_SECONDS, headers={
                "Accept": "application/json", "User-Agent": "MemecoinSpyBot/2.3"}) as client:
                fetched = await _fetch_prices_batch(client, still_missing)
        except (httpx.HTTPError, ValueError, RuntimeError):
            fetched = {}
        for address in still_missing:
            if address in fetched:
                _price_last_request[address] = time.monotonic()
            price = _num(fetched.get(address))
            if price <= 0:
                continue
            accepted_at = time.monotonic()
            if not _validate_price_jump(address, price, accepted_at):
                prices[address] = None
                continue
            _price_cache[address] = {"at": accepted_at, "price": price}
            prices[address] = price
    return prices


async def get_token_price_usd(token_address):
    if not token_address:
        return None
    return (await get_token_prices_usd([token_address])).get(token_address)


def record_paper_trade(token_address, side, amount_usd, price_usd, token_symbol="", source="manual", reason=None):
    global _realized_pnl_usd
    side = str(side).upper()
    amount_usd, price_usd = _num(amount_usd), _num(price_usd)
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not token_address or amount_usd <= 0 or price_usd <= 0:
        raise ValueError("token_address, amount_usd and price_usd must be valid")
    trade = {"token_address": token_address, "token_symbol": token_symbol or token_address[:8],
             "side": side, "amount_usd": round(amount_usd, 8), "price_usd": price_usd,
             "source": source, "reason": reason, "timestamp": time.time(), "mode": "paper"}
    if side == "BUY":
        quantity = amount_usd / price_usd
        position = _open_positions.get(token_address)
        _paper_trades.append(trade)
        if position is None:
            _open_positions[token_address] = {"token_address": token_address, "token_symbol": trade["token_symbol"],
                "entry_price_usd": price_usd, "current_price_usd": price_usd, "invested_usd": amount_usd,
                "quantity": quantity, "unrealized_pnl_usd": 0.0, "opened_at": time.time()}
        else:
            old_qty, old_invested = _num(position.get("quantity")), _num(position.get("invested_usd"))
            total_qty = old_qty + quantity
            position["entry_price_usd"] = (_num(position.get("entry_price_usd")) * old_qty + price_usd * quantity) / total_qty
            position["quantity"] = total_qty
            position["invested_usd"] = old_invested + amount_usd
            position["current_price_usd"] = price_usd
            position["unrealized_pnl_usd"] = total_qty * price_usd - old_invested - amount_usd
    else:
        position = _open_positions.get(token_address)
        if not position:
            raise ValueError("cannot sell: no open paper position for this token")
        qty, invested = _num(position.get("quantity")), _num(position.get("invested_usd"))
        if qty <= 0:
            raise ValueError("cannot sell: position quantity is invalid")
        sell_qty = min(qty, amount_usd / price_usd)
        cost = invested * sell_qty / qty
        proceeds = sell_qty * price_usd
        realized = proceeds - cost
        remaining_qty, remaining_invested = max(0.0, qty - sell_qty), max(0.0, invested - cost)
        trade["amount_usd"] = round(proceeds, 8)
        trade["realized_pnl_usd"] = round(realized, 8)
        _paper_trades.append(trade)
        _realized_pnl_usd += realized
        if remaining_qty <= max(1e-12, qty * 1e-10):
            _open_positions.pop(token_address, None)
        else:
            position["quantity"] = remaining_qty
            position["invested_usd"] = remaining_invested
            position["unrealized_pnl_usd"] = remaining_qty * _num(position.get("current_price_usd")) - remaining_invested
    _save_paper_state()
    return {"ok": True, "mode": "paper", "trade": trade}


def open_paper_position(token_address, token_symbol="", amount_usd=10.0, price_usd=None, source="auto", max_open_positions=None, metadata=None):
    if not token_address:
        raise ValueError("token_address is required")
    if price_usd is None or _num(price_usd) <= 0:
        raise ValueError("price_usd is required and must be positive")
    if _num(amount_usd) <= 0:
        raise ValueError("amount_usd must be positive")
    if token_address in _open_positions:
        return {"ok": True, "mode": "paper", "skipped": True, "reason": "position_already_open"}
    if max_open_positions is not None and len(_open_positions) >= int(max_open_positions):
        return {"ok": True, "mode": "paper", "skipped": True, "reason": "max_open_positions_reached"}
    result = record_paper_trade(token_address, "BUY", amount_usd, price_usd, token_symbol, source)
    position = _open_positions.get(token_address)
    if position is not None and metadata:
        position["metadata"] = dict(metadata)
        _save_paper_state()
    return result


def _close_position(address, position, price, reason, observed_change_pct=None):
    global _realized_pnl_usd
    qty, invested, entry = _num(position.get("quantity")), _num(position.get("invested_usd")), _num(position.get("entry_price_usd"))
    if qty <= 0 or invested < 0 or price <= 0 or entry <= 0:
        logger.error("Refusing to close invalid paper position for %s", address)
        return False
    value, pnl = qty * price, qty * price - invested
    trigger = entry * (1 + STOP_LOSS_PCT / 100) if reason == "stop_loss_5pct" else entry * (1 + TAKE_PROFIT_PCT / 100) if reason == "take_profit_10pct" else None
    change = (price / entry - 1) * 100
    _paper_trades.append({"token_address": address, "token_symbol": position.get("token_symbol", address[:8]),
        "side": "SELL", "amount_usd": round(value, 8), "price_usd": price, "source": "auto", "reason": reason,
        "realized_pnl_usd": round(pnl, 8), "entry_price_usd": entry, "change_pct": round(change, 4),
        "trigger_price_usd": trigger, "stop_loss_threshold_pct": STOP_LOSS_PCT,
        "take_profit_threshold_pct": TAKE_PROFIT_PCT,
        "observed_change_pct": round(change if observed_change_pct is None else observed_change_pct, 4),
        "quantity_sold": qty, "cost_basis_usd": round(invested, 8), "timestamp": time.time(), "mode": "paper"})
    _realized_pnl_usd += pnl
    _open_positions.pop(address, None)
    _save_paper_state()
    return True


async def update_paper_prices():
    updated = closed = errors = 0
    addresses = list(_open_positions)
    if not addresses:
        return {"updated": 0, "closed": 0, "price_errors": 0, "note": "No open positions"}
    prices = await get_token_prices_usd(addresses)
    for address in addresses:
        position = _open_positions.get(address)
        if not position:
            continue
        price = prices.get(address)
        if price is None or price <= 0:
            errors += 1
            continue
        position["current_price_usd"] = price
        qty, invested = _num(position.get("quantity")), _num(position.get("invested_usd"))
        position["unrealized_pnl_usd"] = round(qty * price - invested, 8)
        position["last_price_update_utc"] = _utc_now()
        updated += 1
        entry, opened = _num(position.get("entry_price_usd")), _num(position.get("opened_at"))
        if entry <= 0 or (opened and time.time() - opened < MIN_HOLD_SECONDS):
            continue
        change = (price / entry - 1) * 100
        if change >= TAKE_PROFIT_PCT:
            closed += int(_close_position(address, position, price, "take_profit_10pct", change))
        elif change <= STOP_LOSS_PCT:
            closed += int(_close_position(address, position, price, "stop_loss_5pct", change))
    _save_paper_state()
    if not _open_positions:
        note = "No open positions"
    elif updated:
        note = "Some prices updated; some unavailable" if errors else "Paper prices updated"
    elif _provider_is_cooling():
        note = "Birdeye rate-limited; waiting before retry"
    else:
        note = "Price unavailable or awaiting large-jump confirmation"
    return {"updated": updated, "closed": closed, "price_errors": errors, "note": note}


def get_paper_status():
    positions = list(_open_positions.values())
    invested = sum(_num(p.get("invested_usd")) for p in positions)
    value = sum(_num(p.get("quantity")) * _num(p.get("current_price_usd")) for p in positions)
    unrealized = sum(_num(p.get("unrealized_pnl_usd")) for p in positions)
    return {"mode": "paper", "real_trading_enabled": False, "simulated_trade_count": len(_paper_trades),
        "open_positions": positions, "realized_pnl_usd": round(_realized_pnl_usd, 8),
        "take_profit_pct": TAKE_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
        "paper_invested_usd": round(invested, 8), "paper_current_value_usd": round(value, 8),
        "paper_unrealized_pnl_usd": round(unrealized, 8), "paper_realized_pnl_usd": round(_realized_pnl_usd, 8),
        "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 8), "persistence": get_persistence_status(),
        "note": "Paper only; no real orders. One unusually large quote is ignored until a second quote corroborates it. This can delay a genuine stop-loss; a single provider cannot fully distinguish bad data from a real crash."}


def portfolio_status():
    status = get_paper_status()
    return {key: status[key] for key in ("paper_invested_usd", "paper_current_value_usd", "paper_unrealized_pnl_usd", "paper_realized_pnl_usd", "paper_total_pnl_usd")}


def paper_status():
    return get_paper_status()


def get_paper_trades():
    return list(_paper_trades)


def list_paper_trades():
    return get_paper_trades()
