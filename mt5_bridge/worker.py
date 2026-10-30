import os
import time
import asyncio
import logging
from datetime import datetime, timezone

import MetaTrader5 as mt5
from xauusd_bot.strategy import build_signal
from xauusd_bot.telegram import send_telegram

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mt5-bridge")

TIMEFRAMES = {
    "1min": mt5.TIMEFRAME_M1,
    "5min": mt5.TIMEFRAME_M5,
    "15min": mt5.TIMEFRAME_M15,
}
BARS = 120
MAX_SYMBOLS = max(1, int(os.getenv("MAX_SYMBOLS", "100")))
SCAN_SECONDS = max(60, int(os.getenv("SCAN_SECONDS", "300")))
ONLY_VISIBLE = os.getenv("ONLY_VISIBLE", "false").lower() == "true"
_last_direction = {}

def connect():
    path = os.getenv("MT5_TERMINAL_PATH")
    kwargs = {}
    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")
    if login and password and server:
        kwargs.update(login=int(login), password=password, server=server)
    ok = mt5.initialize(path=path, **kwargs) if path else mt5.initialize(**kwargs)
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"MT5 account unavailable: {mt5.last_error()}")
    log.info("Connected to MT5 server=%s login=%s", account.server, account.login)

def get_symbols():
    symbols = mt5.symbols_get()
    if symbols is None:
        raise RuntimeError(f"symbols_get failed: {mt5.last_error()}")
    chosen = [s for s in symbols if (s.visible or not ONLY_VISIBLE)]
    chosen = chosen[:MAX_SYMBOLS]
    return [s.name for s in chosen]

def get_candles(symbol, timeframe):
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"symbol_select failed: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, BARS)
    if rates is None or len(rates) < 60:
        raise RuntimeError(f"Insufficient rates: {mt5.last_error()}")
    return [{"open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])} for r in rates]

def analyze(symbol):
    data = {name: get_candles(symbol, tf) for name, tf in TIMEFRAMES.items()}
    return build_signal(data)

def run():
    connect()
    try:
        while True:
            started = time.time()
            symbols = get_symbols()
            log.info("Scanning %d symbols (visible_only=%s)", len(symbols), ONLY_VISIBLE)
            signals = []
            for symbol in symbols:
                try:
                    result = analyze(symbol)
                    direction = result["direction"]
                    if direction in ("BUY", "SELL"):
                        signals.append((symbol, result))
                        if _last_direction.get(symbol) != direction:
                            sent = asyncio.run(send_telegram(result, symbol, "MT5 broker feed"))
                            if sent:
                                _last_direction[symbol] = direction
                    else:
                        _last_direction.pop(symbol, None)
                except Exception as exc:
                    log.warning("%s skipped: %s", symbol, exc)
            log.info("Scan complete: %d actionable signals / %d symbols", len(signals), len(symbols))
            delay = max(1, SCAN_SECONDS - int(time.time() - started))
            time.sleep(delay)
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    run()
