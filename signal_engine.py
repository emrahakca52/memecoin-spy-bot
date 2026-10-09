
async def _fetch_candidates():
    global _next_request_time, _consecutive_errors

    errors = []
    combined = []
    source_diagnostics = []

    # 1. CoinGecko: ana kaynak
    try:
        candidates, diagnostics = await _fetch_coingecko()
        combined.extend(candidates)
        source_diagnostics.append(diagnostics)
    except Exception as exc:
        errors.append(
            f"coingecko_failed:{type(exc).__name__}:{str(exc)[:120]}"
        )
        logger.warning("CoinGecko scan failed: %s", errors[-1])

    # 2. DEX Screener: ek keşif kaynağı
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/json",
                "User-Agent": "MemecoinSpyBot/1.5",
            },
        ) as client:
            pairs, discovered, batch_errors = await _discover_dex_pairs(client)

        candidates, diagnostics = _normalize_dex_pairs(pairs)
        diagnostics.update({
            "provider": "DEX Screener",
            "discovered_solana_tokens": discovered,
            "batch_errors": batch_errors,
        })
        combined.extend(candidates)
        source_diagnostics.append(diagnostics)

    except Exception as exc:
        errors.append(
            f"dexscreener_failed:{type(exc).__name__}:{str(exc)[:120]}"
        )
        logger.warning("DEX Screener scan failed: %s", errors[-1])

    # 3. GeckoTerminal: yedek keşif kaynağı
    try:
        candidates, diagnostics = await _fetch_gecko_fallback()
        combined.extend(candidates)
        source_diagnostics.append(diagnostics)

    except Exception as exc:
        errors.append(
            f"geckoterminal_failed:{type(exc).__name__}:{str(exc)[:120]}"
        )
        logger.warning("GeckoTerminal scan failed: %s", errors[-1])

    if not combined:
        _consecutive_errors += 1
        _next_request_time = time.time() + min(
            ERROR_COOLDOWN_SECONDS
            * (2 ** min(_consecutive_errors - 1, 4)),
            MAX_ERROR_COOLDOWN_SECONDS,
        )
        raise RuntimeError("; ".join(errors) or "no_market_data")

    # Aynı tokenı tekilleştir; en yüksek likiditeli havuzu koru.
    combined = _dedupe(combined)
    _consecutive_errors = 0
    _next_request_time = time.time() + CACHE_SECONDS

    diagnostics = {
        "provider": "Combined",
        "sources": source_diagnostics,
        "source_errors": errors,
        "unique_candidates": len(combined),
    }

    return combined, diagnostics
