# MT5 Multi-Asset Scanner

Standalone signals-only MT5 scanner project.

## Architecture
- Runs on a Windows VPS beside the MetaTrader 5 desktop terminal.
- Reads broker-provided symbols and M1/M5/M15/H1 market candles using the MetaTrader5 Python package.
- Computes EMA, RSI, MACD, support/resistance and produces BUY/SELL/WAIT alerts.
- Sends optional Telegram notifications.
- Does not place, modify, or close trades.

## Requirements
- Windows VPS or Windows PC
- MetaTrader 5 desktop terminal logged into a demo account
- Python 3.10+
- Telegram bot token/chat ID (optional)

## Run
`pip install -r requirements.txt`
`python -m mt5_multiasset_scanner.worker`

See `SETUP.md` for configuration. Demo-test only; signals are experimental and not financial advice.
