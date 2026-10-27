"""Paper trading engine with basic price diagnostics."""
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
        _paper_trades = state.get("paper_trades", [])
        _realized_pnl_usd = _num(state.get("realized_pnl_usd"))
        return {"ok": True, "loaded": True}
    except Exception as e:
        logger.exception("Load failed")
        return {"ok": False, "error": str(e)}

async def get_token_prices_usd(token_addresses, reference_prices=None):
    addresses = [str(a) for a in token_addresses if a]
    if not addresses:
        return {}
    results = {}
    async with httpx.AsyncClient(timeout=12) as client:
        for offset in range(0, len(addresses), 30):
            batch = addresses[offset:offset+30]
            url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch)
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                pairs = r.json()
                if not isinstance(pairs, list):
                    continue
                for pair in pairs:
                    if pair.get("chainId") != "solana":
                        continue
                    base = pair.get("baseToken", {})
                    addr = base.get("address")
                    price = _num(pair.get("priceUsd"))
                    if addr and price > 0:
                        results[addr] = price
            except Exception:
                continue
    return results

async def get_token_price_usd(token_address):
    prices = await get_token_prices_usd([token_address])
    return prices.get(token_address)

def get_price_diagnostics():
    return {
        "provider": "dexscreener",
        "status": "ok",
        "cached_prices": len(_price_cache),
    }

def record_paper_trade(token_address, side, amount_usd, price_usd, token_symbol="", source="manual", reason=None):
    global _realized_pnl_usd
    side = side.upper()
    amount_usd = _num(amount_usd)
    price_usd = _num(price_usd)
    if side not in ("BUY", "SELL") or amount_usd <= 0 or price_usd <= 0:
        raise ValueError("Invalid trade parameters")

    trade = {
        "token_address": token_address,
        "token_symbol": token_symbol or token_address[:8],
        "side": side,
        "amount_usd": round(amount_usd, 4),
        "price_usd": price_usd,
        "source": source,
        "reason": reason,
        "timestamp": time.time(),
    }

    if side == "BUY":
        qty = amount_usd / price_usd
        pos = _open_positions.get(token_address)
        if pos is None:
            _open_positions[token_address] = {
                "token_address": token_address,
                "token_symbol": trade["token_symbol"],
                "entry_price_usd": price_usd,
                "current_price_usd": price_usd,
                "highest_price": price_usd,
                "invested_usd": amount_usd,
                "quantity": qty,
                "opened_at": time.time(),
            }
        else:
            old_qty = _num(pos.get("quantity"))
            total_qty = old_qty + qty
            pos["entry_price_usd"] = (pos["entry_price_usd"] * old_qty + price_usd * qty) / total_qty
            pos["quantity"] = total_qty
            pos["invested_usd"] += amount_usd
            pos["current_price_usd"] = price_usd
            pos["highest_price"] = max(pos.get("highest_price", price_usd), price_usd)
        _paper_trades.append(trade)
    else:
        pos = _open_positions.get(token_address)
        if not pos:
            raise ValueError("No open position")
        qty = _num(pos["quantity"])
        invested = _num(pos["invested_usd"])
        sell_qty = min(qty, amount_usd / price_usd)
        cost = invested * sell_qty / qty
        proceeds = sell_qty * price_usd
        realized = proceeds - cost
        trade["realized_pnl_usd"] = round(realized, 4)
        _paper_trades.append(trade)
        _realized_pnl_usd += realized
        remaining = qty - sell_qty
        if remaining <= 1e-9:
            _open_positions.pop(token_address, None)
        else:
            pos["quantity"] = remaining
            pos["invested_usd"] = invested - cost

    _save_paper_state()
    return {"ok": True, "trade": trade}

def open_paper_position(token_address, token_symbol="", amount_usd=10.0, price_usd=None, source="auto", max_open_positions=None, metadata=None):
    if token_address in _open_positions:
        return {"ok": True, "skipped": True, "reason": "already_open"}
    if max_open_positions and len(_open_positions) >= max_open_positions:
        return {"ok": True, "skipped": True, "reason": "max_positions"}
    result = record_paper_trade(token_address, "BUY", amount_usd, price_usd, token_symbol, source)
    if metadata and token_address in _open_positions:
        _open_positions[token_address]["metadata"] = metadata
    return result

def _close_position(address, position, price, reason, change_pct):
    global _realized_pnl_usd
    qty = _num(position.get("quantity"))
    invested = _num(position.get("invested_usd"))
    if qty <= 0 or price <= 0:
        return False
    proceeds = qty * price
    pnl = proceeds - invested
    trade = {
        "token_address": address,
        "token_symbol": position.get("token_symbol"),
        "side": "SELL",
        "amount_usd": round(proceeds, 4),
        "price_usd": price,
        "source": "auto",
        "reason": reason,
        "realized_pnl_usd": round(pnl, 4),
        "change_pct": round(change_pct, 2),
        "timestamp": time.time(),
    }
    _paper_trades.append(trade)
    _realized_pnl_usd += pnl
    _open_positions.pop(address, None)
    _save_paper_state()
    return True

async def update_paper_prices():
    closed = 0
    addresses = list(_open_positions.keys())
    if not addresses:
        return {"updated": 0, "closed": 0}

    prices = await get_token_prices_usd(addresses)
    for address in addresses:
        pos = _open_positions.get(address)
        if not pos:
            continue
        price = prices.get(address)
        if not price or price <= 0:
            continue

        entry = _num(pos.get("entry_price_usd"))
        if entry <= 0:
            continue
        change = (price / entry - 1) * 100
        opened = _num(pos.get("opened_at"))
        if time.time() - opened < MIN_HOLD_SECONDS:
            pos["current_price_usd"] = price
            continue

        highest = max(pos.get("highest_price", entry), price)
        pos["highest_price"] = highest
        pos["current_price_usd"] = price

        if change > 8:
            trail = highest * (1 - TRAILING_STOP_PCT / 100)
            if price <= trail:
                if _close_position(address, pos, price, "trailing_stop", change):
                    closed += 1
                    continue

        if change >= TAKE_PROFIT_PCT:
            if _close_position(address, pos, price, "take_profit", change):
                closed += 1
        elif change <= STOP_LOSS_PCT:
            if _close_position(address, pos, price, "stop_loss", change):
                closed += 1

    _save_paper_state()
    return {"updated": len(addresses), "closed": closed}

def get_paper_status():
    invested = sum(_num(p.get("invested_usd")) for p in _open_positions.values())
    current = sum(_num(p.get("quantity")) * _num(p.get("current_price_usd")) for p in _open_positions.values())
    unrealized = current - invested
    return {
        "mode": "paper",
        "real_trading_enabled": False,
        "open_positions": len(_open_positions),
        "paper_invested_usd": round(invested, 2),
        "paper_current_value_usd": round(current, 2),
        "paper_unrealized_pnl_usd": round(unrealized, 2),
        "paper_realized_pnl_usd": round(_realized_pnl_usd, 2),
        "paper_total_pnl_usd": round(unrealized + _realized_pnl_usd, 2),
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "trailing_stop_pct": TRAILING_STOP_PCT,
    }

def get_paper_trades():
    return list(_paper_trades)

def paper_status():
    return get_paper_status()

def list_paper_trades():
    return get_paper_trades()
