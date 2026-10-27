# Memecoin Spy Pro — paper-trading bot

## Safety and scope

- This project runs a **paper-trading simulation**. It does not execute real swaps or sign wallet transactions.
- The application reports `real_trading_enabled: false`; do not change this as part of testing.
- On application startup, `app.py` loads saved paper state and starts the paper-only scanner. Use `POST /paper-bot/stop` to stop the scanner; `POST /paper-bot/start` starts it again.
- Simulated P/L is not real investment performance and does not guarantee future results.

## Render configuration

- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Preserve the existing `DATABASE_URL`, `BIRDEYE_API_KEY`, and other configured environment variables.
- **Do not delete, reset, or recreate the PostgreSQL database.** Existing paper state and trade history must be preserved.

## Post-deployment verification

After deployment, inspect these read-only endpoints:

1. `GET /health` — should report paper mode and `real_trading_enabled: false`.
2. `GET /paper/status` — check bot state, persistence, and accounting diagnostics.
3. `GET /paper/trades` — confirm saved simulated trade history is present.

The application is expected to start the paper scanner automatically after successful startup. If health, persistence, or history checks fail, inspect deployment logs before attempting a restart or changing configuration. Do not manually start another process to work around an unresolved startup failure.

## Accounting and price-safety behavior

- Historical trades are retained; suspicious records may be quarantined from reported P/L without deleting the original history.
- If legacy SELL records lack realized P/L fields, the engine preserves stored lifetime P/L rather than replacing it with a partial ledger sum.
- Extreme or uncorroborated price quotes may be withheld from marking a position or triggering an automatic paper exit. This protects accounting from obvious bad quotes but can leave a position open during a genuine rapid market move; review diagnostics rather than assuming the mark is current.
- Price and P/L values are simulated and depend on provider data, API availability, and the configured strategy.

## Limits of verification

Passing automated tests and Python syntax checks does not prove the deployed Render service can reach the database or market-data provider. Live service health, environment variables, API access, and persisted state must be verified on the deployed service. No production database reset is required for this verification.
