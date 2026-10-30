# XAUUSD Signal Bot

This branch contains a clean XAUUSD technical-analysis alert service. The previous memecoin application remains intact on the `main` branch.

## Safety
- Signals only; no MT5 connection and no order execution.
- `real_trading_enabled` is always false.
- Use a demo account to validate signals. This is not financial advice.
- Twelve Data XAU/USD quotes may differ from the MetaQuotes-Demo or broker feed.

## Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn xauusd_bot.main:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`

## Required environment
- `TWELVE_DATA_API_KEY` — Twelve Data API key.
- `TELEGRAM_BOT_TOKEN` — optional Telegram bot token.
- `TELEGRAM_CHAT_ID` — optional Telegram chat ID.
- `TWELVE_DATA_SYMBOL` — optional, defaults to `XAU/USD`.
- `POLL_SECONDS` — optional, defaults to 60 seconds.

## Endpoints
- `GET /health`
- `GET /signal`
- `POST /scan`
- `GET /docs`

The service evaluates 1m, 5m and 15m candles using EMA, RSI and MACD confirmation. Entry/SL/TP are indicative levels only. Verify symbol, spread, quote and contract specifications in the demo terminal before considering any manual order.
