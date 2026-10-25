"""
Memecoin Spy Pro — paper_engine.py
Hardened paper-only simulation engine.

Important:
- Keeps historical trade records; does not delete or rewrite old trade rows.
- Excludes the known impossible MRBEAST SELL from reported realized P/L.
- Prevents duplicate open positions for the same token address.
- Validates unusually large quote jumps and flags extreme exits for review.
- Uses PostgreSQL persistence when DATABASE_URL is configured.
- No real orders are sent.
"""
import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import httpx
import psycopg

logger = logging.getLogger("memecoin_spy.paper_engine")

BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
PRICE_TIMEOUT_SECONDS = float(os.getenv("PRICE_TIMEOUT_SECONDS", "12"))
PRICE_CACHE_SECONDS = max(5, int(os.getenv("PRICE_CACHE_SECONDS", "90")))
PRICE_COOLDOWN_SECONDS = max(1, int(os.getenv("PRICE_COOLDOWN_SECONDS", "120")))
MIN_HOLD_SECONDS = max(0, int(os.getenv("MIN_HOLD_SECONDS", "30")))
MAX_ACCEPTED_PRICE_JUMP_PCT = max(1.0, float(os.getenv("MAX_ACCEPTED_PRICE_JUMP_PCT", "35")))
BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS = max(1.1, float(os.getenv("BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS", "5")))
PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS = max(PRICE_CACHE_SECONDS, int(os.getenv("PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS", "180")))
PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT = max(1.0, float(os.getenv("PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT", "15")))
PRICE_JUMP_PENDING_MAX_AGE_SECONDS = max(30, int(os.getenv("PRICE_JUMP_PENDING_MAX_AGE_SECONDS", "600")))

TAKE_PROFIT_PCT = 10.0
STOP_LOSS_PCT = -5.0
MAX_STOP_LOSS_OVERSHOOT_PCT = max(10.0, float(os.getenv("MAX_STOP_LOSS_OVERSHOOT_PCT", "35")))
MAX_TAKE_PROFIT_OVERSHOOT_PCT = max(20.0, float(os.getenv("MAX_TAKE_PROFIT_OVERSHOOT_PCT", "100")))

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

_database_status = {
    "configured": bool(os.getenv("DATABASE_URL", "").strip()),
    "connected": False,
    "last_error": None,
    "last_saved_utc": None,
    "last_loaded_utc": None,
    "accounting_warning": None,
}
_price_diagnostics = {
    "birdeye": {
        "requests": 0, "last_attempt_utc": None, "last_endpoint": None,
        "last_http_status": None, "last_error": None, "last_prices_found": 0,
        "last_price_jump_warning": None, "cooldown_seconds_remaining": 0,
    }
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


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
        id SMALLINT PRIMARY KEY CHECK (id = 1),
        state JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")


def _state_snapshot():
    return {
        "open_positions": _open_positions,
        "paper_trades": _paper_trades,
        "realized_pnl_usd": _realized_pnl_usd,
        "saved_at_utc": _utc_now(),
        "mode": "paper",
    }


def _save_paper_state():
    """Persist state; never expose DATABASE_URL."""
    if not _database_url():
        _database_status.update(configured=False, connected=False, last_error="DATABASE_URL is not configured")
        return False
    try:
        snapshot = json.dumps(_state_snapshot(), default=str, allow_nan=False)
        with _connect_database() as conn:
            _ensure_state_table(conn)
            conn.execute(
                """INSERT INTO memecoin_paper_state (id, state, updated_at)
                   VALUES (1, %s::jsonb, NOW())
                   ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()""",
                (snapshot,),
            )
        _database_status.update(configured=True, connected=True, last_error=None, last_saved_utc=_utc_now())
        return True
    except Exception as exc:
        _database_status.update(configured=True, connected=False, last_error=f"{type(exc).__name__}: {str(exc)[:240]}")
        logger.exception("Could not save paper state to database.")
        return False


def _num(value, default=0.0):
    try:
        result = float(value or 0)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _accounting_diagnostics(trades, stored_realized):
    sells = [t for t in trades if isinstance(t, dict) and str(t.get("side", "")).upper() == "SELL"]
    values = [_num(t.get("realized_pnl_usd"), default=float("nan")) for t in sells if t.get("realized_pnl_usd") is not None]
    values = [v for v in values if math.isfinite(v)]
    sell_sum = sum(values)
    missing = len(sells) - len(values)
    difference = stored_realized - sell_sum
    warning = None
    if missing:
        warning = f"{missing} SELL record(s) have no realized_pnl_usd; full ledger reconciliation is not possible."
    elif abs(difference) > 0.02:
        warning = f"Stored realized P/L differs from recorded SELL P/L by {difference:.8f} USD. Stored value was preserved."
    return {
        "sell_record_count": len(sells), "sell_records_with_pnl": len(values),
        "sell_records_missing_pnl": missing, "sum_recorded_sell_pnl_usd": round(sell_sum, 8),
        "stored_realized_pnl_usd": round(stored_realized, 8), "difference_usd": round(difference, 8),
        "warning": warning,
    }


def _is_quarantined_trade(trade):
    if not isinstance(trade, dict):
        return False
    if trade.get("accounting_quarantined") is True:
        return True
    return (
        str(trade.get("side", "")).upper() == "SELL"
        and str(trade.get("token_symbol", "")).upper() == "MRBEAST"
        and str(trade.get("token_address", "")) == "4hqN5vhCG1q13qYUQvBhyMDDnpWKP98DoswDeDCVQaCn"
        and _num(trade.get("price_usd")) > 100.0
        and _num(trade.get("realized_pnl_usd")) > 1_000_000.0
    )


def _reported_realized_pnl(trades):
    total, included, missing, quarantined = 0.0, 0, 0, []
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict) or str(trade.get("side", "")).upper() != "SELL":
            continue
        if _is_quarantined_trade(trade):
            quarantined.append({
                "index": index, "token_symbol": trade.get("token_symbol"),
                "token_address": trade.get("token_address"),
                "reason": "implausible_historical_price_and_pnl",
                "realized_pnl_usd_excluded": _num(trade.get("realized_pnl_usd")),
            })
            continue
        if trade.get("realized_pnl_usd") is None:
            missing += 1
            continue
        value = _num(trade.get("realized_pnl_usd"), default=float("nan"))
        if math.isfinite(value):
            total += value
            included += 1
        else:
            missing += 1
    return {
        "realized_pnl_usd": round(total, 8), "included_sell_records": included,
        "quarantined_sell_records": quarantined, "sell_records_missing_pnl": missing,
        "note": "Computed from recorded SELL P/L fields; legacy records may be incomplete.",
    }


