import os
import time
import asyncio
import logging
import MetaTrader5 as mt5

from .strategy import build_signal
from .telegram import send_telegram

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mt5-multiasset")

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
}
BARS = 120
MAX_SYMBOLS = max(1, int(os.getenv("MAX_SYMBOLS", "100")))
SCAN_SECONDS = max(60, int(os.getenv("SCAN_SECONDS", "300")))
ONLY_VISIBLE = os.getenv("ONLY_VISIBLE", "false").lower() == "true"
_last_direction = {}

def connect():
    path = os.getenv("MT5_TERMINAL_PATH")
    login, password, server = os.getenv("MT5_LOGIN"), os.getenv("MT5_PASSWORD"), os.getenv("MT5_SERVER")
    kwargs = {}
    if login and password and server:
        kwargs.update(login=int(login), password=password, server=server)
    initialized = mt5.initialize(path=path, **kwargs) if path else mt5.initialize(**kwargs)
    if not initialized:
        raise RuntimeError(f"MT5 bağlantısı kurulamadı: {mt5.last_error()}")
    account = mt5.account_info()
    if account is None:
        raise RuntimeError(f"MT5 hesap bilgisi alınamadı: {mt5.last_error()}")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        raise RuntimeError("Güvenlik kilidi: Bu yazılım yalnızca DEMO hesapta çalışır.")
    log.info("DEMO bağlantısı hazır. Sunucu=%s, hesap=%s", account.server, account.login)

def catalogue():
    all_symbols = mt5.symbols_get()
    if all_symbols is None:
        raise RuntimeError(f"Sembol listesi alınamadı: {mt5.last_error()}")
    available = [s for s in all_symbols if (s.visible or not ONLY_VISIBLE)]
    return [s.name for s in available[:MAX_SYMBOLS]]

def candles(symbol, timeframe):
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Sembol seçilemedi: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, BARS)
    if rates is None or len(rates) < 60:
        raise RuntimeError(f"Yeterli mum yok: {mt5.last_error()}")
    return [{"open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"])} for r in rates]

def analyze(symbol):
    market_data = {name: candles(symbol, tf) for name, tf in TIMEFRAMES.items()}
    return build_signal(market_data)

def run():
    connect()
    try:
        while True:
            started = time.time()
            symbols = catalogue()
            log.info("%d sembol taranacak.", len(symbols))
            actionable = 0
            for symbol in symbols:
                try:
                    signal = analyze(symbol)
                    direction = signal["direction"]
                    if direction in ("BUY", "SELL"):
                        actionable += 1
                        if _last_direction.get(symbol) != direction:
                            try:
                                sent = asyncio.run(send_telegram(signal, symbol))
                                if sent:
                                    _last_direction[symbol] = direction
                            except Exception:
                                log.exception("Telegram bildirimi gönderilemedi: %s", symbol)
                    else:
                        _last_direction.pop(symbol, None)
                except Exception as exc:
                    log.warning("%s atlandı: %s", symbol, exc)
            log.info("Tarama bitti. Sinyal=%d, sembol=%d", actionable, len(symbols))
            time.sleep(max(1, SCAN_SECONDS - int(time.time() - started)))
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    run()
