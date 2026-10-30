import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from .market import fetch_candles
from .strategy import build_signal
from .telegram import send_telegram

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("xauusd")

SYMBOL = os.getenv("TWELVE_DATA_SYMBOL", "XAU/USD")
INTERVALS = ("1min", "5min", "15min")
POLL_SECONDS = max(60, int(os.getenv("POLL_SECONDS", "60")))
_last_signal_key = None
_latest = {"status": "starting", "symbol": SYMBOL, "updated_at": None, "signal": None, "data_source": None}

async def scan_once():
    global _last_signal_key, _latest
    try:
        candles = {}
        source = None
        for interval in INTERVALS:
            source, candles[interval] = await fetch_candles(SYMBOL, interval)
        result = build_signal(candles)
        _latest = {
            "status": "ok",
            "symbol": SYMBOL,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": source,
            "signal": result,
        }
        key = f"{result['direction']}:{result['entry']}"
        if result["direction"] in ("BUY", "SELL") and key != _last_signal_key:
            sent = await send_telegram(result, SYMBOL, source)
            _latest["telegram_sent"] = sent
            _last_signal_key = key
        return _latest
    except Exception as exc:
        log.exception("Market scan failed")
        _latest = {"status": "error", "symbol": SYMBOL, "updated_at": datetime.now(timezone.utc).isoformat(), "error": str(exc), "signal": None}
        return _latest

async def scan_loop():
    while True:
        await scan_once()
        await asyncio.sleep(POLL_SECONDS)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scan_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="XAUUSD Signal Bot", version="0.1.0", lifespan=lifespan)

@app.get("/")
async def root():
    return {"name": "XAUUSD Signal Bot", "mode": "signals_only", "real_trading_enabled": False, "docs": "/docs"}

@app.get("/health")
async def health():
    return {"status": "ok", "mode": "signals_only", "real_trading_enabled": False, "symbol": SYMBOL, "poll_seconds": POLL_SECONDS, "market_data_configured": bool(os.getenv("TWELVE_DATA_API_KEY"))}

@app.get("/signal")
async def signal():
    return _latest

@app.post("/scan")
async def scan():
    return await scan_once()
