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
    PARTIAL_TAKE_PROFIT_PCT, PARTIAL_SELL_FRACTION, MAX_HOLD_SECONDS,
    MIN_HOLD_SECONDS, MAX_DAILY_LOSS_USD, MAX_OPEN_POSITIONS,
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

# Paper-only starting capital; adjustable in Render environment variables.
_paper_starting_balance_usd = max(0.0, _num(os.getenv("PAPER_STARTING_BALANCE_USD", "500"), 500.0))

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
            "starting_balance_usd": _paper_starting_balance_usd,
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
    global _open_positions, _paper_trades, _realized_pnl_usd, _paper_starting_balance_usd
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

async def _fetch_geckoterminal_price(token_address: str) -> float | None:
    """Fallback USD mark when DexScreener is rate-limited or has no pair."""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{token_address}/pools"
    try:
        async with httpx.AsyncClient(timeout=8, headers={"accept": "application/json"}) as client:
            response = await client.get(url, params={"page": "1"})
            response.raise_for_status()
            payload = response.json()
        pools = (payload.get("data") or []) if isinstance(payload, dict) else []
        ranked = []
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            attrs = pool.get("attributes") or {}
            rel = pool.get("relationships") or {}
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
            quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "")
            base_price = _num(attrs.get("base_token_price_usd"))
            quote_price = _num(attrs.get("quote_token_price_usd"))
            price = base_price if base.endswith("_" + token_address) else (quote_price if quote.endswith("_" + token_address) else 0)
            liquidity = _num(attrs.get("reserve_in_usd"))
            if price > 0:
                ranked.append((liquidity, price))
        if ranked:
            return max(ranked, key=lambda row: row[0])[1]
    except Exception as exc:
        logger.debug("GeckoTerminal price fallback failed for %s: %s", token_address, exc)
    return None