def load_paper_state():
    """Load persisted state; quarantine known bad accounting rows without deleting them."""
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
        if not isinstance(state, dict):
            raise ValueError("Saved paper state has an invalid format")
        positions, trades = state.get("open_positions", {}), state.get("paper_trades", [])
        if not isinstance(positions, dict) or not isinstance(trades, list):
            raise ValueError("Saved positions or trades have an invalid format")
        for address, position in positions.items():
            if not isinstance(position, dict):
                raise ValueError(f"Invalid position record for {str(address)[:12]}")
            if _num(position.get("quantity")) < 0 or _num(position.get("invested_usd")) < 0:
                raise ValueError(f"Invalid quantity/cost for {str(address)[:12]}")
        stored_realized = _num(state.get("realized_pnl_usd"))
        diagnostics = _accounting_diagnostics(trades, stored_realized)
        reconciled = _reported_realized_pnl(trades)
        diagnostics["historical_stored_value"] = stored_realized
        diagnostics["historical_stored_value_mismatch"] = diagnostics.get("warning")
        diagnostics["warning"] = None
        for trade in trades:
            if isinstance(trade, dict) and _is_quarantined_trade(trade):
                trade["accounting_quarantined"] = True
                trade["accounting_quarantine_reason"] = "Implausible historical quote; excluded from reported paper P/L pending review"
        _open_positions, _paper_trades = positions, trades
        _realized_pnl_usd = reconciled["realized_pnl_usd"]
        warning_parts = []
        if reconciled["quarantined_sell_records"]:
            warning_parts.append(f"{len(reconciled['quarantined_sell_records'])} SELL record(s) quarantined from reported P/L.")
        _database_status["accounting_warning"] = " ".join(warning_parts) or None
        _save_paper_state()
        logger.info("Loaded paper state: %s positions, %s trades.", len(_open_positions), len(_paper_trades))
        return {
            "ok": True, "loaded": True, "open_positions": len(_open_positions),
            "trade_count": len(_paper_trades),
            "accounting": {**diagnostics, "reported_realized_pnl": reconciled,
                           "stored_realized_pnl_preserved_in_source_state": stored_realized},
        }
    except Exception as exc:
        _database_status.update(configured=True, connected=False, last_error=f"{type(exc).__name__}: {str(exc)[:240]}")
        logger.exception("Could not load paper state from database.")
        raise


def _record_provider(provider, **updates):
    _price_diagnostics.setdefault(provider, {}).update(updates)


def get_price_diagnostics():
    now = time.monotonic()
    result = {}
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
        response = await client.get(BIRDEYE_PRICE_URL, params={"address": address},
                                    headers={"Accept": "application/json", "X-API-KEY": api_key, "x-chain": "solana"})
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


