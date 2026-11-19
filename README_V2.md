# Memecoin Spy Bot v2 — clean architecture foundation

This is the parallel v2 rebuild. The current production entry points and PostgreSQL paper history are intentionally untouched while v2 is validated.

## Current scope
- Provider adapter: latest Solana token profiles and pair metrics from DexScreener.
- Provider-independent market model.
- Transparent momentum ranking, separate from hard risk decisions.
- Fail-closed risk gate: missing market data is reported; holder concentration, authority revocation, LP lock and bundle/sniper checks remain explicitly unverified until independent sources are connected.
- Paper-only settings. `real_trading_enabled` is hard-coded false.

## Run tests
```bash
python -m unittest discover -s tests -v
```

## Important limitations
DexScreener market data is discovery data, not proof that a token is safe. This foundation does not execute swaps, sign transactions, or read/write the existing PostgreSQL ledger. Do not delete legacy files or reset the database until v2 has equivalent feature coverage, migration checks, deployment verification and a separately approved cutover.
