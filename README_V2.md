# Memecoin Spy Bot V2 — staged rebuild

V2 is a separate Solana discovery and security-triage service. It is isolated from production V1; V1's entry point, database tables and trading behavior are not changed.

## Implemented in V2

Discovery fallback behavior is logged with primary and fallback candidate counts. A provider returning HTTP 200 with an empty list is treated as an empty feed, not as proof that no new Solana tokens exist. The scanner falls back to DexPaprika's Solana new-pools search (sorted by pool creation time), then GeckoTerminal; provider errors remain fail-closed.
- Modular scanner, transparent market scoring and fail-closed risk gates.
- Multi-source Solana discovery: DexScreener latest token profiles first, DexPaprika's public Solana pool search as the second source, and GeckoTerminal's public new-pools feed as the third fallback. DexPaprika sorts pools by creation time and requires no API key; pool creation is not proof that a token is new or safe. GeckoTerminal is beta and rate-limited. RugCheck security enrichment remains separate.
- Deterministic local screening when no Grok/Gemini key is configured. This is a rules engine, **not AI advice**; the dashboard reports AI availability separately.
- Optional Grok/Gemini screening when a valid provider key is configured.
- Public Solana RPC transaction sampling without a paid key: up to 12 recent signatures, at most 3 concurrent transaction lookups, and endpoint fallback. This is an incomplete heuristic; unavailable or unclassifiable samples remain unknown and fail closed. It is not equivalent to Helius Enhanced Transactions.
- Public Solana RPC mint/freeze authority checks and a labelled top-20 token-account sample. The sample is incomplete and never treated as a complete holder index.
- Isolated PostgreSQL paper ledger using only `v2_paper_*` tables. Paper writes require `V2_PAPER_API_KEY`; V1 tables are not altered.
- Standalone FastAPI endpoints: `GET /health`, `GET /v2/scan?limit=20`, read-only `GET /v2/wallet/{wallet_address}?limit=10`, `GET /v2/paper/status`, and authenticated paper buy/sell endpoints.
- Optional Telegram scan summary, disabled unless `V2_TELEGRAM_REPORTS_ENABLED=true`.
- `real_trading_enabled` is hard-coded false. No wallet signing or live swap execution exists.

## Risk decisions and limitations
A candidate is eligible only when every configured risk gate passes. Missing or malformed data is never treated as zero or as approval. Public RPC transaction clustering examines only a small recent sample and can miss activity; it is a conservative signal, not proof of bundle/sniper behavior. Top-account observations do not clear a full holder-index gate. LP-lock and holder fields depend on RugCheck report availability and schema. Local screening is deterministic threshold logic, not a substitute for an AI model or independent audit.

## Run locally
```bash
pip install -r requirements.txt
uvicorn v2_app:app --host 0.0.0.0 --port 8001
```

## Environment settings
- `V2_MIN_LIQUIDITY_USD` (default 20000)
- `V2_MIN_VOLUME_24H_USD` (default 30000)
- `V2_MIN_TXNS_24H` (default 30)
- `V2_MAX_PAIR_AGE_HOURS` (default 72)
- `V2_MAX_TOP10_HOLDER_PCT` (default 25)
- `V2_MIN_LP_LOCKED_PCT` (default 50)
- `V2_MAX_RUGCHECK_SCORE` (default 55)
- `AI_PROVIDER` (`grok` or `gemini`)
- `XAI_API_KEY` or `GEMINI_API_KEY` (optional AI screening)
- `HELIUS_API_KEY` (optional; enables the separate Helius integration if implemented/configured)
- `V2_PAPER_API_KEY` (required for paper buy/sell writes)
- `V2_SOLANA_RPC_URL` (optional preferred RPC endpoint)
- `V2_MAX_DAILY_LOSS_USD` and `V2_PAPER_STARTING_BALANCE_USD`

## Deployment and migration safety
V2 is deployed as a separate staging service using `v2_app:app`; V1 remains on `app:app`. Do not replace V1 or migrate/delete V1 data until the V2 paper ledger, backup/restore, external data dependencies and staging behavior have been independently verified.
