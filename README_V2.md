# Memecoin Spy Bot V2 — staged rebuild

V2 is a separate Solana discovery and security-triage service. It remains isolated from the production V1 process until full paper-trading parity, migration review, backup/restore verification and staging deployment are complete.

## Implemented in V2
- Modular package with typed market models, environment settings, transparent momentum scoring, fail-closed risk gates and scanner orchestration.
- DexScreener Solana token-profile discovery, pair selection by liquidity and duplicate-address filtering.
- Public RugCheck report enrichment: normalized risk score, top-holder concentration, mint/freeze authority metadata, LP-lock percentage and dangerous risk flags. The lowest reported LP-lock percentage across reported markets is used.
- Optional Helius Enhanced Transactions same-slot swap clustering for the first 10 candidates per scan. It is a conservative heuristic, not proof of coordinated sniping; unavailable data remains unknown and fails closed.
- Isolated PostgreSQL paper ledger using only v2_paper_* tables. It supports paper buys, sells, realized PnL, open positions, cash balance and a realized daily-loss brake. Write endpoints require V2_PAPER_API_KEY; the existing V1 tables are not altered.
- Public Solana RPC enrichment with endpoint fallback: mint/freeze authority checks and a labelled top-20 token-account sample. This sample is incomplete and is never treated as a complete holder index.
- Security-provider calls are bounded to the first 10 candidates per scan to reduce public API load.
- Standalone FastAPI endpoints: `GET /health`, `GET /v2/scan?limit=20`, and read-only `GET /v2/wallet/{wallet_address}?limit=10`, GET /v2/paper/status, and authenticated POST /v2/paper/buy / POST /v2/paper/sell.
- Optional Telegram scan summary, disabled unless `V2_TELEGRAM_REPORTS_ENABLED=true`. It uses the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; reports are throttled to at most one per process per 15 minutes.
- Unit tests cover scoring, fail-closed risk decisions, RugCheck parsing, RPC lookup bounds, Helius same-slot clustering, scanner deduplication and paper-only output.
- `real_trading_enabled` is hard-coded false. No wallet signing or swap execution exists. The only V2 database writes are additive paper-ledger records in v2_paper_* tables.

## Risk decisions
A candidate cannot be marked eligible unless market data, RugCheck normalized score, top-10 holder concentration, mint/freeze authority status and LP-lock status pass configured thresholds. Missing data is not treated as zero. Transaction-level same-slot swap clustering is checked through Helius when configured. It is heuristic rather than exhaustive; if the key, provider fields, or sample are unavailable, candidates remain ineligible. Public RPC top-account observations do not clear the holder-index gate.

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
- `HELIUS_API_KEY` (optional; enables enhanced same-slot swap clustering; configure only in Render environment, never paste into chat)
- `V2_PAPER_API_KEY` (required to authorize paper buy/sell endpoints; choose a long random secret)
- `V2_SOLANA_RPC_URL` (optional preferred RPC endpoint; fallback public endpoints are tried)
- `V2_TELEGRAM_REPORTS_ENABLED` (default false; opt-in)
- `V2_MAX_DAILY_LOSS_USD` controls the realized daily paper-loss brake; `V2_PAPER_STARTING_BALANCE_USD` seeds the isolated V2 paper account once.

## Not yet complete — external and production gates
- Helius credentials are not configured yet. Without them, same-slot swap clustering remains unknown and the eligibility gate fails closed. Complete wallet-level holder indexing is still not provided by the top-20 RPC sample.
- LP-lock and holder fields depend on the third-party RugCheck report schema and availability; missing or malformed fields fail closed.
- A second AI opinion provider is not connected; no AI-based pass override exists in V2.
- V2 paper ledger is additive and separate; it does not import V1 trade history or calculate unrealized PnL from live prices. V1's PostgreSQL paper history remains untouched.
- Helius classification uses enhanced transaction type and slot fields when available. It is a heuristic only; it cannot guarantee detection of every bundle/sniper and should not be represented as exhaustive.
- V2 is not deployed or wired into the production Render start command. The current Render service still starts `app:app`.

## Migration safety
Do not merge/deploy V2 as a replacement or delete V1 files/reset PostgreSQL until the remaining transaction-indexing gate is resolved or explicitly accepted, V2 paper ledger has feature parity, backup/restore is verified, and a staging deployment has been checked. V1 remains the production entry point.
