# Memecoin Spy Pro — clean paper-only deployment bundle

This package is based on the project ZIP supplied by the owner. It preserves the existing Python modules and adds a safer startup behavior.

## Safety defaults
- `TRADING_MODE` is paper.
- `REAL_TRADING_ENABLED` is hard-coded `False` in the strategy engine.
- The bot does **not** start automatically on deployment. It loads saved paper state and remains stopped until `POST /paper-bot/start` is explicitly called.
- No wallet signing or real swap/order implementation is included.

## Render settings
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Keep the existing `DATABASE_URL` and `BIRDEYE_API_KEY` environment variables. Do not delete or recreate the PostgreSQL database.
- Other existing environment variables should be preserved unless intentionally changed.

## Deploying from GitHub
1. Download and unzip this bundle.
2. Replace the repository's source files with the files in this folder. Upload the *contents* of the folder to the repository root, not the enclosing folder itself.
3. Do not upload `__pycache__` or `.pyc` files.
4. Commit the changes and let Render deploy.
5. Check `/health`, `/paper/status`, and `/paper/trades` before starting anything.

## Important database note
The engine uses the existing PostgreSQL `DATABASE_URL` and the `memecoin_paper_state` table. Do not delete the Render PostgreSQL database. The first startup attempts to load the saved state; if there is no saved state or the database URL is missing, verify configuration before taking further action.

## Safe verification
- `GET /health` should show `mode: paper`, `real_trading_enabled: false`, and `bot.running: false` after deployment.
- `GET /paper/status` reports persistence and paper accounting status.
- `GET /paper/trades` lists persisted simulated trades.
- Only after those checks should you manually call `POST /paper-bot/start` if you want the paper scanner running.

## Limitations
Python syntax and module-import checks are not the same as a live Render integration test. Provider access, API plan limits, and database connectivity must be verified on the deployed service. Paper P/L is simulated and not real investment performance.


## Safety patch v2 (paper mode)

- Keeps all existing PostgreSQL paper state and trade history; do not delete `DATABASE_URL` or the database.
- Real trading remains disabled; this application is a paper simulation only.
- Tightens automatic-exit quote guards: stop-loss overshoot is capped at 10 percentage points (default 5), and take-profit overshoot is capped at 50 percentage points (default 25). Excessive quotes are flagged and the position remains open for review; this is safer for simulation integrity but can leave a position open during a genuine rapid market move.
- This patch does not rewrite or delete historical trade records, including quarantined anomalous records.

Deploy by replacing the repository's source files with the contents of this ZIP. Do not upload the ZIP file itself and do not remove the Render PostgreSQL database or environment variables. Start the paper bot only after checking `/health`, `/paper/status`, and `/paper/trades`.
