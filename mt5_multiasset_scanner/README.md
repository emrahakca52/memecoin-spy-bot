# MT5 Multi-Asset Scanner

Independent MT5 scanner package. It reads the connected broker's symbol catalogue and native M1/M5/M15/H1 candles from the MetaTrader 5 **Windows desktop terminal**. It sends BUY/SELL alerts to Telegram and never places orders.

## Safety
The worker refuses to start unless the connected account is DEMO. It has no order-send function. Keep all testing on demo.

## Requirements
- Windows VPS or Windows PC
- MetaTrader 5 desktop terminal installed and logged into the intended demo account
- Python 3.10+ (64-bit recommended)
- Telegram bot token/chat ID for alerts (optional)

## Install
From the repository root:
`python -m pip install -r mt5_multiasset_scanner/requirements.txt`

Copy `.env.example` values into Windows environment variables (or configure them in your process manager). Never commit credentials.

Open MT5 desktop and log into the demo account. Then run:
`python -m mt5_multiasset_scanner.worker`

Or double-click `mt5_multiasset_scanner/start.bat`.

## Configuration
- `MAX_SYMBOLS=100`: max broker catalogue entries per cycle.
- `ONLY_VISIBLE=false`: false scans the first MAX_SYMBOLS broker symbols; true scans Market Watch visible symbols only.
- `SCAN_SECONDS=300`: wait between scan cycles, minimum 60 seconds.
- `MT5_TERMINAL_PATH`: optional path to terminal64.exe.
- `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`: optional; preferably log in through terminal UI instead.

Symbols with missing history or unavailable quotes are skipped. Scan duration depends on symbol count and broker history availability.

## Important limitations
- iOS MT5 cannot host this worker. The official Python integration requires the Windows desktop terminal.
- This package does not create or pay for a VPS. A free always-on Windows VPS is not guaranteed; free cloud trials often expire or have resource restrictions.
- Broker symbol catalogue, naming, trading hours and history vary.
- Technical signals are experimental and do not guarantee results. No order execution, position sizing or martingale is implemented.
