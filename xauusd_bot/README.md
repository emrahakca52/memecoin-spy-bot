# Multi-Asset MT5 Signal Scanner

A cloud-hosted, signals-only market scanner intended to complement the MT5 mobile app. It does not connect to the user's MT5 account and never places orders.

## Markets and symbols
The scanner can evaluate a configurable list of instruments supported by Twelve Data: forex, metals/commodities, crypto and supported indices/stocks. The starter watchlist includes XAU/USD, XAG/USD, major FX pairs, BTC/USD, ETH/USD and WTI/USD.

Set `SYMBOLS` to a comma-separated list of provider symbols to change or expand the watchlist. Provider symbol formats and plan entitlements vary. This is not an automatic export of the symbols visible in MetaTrader 5.

**Important MT5 limitation:** the iPhone MT5 app does not expose its broker's full Market Watch catalogue or quote feed to this service. Exact parity with the MetaQuotes-Demo/broker symbol list and prices requires a supported broker API or a Windows MT5 terminal/VPS integration. External provider quotes, spreads, sessions and symbol names may differ.

## Analysis
For each configured symbol the service retrieves 1min, 5min and 15min candles and evaluates EMA20/50 trend, RSI, MACD histogram and recent support/resistance. BUY/SELL/WAIT signals include indicative entry, stop-loss and take-profit levels. These are experimental technical signals, not guarantees or financial advice.

## Environment
- `TWELVE_DATA_API_KEY` required
- `SYMBOLS` optional comma-separated provider symbols
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` optional
- `POLL_SECONDS` optional, default 900 seconds (minimum 300)
- `SCAN_CONCURRENCY` optional, default 2; keep low to respect provider limits

Each symbol requires three time-series requests per scan. Set a watchlist and scan interval that fit your provider's API quota.

## Endpoints
- `/health`
- `/symbols`
- `/signal`
- `POST /scan`
- `/docs`

## Run
`pip install -r requirements.txt`
`uvicorn xauusd_bot.main:app --host 0.0.0.0 --port 8000`

No MT5 login, live trading, position sizing, martingale or order execution is implemented. Validate signals on a demo account before relying on them.
