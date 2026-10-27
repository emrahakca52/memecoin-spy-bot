"""Paper engine with Jupiter + DexScreener + Telegram."""
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
    JUPITER_QUOTE_URL, JUPITER_SLIPPAGE_BPS, USDC_MINT,
)

logger = logging.getLogger("memecoin_spy.paper_engine")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_open_positions = {}
_paper_trades = []
_realized_pnl_usd = 0.0

def _num(value, default=0.0):
    try:
        n = float(value or 0)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError, OverflowError):
        return default

async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            })
    except Exception:
        pass

def _database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        return "postgresql://" + url[11:]
    return url

def _connect_database():
    url = _database_url()
    return psycopg.connect(url, connect_timeout=8, autocommit=True) if url else None

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
        return
    try:
        snapshot = json.dumps({
            "open_positions": _open_positions,
            "paper_trades": _paper_trades,
            "realized_pnl_usd": _realized_pnl_usd,
        }, default=str)
        with _connect_database() as conn:
            _ensure_state_table(conn)
            conn.execute("""
                INSERT INTO memecoin_paper_state (id, state) VALUES (1, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()
            """, (snapshot,))
    except Exception as e:
        logger.exception("Save error: %s", e)

def load_paper_state():
    global _open_positions, _paper_trades, _realized_pnl_usd
    if not _database_url():
        return {"ok": False}
    try:
        with _connect_database() as conn:
            _ensure_state_table(conn)
            row = conn.execute("SELECT state FROM memecoin_paper_state WHERE id = 1").fetchone()
        if row:
            state = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            _open_positions = state.get("open_positions", {})
            _paper_trades = state.get("paper_trades", [])
            _realized_pnl_usd = _num(state.get("realized_pnl_usd"))
        return {"ok": True}
    except Exception:
        return {"ok": False}

async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int) -> dict | None:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": JUPITER_SLIPPAGE_BPS,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(JUPITER_QUOTE_URL, params=params)
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None

async def get_token_price_usd(token_address: str) -> float | None:
    quote = await get_jupiter_quote(token_address, USDC_MINT, 1_000_000)
    if quote and "outAmount" in quote:
        try:
            out = int(quote["outAmount"])
            price = out / 1_000_000
            if price > 0:
                return price
        except Exception:
            pass

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"https://api.dexscreener.com/tokens/v1/solana/{token_address}")
            if r.status_code == 200:
                pairs = r.json()
                if isinstance(pairs, list) and pairs:
                    best = max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")))
                    price = _num(best.get("priceUsd"))
                    if price > 0:
                        return price
    except Exception:
        pass
    return None

async def get_token_prices_usd(token_addresses, reference_prices=None):
    results = {}
    for addr in token_addresses:
        price = await get_token_price_usd(addr)
        if price and price > 0:
            results[addr] = price
    return results

def get_price_diagnostics():
    return {"provider": "jupiter+dexscreener", "status": "ok"}

def record_paper_trade(token_address, side, amount_usd, price_usd, token_symbol="", source="manual", reason=None):
    global _realized_pnl_usd
    side = side.upper()
    amount_usd = _num(amount_usd)
    price_usd = _num(price_usd)
    if side not in ("BUY", "SELL") or amount_usd <= 0 or price_usd <= 0:
        raise ValueError("Invalid params")

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
            total = old_qty + qty
            pos["entry_price_usd"] = (pos["entry_price_usd"] * old_qty + price_usd * qty) / total
            pos["quantity"] = total
            pos["invested_usd"] += amount_usd
            pos["highest_price"] = max(pos.get("highest_price", price_usd), price_usd)
        _paper_trades.append(trade)
        asyncio.create_task(send_telegram(f"🟢 <b>BUY</b>\n{token_symbol}\n${amount_usd:.2f} @ {price_usd}"))
    else:
        pos = _open_positions.get(token_address)
        if not pos:
            raise ValueError("No position")
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
        asyncio.create_task(send_telegram(f"🔴 <b>SELL</b>\n{token_symbol}\nP/L: {realized:.2f}$"))

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
    asyncio.create_task(send_telegram(
        f"🔴 <b>SELL</b> ({reason})\n{position.get('token_symbol')}\n"
        f"P/L: {pnl:.2f}$ ({change_pct:.1f}%)"
    ))
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
        if time.time() - _num(pos.get("opened_at")) < MIN_HOLD_SECONDS:
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
        "provider": "jupiter+dexscreener",
    }

def get_paper_trades():
    return list(_paper_trades)

def paper_status():
    return get_paper_status()

def list_paper_trades():
    return get_paper_trades()


# --- Safety, accounting reconciliation, and conservative paper valuation ---
_database_status = {}
PAPER_FEE_PCT_PER_SIDE = float(os.getenv("PAPER_FEE_PCT_PER_SIDE", "0"))
PAPER_SLIPPAGE_PCT_PER_SIDE = float(os.getenv("PAPER_SLIPPAGE_PCT_PER_SIDE", "0"))
STALE_PRICE_THRESHOLD_SECONDS = max(60, int(os.getenv("STALE_PRICE_THRESHOLD_SECONDS", "900")))
_ORIGINAL_RECORD_PAPER_TRADE = record_paper_trade

