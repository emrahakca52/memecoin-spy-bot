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
log = logging.getLogger("multiasset")

# Configure this list with symbols supported by the chosen market-data provider.
DEFAULT_SYMBOLS = [
    "XAU/USD", "XAG/USD", "EUR/USD", "GBP/USD", "USD/JPY",
    "USD/CHF", "AUD/USD", "USD/CAD", "NZD/USD", "BTC/USD",
    "ETH/USD", "WTI/USD"
]
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]
INTERVALS = ("1min", "5min", "15min")
POLL_SECONDS = max(300, int(os.getenv("POLL_SECONDS", "900")))
_last_alerts = set()
_latest = {"status": "starting", "updated_at": None, "symbols": {}, "data_source": None}

async def scan_symbol(symbol):
    candles = {}
    source = None
    for interval in INTERVALS:
        source, candles[interval] = await fetch_candles(symbol, interval)
    result = build_signal(candles)
    return {"symbol": symbol, "signal": result, "data_source": source}

async def scan_once():
    global _latest
    semaphore = asyncio.Semaphore(max(1, int(os.getenv("SCAN_CONCURRENCY", "2"))))
    async def limited(symbol):
        async with semaphore:
            try:
                return symbol, await scan_symbol(symbol)
            except Exception as exc:
                log.warning("Scan failed for %s: %s", symbol, exc)
                return symbol, {"symbol": symbol, "status": "error", "error": str(exc), "signal": None}

    scanned = await asyncio.gather(*(limited(symbol) for symbol in SYMBOLS))
    results = {symbol: payload for symbol, payload in scanned}
    actionable = []
    for symbol, payload in results.items():
        signal = payload.get("signal") or {}
        direction = signal.get("direction")
        if direction in ("BUY", "SELL"):
            actionable.append({"symbol": symbol, **signal})
            key = f"{symbol}:{direction}:{signal.get('entry')}"
            if key not in _last_alerts:
                try:
                    await send_telegram(signal, symbol, payload.get("data_source", "market data"))
                    _last_alerts.add(key)
                except Exception:
                    log.exception("Telegram alert failed for %s", symbol)
    actionable.sort(key=lambda x: abs(x.get("rsi_m1", 50) - 50), reverse=True)
    _latest = {
        "status": "ok" if any(p.get("signal") for p in results.values()) else "error",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "Twelve Data",
        "symbols_scanned": len(SYMBOLS),
        "symbols": results,
        "top_signals": actionable[:10],
        "mode": "signals_only",
        "real_trading_enabled": False
    }
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

app = FastAPI(title="Multi-Asset MT5 Signal Scanner", version="0.2.0", lifespan=lifespan)

@app.get("/")
async def root():
    return {"name": "Multi-Asset MT5 Signal Scanner", "mode": "signals_only", "real_trading_enabled": False, "docs": "/docs"}

@app.get("/health")
async def health():
    return {
        "status": "ok", "mode": "signals_only", "real_trading_enabled": False,
        "symbols_configured": len(SYMBOLS), "poll_seconds": POLL_SECONDS,
        "market_data_configured": bool(os.getenv("TWELVE_DATA_API_KEY"))
    }

@app.get("/symbols")
async def symbols():
    return {"count": len(SYMBOLS), "symbols": SYMBOLS, "note": "Bu liste veri sağlayıcısının desteklediği sembollerdir; MT5 hesabındaki sembol listesiyle birebir aynı olduğu garanti edilmez."}

@app.get("/signal")
async def signal():
    return _latest

@app.post("/scan")
async def scan():
    return await scan_once()
