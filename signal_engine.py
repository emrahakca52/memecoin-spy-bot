            return _response(
                pairs, min_liquidity_usd, min_volume_24h_usd,
                min_buys_sells_ratio, limit, "DexScreener",
            )

        except Exception as exc:
            _last_error = str(exc)[:250]

            print(
                f"DexScreener request failed: "
                f"{type(exc).__name__}: {_last_error}",
                flush=True,
            )

            if cached_pairs is not None:
                return _response(
                    cached_pairs,
                    min_liquidity_usd,
                    min_volume_24h_usd,
                    min_buys_sells_ratio,
                    limit,
                    "DexScreener stale cache",
                    "Provider failed; cached quotes may be stale.",
                )

            return {
                "provider": "DexScreener",
                "checked": 0,
                "candidate_count": 0,
                "signals": [],
                "mode": "paper",
                "real_trading_enabled": False,
                "note": "Market data unavailable; no fresh signals returned.",
            }