def _validate_price_jump(address, price, now, reference_price=None):
    previous = _price_cache.get(address)
    old_price = _num(previous.get("price")) if previous else 0.0
    age = max(0.0, now - _num(previous.get("at"))) if previous else float("inf")
    if old_price <= 0 or age > PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS:
        old_price = _num(reference_price)
    if old_price <= 0:
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
            message = f"{address[:10]} large move corroborated by consecutive quotes ({jump:.2f}%)"
            _record_provider("birdeye", last_price_jump_warning=message)
            logger.warning(message)
            return True
    _pending_price_jumps[address] = {"price": price, "at": now}
    message = f"{address[:10]} quote moved {jump:.2f}%; awaiting confirming quote; ignored this cycle"
    _record_provider("birdeye", last_price_jump_warning=message)
    logger.warning(message)
    return False


async def get_token_prices_usd(token_addresses, reference_prices=None):
    """Return cached/fetched prices, withholding unconfirmed large jumps."""
    addresses = list(dict.fromkeys(str(a) for a in token_addresses if a))
    if not addresses:
        return {}
    reference_prices = reference_prices or {}
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
            async with httpx.AsyncClient(timeout=PRICE_TIMEOUT_SECONDS,
                                         headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/2.3"}) as client:
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
            if not _validate_price_jump(address, price, accepted_at, reference_prices.get(address)):
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
    """Record a paper trade; prevent adding a second BUY to an open position."""
    global _realized_pnl_usd
    side = str(side).upper()
    amount_usd, price_usd = _num(amount_usd), _num(price_usd)
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not token_address or amount_usd <= 0 or price_usd <= 0:
        raise ValueError("token_address, amount_usd and price_usd must be valid")
    if not math.isfinite(amount_usd) or not math.isfinite(price_usd):
        raise ValueError("amount_usd and price_usd must be finite")
    trade = {
        "token_address": token_address, "token_symbol": token_symbol or token_address[:8],
        "side": side, "amount_usd": round(amount_usd, 8), "price_usd": price_usd,
        "source": source, "reason": reason, "timestamp": time.time(), "mode": "paper",
    }
    if side == "BUY":
        if token_address in _open_positions:
            raise ValueError("position already open for this token")
        quantity = amount_usd / price_usd
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("calculated buy quantity is invalid")
        _paper_trades.append(trade)
        _open_positions[token_address] = {
            "token_address": token_address, "token_symbol": trade["token_symbol"],
            "entry_price_usd": price_usd, "current_price_usd": price_usd,
            "invested_usd": amount_usd, "quantity": quantity,
            "unrealized_pnl_usd": 0.0, "opened_at": time.time(),
        }
    else:
        position = _open_positions.get(token_address)
        if not position:
            raise ValueError("cannot sell: no open paper position for this token")
        qty, invested = _num(position.get("quantity")), _num(position.get("invested_usd"))
        if qty <= 0 or invested < 0:
            raise ValueError("existing position has invalid quantity/cost")
        sell_qty = min(qty, amount_usd / price_usd)
        if sell_qty <= 0 or not math.isfinite(sell_qty):
            raise ValueError("calculated sell quantity is invalid")
        cost = invested * sell_qty / qty
        proceeds = sell_qty * price_usd
        realized = proceeds - cost
        remaining_qty, remaining_invested = max(0.0, qty - sell_qty), max(0.0, invested - cost)
        trade["amount_usd"] = round(proceeds, 8)
        trade["quantity_sold"] = sell_qty
        trade["cost_basis_usd"] = round(cost, 8)
        trade["realized_pnl_usd"] = round(realized, 8)
        _paper_trades.append(trade)
        _realized_pnl_usd += realized
        if remaining_qty <= max(1e-12, qty * 1e-10):
            _open_positions.pop(token_address, None)
        else:
            position["quantity"], position["invested_usd"] = remaining_qty, remaining_invested
            position["unrealized_pnl_usd"] = remaining_qty * _num(position.get("current_price_usd")) - remaining_invested
    _save_paper_state()
    return {"ok": True, "mode": "paper", "trade": trade}


def open_paper_position(token_address, token_symbol="", amount_usd=10.0, price_usd=None, source="auto", max_open_positions=None, metadata=None):
    if not token_address:
        raise ValueError("token_address is required")
    if price_usd is None or _num(price_usd) <= 0 or _num(amount_usd) <= 0:
        raise ValueError("positive price_usd and amount_usd are required")
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
    if not math.isfinite(value) or not math.isfinite(pnl):
        logger.error("Refusing to close non-finite paper position for %s", address)
        return False
    trigger = entry * (1 + STOP_LOSS_PCT / 100) if reason == "stop_loss_5pct" else entry * (1 + TAKE_PROFIT_PCT / 100) if reason == "take_profit_10pct" else None
    change = (price / entry - 1) * 100
    trade = {
        "token_address": address, "token_symbol": position.get("token_symbol", address[:8]),
        "side": "SELL", "amount_usd": round(value, 8), "price_usd": price,
        "source": "auto", "reason": reason, "realized_pnl_usd": round(pnl, 8),
        "entry_price_usd": entry, "change_pct": round(change, 4),
        "trigger_price_usd": trigger, "stop_loss_threshold_pct": STOP_LOSS_PCT,
        "take_profit_threshold_pct": TAKE_PROFIT_PCT,
        "observed_change_pct": round(change if observed_change_pct is None else observed_change_pct, 4),
        "quantity_sold": qty, "cost_basis_usd": round(invested, 8),
        "timestamp": time.time(), "mode": "paper",
    }
    _paper_trades.append(trade)
    _realized_pnl_usd += pnl
    _open_positions.pop(address, None)
    _save_paper_state()
    return True


async def update_paper_prices():
    updated = closed = errors = 0
    addresses = list(_open_positions)
    if not addresses:
        return {"updated": 0, "closed": 0, "price_errors": 0, "note": "No open positions"}
    references = {a: _num(p.get("current_price_usd")) or _num(p.get("entry_price_usd")) for a, p in _open_positions.items()}
    prices = await get_token_prices_usd(addresses, references)
    for address in addresses:
        position = _open_positions.get(address)
        if not position:
            continue
        price = prices.get(address)
        if price is None or price <= 0:
            errors += 1
            continue
        entry = _num(position.get("entry_price_usd"))
        qty, invested = _num(position.get("quantity")), _num(position.get("invested_usd"))
        if entry <= 0 or qty <= 0 or invested < 0:
            errors += 1
            message = f"{address[:10]} invalid position values; automatic close skipped"
            position["price_safety_warning"] = message
            _record_provider("birdeye", last_price_jump_warning=message)
            continue

        # Latest accepted quote is shown as a mark-to-market value, but is not
        # claimed to be independently verified or necessarily executable.
        position["current_price_usd"] = price
        position["unrealized_pnl_usd"] = round(qty * price - invested, 8)
        position["last_price_update_utc"] = _utc_now()
        updated += 1

        opened = _num(position.get("opened_at"))
        if opened and time.time() - opened < MIN_HOLD_SECONDS:
            continue
        change = (price / entry - 1.0) * 100.0

        if change <= STOP_LOSS_PCT:
            overshoot = STOP_LOSS_PCT - change
            if overshoot > MAX_STOP_LOSS_OVERSHOOT_PCT:
                errors += 1
                message = (
                    f"{position.get('token_symbol', address[:8])} quote implies {change:.2f}% from entry; "
                    "extreme stop-loss quote requires review; position remains open"
                )
                position["price_safety_warning"] = message
                _record_provider("birdeye", last_price_jump_warning=message)
                logger.error(message)
                continue
            position.pop("price_safety_warning", None)
            closed += int(_close_position(address, position, price, "stop_loss_5pct", change))
            continue

        if change >= TAKE_PROFIT_PCT:
            overshoot = change - TAKE_PROFIT_PCT
            if overshoot > MAX_TAKE_PROFIT_OVERSHOOT_PCT:
                errors += 1
                message = (
                    f"{position.get('token_symbol', address[:8])} quote implies +{change:.2f}% from entry; "
                    "extreme take-profit quote requires review; position remains open"
                )
                position["price_safety_warning"] = message
                _record_provider("birdeye", last_price_jump_warning=message)
                logger.error(message)
                continue
            position.pop("price_safety_warning", None)
            closed += int(_close_position(address, position, price, "take_profit_10pct", change))
            continue

        position.pop("price_safety_warning", None)
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
    return {
        "mode": "paper", "real_trading_enabled": False,
        "simulated_trade_count": len(_paper_trades), "open_positions": positions,
        "realized_pnl_usd": round(_realized_pnl_usd, 8),
        "take_profit_pct": TAKE_PROFIT_PCT, "stop_loss_pct": STOP_LOSS_PCT,
        "paper_invested_usd": round(invested, 8), "paper_current_value_usd": round(value, 8),
        "paper_unrealized_pnl_usd": round(unrealized, 8),
        "paper_realized_pnl_usd": round(_realized_pnl_usd, 8),
        "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 8),
        "persistence": get_persistence_status(), "accounting": _reported_realized_pnl(_paper_trades),
        "price_diagnostics": get_price_diagnostics(),
        "note": "Paper only. Same-provider repeated quotes are not independent verification; extreme TP/SL overshoots remain open for review.",
    }


def portfolio_status():
    status = get_paper_status()
    keys = ("paper_invested_usd", "paper_current_value_usd", "paper_unrealized_pnl_usd", "paper_realized_pnl_usd", "paper_total_pnl_usd")
    return {key: status[key] for key in keys}


def paper_status():
    return get_paper_status()


def get_paper_trades():
    return list(_paper_trades)


def list_paper_trades():
    return get_paper_trades()
