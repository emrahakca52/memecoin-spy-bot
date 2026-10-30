# MT5 native-feed bridge (Windows VPS)

This worker runs beside the MetaTrader 5 **desktop terminal** on Windows. It reads the broker's own symbol catalogue and M1/M5/M15 candles through the official MetaTrader5 Python package. It does not use Twelve Data and does not place orders.

## Setup
1. Rent/prepare a Windows VPS and install MetaTrader 5 desktop.
2. Log in to the user's demo account inside the terminal. Keep Algo Trading disabled; this worker only reads prices.
3. In this repository root, install the normal project requirements, then run `pip install -r mt5_bridge/requirements.txt`.
4. Set Telegram environment variables `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
5. Optional: set `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` to let the Python package initialize the account. Prefer logging in through the terminal UI and leaving these unset. Never commit credentials.
6. Run `python -m mt5_bridge.worker` from the repository root, or use `mt5_bridge/start.bat`.

## Controls
- `MAX_SYMBOLS`: maximum catalogue symbols scanned per cycle; default 100.
- `ONLY_VISIBLE=true`: scan only Market Watch visible symbols. Default false (first MAX_SYMBOLS from broker catalogue).
- `SCAN_SECONDS`: scan interval; default 300 seconds, minimum 60.
- `MT5_TERMINAL_PATH`: optional full path to terminal64.exe.
- `LOG_LEVEL`: default INFO.

The broker may expose a large catalogue; scan time depends on symbol count and terminal/data availability. Increase MAX_SYMBOLS gradually. Some symbols may lack history or be unavailable and are skipped.

## Important
- Requires Windows; the MetaTrader5 Python package is not supported on iOS or ordinary Linux hosting.
- A Windows VPS and an active MT5 terminal/session are required. VPS hosting may cost money; no VPS has been purchased or deployed by this code change.
- The mobile MT5 app alone cannot run this bridge.
- Signals are experimental technical analysis, not financial advice. Demo-test before relying on them.
- No order placement, account trading, position sizing or live execution is implemented.
