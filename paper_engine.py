"""Paper trading engine with improved exits and risk controls."""
import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import httpx
import psycopg

from config import (
    TAKE_PROFIT_PCT, STOP_LOSS_PCT, TRAILING_STOP_PCT,
    MIN_HOLD_SECONDS, MAX_DAILY_LOSS_USD,
)

logger = logging.getLogger("memecoin_spy.paper_engine")

PRICE_TIMEOUT_SECONDS = 12
PRICE_CACHE_SECONDS = 90
MIN_HOLD_SECONDS = MIN_HOLD_SECONDS

_price_cache = {}
_open_positions = {}
_paper_trades = []
_realized_pnl_usd = 0.0
_price_lock = asyncio.Lock()

def _utc_now():
    return datetime.now(timezone.utc).isoformat()

def _num(value, default=0.0):
    try:
        n = float(value or 0)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default

def _database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[11:]
    return url

def _connect_database():
    url = _database_url()
    if not url:
        return None
    return psycopg.connect(url, connect_timeout=8, autocommit=True)

def _ensure_state_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_paper_state (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            state JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

def _save_paper_state():
    if not _database_url():
        return False
    try:
        snapshot = json.dumps({
            "open_positions": _open_positions,
            "paper_trades": _paper_trades,
            "realized_pnl_usd": _realized_pnl_usd,
            "saved_at_utc": _utc_now(),
        }, default=str)
        with _connect_database() as conn:
            _ensure_state_table(conn)
            conn.execute("""
                INSERT INTO memecoin_paper_state (id, state, updated_at)
                VALUES (1, %s::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE
                SET state = EXCLUDED.state, updated_at = NOW()
            """, (snapshot,))
        return True
    except Exception as e:
        logger.exception("Save failed: %s", e)
        return False

def load_paper_state():
    global _open_positions, _paper_trades, _realized_pnl_usd
    if not _database_url():
        return {"ok": False, "loaded": False}
    try:
        with _connect_database() as conn:
            _ensure_state_table(conn)
            row = conn.execute("SELECT state FROM memecoin_paper_state WHERE id = 1").fetchone()
        if not row:
            return {"ok": True, "loaded": False}
        state = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        _open_positions = state.get("open_positions", {})
        _paper_trades = state.get
