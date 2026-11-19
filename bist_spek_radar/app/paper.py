"""SQLite-backed virtual portfolio. No broker connectivity or real orders."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("BIST_PAPER_DB", str(Path(__file__).resolve().parent.parent / "paper.db")))
STARTING_CASH = float(os.getenv("BIST_PAPER_STARTING_CASH", "100000"))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK (id=1), cash REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
        side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), quantity REAL NOT NULL,
        price REAL NOT NULL, gross REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("INSERT OR IGNORE INTO account(id,cash) VALUES(1,?)", (STARTING_CASH,))
    con.commit()
    return con


def status() -> dict[str, Any]:
    with connect() as con:
        cash = float(con.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"])
        trades = [dict(row) for row in con.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 100")]
        positions = [dict(row) for row in con.execute("""
            SELECT symbol, SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END) AS quantity,
            SUM(CASE WHEN side='BUY' THEN gross ELSE -gross END) AS cost_basis
            FROM trades GROUP BY symbol HAVING quantity > 0.0000001 ORDER BY symbol""")]
        return {"mode":"PAPER_ONLY","starting_cash":STARTING_CASH,"cash":round(cash,2),
                "positions":positions,"recent_trades":trades}


def trade(symbol: str, side: str, quantity: float, price: float) -> dict[str, Any]:
    symbol = symbol.upper().removesuffix(".IS")
    side = side.upper()
    if side not in {"BUY","SELL"} or quantity <= 0 or price <= 0:
        raise ValueError("Geçersiz yön, miktar veya fiyat")
    gross = quantity * price
    with connect() as con:
        cash = float(con.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"])
        held = float(con.execute("""SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN quantity ELSE -quantity END),0)
                                  FROM trades WHERE symbol=?""", (symbol,)).fetchone()[0])
        if side == "BUY" and gross > cash:
            raise ValueError("Sanal bakiye yetersiz")
        if side == "SELL" and quantity > held + 1e-9:
            raise ValueError("Sanal portföyde yeterli adet yok")
        new_cash = cash - gross if side == "BUY" else cash + gross
        con.execute("UPDATE account SET cash=? WHERE id=1", (new_cash,))
        cur = con.execute("INSERT INTO trades(symbol,side,quantity,price,gross) VALUES(?,?,?,?,?)",
                          (symbol,side,quantity,price,gross))
        con.commit()
        return {"id":cur.lastrowid,"symbol":symbol,"side":side,"quantity":quantity,
                "price":price,"gross":round(gross,2),"cash":round(new_cash,2),"mode":"PAPER_ONLY"}
