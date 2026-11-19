# Memecoin Spy Bot V2 — staged rebuild

V2 is a separate Solana discovery and security-triage service. It remains isolated from the production V1 process until full paper-trading parity, migration review, backup/restore verification and staging deployment are complete.

## Implemented in V2
- Modular package with typed market models, environment settings, transparent momentum scoring, fail-closed risk gates and scanner orchestration.
- DexScreener Solana token-profile discovery, pair selection by liquidity and duplicate-address filtering.
- Public RugCheck report enrichment: normalized risk score, top-holder concentration, mint/freeze authority metadata, LP-lock percentage and dangerous risk flags. The lowest reported LP-lock percentage across reported markets is used.
- Public Solana RPC enrichment with endpoint fallback: mint/freeze authority checks and a labelled top-20 token-account sample. This sample is incomplete and is never treated as a complete holder index.
- Security-provider calls are bounded to the first 10 candidates per scan to reduce public API load.
- Standalone FastAPI endpoints: `GET /health`, `GET /v2/scan?limit=20`, and read-only `GET /v2/wallet/{wallet_address}?limit=10`.
- Optional Telegram scan summary, disabled unless `V2_TELEGRAM_REPORTS_ENABLED=true`. It uses the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; reports are throttled to at most one per process per 15 minutes.
- Unit tests cover scoring, fail-closed risk decisions, RugCheck parsing, RPC lookup bounds, scanner deduplication and paper-only output.
- `real_trading_enabled` is hard-coded false. No wallet signing, swap execution, or V2 database writes exist.

## Risk decisions
A candidate cannot be marked eligible unless market data, RugCheck normalized score, top-10 holder concentration, mint/freeze authority status and LP-lock status pass configured thresholds. Missing data is not treated as zero. Transaction-level bundle/sniper detection remains explicitly missing without a dedicated indexed transaction feed; therefore candidates remain ineligible even when other checks pass. Public RPC top-account observations do not clear that gate.

## Run locally
```bash
pip install -r requirements.txt
uvicorn v2_app:app --host 0.0.0.0 --port 8001
```

Open `/health`, `/v2/scan`, or `/docs`. Public providers may rate-limit or temporarily fail; V2 returns unknown/missing risk data rather than approving a candidate.

## Environment settings
- `V2_MIN_LIQUIDITY_USD` (default 20000)
- `V2_MIN_VOLUME_24H_USD` (default 30000)
- `V2_MIN_TXNS_24H` (default 30)
- `V2_MAX_PAIR_AGE_HOURS` (default 72)
- `V2_MAX_TOP10_HOLDER_PCT` (default 25)
- `V2_MIN_LP_LOCKED_PCT` (default 50)
- `V2_MAX_RUGCHECK_SCORE` (default 55; normalized 0–100 score)
- `V2_SOLANA_RPC_URL` (optional preferred RPC endpoint; fallback public endpoints are tried)
- `V2_TELEGRAM_REPORTS_ENABLED` (default false; opt-in)
- `V2_MAX_DAILY_LOSS_USD` and `V2_PAPER_STARTING_BALANCE_USD` are reserved for the future V2 paper ledger; they do not currently drive a V2 ledger.

## Not yet complete — external and production gates
- Reliable launch-bundle/sniper analytics and complete wallet-level holder indexing require a transaction-indexing provider (for example Helius). No such credentials are configured. V2 does not pretend the heuristic top-20 sample is a substitute.
- LP-lock and holder fields depend on the third-party RugCheck report schema and availability; missing or malformed fields fail closed.
- A second AI opinion provider is not connected; no AI-based pass override exists in V2.
- V2 paper positions, persistent accounting and trade-history migration are not implemented. V1's PostgreSQL paper history remains untouched.
- V2 is not deployed or wired into the production Render start command. The current Render service still starts `app:app`.

## Migration safety
Do not merge/deploy V2 as a replacement or delete V1 files/reset PostgreSQL until the remaining transaction-indexing gate is resolved or explicitly accepted, V2 paper ledger has feature parity, backup/restore is verified, and a staging deployment has been checked. V1 remains the production entry point.
