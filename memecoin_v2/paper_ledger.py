"""Isolated PostgreSQL paper ledger. Creates only v2-prefixed tables; never alters V1 data."""
from __future__ import annotations
import os
import math
import psycopg
from psycopg.rows import dict_row

class LedgerError(ValueError):
    pass

def _connect():
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=8)

def _starting_balance() -> float:
    try:
        value=float(os.getenv("V2_PAPER_STARTING_BALANCE_USD","500"))
        return value if math.isfinite(value) and 1 <= value <= 1_000_000_000 else 500.0
    except (TypeError,ValueError,OverflowError):
        return 500.0

def initialize():
    """Idempotent, additive schema creation isolated to V2."""
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS v2_paper_account (
                id SMALLINT PRIMARY KEY CHECK (id = 1),
                starting_cash NUMERIC(20,8) NOT NULL CHECK (starting_cash > 0),
                cash NUMERIC(20,8) NOT NULL CHECK (cash >= 0),
                realized_pnl NUMERIC(20,8) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS v2_paper_positions (
                mint TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                quantity NUMERIC(30,12) NOT NULL CHECK (quantity >= 0),
                average_entry NUMERIC(30,12) NOT NULL CHECK (average_entry > 0),
                invested_usd NUMERIC(20,8) NOT NULL CHECK (invested_usd >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS v2_paper_trades (
                id BIGSERIAL PRIMARY KEY,
                mint TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
                quantity NUMERIC(30,12) NOT NULL CHECK (quantity > 0),
                price_usd NUMERIC(30,12) NOT NULL CHECK (price_usd > 0),
                notional_usd NUMERIC(20,8) NOT NULL CHECK (notional_usd > 0),
                realized_pnl_usd NUMERIC(20,8) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute("INSERT INTO v2_paper_account(id,starting_cash,cash) VALUES (1,%s,%s) ON CONFLICT (id) DO NOTHING",
                     (_starting_balance(), _starting_balance()))

def buy(mint: str, symbol: str, price: float, amount_usd: float, daily_loss_limit: float):
    if not mint or len(mint) > 64 or not symbol or len(symbol) > 20:
        raise LedgerError("Invalid mint or symbol")
    if not math.isfinite(price) or not math.isfinite(amount_usd) or price <= 0 or amount_usd <= 0:
        raise LedgerError("Price and amount must be positive")
    with _connect() as conn:
        account = conn.execute("SELECT * FROM v2_paper_account WHERE id=1 FOR UPDATE").fetchone()
        if not account:
            raise RuntimeError("Paper ledger is not initialized")
        today = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) AS pnl FROM v2_paper_trades WHERE created_at >= date_trunc('day', now())").fetchone()
        if float(today["pnl"]) <= -abs(daily_loss_limit):
            raise LedgerError("Daily paper loss limit reached")
        if float(account["cash"]) < amount_usd:
            raise LedgerError("Insufficient paper cash")
        qty = amount_usd / price
        if not math.isfinite(qty):
            raise LedgerError("Paper quantity is outside the supported range")
        pos = conn.execute("SELECT * FROM v2_paper_positions WHERE mint=%s FOR UPDATE", (mint,)).fetchone()
        if pos:
            new_qty = float(pos["quantity"]) + qty
            avg = (float(pos["quantity"])*float(pos["average_entry"]) + amount_usd) / new_qty
            conn.execute("UPDATE v2_paper_positions SET symbol=%s,quantity=%s,average_entry=%s,invested_usd=invested_usd+%s,updated_at=now() WHERE mint=%s",
                         (symbol, new_qty, avg, amount_usd, mint))
        else:
            conn.execute("INSERT INTO v2_paper_positions(mint,symbol,quantity,average_entry,invested_usd) VALUES (%s,%s,%s,%s,%s)",
                         (mint,symbol,qty,price,amount_usd))
        conn.execute("UPDATE v2_paper_account SET cash=cash-%s,updated_at=now() WHERE id=1", (amount_usd,))
        conn.execute("INSERT INTO v2_paper_trades(mint,symbol,side,quantity,price_usd,notional_usd) VALUES (%s,%s,'BUY',%s,%s,%s)",
                     (mint,symbol,qty,price,amount_usd))
        return {"mint":mint,"symbol":symbol,"quantity":qty,"price_usd":price,"notional_usd":amount_usd,"side":"BUY","mode":"paper","real_trading_enabled":False}

def sell(mint: str, price: float, quantity: float):
    if not mint or not math.isfinite(price) or not math.isfinite(quantity) or price <= 0 or quantity <= 0:
        raise LedgerError("Mint, price and quantity must be valid positive values")
    with _connect() as conn:
        account = conn.execute("SELECT * FROM v2_paper_account WHERE id=1 FOR UPDATE").fetchone()
        pos = conn.execute("SELECT * FROM v2_paper_positions WHERE mint=%s FOR UPDATE", (mint,)).fetchone()
        if not account or not pos or float(pos["quantity"]) < quantity:
            raise LedgerError("Position not found or sell quantity exceeds paper holdings")
        notional = price * quantity
        if not math.isfinite(notional):
            raise LedgerError("Paper sale value is outside the supported range")
        cost = float(pos["average_entry"]) * quantity
        pnl = notional - cost
        remaining = float(pos["quantity"]) - quantity
        if remaining <= 1e-12:
            conn.execute("DELETE FROM v2_paper_positions WHERE mint=%s", (mint,))
        else:
            conn.execute("UPDATE v2_paper_positions SET quantity=%s,invested_usd=GREATEST(0,invested_usd-%s),updated_at=now() WHERE mint=%s",
                         (remaining,cost,mint))
        conn.execute("UPDATE v2_paper_account SET cash=cash+%s,realized_pnl=realized_pnl+%s,updated_at=now() WHERE id=1", (notional,pnl))
        conn.execute("INSERT INTO v2_paper_trades(mint,symbol,side,quantity,price_usd,notional_usd,realized_pnl_usd) VALUES (%s,%s,'SELL',%s,%s,%s,%s)",
                     (mint,pos["symbol"],quantity,price,notional,pnl))
        return {"mint":mint,"symbol":pos["symbol"],"quantity":quantity,"price_usd":price,"notional_usd":notional,"realized_pnl_usd":pnl,"side":"SELL","mode":"paper","real_trading_enabled":False}

def status():
    with _connect() as conn:
        account=conn.execute("SELECT * FROM v2_paper_account WHERE id=1").fetchone()
        if not account:
            return {"initialized":False,"mode":"paper","real_trading_enabled":False}
        positions=conn.execute("SELECT mint,symbol,quantity,average_entry,invested_usd,updated_at FROM v2_paper_positions ORDER BY invested_usd DESC").fetchall()
        trades=conn.execute("SELECT id,mint,symbol,side,quantity,price_usd,notional_usd,realized_pnl_usd,created_at FROM v2_paper_trades ORDER BY id DESC LIMIT 100").fetchall()
        return {"initialized":True,"cash_usd":float(account["cash"]),"starting_cash_usd":float(account["starting_cash"]),
                "realized_pnl_usd":float(account["realized_pnl"]),"positions":[dict(x) for x in positions],
                "recent_trades":[dict(x) for x in trades],"mode":"paper","real_trading_enabled":False}
