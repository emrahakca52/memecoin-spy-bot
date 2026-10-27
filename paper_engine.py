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
        