async def _fetch_jupiter_price(token_address: str) -> float | None:
    """Third-party fallback: Jupiter Price API v3, returning only fresh positive USD marks."""
    url = "https://api.jup.ag/price/v3"
    headers = {"accept": "application/json", "user-agent": "MemecoinSpyPro/1.0"}
    api_key = os.getenv("JUPITER_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            response = await client.get(url, params={"ids": token_address})
            response.raise_for_status()
            payload = response.json()
        item = payload.get(token_address, {}) if isinstance(payload, dict) else {}
        price = _num(item.get("usdPrice")) if isinstance(item, dict) else 0.0
        if price > 0:
            return price
    except Exception as exc:
        logger.debug("Jupiter price fallback failed for %s: %s", token_address, type(exc).__name__)
    return None


async def get_token_price_usd(token_address: str) -> float | None:
    # Use independent providers in sequence; never substitute a stale scan price
    # for a live observation because that would fake momentum confirmation.
    for provider in (_fetch_dexscreener_price, _fetch_geckoterminal_price, _fetch_jupiter_price):
        price = await provider(token_address)
        if price is not None and price > 0:
            return price
    logger.warning("No reliable USD price available for %s; skipping paper entry/mark", token_address)
    return None

async def get_token_prices_usd(token_addresses, reference_prices=None):
    results = {}
    for addr in token_addresses:
        price = await get_token_price_usd(addr)
        if price and price > 0:
            results[addr] = price
    return results

def get_price_diagnostics():
    return {"provider": "DexScreener", "status": "price_lookup_on_demand"}

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
                "initial_invested_usd": amount_usd,
                "quantity": qty,
                "stop_reference_price": price_usd,
                "averaging_done": False,
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
        _schedule_telegram(send_telegram(f"🟢 <b>BUY</b>\n{token_symbol}\n${amount_usd:.2f} @ {price_usd}"))
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
        _schedule_telegram(send_telegram(f"🔴 <b>SELL</b>\n{token_symbol}\nP/L: {realized:.2f}$"))

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
    # Idempotency guard: concurrent price-update cycles may hold the same
    # position reference across awaited provider calls. Only the cycle that
    # still owns the currently-open position may book the exit.
    if _open_positions.get(address) is not position:
        logger.warning("Ignoring duplicate/stale close attempt for %s (%s)", address, reason)
        return False
    qty = _num(position.get("quantity"))
    invested = _num(position.get("invested_usd"))
    if qty <= 0 or price <= 0:
        return False
    proceeds = qty * price
    pnl = proceeds - invested
    fee_pct = max(0.0, _num(PAPER_FEE_PCT_PER_SIDE))
    slip_pct = max(0.0, _num(PAPER_SLIPPAGE_PCT_PER_SIDE))
    exit_cost = proceeds * (fee_pct + slip_pct) / 100.0
    entry_cost = _num(position.get("estimated_entry_cost_usd"))
    trade = {
        "token_address": address,
        "token_symbol": position.get("token_symbol"),
        "side": "SELL",
        "amount_usd": round(proceeds, 4),
        "price_usd": price,
        "source": "auto",
        "reason": reason,
        "realized_pnl_usd": round(pnl, 4),
        "estimated_entry_cost_allocated_usd": round(entry_cost, 6),
        "estimated_exit_cost_usd": round(exit_cost, 6),
        "estimated_transaction_cost_usd": round(entry_cost + exit_cost, 6),
        "net_realized_pnl_usd": round(pnl - entry_cost - exit_cost, 6),
        "change_pct": round(change_pct, 2),
        "timestamp": time.time(),
    }
    _paper_trades.append(trade)
    _realized_pnl_usd += pnl
    _open_positions.pop(address, None)
    _save_paper_state()
    _schedule_telegram(send_telegram(
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
        "provider": "DexScreener",
    }

def get_paper_trades():
    return list(_paper_trades)

def paper_status():
    return get_paper_status()

def list_paper_trades():
    return get_paper_trades()


# --- Safety, accounting reconciliation, and conservative paper valuation ---
_database_status = {}
PAPER_FEE_PCT_PER_SIDE = max(0.0, _num(os.getenv("PAPER_FEE_PCT_PER_SIDE", "0.25"), 0.25))
PAPER_SLIPPAGE_PCT_PER_SIDE = max(0.0, _num(os.getenv("PAPER_SLIPPAGE_PCT_PER_SIDE", "0.50"), 0.50))
STALE_PRICE_THRESHOLD_SECONDS = max(60, int(os.getenv("STALE_PRICE_THRESHOLD_SECONDS", "900")))
MAX_STOP_LOSS_OVERSHOOT_PCT = min(10.0, max(5.0, _num(os.getenv("MAX_STOP_LOSS_OVERSHOOT_PCT", 8.0), 8.0)))
MAX_TAKE_PROFIT_OVERSHOOT_PCT = min(50.0, max(10.0, _num(os.getenv("MAX_TAKE_PROFIT_OVERSHOOT_PCT", 25.0), 25.0)))
_ORIGINAL_RECORD_PAPER_TRADE = record_paper_trade

def _schedule_telegram(coroutine):
    try:
        asyncio.get_running_loop().create_task(coroutine)
    except RuntimeError:
        coroutine.close()

def _stale_price_threshold_seconds():
    return STALE_PRICE_THRESHOLD_SECONDS

def _price_mark_age_seconds(position):
    stamp = (position or {}).get("last_price_update_utc")
    if stamp:
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
        except (TypeError, ValueError):
            return float("inf")
    opened = _num((position or {}).get("opened_at"))
    return max(0.0, time.time() - opened) if opened > 0 else float("inf")

async def _fetch_dexscreener_price(token_address):
    """Fetch a USD mark through both DexScreener response formats before Gecko fallback."""
    urls = (
        f"https://api.dexscreener.com/tokens/v1/solana/{token_address}",
        f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
    )
    async with httpx.AsyncClient(timeout=8) as client:
        for url in urls:
            try:
                response = await client.get(url, headers={"accept": "application/json"})
                if response.status_code == 429:
                    logger.warning("DexScreener rate limited price lookup for %s", token_address)
                    continue
                response.raise_for_status()
                payload = response.json()
                # /tokens/v1 returns a list; /latest/dex/tokens returns {"pairs": [...]}.
                pairs = payload if isinstance(payload, list) else (payload.get("pairs") or [] if isinstance(payload, dict) else [])
                if not isinstance(pairs, list) or not pairs:
                    continue
                ranked = []
                for pair in pairs:
                    if not isinstance(pair, dict):
                        continue
                    # Only accept Solana pairs and valid positive USD marks.
                    chain = str(pair.get("chainId") or "solana").lower()
                    price = _num(pair.get("priceUsd"))
                    liquidity = _num((pair.get("liquidity") or {}).get("usd"))
                    if chain == "solana" and price > 0:
                        ranked.append((liquidity, price))
                if ranked:
                    return max(ranked, key=lambda row: row[0])[1]
            except Exception as exc:
                logger.debug("DexScreener price endpoint failed for %s (%s): %s", token_address, url.rsplit("/", 1)[-1], type(exc).__name__)
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
    global _open_positions, _paper_trades, _realized_pnl_usd, _paper_starting_balance_usd
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
        _paper_starting_balance_usd = max(0.0, _num(state.get("starting_balance_usd", os.getenv("PAPER_STARTING_BALANCE_USD", "500")), 500.0))

        # Backfill estimated round-trip costs for legacy SELL rows. Older versions
        # recorded gross realized PnL but did not persist fee/slippage fields.
        # The SELL proceeds and gross PnL imply the allocated entry cost basis:
        # cost_basis = proceeds - gross_pnl. Preserve all original trade fields
        # and gross PnL; explicitly label the reconstructed costs as estimates.
        cost_rate = (
            max(0.0, _num(PAPER_FEE_PCT_PER_SIDE))
            + max(0.0, _num(PAPER_SLIPPAGE_PCT_PER_SIDE))
        ) / 100.0
        migrated_cost_rows = 0
        for trade in trades:
            if not isinstance(trade, dict) or str(trade.get("side", "")).upper() != "SELL":
                continue
            if "estimated_transaction_cost_usd" in trade:
                continue
            if "realized_pnl_usd" not in trade or not math.isfinite(_num(trade.get("realized_pnl_usd"), float("nan"))):
                continue
            proceeds = _num(trade.get("amount_usd"), -1.0)
            gross_pnl = _num(trade.get("realized_pnl_usd"), float("nan"))
            cost_basis = proceeds - gross_pnl
            if proceeds < 0 or cost_basis < 0 or not math.isfinite(cost_basis):
                continue
            entry_cost = cost_basis * cost_rate
            exit_cost = proceeds * cost_rate
            trade["estimated_entry_cost_allocated_usd"] = round(entry_cost, 6)
            trade["estimated_exit_cost_usd"] = round(exit_cost, 6)
            trade["estimated_transaction_cost_usd"] = round(entry_cost + exit_cost, 6)
            trade["net_realized_pnl_usd"] = round(gross_pnl - entry_cost - exit_cost, 6)
            trade["cost_estimate_method"] = "legacy_gross_pnl_implied_cost_basis"
            trade["costs_are_estimated"] = True
            migrated_cost_rows += 1

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
            _database_status["accounting_warning"] = "Ledger lack realized_pnl_usd on one or more SELL records; preserved stored lifetime PnL."
        if accounting["quarantined_sell_records"]:
            _database_status["accounting_warning"] = "One or more historical SELL records were quarantined from reported PnL; history was preserved."
        _save_paper_state()
        return {"ok": True, "loaded": True}
    except Exception as exc:
        _database_status["load_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        return {"ok": False, "loaded": False}

def _daily_realized_pnl_usd(now=None):
    """Today's non-quarantined realized paper PnL, using UTC day boundaries."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return round(sum(
        _num(t.get("realized_pnl_usd")) for t in _paper_trades
        if isinstance(t, dict) and str(t.get("side", "")).upper() == "SELL"
        and not t.get("accounting_quarantined") and "realized_pnl_usd" in t
        and _num(t.get("timestamp")) >= start
    ), 4)


def _paper_available_cash_usd():
    committed = sum(_num(p.get("invested_usd")) for p in _open_positions.values())
    return round(_paper_starting_balance_usd + _realized_pnl_usd - committed, 6)


def record_paper_trade(token_address, side, amount_usd, price_usd, token_symbol="", source="manual", reason=None):
    normalized_side = str(side).upper()
    amount = _num(amount_usd, -1.0)
    if normalized_side == "BUY":
        if amount <= 0:
            raise ValueError("Invalid paper order amount")
        if token_address not in _open_positions and len(_open_positions) >= MAX_OPEN_POSITIONS:
            raise ValueError("Paper wallet risk limit: maximum open positions reached")
        if MAX_DAILY_LOSS_USD > 0 and _daily_realized_pnl_usd() <= -abs(MAX_DAILY_LOSS_USD):
            raise ValueError("Paper wallet risk limit: daily realized loss limit reached; new entries paused")
        available = _paper_available_cash_usd()
        if amount > available + 1e-8:
            raise ValueError(f"Insufficient paper cash: requested ${amount:.2f}, available ${max(0.0, available):.2f}")
    pos_before = _open_positions.get(token_address)
    entry_cost = _num((pos_before or {}).get("estimated_entry_cost_usd"))
    # Allocate entry costs only to the fraction actually sold; partial exits must
    # not charge the same entry cost repeatedly.
    qty_before = _num((pos_before or {}).get("quantity"))
    sell_qty_before = min(qty_before, amount / _num(price_usd)) if normalized_side == "SELL" and _num(price_usd) > 0 else 0.0
    entry_cost_allocated = entry_cost * (sell_qty_before / qty_before) if qty_before > 0 else 0.0
    result = _ORIGINAL_RECORD_PAPER_TRADE(token_address, side, amount_usd, price_usd, token_symbol, source, reason)
    trade = result.get("trade", {})
    fee_pct = max(0.0, _num(PAPER_FEE_PCT_PER_SIDE))
    slip_pct = max(0.0, _num(PAPER_SLIPPAGE_PCT_PER_SIDE))
    cost_pct = (fee_pct + slip_pct) / 100.0
    if normalized_side == "BUY":
        pos = _open_positions.get(token_address)
        if pos is not None:
            pos["estimated_entry_cost_usd"] = round(_num(pos.get("estimated_entry_cost_usd")) + _num(amount_usd) * cost_pct, 6)
    elif normalized_side == "SELL":
        proceeds = _num(trade.get("amount_usd", amount_usd))
        exit_cost = proceeds * cost_pct
        trade["estimated_entry_cost_allocated_usd"] = round(entry_cost_allocated, 6)
        trade["estimated_exit_cost_usd"] = round(exit_cost, 6)
        trade["estimated_transaction_cost_usd"] = round(exit_cost + entry_cost_allocated, 6)
        trade["net_realized_pnl_usd"] = round(_num(trade.get("realized_pnl_usd")) - exit_cost - entry_cost_allocated, 6)
        remaining_pos = _open_positions.get(token_address)
        if remaining_pos is not None:
            remaining_pos["estimated_entry_cost_usd"] = round(max(0.0, entry_cost - entry_cost_allocated), 6)
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
        # Track the drawdown low; allow one small paper add only after a
        # confirmed 2% rebound from that low, never while price is still falling.
        if not pos.get("averaging_done") and change <= -8.0:
            low = _num(pos.get("averaging_low_price"), price)
            pos["averaging_low_price"] = min(low, price)
        averaging_low = _num(pos.get("averaging_low_price"))
        if (
            not pos.get("averaging_done")
            and averaging_low > 0
            and price >= averaging_low * 1.02
            and change > -12.0
        ):
            add_amount = min(4.0, _num(pos.get("initial_invested_usd"), 8.0) * 0.5)
            try:
                record_paper_trade(
                    address, "BUY", add_amount, price,
                    pos.get("token_symbol", ""), "auto", "controlled_cost_averaging"
                )
                pos = _open_positions.get(address)
                if pos:
                    pos["stop_reference_price"] = _num(pos.get("stop_reference_price"), entry)
                    pos["averaging_done"] = True
                    pos["averaging_price_usd"] = price
                    pos["averaging_low_price"] = averaging_low
                    _save_paper_state()
                    logger.info("One-time paper cost average: %s at %.2f", address, price)
                    entry = _num(pos.get("entry_price_usd"), entry)
                    change = (price / entry - 1.0) * 100.0
            except Exception as exc:
                logger.info("Cost averaging skipped for %s: %s", address, str(exc)[:160])
                pos["averaging_done"] = True
                _save_paper_state()
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
        age_seconds = max(0.0, time.time() - _num(pos.get("opened_at")))
        if age_seconds < MIN_HOLD_SECONDS:
            continue

        highest = max(_num(pos.get("highest_price"), entry), price)
        pos["highest_price"] = highest

        # Priority 1: hard stop. Cost averaging never moves this original stop.
        stop_reference = _num(pos.get("stop_reference_price"), entry)
        stop_change = (price / stop_reference - 1.0) * 100.0 if stop_reference > 0 else change
        if stop_change <= STOP_LOSS_PCT:
            if _close_position(address, pos, price, "stop_loss", change):
                closed += 1
            continue

        # Inspired by strategy bots that scale out instead of waiting for one
        # distant target: realize half at the first target, then manage the rest.
        if change >= PARTIAL_TAKE_PROFIT_PCT and not pos.get("partial_take_profit_done"):
            fraction = min(0.9, max(0.1, _num(PARTIAL_SELL_FRACTION, 0.5)))
            sell_proceeds = _num(pos.get("quantity")) * fraction * price
            try:
                record_paper_trade(
                    address, "SELL", sell_proceeds, price,
                    pos.get("token_symbol", ""), "auto", "partial_take_profit"
                )
                pos = _open_positions.get(address)
                if pos:
                    pos["partial_take_profit_done"] = True
                    pos["highest_price"] = max(_num(pos.get("highest_price"), entry), price)
                    pos["breakeven_stop_active"] = True
                logger.info("Partial take profit: %s at %.2f%%", address, change)
            except Exception:
                logger.exception("Partial take-profit failed for %s", address)
            if not pos:
                closed += 1
                continue

        # Once partial profit is booked, protect the remaining position near entry.
        if pos.get("breakeven_stop_active") and price <= entry:
            if _close_position(address, pos, price, "breakeven_stop", change):
                closed += 1
            continue

        # Trail the peak after a meaningful move; avoid closing before the first
        # partial target has had a chance to reduce exposure.
        if change >= 8 and price <= highest * (1 - TRAILING_STOP_PCT / 100):
            if _close_position(address, pos, price, "trailing_stop", change):
                closed += 1
            continue

        # A timer alone must never force a sale. Memecoins can remain volatile
        # beyond the observation window; closing solely because time elapsed
        # repeatedly cut winners and crystallized small/temporary drawdowns.
        # MAX_HOLD_SECONDS is retained for backward-compatible configuration
        # and telemetry, but exits are driven by stop, breakeven, trailing stop,
        # and take-profit conditions only.
        if change >= TAKE_PROFIT_PCT:
            if _close_position(address, pos, price, "take_profit_final", change):
                closed += 1
    _save_paper_state()
    return {"updated": len(addresses), "closed": closed}

def get_paper_status():
    invested = current = unrealized = 0.0
    stale_count = stale_invested = 0
    rows = []
    for address, pos in _open_positions.items():
        row = dict(pos)
        mark = _num(pos.get("current_price_usd"))
        stale = _price_mark_age_seconds(pos) > _stale_price_threshold_seconds()
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
    sell_rows = [t for t in _paper_trades if isinstance(t, dict) and str(t.get("side", "")).upper() == "SELL"]
    sell_missing_costs = sum(1 for t in sell_rows if "estimated_transaction_cost_usd" not in t)
    quarantined_sell_costs = sum(1 for t in sell_rows if t.get("accounting_quarantined"))
    open_missing_entry_costs = sum(
        1 for p in _open_positions.values()
        if p.get("estimated_entry_cost_usd") is None or isinstance(p.get("estimated_entry_cost_usd"), bool) or not math.isfinite(_num(p.get("estimated_entry_cost_usd"), float("nan")))
    )
    legacy_cost_missing = sell_missing_costs > 0 or open_missing_entry_costs > 0
    partial_net = sum(
        _num(t.get("net_realized_pnl_usd", _num(t.get("realized_pnl_usd")) - _num(t.get("estimated_transaction_cost_usd"))))
        for t in sell_rows if not t.get("accounting_quarantined")
    )
    total_incomplete = stale_count > 0 or bool(accounting["quarantined_sell_records"])
    cash_available = _paper_available_cash_usd()
    total_equity = None if stale_count > 0 else cash_available + current
    status = {
        "mode": "paper", "real_trading_enabled": False, "open_positions": rows,
        "open_position_count": len(rows), "paper_invested_usd": round(invested, 2),
        "paper_current_value_usd": round(current, 2), "paper_unrealized_pnl_usd": round(unrealized, 2),
        "paper_realized_pnl_usd": round(accounting["realized_pnl_usd"], 2),
        "paper_total_pnl_usd": None if total_incomplete else round(unrealized + accounting["realized_pnl_usd"], 2),
        "paper_total_pnl_incomplete": total_incomplete,
        "stale_price_position_count": stale_count,
        "stale_price_position_invested_usd": round(stale_invested, 2),
        "accounting": accounting, "provider": "jupiter+dexscreener",
        "paper_sell_records_missing_costs": sell_missing_costs,
        "paper_quarantined_sell_cost_records": quarantined_sell_costs,
        "paper_open_positions_missing_entry_costs": open_missing_entry_costs,
        "paper_estimated_net_pnl_incomplete": legacy_cost_missing,
        "paper_estimated_net_total_pnl_usd": None if legacy_cost_missing else round(partial_net + unrealized, 2),
        "paper_estimated_net_total_pnl_partial_usd": round(partial_net + unrealized, 2),
        "paper_starting_balance_usd": round(_paper_starting_balance_usd, 2),
        "paper_cash_available_usd": round(cash_available, 2),
        "paper_cash_committed_usd": round(invested, 2),
        "paper_total_equity_usd": None if total_equity is None else round(total_equity, 2),
        "paper_daily_realized_pnl_usd": _daily_realized_pnl_usd(),
        "paper_daily_loss_limit_usd": round(abs(MAX_DAILY_LOSS_USD), 2),
        "paper_max_open_positions": MAX_OPEN_POSITIONS,
    }
    return status
