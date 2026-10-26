# Grok + Copyfomo strategy integration (paper-only)

## Added modules
- `config.py`: thresholds and paper risk budget.
- `rugcheck.py`: fetches RugCheck report and applies conservative, fail-closed safety checks.
- `prompts.py`, `grok.py`: xAI Grok JSON decision gate (COPY requires confidence >= 0.65).
- `pick_traders.py`: evaluates trader metrics only when sourced from verified realized trade history.
- `check.py`: runs RugCheck first, then Grok.
- `auto_paper_bot.py`: can optionally run the gate on the top candidates.

## Render environment variables
- `COPYFOMO_GATE_ENABLED=true` to enable the extra gate (default false to avoid silently stopping the existing paper scanner until configured).
- `XAI_API_KEY` (secret, do not commit it), optionally `XAI_MODEL` (default `grok-3-mini`) and `XAI_API_BASE`.
- `RUGCHECK_API_BASE` optional; defaults to `https://api.rugcheck.xyz/v1`.
- `MAX_GROK_CHECKS_PER_SCAN=3` limits external AI calls per scan.

## Safety and known limits
- `real_trading_enabled` is hard-coded false; no wallet signing, swap, or real order code is added.
- Paper positions remain $10 each, max 5 open positions, and the current paper engine retains its existing +10% take-profit / -5% stop-loss behavior.
- Copyfomo profile values (+100% take-profit half-position, -50% stop-loss, 1h trader cooldown, 24h token re-entry cooldown, max 3% slippage) are recorded as planned settings only; they are NOT represented as active exit behavior in this build. Per-strategy exits must be implemented and tested separately before claiming they are active.
- The source project currently has no trusted historical trader PnL feed or token transfer/holder graph. `pick_traders.py` validates supplied metrics but cannot derive them from raw RPC transaction counts. Insider-network count and authority/LP fields can be missing in RugCheck responses; missing critical data rejects the candidate when gate enabled.
- The gate is disabled by default. Enable only after setting `XAI_API_KEY` and validating RugCheck response fields with live examples. All paper-only; no profit guarantee.
