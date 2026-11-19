# BIST Spek Radar

This is a separate BIST equities research application, isolated under `bist_spek_radar/` on branch `bist-spek-radar`. It does not import or modify the meme-coin bot.

## Scope
- Detect unusual price/volume/turnover and volatility patterns in Borsa Istanbul symbols.
- Rank candidates with explainable, rule-based signals; never claim that a pattern proves manipulation.
- Paper portfolio only. No brokerage integration, order routing, credentials, or real-money trading.
- Data provider in the starter build: Yahoo Finance via `yfinance` (`.IS` tickers). This is not an exchange-authoritative real-time feed; timestamps and data freshness must be checked before decisions.

## Run
```bash
cd bist_spek_radar
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Set `BIST_SYMBOLS` to a comma-separated universe such as `THYAO,ASELS,TUPRS,EREGL`. Symbols are normalized to Yahoo Finance's `.IS` format.

## Safety
Research and simulation only. Signals are screening flags, not investment advice or evidence of market abuse. Paper trades use a virtual balance and do not reach a broker.
