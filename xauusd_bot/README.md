# XAUUSD Signal Bot

Signals-only service for XAUUSD. It never connects to MT5 and never places orders.

## Data
Uses Twelve Data time-series API for XAU/USD 1min, 5min and 15min candles. Configure `TWELVE_DATA_API_KEY`. Provider quotes can differ from MetaQuotes-Demo/broker quotes and spreads.

## Environment
- `TWELVE_DATA_API_KEY` required
- `TWELVE_DATA_SYMBOL` optional, defaults to `XAU/USD`
- `TELEGRAM_BOT_TOKEN` optional
- `TELEGRAM_CHAT_ID` optional
- `POLL_SECONDS` optional, defaults to 60 (minimum 60)

## Run
`pip install -r requirements.txt`
`uvicorn xauusd_bot.main:app --host 0.0.0.0 --port 8000`

Endpoints: `/health`, `/signal`, `POST /scan`, `/docs`.

This is an experimental technical-analysis alert, not financial advice. Paper/demo validation is required. No live trading, position sizing, martingale, or order execution is implemented.
