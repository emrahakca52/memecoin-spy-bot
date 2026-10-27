"""Paper-trading engine with persistence and conservative price validation.

Historical trade records are retained. The known implausible MRBEAST SELL is
excluded from reported realized P/L without deleting its history. Price sanity
checks can defer marks and automatic exits when a quote is not corroborated.
Real-money trading is not implemented by this engine.
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
MAX_ACCEPTED_PRICE_JUMP_PCT = max(
    1.0, float(os.getenv("MAX_ACCEPTED_PRICE_JUMP_PCT", "35"))
)
BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS = max(
    1.1, float(os.getenv("BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS", "5"))
)
PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS = max(
    PRICE_CACHE_SECONDS,
    int(os.getenv("PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS", "180")),
)
PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT = max(
    1.0, float(os.getenv("PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT", "15"))
)
PRICE_JUMP_PENDING_MAX_AGE_SECONDS = max(
    30, int(os.getenv("PRICE_JUMP_PENDING_MAX_AGE_SECONDS", "600"))
)

# Illustrative paper-only execution-cost assumptions; override with env vars.
# These are estimates, not measured venue fees or guaranteed slippage.
PAPER_FEE_PCT_PER_SIDE = max(0.0, float(os.getenv("PAPER_FEE_PCT_PER_SIDE", "0.1")))
PAPER_SLIPPAGE_PCT_PER_SIDE = max(0.0, float(os.getenv("PAPER_SLIPPAGE_PCT_PER_SIDE", "0.2")))


def _estimated_execution_cost(notional_usd):
    notional = max(0.0, _num(notional_usd))
    return round(notional * (PAPER_FEE_PCT_PER_SIDE + PAPER_SLIPPAGE_PCT_PER_SIDE) / 100.0, 8)


# These remain the existing paper-strategy thresholds. They are not validated
# live-trading parameters.
TAKE_PROFIT_PCT = 10.0
STOP_LOSS_PCT = -5.0

# If a quote implies a loss far beyond the configured stop, do not auto-close
# from that quote alone. This is a safety brake for bad price feeds, not a
# guarantee that the quote is false. Review/verify the position manually.
MAX_STOP_LOSS_OVERSHOOT_PCT = min(
    10.0, max(5.0, float(os.getenv("MAX_STOP_LOSS_OVERSHOOT_PCT", "10")))
)
MAX_TAKE_PROFIT_OVERSHOOT_PCT = min(
    50.0, max(10.0, float(os.getenv("MAX_TAKE_PROFIT_OVERSHOOT_PCT", "50")))
)

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
        "requests": 0,
        "last_attempt_utc": None,
        "last_endpoint": None,
        "last_http_status": None,
        "last_error": None,
        "last_prices_found": 0,
        "last_price_jump_warning": None,
        "cooldown_seconds_remaining": 0,
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
    # Return a copy so callers cannot mutate internal status.
    return dict(_database_status)


def _connect_database():
    url = _database_url()
    if not url:
        _database_status.update(
            configured=False,
            connected=False,
            last_error="DATABASE_URL is not configured",
        )
        return None
    return psycopg.connect(url, connect_timeout=8, autocommit=True)


def _ensure_state_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memecoin_paper_state (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            state JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""
    )


def _state_snapshot():
    # JSON serialization below rejects NaN/Infinity.
    return {
        "open_positions": _open_positions,
        "paper_trades": _paper_trades,
        "realized_pnl_usd": _realized_pnl_usd,
        "saved_at_utc": _utc_now(),
        "mode": "paper",
    }


def _save_paper_state():
    """Persist the current paper state. Never expose DATABASE_URL."""
    if not _database_url():
        _database_status.update(
            configured=False,
            connected=False,
            last_error="DATABASE_URL is not configured",
        )
        return False

    try:
        snapshot = json.dumps(
            _state_snapshot(), default=str, allow_nan=False
        )
        with _connect_database() as conn:
            _ensure_state_table(conn)
            conn.execute(
                """INSERT INTO memecoin_paper_state (id, state, updated_at)
                   VALUES (1, %s::jsonb, NOW())
                   ON CONFLICT (id) DO UPDATE
                   SET state = EXCLUDED.state, updated_at = NOW()""",
                (snapshot,),
            )
        _database_status.update(
            configured=True,
            connected=True,
            last_error=None,
            last_saved_utc=_utc_now(),
        )
        return True
    except Exception as exc:
        _database_status.update(
            configured=True,
            connected=False,
            last_error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )
        logger.exception("Could not save paper state to database.")
        return False


def _num(value, default=0.0):
    try:
        result = float(value or 0)
        if math.isfinite(result):
            return result
        return default
    except (TypeError, ValueError, OverflowError):
        return default


def _accounting_diagnostics(trades, stored_realized):
    """Compare stored P/L to SELL records only; do not auto-correct it.

    Legacy SELL records may not contain complete quantity/cost-basis details.
    Therefore this is a warning check, not a ledger reconstruction.
    """
    sell_records = [
        t for t in trades
        if isinstance(t, dict) and str(t.get("side", "")).upper() == "SELL"
    ]
    known_values = [
        _num(t.get("realized_pnl_usd"), default=float("nan"))
        for t in sell_records
        if t.get("realized_pnl_usd") is not None
    ]
    known_values = [v for v in known_values if math.isfinite(v)]
    sell_sum = sum(known_values)
    missing_count = len(sell_records) - len(known_values)
    difference = stored_realized - sell_sum
    warning = None
    if missing_count:
        warning = (
            f"{missing_count} SELL record(s) have no realized_pnl_usd; "
            "full ledger reconciliation is not possible."
        )
    elif abs(difference) > 0.02:
        warning = (
            f"Stored realized P/L differs from recorded SELL P/L by "
            f"{difference:.8f} USD. Stored value was preserved; no automatic correction."
        )
    return {
        "sell_record_count": len(sell_records),
        "sell_records_with_pnl": len(known_values),
        "sell_records_missing_pnl": missing_count,
        "sum_recorded_sell_pnl_usd": round(sell_sum, 8),
        "stored_realized_pnl_usd": round(stored_realized, 8),
        "difference_usd": round(difference, 8),
        "warning": warning,
    }


def _is_quarantined_trade(trade):
    """Exclude explicitly quarantined or clearly implausible legacy auto-exits from P/L."""
    if not isinstance(trade, dict):
        return False
    if trade.get("accounting_quarantined") is True:
        return True
    if str(trade.get("side", "")).upper() != "SELL":
        return False

    # Specific historical bad quote confirmed in the paper-trade log.
    if (
        str(trade.get("token_symbol", "")).upper() == "MRBEAST"
        and str(trade.get("token_address", "")) == "4hqN5vhCG1q13qYUQvBhyMDDnpWKP98DoswDeDCVQaCn"
        and _num(trade.get("price_usd")) > 100.0
        and _num(trade.get("realized_pnl_usd")) > 1_000_000.0
    ):
        return True

    # Older builds could close a -5% stop or +10% target on an extreme quote.
    # Quarantine those historical auto-exits from aggregate P/L when the recorded
    # move exceeds the current safety envelope. Keep the original trade untouched
    # for audit; this is not a claim that the market move was impossible.
    if str(trade.get("source", "")).lower() != "auto":
        return False
    reason = str(trade.get("reason", "")).lower()
    observed = trade.get("observed_change_pct", trade.get("change_pct"))
    if observed is None:
        return False
    change = _num(observed, default=float("nan"))
    if not math.isfinite(change):
        return False
    if reason.startswith("stop_loss"):
        return change < STOP_LOSS_PCT - MAX_STOP_LOSS_OVERSHOOT_PCT
    if reason.startswith("take_profit"):
        return change > TAKE_PROFIT_PCT + MAX_TAKE_PROFIT_OVERSHOOT_PCT
    return False


def _reported_realized_pnl(trades):
    """Sum recorded SELL P/L excluding quarantined trades; never mutate history."""
    total = 0.0
    included = 0
    quarantined = []
    missing = 0
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict) or str(trade.get("side", "")).upper() != "SELL":
            continue
        if _is_quarantined_trade(trade):
            quarantined.append({
                "index": index,
                "token_symbol": trade.get("token_symbol"),
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
        "realized_pnl_usd": round(total, 8),
        "included_sell_records": included,
        "quarantined_sell_records": quarantined,
        "sell_records_missing_pnl": missing,
        "note": "Computed from recorded SELL P/L fields; legacy records may be incomplete.",
    }


def load_paper_state():
    """Load persisted state; quarantine known bad accounting rows without deleting history."""
    global _open_positions, _paper_trades, _realized_pnl_usd

    if not _database_url():
        _database_status.update(
            configured=False,
            connected=False,
            last_error="DATABASE_URL is not configured",
        )
        return {
            "ok": False,
            "loaded": False,
            "reason": "database_url_missing",
        }

    try:
        with _connect_database() as conn:
            _ensure_state_table(conn)
            row = conn.execute(
                "SELECT state FROM memecoin_paper_state WHERE id = 1"
            ).fetchone()

        _database_status.update(
            configured=True,
            connected=True,
            last_error=None,
            last_loaded_utc=_utc_now(),
        )
        if not row:
            return {
                "ok": True,
                "loaded": False,
                "reason": "no_saved_state",
            }

        state = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if not isinstance(state, dict):
            raise ValueError("Saved paper state has an invalid format")

        positions = state.get("open_positions", {})
        trades = state.get("paper_trades", [])
        if not isinstance(positions, dict) or not isinstance(trades, list):
            raise ValueError("Saved positions or trades have an invalid format")

        # Validate basic position structure before replacing current memory state.
        for address, position in positions.items():
            if not isinstance(position, dict):
                raise ValueError(f"Invalid position record for {str(address)[:12]}")
            if _num(position.get("quantity")) < 0:
                raise ValueError(f"Negative position quantity for {str(address)[:12]}")
            if _num(position.get("invested_usd")) < 0:
                raise ValueError(f"Negative position cost for {str(address)[:12]}")

        stored_realized = _num(state.get("realized_pnl_usd"))
        diagnostics = _accounting_diagnostics(trades, stored_realized)
        reconciled = _reported_realized_pnl(trades)

        # Keep the historical stored-vs-ledger comparison for audit. A complete
        # ledger can safely rebuild the aggregate; an incomplete legacy ledger
        # must not overwrite the stored lifetime total with a partial sum.
        diagnostics["historical_stored_value"] = stored_realized
        diagnostics["historical_stored_value_mismatch"] = diagnostics.get("warning")
        diagnostics["warning"] = None

        # Preserve each original trade. Mark the known invalid MRBEAST sale instead
        # of deleting it; the displayed aggregate excludes it.
        for trade in trades:
            if _is_quarantined_trade(trade) and isinstance(trade, dict):
                trade["accounting_quarantined"] = True
                trade["accounting_quarantine_reason"] = (
                    "Implausible historical auto-exit quote; excluded from reported paper P/L pending review"
                )

        _open_positions = positions
        _paper_trades = trades
        warning_parts = []

        # Rebuild the aggregate only when every SELL row has a usable realized-P/L
        # value. If legacy rows are missing that field, summing only the known rows
        # would silently erase part of the saved lifetime total on the next save.
        # In that case preserve the stored aggregate and make the incomplete ledger
        # explicit until an operator can reconcile it from source records.
        if reconciled["sell_records_missing_pnl"] == 0:
            _realized_pnl_usd = reconciled["realized_pnl_usd"]
        else:
            _realized_pnl_usd = stored_realized
            warning_parts.append(
                f"{reconciled['sell_records_missing_pnl']} SELL record(s) lack realized_pnl_usd; "
                "stored realized P/L preserved because full reconciliation is incomplete."
            )

        if reconciled["quarantined_sell_records"]:
            warning_parts.append(
                f"{len(reconciled['quarantined_sell_records'])} SELL record(s) quarantined from reported P/L."
            )
        _database_status["accounting_warning"] = " ".join(warning_parts) or None

        if _database_status["accounting_warning"]:
            logger.error("Paper accounting warning: %s", _database_status["accounting_warning"])
        # Save annotations and, only when reconciliation is complete, the rebuilt
        # aggregate. Original trade rows are retained for audit. A DB backup is still recommended.
        _save_paper_state()
        logger.info(
            "Loaded paper state: %s positions, %s trades.",
            len(_open_positions), len(_paper_trades),
        )
        return {
            "ok": True,
            "loaded": True,
            "open_positions": len(_open_positions),
            "trade_count": len(_paper_trades),
            "accounting": {
                **diagnostics,
                "reported_realized_pnl": reconciled,
                "stored_realized_pnl_preserved_in_source_state": stored_realized,
            },
        }
    except Exception as exc:
        _database_status.update(
            configured=True,
            connected=False,
            last_error=f"{type(exc).__name__}: {str(exc)[:240]}",
        )
        logger.exception("Could not load paper state from database.")
        raise


def _record_provider(provider, **updates):
    _price_diagnostics.setdefault(provider, {}).update(updates)


def get_price_diagnostics():
    result = {}
    now = time.monotonic()
    for provider, details in _price_diagnostics.items():
        item = dict(details)
        item["cooldown_seconds_remaining"] = max(
            0, int(_provider_cooldown_until.get(provider, 0.0) - now)
        )
        item["cooldown_reason"] = _provider_cooldown_reason.get(provider)
        result[provider] = item
    return result


def _provider_is_cooling(provider="birdeye"):
    return time.monotonic() < _provider_cooldown_until.get(provider, 0.0)


def _set_provider_cooldown(provider, seconds, reason):
    _provider_cooldown_until[provider] = max(
        _provider_cooldown_until.get(provider, 0.0),
        time.monotonic() + max(1, int(seconds)),
    )
    _provider_cooldown_reason[provider] = reason


async def _respect_birdeye_rate_limit():
    global _birdeye_last_request_at
    wait = BIRDEYE_MIN_REQUEST_INTERVAL_SECONDS - (
        time.monotonic() - _birdeye_last_request_at
    )
    if wait > 0:
        await asyncio.sleep(wait)
    _birdeye_last_request_at = time.monotonic()


async def _fetch_birdeye_price(client, address):
    api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
    if not api_key:
        _record_provider(
            "birdeye", last_error="BIRDEYE_API_KEY is not configured"
        )
        return None
    if _provider_is_cooling():
        remain = max(
            1,
            int(_provider_cooldown_until.get("birdeye", 0) - time.monotonic()),
        )
        _record_provider("birdeye", last_error=f"provider cooldown ({remain}s)")
        return None

    await _respect_birdeye_rate_limit()
    _record_provider(
        "birdeye",
        requests=int(_price_diagnostics["birdeye"].get("requests", 0)) + 1,
        last_attempt_utc=_utc_now(),
        last_endpoint=BIRDEYE_PRICE_URL,
        last_http_status=None,
        last_error=None,
        last_prices_found=0,
    )
    try:
        response = await client.get(
            BIRDEYE_PRICE_URL,
            params={"address": address},
            headers={
                "Accept": "application/json",
                "X-API-KEY": api_key,
                "x-chain": "solana",
            },
        )
        _record_provider("birdeye", last_http_status=response.status_code)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            try:
                cooldown = min(900, max(60, int(float(retry_after))))
            except (TypeError, ValueError, OverflowError):
                cooldown = 300
            _set_provider_cooldown(
                "birdeye", cooldown, f"HTTP 429; cooldown {cooldown}s"
            )
            _record_provider(
                "birdeye", last_error=f"HTTP 429; cooldown {cooldown}s"
            )
            return None
        if response.status_code in (401, 403):
            _record_provider(
                "birdeye",
                last_error=f"HTTP {response.status_code}; check API key and package access",
            )
            return None

        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        price = _num(data.get("value")) if isinstance(data, dict) else 0.0
        if price <= 0:
            _record_provider(
                "birdeye",
                last_error="HTTP 200 but no usable positive data.value price",
            )
            return None
        _record_provider("birdeye", last_prices_found=1, last_error=None)
        return price

    except httpx.HTTPStatusError as exc:
        _record_provider(
            "birdeye",
            last_http_status=exc.response.status_code,
            last_error=f"HTTP {exc.response.status_code}",
        )
    except httpx.TimeoutException:
        _record_provider("birdeye", last_error="request timed out")
    except httpx.HTTPError as exc:
        _record_provider(
            "birdeye", last_error=f"network error: {type(exc).__name__}"
        )
    except (ValueError, AttributeError, TypeError) as exc:
        _record_provider(
            "birdeye", last_error=f"invalid response: {type(exc).__name__}"
        )
    return None


async def _fetch_prices_batch(client, token_addresses):
    results = {}
    for address in token_addresses:
        results[address] = await _fetch_birdeye_price(client, address)
        if _provider_is_cooling():
            break
    return results


def _validate_price_jump(address, price, now, reference_price=None):
    """Withhold a large move until a second nearby quote corroborates it.

    If there is no recent cached quote, the open position's entry price can
    serve as a conservative reference. A confirmed same-provider quote is not
    proof of truth; it only reduces one-off bad-quote risk.
    """
    previous = _price_cache.get(address)
    old_price = _num(previous.get("price")) if previous else 0.0
    age = (
        max(0.0, now - _num(previous.get("at")))
        if previous else float("inf")
    )

    if old_price <= 0 or age > PRICE_JUMP_REFERENCE_MAX_AGE_SECONDS:
        old_price = _num(reference_price)
        age = 0.0 if old_price > 0 else float("inf")

    if old_price <= 0:
        _pending_price_jumps.pop(address, None)
        return True

    # A supplied reference (signal price for entries, last accepted mark for
    # open positions) is a hard safety boundary. Repeated quotes from the same
    # provider must NOT override a large disagreement with that reference.
    supplied_reference = _num(reference_price)
    if supplied_reference > 0:
        reference_jump = abs(price / supplied_reference - 1.0) * 100.0
        if reference_jump > MAX_ACCEPTED_PRICE_JUMP_PCT:
            _pending_price_jumps[address] = {"price": price, "at": now}
            message = (
                f"{address[:10]} quote differs {reference_jump:.2f}% from supplied "
                "reference; withheld pending independent verification"
            )
            _record_provider("birdeye", last_price_jump_warning=message)
            logger.error(message)
            return False

    jump = abs(price / old_price - 1.0) * 100.0
    if jump <= MAX_ACCEPTED_PRICE_JUMP_PCT:
        _pending_price_jumps.pop(address, None)
        return True

    pending = _pending_price_jumps.get(address)
    if pending and now - _num(pending.get("at")) <= PRICE_JUMP_PENDING_MAX_AGE_SECONDS:
        pending_price = _num(pending.get("price"))
        if (
            pending_price > 0
            and abs(price / pending_price - 1.0) * 100.0
            <= PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT
        ):
            _pending_price_jumps.pop(address, None)
            message = (
                f"{address[:10]} large move corroborated by consecutive quotes "
                f"({jump:.2f}%)"
            )
            _record_provider("birdeye", last_price_jump_warning=message)
            logger.warning(message)
            return True

    _pending_price_jumps[address] = {"price": price, "at": now}
    message = (
        f"{address[:10]} quote moved {jump:.2f}%; awaiting confirming quote; "
        "ignored this cycle"
    )
    _record_provider("birdeye", last_price_jump_warning=message)
    logger.warning(message)
    return False


async def get_token_prices_usd(token_addresses, reference_prices=None):
    """Return prices; withhold unconfirmed large jumps from the current cycle."""
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
            async with httpx.AsyncClient(
                timeout=PRICE_TIMEOUT_SECONDS,
                headers={"Accept": "application/json", "User-Agent": "MemecoinSpyBot/2.3"},
            ) as client:
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
            if not _validate_price_jump(
                address, price, accepted_at, reference_prices.get(address)
            ):
                prices[address] = None
                continue

            _price_cache[address] = {"at": accepted_at, "price": price}
            prices[address] = price
    return prices


async def get_token_price_usd(token_address):
    if not token_address:
        return None
    return (await get_token_prices_usd([token_address])).get(token_address)


def record_paper_trade(
    token_address, side, amount_usd, price_usd,
    token_symbol="", source="manual", reason=None
):
    """Record a paper trade using USD notional and token quantity consistently."""
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

    if side == "BUY":
        entry_cost = _estimated_execution_cost(amount_usd)
        trade["estimated_entry_cost_usd"] = entry_cost
        trade["estimated_transaction_cost_usd"] = entry_cost
        quantity = amount_usd / price_usd
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("calculated buy quantity is invalid")

        position = _open_positions.get(token_address)
        _paper_trades.append(trade)
        if position is None:
            _open_positions[token_address] = {
                "token_address": token_address,
                "token_symbol": trade["token_symbol"],
                "entry_price_usd": price_usd,
                "current_price_usd": price_usd,
                "invested_usd": amount_usd,
                "estimated_entry_cost_usd": entry_cost,
                "quantity": quantity,
                "unrealized_pnl_usd": 0.0,
                "opened_at": time.time(),
            }
        else:
            old_qty = _num(position.get("quantity"))
            old_invested = _num(position.get("invested_usd"))
            if old_qty <= 0 or old_invested < 0:
                _paper_trades.pop()
                raise ValueError("existing position has invalid quantity/cost")
            total_qty = old_qty + quantity
            if total_qty <= 0 or not math.isfinite(total_qty):
                _paper_trades.pop()
                raise ValueError("combined position quantity is invalid")
            position["entry_price_usd"] = (
                _num(position.get("entry_price_usd")) * old_qty
                + price_usd * quantity
            ) / total_qty
            position["quantity"] = total_qty
            position["invested_usd"] = old_invested + amount_usd
            position["estimated_entry_cost_usd"] = (
                _num(position.get("estimated_entry_cost_usd")) + entry_cost
            )
            position["current_price_usd"] = price_usd
            position["unrealized_pnl_usd"] = (
                total_qty * price_usd - old_invested - amount_usd
            )
    else:
        position = _open_positions.get(token_address)
        if not position:
            raise ValueError("cannot sell: no open paper position for this token")

        qty = _num(position.get("quantity"))
        invested = _num(position.get("invested_usd"))
        if qty <= 0 or invested < 0:
            raise ValueError("existing position has invalid quantity/cost")

        # amount_usd is the requested sale notional; cap at the full position.
        sell_qty = min(qty, amount_usd / price_usd)
        if sell_qty <= 0 or not math.isfinite(sell_qty):
            raise ValueError("calculated sell quantity is invalid")

        cost = invested * sell_qty / qty
        proceeds = sell_qty * price_usd
        realized = proceeds - cost
        remaining_qty = max(0.0, qty - sell_qty)
        remaining_invested = max(0.0, invested - cost)

        trade["amount_usd"] = round(proceeds, 8)
        trade["quantity_sold"] = sell_qty
        trade["cost_basis_usd"] = round(cost, 8)
        entry_cost_known = position.get("estimated_entry_cost_usd") is not None
        allocated_entry_cost = (
            _num(position.get("estimated_entry_cost_usd")) * sell_qty / qty
            if entry_cost_known else None
        )
        exit_cost = _estimated_execution_cost(proceeds)
        trade["realized_pnl_usd"] = round(realized, 8)
        trade["estimated_entry_cost_allocated_usd"] = (
            round(allocated_entry_cost, 8) if entry_cost_known else None
        )
        trade["estimated_exit_cost_usd"] = exit_cost
        trade["estimated_transaction_cost_usd"] = (
            round(allocated_entry_cost + exit_cost, 8) if entry_cost_known else None
        )
        trade["net_realized_pnl_usd"] = (
            round(realized - allocated_entry_cost - exit_cost, 8)
            if entry_cost_known else None
        )
        _paper_trades.append(trade)
        _realized_pnl_usd += realized

        if remaining_qty <= max(1e-12, qty * 1e-10):
            _open_positions.pop(token_address, None)
        else:
            position["quantity"] = remaining_qty
            position["invested_usd"] = remaining_invested
            if entry_cost_known:
                position["estimated_entry_cost_usd"] = max(
                    0.0, _num(position.get("estimated_entry_cost_usd")) - allocated_entry_cost
                )
            position["unrealized_pnl_usd"] = (
                remaining_qty * _num(position.get("current_price_usd"))
                - remaining_invested
            )

    _save_paper_state()
    return {"ok": True, "mode": "paper", "trade": trade}


def open_paper_position(
    token_address, token_symbol="", amount_usd=10.0, price_usd=None,
    source="auto", max_open_positions=None, metadata=None
):
    if not token_address:
        raise ValueError("token_address is required")
    if price_usd is None or _num(price_usd) <= 0:
        raise ValueError("price_usd is required and must be positive")
    if _num(amount_usd) <= 0:
        raise ValueError("amount_usd must be positive")
    if token_address in _open_positions:
        return {
            "ok": True, "mode": "paper", "skipped": True,
            "reason": "position_already_open",
        }
    if max_open_positions is not None and len(_open_positions) >= int(max_open_positions):
        return {
            "ok": True, "mode": "paper", "skipped": True,
            "reason": "max_open_positions_reached",
        }

    result = record_paper_trade(
        token_address, "BUY", amount_usd, price_usd, token_symbol, source
    )
    position = _open_positions.get(token_address)
    if position is not None and metadata:
        position["metadata"] = dict(metadata)
        _save_paper_state()
    return result


def _close_position(address, position, price, reason, observed_change_pct=None):
    global _realized_pnl_usd

    qty = _num(position.get("quantity"))
    invested = _num(position.get("invested_usd"))
    entry = _num(position.get("entry_price_usd"))
    if qty <= 0 or invested < 0 or price <= 0 or entry <= 0:
        logger.error("Refusing to close invalid paper position for %s", address)
        return False

    # Defense in depth: enforce the same extreme-quote limits at the final
    # close boundary, even if a future caller bypasses update_paper_prices().
    # A suspicious quote must not realize a paper loss/profit solely because a
    # caller invokes this helper directly. Keep the position open for review.
    change = (price / entry - 1) * 100
    if reason == "stop_loss_5pct":
        overshoot = STOP_LOSS_PCT - change
        if overshoot > MAX_STOP_LOSS_OVERSHOOT_PCT:
            message = (
                f"{position.get('token_symbol', address[:8])} close refused: "
                f"stop-loss quote implies {change:.2f}% from entry "
                f"(overshoot {overshoot:.2f}% exceeds "
                f"{MAX_STOP_LOSS_OVERSHOOT_PCT:.2f}%)"
            )
            position["last_suspect_price_usd"] = price
            position["last_suspect_price_utc"] = _utc_now()
            position["price_safety_warning"] = message
            position["price_mark_status"] = "unverified_extreme_quote"
            _record_provider("birdeye", last_price_jump_warning=message)
            logger.error(message)
            return False
    elif reason == "take_profit_10pct":
        overshoot = change - TAKE_PROFIT_PCT
        if overshoot > MAX_TAKE_PROFIT_OVERSHOOT_PCT:
            message = (
                f"{position.get('token_symbol', address[:8])} close refused: "
                f"take-profit quote implies {change:.2f}% from entry "
                f"(overshoot {overshoot:.2f}% exceeds "
                f"{MAX_TAKE_PROFIT_OVERSHOOT_PCT:.2f}%)"
            )
            position["last_suspect_price_usd"] = price
            position["last_suspect_price_utc"] = _utc_now()
            position["price_safety_warning"] = message
            position["price_mark_status"] = "unverified_extreme_quote"
            _record_provider("birdeye", last_price_jump_warning=message)
            logger.error(message)
            return False

    value = qty * price
    pnl = value - invested
    if not math.isfinite(value) or not math.isfinite(pnl):
        logger.error("Refusing to close non-finite paper position for %s", address)
        return False

    trigger = (
        entry * (1 + STOP_LOSS_PCT / 100)
        if reason == "stop_loss_5pct"
        else entry * (1 + TAKE_PROFIT_PCT / 100)
        if reason == "take_profit_10pct"
        else None
    )
    change = (price / entry - 1) * 100
    estimated_costs_known = position.get("estimated_entry_cost_usd") is not None
    estimated_entry_cost = _num(position.get("estimated_entry_cost_usd")) if estimated_costs_known else 0.0
    estimated_exit_cost = _estimated_execution_cost(value)
    trade = {
        "token_address": address,
        "token_symbol": position.get("token_symbol", address[:8]),
        "side": "SELL",
        "amount_usd": round(value, 8),
        "price_usd": price,
        "source": "auto",
        "reason": reason,
        "realized_pnl_usd": round(pnl, 8),
        "estimated_exit_cost_usd": estimated_exit_cost,
        "estimated_entry_cost_allocated_usd": round(estimated_entry_cost, 8) if estimated_costs_known else None,
        "estimated_transaction_cost_usd": round(estimated_entry_cost + estimated_exit_cost, 8) if estimated_costs_known else None,
        "net_realized_pnl_usd": round(pnl - estimated_entry_cost - estimated_exit_cost, 8) if estimated_costs_known else None,
        "entry_price_usd": entry,
        "change_pct": round(change, 4),
        "trigger_price_usd": trigger,
        "stop_loss_threshold_pct": STOP_LOSS_PCT,
        "take_profit_threshold_pct": TAKE_PROFIT_PCT,
        "observed_change_pct": round(
            change if observed_change_pct is None else observed_change_pct, 4
        ),
        "quantity_sold": qty,
        "cost_basis_usd": round(invested, 8),
        "timestamp": time.time(),
        "mode": "paper",
    }

    _paper_trades.append(trade)
    _realized_pnl_usd += pnl
    _open_positions.pop(address, None)
    _save_paper_state()
    return True


async def _fetch_dexscreener_price(address):
    """Fetch an independent Solana token price from DexScreener for safety checks."""
    if not address:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"Accept": "application/json"}) as client:
            response = await client.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{address}"
            )
            response.raise_for_status()
            payload = response.json()
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        candidates = []
        for pair in pairs or []:
            if not isinstance(pair, dict) or str(pair.get("chainId", "")).lower() != "solana":
                continue
            base = pair.get("baseToken") or {}
            if str(base.get("address", "")).lower() != str(address).lower():
                continue
            price = _num(pair.get("priceUsd"))
            liquidity = _num((pair.get("liquidity") or {}).get("usd"))
            if price > 0 and liquidity > 0 and math.isfinite(price):
                candidates.append((liquidity, price))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Independent DexScreener price check failed for %s: %s", address[:10], type(exc).__name__)
        return None


async def update_paper_prices():
    updated = closed = errors = 0
    addresses = list(_open_positions)
    if not addresses:
        return {
            "updated": 0, "closed": 0, "price_errors": 0,
            "note": "No open positions",
        }

    # Entry prices provide a reference when the in-memory quote cache is empty.
    reference_prices = {
        address: _num(position.get("current_price_usd"))
        or _num(position.get("entry_price_usd"))
        for address, position in _open_positions.items()
    }
    prices = await get_token_prices_usd(addresses, reference_prices)

    for address in addresses:
        position = _open_positions.get(address)
        if not position:
            continue
        price = prices.get(address)
        if price is None or price <= 0:
            errors += 1
            continue

        # Validate the quote before changing the mark. Large price jumps are
        # screened by get_token_prices_usd(); if a quote passes that gate but
        # implies an extreme TP/SL overshoot, update the paper mark/P&L for
        # visibility while keeping the position OPEN. The separate auto-close
        # guard below still blocks an extreme exit pending verification.
        entry = _num(position.get("entry_price_usd"))
        opened = _num(position.get("opened_at"))
        if entry <= 0:
            errors += 1
            position["price_safety_warning"] = "Invalid entry price; quote withheld"
            continue

        change = (price / entry - 1) * 100
        if opened and time.time() - opened < MIN_HOLD_SECONDS:
            # During the minimum hold window, still validate the raw move below
            # before allowing the quote to update the mark.
            pass

        extreme_quote = False
        if change <= STOP_LOSS_PCT:
            overshoot = STOP_LOSS_PCT - change
            if overshoot > MAX_STOP_LOSS_OVERSHOOT_PCT:
                message = (
                    f"{position.get('token_symbol', address[:8])} quote implies "
                    f"{change:.2f}% from entry; suspect extreme quote is stored "
                    "for diagnostics only; current mark/P&L and auto stop-loss are withheld pending verification"
                )
                position["last_suspect_price_usd"] = price
                position["last_suspect_price_utc"] = _utc_now()
                position["price_safety_warning"] = message
                position["price_mark_status"] = "unverified_extreme_quote"
                _record_provider("birdeye", last_price_jump_warning=message)
                logger.error(message)
                extreme_quote = True

        if change >= TAKE_PROFIT_PCT:
            overshoot = change - TAKE_PROFIT_PCT
            if overshoot > MAX_TAKE_PROFIT_OVERSHOOT_PCT:
                message = (
                    f"{position.get('token_symbol', address[:8])} quote implies "
                    f"+{change:.2f}% from entry; suspect extreme quote is stored "
                    "for diagnostics only; current mark/P&L and auto take-profit are withheld pending verification"
                )
                position["last_suspect_price_usd"] = price
                position["last_suspect_price_utc"] = _utc_now()
                position["price_safety_warning"] = message
                position["price_mark_status"] = "unverified_extreme_quote"
                _record_provider("birdeye", last_price_jump_warning=message)
                logger.error(message)
                extreme_quote = True

        # Critical safety rule: a quote that implies an implausibly large move
        # from entry is diagnostic-only. Do not let it corrupt the displayed
        # position mark/unrealized P&L or trigger an automatic paper sale.
        # Existing current_price_usd and unrealized_pnl_usd remain unchanged.
        if extreme_quote:
            # An independent market-data source may corroborate a genuine crash.
            # This makes the paper mark visible, but deliberately never auto-sells
            # on an extreme move; the position remains open for review.
            independent_price = await _fetch_dexscreener_price(address)
            corroborated = (
                independent_price is not None
                and abs(independent_price / price - 1.0) * 100.0
                <= PRICE_JUMP_CONFIRMATION_TOLERANCE_PCT
            )
            if corroborated:
                position["current_price_usd"] = independent_price
                qty = _num(position.get("quantity"))
                invested = _num(position.get("invested_usd"))
                position["unrealized_pnl_usd"] = round(qty * independent_price - invested, 8)
                position["last_price_update_utc"] = _utc_now()
                position["price_mark_status"] = "independently_verified_extreme_quote"
                position["price_verification_provider"] = "DexScreener"
                position["price_verification_price_usd"] = independent_price
                position["price_safety_warning"] = (
                    "Extreme move corroborated by independent DexScreener market data; "
                    "position remains open and automatic exit is withheld for safety."
                )
                updated += 1
            errors += 1
            continue

        # Only accepted, non-extreme quotes update the paper mark and P/L.
        position["current_price_usd"] = price
        qty = _num(position.get("quantity"))
        invested = _num(position.get("invested_usd"))
        position["unrealized_pnl_usd"] = round(qty * price - invested, 8)
        position["last_price_update_utc"] = _utc_now()
        # This quote passed the price-jump validator and the TP/SL extreme-move
        # checks above. Clear a prior quarantine so a recovered, acceptable
        # mark can be included in valuation again. Preserve the suspect quote
        # itself as a diagnostic field for auditability.
        position.pop("price_mark_status", None)
        position.pop("price_safety_warning", None)
        updated += 1

        if opened and time.time() - opened < MIN_HOLD_SECONDS:
            continue

        # Fail-safe auto-close guard:
        # - normal TP/SL signals can close as before;
        # - extreme prices are not allowed to trigger an automatic sale solely
        #   because a single provider repeatedly reports an implausible quote.
        # The position remains open and is flagged for review. This can delay a
        # genuine crash exit, so it is deliberately visible in diagnostics.
        if change >= TAKE_PROFIT_PCT:
            overshoot = change - TAKE_PROFIT_PCT
            if overshoot > MAX_TAKE_PROFIT_OVERSHOOT_PCT:
                errors += 1
                message = (
                    f"{position.get('token_symbol', address[:8])} quote implies "
                    f"+{change:.2f}% from entry; auto take-profit withheld "
                    f"(overshoot {overshoot:.2f}% exceeds "
                    f"{MAX_TAKE_PROFIT_OVERSHOOT_PCT:.2f}%)"
                )
                position["price_safety_warning"] = message
                _record_provider("birdeye", last_price_jump_warning=message)
                logger.error(message)
                continue
            position.pop("price_safety_warning", None)
            closed += int(
                _close_position(
                    address, position, price, "take_profit_10pct", change
                )
            )
        elif change <= STOP_LOSS_PCT:
            overshoot = STOP_LOSS_PCT - change
            if overshoot > MAX_STOP_LOSS_OVERSHOOT_PCT:
                errors += 1
                message = (
                    f"{position.get('token_symbol', address[:8])} quote implies "
                    f"{change:.2f}% from entry; auto stop-loss withheld "
                    f"(overshoot {overshoot:.2f}% exceeds "
                    f"{MAX_STOP_LOSS_OVERSHOOT_PCT:.2f}%). Verify price; "
                    "position remains open."
                )
                position["price_safety_warning"] = message
                _record_provider("birdeye", last_price_jump_warning=message)
                logger.error(message)
                continue
            position.pop("price_safety_warning", None)
            closed += int(
                _close_position(
                    address, position, price, "stop_loss_5pct", change
                )
            )
        else:
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

    return {
        "updated": updated,
        "closed": closed,
        "price_errors": errors,
        "note": note,
    }


def _price_mark_age_seconds(position, now=None):
    """Return age of the last successful price mark, or None if unavailable."""
    raw = position.get("last_price_update_utc")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (time.time() if now is None else now) - parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _stale_price_threshold_seconds():
    """A mark older than three normal polling cycles is not a current valuation."""
    try:
        poll_seconds = max(30, int(os.getenv("PAPER_BOT_POLL_SECONDS", "180")))
    except (TypeError, ValueError):
        poll_seconds = 180
    try:
        configured = int(os.getenv("PAPER_PRICE_STALE_SECONDS", str(max(300, poll_seconds * 3))))
    except (TypeError, ValueError):
        configured = max(300, poll_seconds * 3)
    return max(60, configured)


def get_paper_status():
    # Do not present an unverified extreme quote as a live mark or P/L.
    # Keep the original position/trade data intact; expose the suspect quote
    # separately for diagnostics and omit that position from marked totals.
    positions = []
    invested = 0.0
    verified_value = 0.0
    verified_unrealized = 0.0
    unverified_count = 0
    unverified_invested = 0.0
    stale_count = 0
    stale_invested = 0.0
    stale_threshold = _stale_price_threshold_seconds()

    for stored in _open_positions.values():
        position = dict(stored)
        cost = _num(position.get("invested_usd"))
        invested += cost
        mark_age = _price_mark_age_seconds(position)
        position["price_mark_age_seconds"] = (
            round(mark_age, 1) if mark_age is not None else None
        )
        position["price_mark_stale_after_seconds"] = stale_threshold
        if position.get("price_mark_status") == "unverified_extreme_quote":
            unverified_count += 1
            unverified_invested += cost
            position["current_price_usd"] = None
            position["unrealized_pnl_usd"] = None
            position["price_safety_warning"] = position.get(
                "price_safety_warning",
                "Current quote is unverified; mark and P/L withheld.",
            )
        elif position.get("price_mark_status") == "independently_verified_extreme_quote":
            # Preserve the separately corroborated crash mark and its warning.
            price = _num(position.get("current_price_usd"))
            qty = _num(position.get("quantity"))
            marked_value = qty * price
            pnl = marked_value - cost
            verified_value += marked_value
            verified_unrealized += pnl
        elif mark_age is None or mark_age > stale_threshold:
            stale_count += 1
            stale_invested += cost
            position["price_mark_status"] = "stale_price_mark"
            position["price_safety_warning"] = (
                "Last successful price mark is missing or older than the configured "
                "freshness window; excluded from current portfolio valuation until refreshed."
            )
            position["current_price_usd"] = None
            position["unrealized_pnl_usd"] = None
        else:
            price = _num(position.get("current_price_usd"))
            qty = _num(position.get("quantity"))
            marked_value = qty * price
            pnl = marked_value - cost
            position["unrealized_pnl_usd"] = round(pnl, 8)
            verified_value += marked_value
            verified_unrealized += pnl
        positions.append(position)

    accounting = _reported_realized_pnl(_paper_trades)
    estimated_realized_costs = 0.0
    net_costs_incomplete = False
    for trade in _paper_trades:
        if not isinstance(trade, dict) or str(trade.get("side", "")).upper() != "SELL":
            continue
        if trade.get("accounting_quarantined") or _is_quarantined_trade(trade):
            net_costs_incomplete = True
            continue
        costs = trade.get("estimated_transaction_cost_usd")
        if costs is None:
            net_costs_incomplete = True
        else:
            estimated_realized_costs += _num(costs)
    open_position_estimated_costs = 0.0
    estimated_open_exit_costs = 0.0
    for stored in _open_positions.values():
        entry_cost = stored.get("estimated_entry_cost_usd")
        if entry_cost is None:
            net_costs_incomplete = True
        else:
            open_position_estimated_costs += _num(entry_cost)
        status = stored.get("price_mark_status")
        mark_age = _price_mark_age_seconds(stored)
        if (
            status == "unverified_extreme_quote"
            or (status != "independently_verified_extreme_quote"
                and (mark_age is None or mark_age > stale_threshold))
        ):
            net_costs_incomplete = True
            continue
        marked_notional = _num(stored.get("quantity")) * _num(stored.get("current_price_usd"))
        estimated_open_exit_costs += _estimated_execution_cost(marked_notional)
    estimated_net_total_pnl = (
        verified_unrealized + _realized_pnl_usd
        - estimated_realized_costs
        - open_position_estimated_costs
        - estimated_open_exit_costs
    )
    accounting_incomplete = bool(
        unverified_count
        or stale_count
        or accounting["quarantined_sell_records"]
        or accounting["sell_records_missing_pnl"]
    )

    return {
        "mode": "paper",
        "real_trading_enabled": False,
        "simulated_trade_count": len(_paper_trades),
        "open_positions": positions,
        "realized_pnl_usd": round(_realized_pnl_usd, 8),
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "paper_invested_usd": round(invested, 8),
        "paper_current_value_usd": round(verified_value, 8),
        "paper_unrealized_pnl_usd": round(verified_unrealized, 8),
        "paper_realized_pnl_usd": round(_realized_pnl_usd, 8),
        "paper_total_pnl_usd": round(verified_unrealized + _realized_pnl_usd, 8),
        "paper_estimated_net_total_pnl_usd": round(estimated_net_total_pnl, 8),
        "paper_estimated_realized_transaction_costs_usd": round(estimated_realized_costs, 8),
        "paper_estimated_open_entry_costs_usd": round(open_position_estimated_costs, 8),
        "paper_estimated_open_exit_costs_usd": round(estimated_open_exit_costs, 8),
        "paper_estimated_net_pnl_incomplete": net_costs_incomplete,
        "paper_execution_cost_assumptions": {
            "fee_pct_per_side": PAPER_FEE_PCT_PER_SIDE,
            "slippage_pct_per_side": PAPER_SLIPPAGE_PCT_PER_SIDE,
            "note": "Illustrative estimates only; actual fees and slippage vary by venue, liquidity, and execution."
        },
        "unverified_position_count": unverified_count,
        "unverified_position_invested_usd": round(unverified_invested, 8),
        "stale_price_position_count": stale_count,
        "stale_price_position_invested_usd": round(stale_invested, 8),
        "price_mark_stale_after_seconds": stale_threshold,
        "paper_total_pnl_incomplete": accounting_incomplete,
        "persistence": get_persistence_status(),
        "accounting": accounting,
        "price_diagnostics": get_price_diagnostics(),
        "note": (
            "Paper simulation only; no real orders. Unverified extreme quotes are "
            "excluded from marked value and unrealized P/L until a trustworthy mark "
            "is available; their invested amount is reported separately. Same-provider "
            "confirmation is not independent verification. Realized P/L excludes "
            "quarantined historical rows without deleting trade history. Total P/L is incomplete "            "when any position or historical SELL record is quarantined or missing realized P/L."
        ),
    }

def portfolio_status():
    status = get_paper_status()
    keys = (
        "paper_invested_usd",
        "paper_current_value_usd",
        "paper_unrealized_pnl_usd",
        "paper_realized_pnl_usd",
        "paper_total_pnl_usd",
    )
    return {key: status[key] for key in keys}


def paper_status():
    return get_paper_status()


def get_paper_trades():
    return list(_paper_trades)


def list_paper_trades():
    return get_paper_trades()