def _stale_price_threshold_seconds():
    return STALE_PRICE_THRESHOLD_SECONDS

async def _fetch_dexscreener_price(token_address):
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(f"https://api.dexscreener.com/tokens/v1/solana/{token_address}")
            response.raise_for_status()
            pairs = response.json()
        if not isinstance(pairs, list) or not pairs:
            return None
        best = max(pairs, key=lambda p: _num((p.get("liquidity") or {}).get("usd")))
        price = _num(best.get("priceUsd"))
        return price if price > 0 else None
    except Exception:
        return None

def get_persistence_status():
    return {"connected": bool(_database_url()), "configured": bool(_database_url())}

def _reported_realized_pnl(trades):
    realized = 0.0
    quarantined = []
    for trade in trades or []:
        if not isinstance(trade, dict) or str(trade.get("side", "")).upper() != "SELL":
            continue
        symbol = str(trade.get("token_symbol", "")).upper()
        address = str(trade.get("token_address", ""))
        pnl = _num(trade.get("realized_pnl_usd"))
        suspicious_mrbeast = symbol == "MRBEAST" and address == "4hqN5vhCG1q13qYUQvBhyMDDnpWKP98DoswDeDCVQaCn" and abs(pnl) > 100000
        extreme_legacy_auto_exit = (
            str(trade.get("source", "")).lower() == "auto"
            and str(trade.get("reason", "")).lower() in {"stop_loss_5pct", "take_profit_10pct"}
            and abs(_num(trade.get("observed_change_pct", trade.get("change_pct")))) > 30
        )
        if trade.get("accounting_quarantined") or suspicious_mrbeast or extreme_legacy_auto_exit:
            quarantined.append(dict(trade))
            continue
        if "realized_pnl_usd" in trade and math.isfinite(_num(trade.get("realized_pnl_usd"))):
            realized += pnl
    return {"realized_pnl_usd": round(realized, 4), "quarantined_sell_records": quarantined}

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
        positions = state.get("open_positions", {})
        trades = state.get("paper_trades", [])
        if not isinstance(positions, dict): positions = {}
        if not isinstance(trades, list): trades = []
        _open_positions = positions
        _paper_trades = trades
        accounting = _reported_realized_pnl(trades)
        quarantined_ids = {id(t) for t in []}
        quarantined_keys = {(str(t.get("token_address","")), str(t.get("token_symbol","")), str(t.get("timestamp","")), str(t.get("realized_pnl_usd",""))) for t in accounting["quarantined_sell_records"]}
        for trade in trades:
            if isinstance(trade, dict):
                key = (str(trade.get("token_address","")), str(trade.get("token_symbol","")), str(trade.get("timestamp","")), str(trade.get("realized_pnl_usd","")))
                if key in quarantined_keys:
                    trade["accounting_quarantined"] = True
        sell_rows = [t for t in trades if isinstance(t, dict) and str(t.get("side","")).upper()=="SELL"]
        complete = all("realized_pnl_usd" in t for t in sell_rows)
        if complete:
            _realized_pnl_usd = accounting["realized_pnl_usd"]
            _database_status.pop("accounting_warning", None)
        else:
            _realized_pnl_usd = _num(state.get("realized_pnl_usd"))
            _database_status["accounting_warning"] = "Ledger lacks realized_pnl_usd on one or more SELL records; preserved stored lifetime PnL."
        if accounting["quarantined_sell_records"]:
            _database_status["accounting_warning"] = "One or more historical SELL records were quarantined from reported PnL; history was preserved."
        _save_paper_state()
        return {"ok": True, "loaded": True}
    except Exception as exc:
        _database_status["load_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {"ok": False, "loaded": False}

def record_paper_trade(token_address, side, amount_usd, price_usd, token_symbol="", source="manual", reason=None):
    result = _ORIGINAL_RECORD_PAPER_TRADE(token_address, side, amount_usd, price_usd, token_symbol, source, reason)
    trade = result.get("trade", {})
    fee_pct = max(0.0, _num(PAPER_FEE_PCT_PER_SIDE))
    slip_pct = max(0.0, _num(PAPER_SLIPPAGE_PCT_PER_SIDE))
    cost_pct = (fee_pct + slip_pct) / 100.0
    if str(side).upper() == "BUY":
        pos = _open_positions.get(token_address)
        if pos is not None:
            pos["estimated_entry_cost_usd"] = round(_num(pos.get("estimated_entry_cost_usd")) + _num(amount_usd) * cost_pct, 6)
    elif str(side).upper() == "SELL":
        proceeds = _num(amount_usd)
        exit_cost = proceeds * cost_pct
        entry_cost = 0.0
        if token_address in _open_positions:
            entry_cost = _num(_open_positions[token_address].get("estimated_entry_cost_usd"))
        trade["estimated_exit_cost_usd"] = round(exit_cost, 6)
        trade["estimated_transaction_cost_usd"] = round(exit_cost + entry_cost, 6)
        trade["net_realized_pnl_usd"] = round(_num(trade.get("realized_pnl_usd")) - exit_cost - entry_cost, 6)
    _save_paper_state()
    return result

async def update_paper_prices():
    closed = 0
    addresses = list(_open_positions.keys())
    if not addresses:
        return {"updated": 0, "closed": 0}
    prices = await get_token_prices_usd(addresses)
    for address in addresses:
        pos = _open_positions.get(address)
        if not pos: continue
        raw_price = prices.get(address)
        price = _num(raw_price, 0)
        if price <= 0: continue
        entry = _num(pos.get("entry_price_usd"))
        if entry <= 0: continue
        change = (price / entry - 1) * 100.0
        current = _num(pos.get("current_price_usd"))
        # A move exceeding 50% in one refresh is untrusted until a second source confirms it.
        if current > 0 and abs(change) > 50:
            verify = await _fetch_dexscreener_price(address)
            if verify is None or abs(verify / price - 1) * 100 > 5:
                pos["price_mark_status"] = "unverified_extreme_quote"
                pos["price_safety_warning"] = "Extreme quote not independently confirmed; kept prior mark and blocked auto-exit."
                continue
            price = verify
            change = (price / entry - 1) * 100.0
        pos["current_price_usd"] = price
        pos["last_price_update_utc"] = datetime.now(timezone.utc).isoformat()
        pos["price_mark_status"] = "verified"
        pos.pop("price_safety_warning", None)
        pos["unrealized_pnl_usd"] = _num(pos.get("quantity")) * price - _num(pos.get("invested_usd"))
        if time.time() - _num(pos.get("opened_at")) < MIN_HOLD_SECONDS:
            continue
        highest = max(_num(pos.get("highest_price"), entry), price)
        pos["highest_price"] = highest
        if change > 8 and price <= highest * (1 - TRAILING_STOP_PCT / 100):
            if _close_position(address, pos, price, "trailing_stop", change): closed += 1
        elif change >= TAKE_PROFIT_PCT:
            if _close_position(address, pos, price, "take_profit", change): closed += 1
        elif change <= STOP_LOSS_PCT:
            if _close_position(address, pos, price, "stop_loss_5pct" if change <= -5 and STOP_LOSS_PCT <= -5 else "stop_loss", change): closed += 1
    _save_paper_state()
    return {"updated": len(addresses), "closed": closed}

def get_paper_status():
    now = datetime.now(timezone.utc)
    invested = current = unrealized = 0.0
    stale_count = stale_invested = 0
    rows = []
    for address, pos in _open_positions.items():
        row = dict(pos)
        mark = _num(pos.get("current_price_usd"))
        stamp = pos.get("last_price_update_utc")
        stale = False
        if stamp:
            try:
                parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=timezone.utc)
                stale = (now - parsed).total_seconds() > _stale_price_threshold_seconds()
            except (TypeError, ValueError):
                stale = True
        elif _num(pos.get("opened_at")) and time.time() - _num(pos.get("opened_at")) > _stale_price_threshold_seconds():
            stale = True
        amount = _num(pos.get("invested_usd"))
        invested += amount
        if stale:
            stale_count += 1
            stale_invested += amount
            row["price_mark_status"] = "stale_price_mark"
            row["current_price_usd"] = None
        else:
            current += _num(pos.get("quantity")) * mark
            unrealized += _num(pos.get("quantity")) * mark - amount
        rows.append(row)
    accounting = _reported_realized_pnl(_paper_trades)
    legacy_cost_missing = any(
        isinstance(t, dict) and str(t.get("side","")).upper() == "SELL"
        and ("estimated_exit_cost_usd" not in t or "estimated_transaction_cost_usd" not in t)
        for t in _paper_trades
    )
    partial_net = sum(_num(t.get("net_realized_pnl_usd", t.get("realized_pnl_usd"))) for t in _paper_trades if isinstance(t, dict) and str(t.get("side","")).upper()=="SELL" and not t.get("accounting_quarantined"))
    total_incomplete = stale_count > 0 or bool(accounting["quarantined_sell_records"])
    status = {
        "mode":"paper", "real_trading_enabled":False, "open_positions":rows,
        "open_position_count":len(rows), "paper_invested_usd":round(invested,2),
        "paper_current_value_usd":round(current,2), "paper_unrealized_pnl_usd":round(unrealized,2),
        "paper_realized_pnl_usd":round(accounting["realized_pnl_usd"],2),
        "paper_total_pnl_usd":None if total_incomplete else round(unrealized+accounting["realized_pnl_usd"],2),
        "paper_total_pnl_incomplete":total_incomplete,
        "stale_price_position_count":stale_count,"stale_price_position_invested_usd":round(stale_invested,2),
        "accounting":accounting,"provider":"jupiter+dexscreener",
        "paper_estimated_net_pnl_incomplete":legacy_cost_missing,
        "paper_estimated_net_total_pnl_usd":None if legacy_cost_missing else round(partial_net+unrealized,2),
        "paper_estimated_net_total_pnl_partial_usd":round(partial_net+unrealized,2),
    }
    return status
