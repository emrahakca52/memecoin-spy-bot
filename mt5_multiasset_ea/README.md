# MT5 Multi-Asset Signal Scanner (MQL5)

This Expert Advisor reads market data from the connected broker's MetaTrader 5 terminal. It does not use an external market-data API and it does not place, modify, or close trades.

## What it does
- Enumerates symbols available in the terminal (not only the chart symbol).
- Reads M5 and H1 broker price history.
- Checks EMA 20/50 alignment, RSI(14), recent candle direction and ATR(14).
- Produces an indicative entry zone, stop level and two target levels.
- Prints signals in the MT5 Experts log.
- Optionally sends MT5 push notifications or Telegram messages.

## Important limitations
- All symbols means symbols exposed by the broker to this MT5 terminal. Some instruments may need to be enabled in the broker's Symbols list and may not have downloaded history yet.
- This is a signal scanner, not a trading strategy guarantee. Entry zones and ATR-based levels are estimates; spread, slippage, sessions, news and contract specifications matter.
- It has no order execution code. It cannot open trades.
- The source has not been compiled in MetaEditor or validated against your broker's symbol catalogue yet. Compile and demo-test before relying on alerts.
- A running MT5 desktop terminal or compatible MT5 VPS is required. MT5 for iPhone cannot run Expert Advisors.

## Install
1. On an MT5 desktop terminal, open File > Open Data Folder.
2. Open MQL5/Experts/.
3. Copy MT5_MultiAsset_SignalScanner.mq5 there.
4. Open MetaEditor, compile the file, then attach the EA to one chart.
5. Keep the terminal connected to your broker. The EA scans the terminal symbol catalogue every 5 minutes by default.

## Telegram alerts (optional)
1. Create a Telegram bot and obtain its bot token and chat ID.
2. In EA Inputs set EnableTelegram=true, TelegramBotToken, and TelegramChatId.
3. In MT5 desktop open Tools > Options > Expert Advisors and add https://api.telegram.org to the WebRequest allowlist.
4. Never publish your real token or chat ID in a public repository. Enter them locally in the EA Inputs only.

## Push notifications (optional)
Enable push notifications in MT5 desktop and configure your MetaQuotes ID in terminal options, then leave EnablePush=true.

## Default settings
- Scan interval: 300 seconds
- Maximum symbols per scan: 250
- Signal threshold: 4 points
- Entry zone: current quote +/- 0.15 ATR
- Stop: 1.5 ATR
- Targets: 1.5 ATR and 2.5 ATR

These defaults are starting parameters for demo evaluation, not optimized settings.