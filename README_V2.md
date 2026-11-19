# Memecoin Spy Bot v2 — staged rebuild

V2 is a separate, read-only Solana discovery and risk-triage service. It does not replace production v1 until migration and deployment checks are completed.

## Implemented
- Modular package with typed market models, environment settings, scoring, risk gate, scanner orchestration and DexScreener adapter.
- Deduplicated Solana token discovery, market momentum ranking and structured risk reasons.\n- Best-effort public Solana RPC checks for mint/freeze authorities and a clearly labelled top-20 token-account sample; limited to 10 candidates per scan to reduce public endpoint load.
- Fail-closed handling: missing liquidity/volume/transaction/age data is reported; complete holder concentration, LP lock and bundle/sniper activity remain explicitly unverified. Public RPC authority checks are shown when available; the top-20 token-account sample is informational only and never clears the holder-concentration gate. Such candidates are not marked eligible.
- Standalone FastAPI service in v2_app.py with GET /health and GET /v2/scan?limit=20.
- Unit tests and GitHub Actions workflow.
- real_trading_enabled is hard-coded false. No wallet signing, swap execution, or database writes exist in v2.

## Run locally
~~~bash
pip install -r requirements.txt
uvicorn v2_app:app --host 0.0.0.0 --port 8001
~~~

Then open /health and /v2/scan. The scan endpoint calls public DexScreener endpoints; it may return HTTP 502 when the provider is unavailable or rate-limits requests.

## Environment settings
- V2_MIN_LIQUIDITY_USD (default 20000)
- V2_MIN_VOLUME_24H_USD (default 30000)
- V2_MIN_TXNS_24H (default 30)
- V2_MAX_PAIR_AGE_HOURS (default 72)
- V2_MAX_DAILY_LOSS_USD (reserved for future paper ledger; default 25)
- V2_PAPER_STARTING_BALANCE_USD (reserved for future paper ledger; default 500)

## Important limitations / next integration gate
DexScreener is market discovery data, not proof a token is safe. V2 does not yet have a transaction-level feed or independent holder/authority/LP-lock verification. Public RPC now provides a limited authority check and partial holder sample without an API key. It does not provide reliable block/bundle/sniper analytics or a complete wallet-holder index; those still require a transaction/indexing provider such as Helius. A second AI provider requires its own credentials. Until those integrations are implemented and tested, v2 intentionally returns candidates as not eligible. Do not treat market scores as buy signals.

## Migration safety
- V1 entry point, Render configuration and PostgreSQL paper state/history are untouched.
- V2 is not wired into production Render start command.
- Do not delete legacy files or reset the database until V2 has feature parity, a reviewed migration plan, backup/restore verification and successful staging deployment.
