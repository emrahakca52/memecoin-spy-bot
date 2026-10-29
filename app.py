import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import math
import os
import re

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from signal_engine import get_signal_candidates, get_signal_engine_status
from wallet_tracker import get_wallet_stats, get_wallet_token_activity
from paper_engine import (
    get_paper_status as paper_status,
    get_paper_trades as list_paper_trades,
    record_paper_trade,
    get_price_diagnostics,
    get_token_price_usd,
    load_paper_state,
)
from auto_paper_bot import bot_status, start_bot, stop_bot
from config import MIN_LIQUIDITY_USD, MIN_VOLUME_24H_USD, MIN_BUYS_SELLS_RATIO
from grok import evaluate_token
from telegram_positions import positions_dashboard_loop

logger = logging.getLogger("memecoin_spy")

BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
BIRDEYE_TEST_TOKEN = "So11111111111111111111111111111111111111112"


@asynccontextmanager
async def lifespan(app: FastAPI):
    dashboard_stop = asyncio.Event()
    dashboard_task = None
    try:
        result = load_paper_state()
        logger.info("Paper state load result: %s", result)
    except Exception:
        logger.exception("Paper state could not be loaded.")

    try:
        result = await start_bot()
        logger.info("Paper bot startup result: %s", result)
    except Exception:
        logger.exception("Paper bot could not be started.")

    # Start the single-message Telegram dashboard only after saved positions load.
    dashboard_task = asyncio.create_task(positions_dashboard_loop(dashboard_stop))
    yield

    dashboard_stop.set()
    try:
        await asyncio.wait_for(dashboard_task, timeout=5)
    except asyncio.TimeoutError:
        dashboard_task.cancel()
    except Exception:
        logger.exception("Telegram positions dashboard shutdown error.")

    try:
        result = await stop_bot()
        logger.info("Paper bot shutdown result: %s", result)
    except Exception:
        logger.exception("Paper bot shutdown error.")


app = FastAPI(title="Memecoin Spy Pro", version="1.4.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://hoppscotch.io", "https://app.hoppscotch.io"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["X-Diagnostic-Token", "Content-Type"],
    allow_credentials=False,
)


@app.get("/")
def home():
    return {
        "name": "Memecoin Spy Pro",
        "status": "running",
        "mode": "paper",
        "real_trading_enabled": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "mode": "paper",
        "real_trading_enabled": False,
        "bot": bot_status(),
    }


@app.get("/signals/status")
def signal_status():
    """Read-only discovery-provider diagnostics; does not trigger a market-data scan."""
    return get_signal_engine_status()


@app.post("/ai/diagnostic")
async def ai_diagnostic(x_diagnostic_token: str = Header(default="", alias="X-Diagnostic-Token")):
    """Make one tiny AI API request for diagnostics only; never scans or opens a paper position."""
    provider = os.getenv("AI_PROVIDER", "grok").strip().lower()
    if provider != "gemini":
        raise HTTPException(status_code=409, detail="AI_PROVIDER is not gemini.")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or not x_diagnostic_token or x_diagnostic_token != api_key:
        raise HTTPException(status_code=401, detail="Send the configured Gemini API key in the X-Diagnostic-Token header.")
    diagnostic_token = {
        "token_address": "diagnostic-only-no-token",
        "symbol": "AI_DIAGNOSTIC",
        "name": "AI API connectivity test",
        "price_usd": 1.0,
        "liquidity_usd": 10000.0,
        "volume_24h_usd": 40000.0,
        "buys_24h": 20,
        "sells_24h": 10,
        "buy_sell_ratio": 2.0,
        "trade_count_24h": 30,
        "unique_wallets_24h": 20,
        "price_change_24h_pct": 0.0,
        "score": 0.0,
    }
    diagnostic_rugcheck = {
        "provider": "diagnostic_only",
        "passed": True,
        "score": 0,
        "top10_holders_pct": 0.0,
        "lp_locked_pct": 100.0,
        "mint_authority_active": False,
        "freeze_authority_active": False,
        "insider_network_count": 0,
        "risk_flags": [],
        "rejection_reasons": [],
        "missing_data": [],
    }
    result = await evaluate_token(diagnostic_token, diagnostic_rugcheck)
    return {
        "diagnostic_only": True,
        "mode": "paper",
        "real_trading_enabled": False,
        "provider": result.get("provider"),
        "model": result.get("model"),
        "api_ok": result.get("api_ok", False),
        "decision": result.get("decision", "SKIP"),
        "http_status": result.get("http_status"),
        "reasons": result.get("reasons", []),
        "note": "This endpoint sends one small AI request and never scans tokens or opens positions.",
    }


@app.get("/wallet/activity/{wallet_address}")
async def wallet_activity(
    wallet_address: str,
    limit: int = Query(10, ge=1, le=20),
):
    """Read-only recent SPL token balance changes for a public wallet."""
    try:
        return await get_wallet_token_activity(wallet_address, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Solana RPC providers are temporarily unavailable.",
        ) from exc


@app.get("/signals")
async def signals(
    min_liquidity_usd: float = Query(MIN_LIQUIDITY_USD, ge=0),
    min_volume_24h_usd: float = Query(MIN_VOLUME_24H_USD, ge=0),
    min_buys_sells_ratio: float = Query(MIN_BUYS_SELLS_RATIO, ge=0),
    limit: int = Query(20, ge=1, le=50),
):
    try:
        return await get_signal_candidates(
            min_liquidity_usd=min_liquidity_usd,
            min_volume_24h_usd=min_volume_24h_usd,
            min_buys_sells_ratio=min_buys_sells_ratio,
            limit=limit,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach market data provider.") from exc


def _chart_num(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes[:period]]
    losses = [max(-change, 0.0) for change in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for change in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


@app.get("/market/chart/{token_address}")
async def market_chart(
    token_address: str,
    timeframe: str = Query("minute", pattern="^(minute|hour|day)$"),
    aggregate: int = Query(1, ge=1, le=24),
    limit: int = Query(60, ge=20, le=200),
):
    """Read-only live price, GeckoTerminal OHLCV candles, and technical indicators."""
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", token_address):
        raise HTTPException(status_code=400, detail="Invalid Solana token address.")

    timeout = httpx.Timeout(10.0, connect=5.0)
    headers = {"accept": "application/json", "user-agent": "MemecoinSpyPro/1.0"}
    provider_diagnostics = {"dexscreener": [], "geckoterminal_pools": None, "geckoterminal_ohlcv": None}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        # DexScreener provides a current market snapshot and identifies the best liquid pair.
        pair_payload = None
        for url in (
            f"https://api.dexscreener.com/tokens/v1/solana/{token_address}",
            f"https://api.dexscreener.com/latest/dex/tokens/{token_address}",
        ):
            try:
                response = await client.get(url)
                if response.status_code == 429:
                    provider_diagnostics["dexscreener"].append({"status": 429, "endpoint": url.rsplit("/", 1)[-1]})
                    continue
                response.raise_for_status()
                payload = response.json()
                pairs = payload if isinstance(payload, list) else payload.get("pairs", []) if isinstance(payload, dict) else []
                sol_pairs = [
                    pair for pair in pairs
                    if isinstance(pair, dict)
                    and str(pair.get("chainId", "solana")).lower() == "solana"
                    and _chart_num(pair.get("priceUsd")) > 0
                ]
                if sol_pairs:
                    pair_payload = max(
                        sol_pairs,
                        key=lambda pair: _chart_num((pair.get("liquidity") or {}).get("usd")),
                    )
                    provider_diagnostics["dexscreener"].append({"status": response.status_code, "pairs_found": len(sol_pairs)})
                    break
                provider_diagnostics["dexscreener"].append({"status": response.status_code, "pairs_found": 0})
            except (httpx.HTTPError, ValueError) as exc:
                provider_diagnostics["dexscreener"].append({"error": type(exc).__name__, "endpoint": url.rsplit("/", 1)[-1]})
                logger.warning("DexScreener chart snapshot failed for token %s", token_address[:8])
            except Exception:
                logger.exception("Unexpected DexScreener chart error")

        # GeckoTerminal supplies actual OHLCV candles; do not fabricate candles from snapshots.
        ranked_pools = []
        try:
            response = await client.get(
                f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{token_address}/pools",
                params={"page": 1},
            )
            provider_diagnostics["geckoterminal_pools"] = {"status": response.status_code}
            response.raise_for_status()
            pools_data = response.json().get("data", [])
            ranked_pools = sorted(
                [item for item in pools_data if isinstance(item, dict)],
                key=lambda item: _chart_num((item.get("attributes") or {}).get("reserve_in_usd")),
                reverse=True,
            )[:3]
            provider_diagnostics["geckoterminal_pools"]["pools_found"] = len(pools_data)
        except (httpx.HTTPError, ValueError) as exc:
            pool_error = {"error": type(exc).__name__}
            if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                pool_error["status"] = exc.response.status_code
                pool_error["response_excerpt"] = exc.response.text[:240]
                pool_error["retry_after"] = exc.response.headers.get("retry-after")
            elif isinstance(exc, httpx.RequestError):
                pool_error["message"] = str(exc)[:240]
            provider_diagnostics["geckoterminal_pools"] = pool_error
            logger.warning("GeckoTerminal pool discovery failed for token %s: %s", token_address[:8], pool_error)

        # If pool discovery is rate-limited, reuse the DexScreener pair address
        # as a direct GeckoTerminal pool candidate when available.
        if pair_payload:
            pair_address = pair_payload.get("pairAddress")
            if pair_address and not any(
                ((item.get("attributes") or {}).get("address") == pair_address)
                for item in ranked_pools
            ):
                base_address = str((pair_payload.get("baseToken") or {}).get("address") or "")
                quote_address = str((pair_payload.get("quoteToken") or {}).get("address") or "")
                ranked_pools.append({
                    "attributes": {"address": pair_address, "reserve_in_usd": _chart_num((pair_payload.get("liquidity") or {}).get("usd"))},
                    "relationships": {
                        "base_token": {"data": {"id": "solana_" + base_address}},
                        "quote_token": {"data": {"id": "solana_" + quote_address}},
                    },
                })

        candles = []
        candle_source = None
        ohlcv_attempts = []
        # Try up to three liquid pools. Select base/quote according to the actual
        # token relationship, instead of always asking for the pool's base token.
        for pool in ranked_pools:
            attrs = pool.get("attributes") or {}
            pool_address = attrs.get("address")
            relationships = pool.get("relationships") or {}
            base_id = str((((relationships.get("base_token") or {}).get("data") or {}).get("id")) or "")
            quote_id = str((((relationships.get("quote_token") or {}).get("data") or {}).get("id")) or "")
            if base_id.endswith("_" + token_address):
                token_side = "base"
            elif quote_id.endswith("_" + token_address):
                token_side = "quote"
            else:
                token_side = "base"
            if not pool_address:
                continue
            try:
                response = await client.get(
                    f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool_address}/ohlcv/{timeframe}",
                    params={"aggregate": aggregate, "limit": limit, "currency": "usd", "token": token_side},
                )
                attempt = {"pool": pool_address, "token_side": token_side, "status": response.status_code}
                ohlcv_attempts.append(attempt)
                response.raise_for_status()
                payload = response.json()
                ohlcv = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list", [])
                parsed_candles = []
                for row in ohlcv if isinstance(ohlcv, list) else []:
                    if not isinstance(row, list) or len(row) < 6:
                        continue
                    timestamp = int(_chart_num(row[0], -1))
                    o, h, low, close, volume = map(_chart_num, row[1:6])
                    if timestamp >= 0 and min(o, h, low, close) > 0:
                        parsed_candles.append({
                            "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                            "timestamp_unix": timestamp,
                            "open": o, "high": h, "low": low, "close": close,
                            "volume_usd": max(0.0, volume),
                        })
                if parsed_candles:
                    candles = parsed_candles
                    candles.sort(key=lambda candle: candle["timestamp_unix"])
                    candle_source = "GeckoTerminal OHLCV"
                    break
                attempt["candles_returned"] = 0
            except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
                ohlcv_attempts.append({"pool": pool_address, "token_side": token_side, "error": type(exc).__name__})
                logger.warning("GeckoTerminal OHLCV request failed for token %s", token_address[:8])
        provider_diagnostics["geckoterminal_ohlcv"] = {"attempts": ohlcv_attempts, "candles_returned": len(candles)}

        # Birdeye is a second real OHLCV source when its API key is configured.
        # Never synthesize candles from current-price snapshots.
        birdeye_key = os.getenv("BIRDEYE_API_KEY", "").strip()
        if not candles and birdeye_key:
            seconds_per_candle = {"minute": 60, "hour": 3600, "day": 86400}[timeframe] * aggregate
            now_unix = int(datetime.now(timezone.utc).timestamp())
            birdeye_type = f"{aggregate}m" if timeframe == "minute" else f"{aggregate}H" if timeframe == "hour" else f"{aggregate}D"
            try:
                response = await client.get(
                    "https://public-api.birdeye.so/defi/ohlcv",
                    params={"address": token_address, "type": birdeye_type, "time_from": now_unix - limit * seconds_per_candle, "time_to": now_unix},
                    headers={"X-API-KEY": birdeye_key, "x-chain": "solana", "accept": "application/json"},
                )
                birdeye_diag = {"status": response.status_code, "type": birdeye_type}
                response.raise_for_status()
                payload = response.json()
                raw_items = ((payload.get("data") or {}).get("items") or []) if isinstance(payload, dict) else []
                parsed_candles = []
                for item in raw_items if isinstance(raw_items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    timestamp = int(_chart_num(item.get("unixTime", item.get("time", -1)), -1))
                    o = _chart_num(item.get("o", item.get("open")))
                    h = _chart_num(item.get("h", item.get("high")))
                    low = _chart_num(item.get("l", item.get("low")))
                    close = _chart_num(item.get("c", item.get("close")))
                    volume = _chart_num(item.get("v", item.get("volumeUsd", item.get("volume"))))
                    if timestamp >= 0 and min(o, h, low, close) > 0:
                        parsed_candles.append({
                            "timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                            "timestamp_unix": timestamp, "open": o, "high": h,
                            "low": low, "close": close, "volume_usd": max(0.0, volume),
                        })
                if parsed_candles:
                    parsed_candles.sort(key=lambda candle: candle["timestamp_unix"])
                    candles = parsed_candles[-limit:]
                    candle_source = "Birdeye OHLCV"
                birdeye_diag["candles_returned"] = len(parsed_candles)
                provider_diagnostics["birdeye_ohlcv"] = birdeye_diag
            except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
                birdeye_diag = {"error": type(exc).__name__, "type": birdeye_type}
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                    birdeye_diag["status"] = exc.response.status_code
                    birdeye_diag["response_excerpt"] = exc.response.text[:200]
                provider_diagnostics["birdeye_ohlcv"] = birdeye_diag
                logger.warning("Birdeye OHLCV request failed for token %s: %s", token_address[:8], birdeye_diag)

    # Final price-only fallback: Jupiter Price API v3. It does not provide candles,
    # so return the live USD mark with candle/indicator fields explicitly unavailable.
    jupiter_price_data = None
    try:
        jupiter_headers = {"accept": "application/json", "user-agent": "MemecoinSpyPro/1.0"}
        jupiter_api_key = os.getenv("JUPITER_API_KEY", "").strip()
        if jupiter_api_key:
            jupiter_headers["x-api-key"] = jupiter_api_key
        async with httpx.AsyncClient(timeout=timeout, headers=jupiter_headers) as jupiter_client:
            response = await jupiter_client.get(
                "https://api.jup.ag/price/v3",
                params={"ids": token_address},
            )
        provider_diagnostics["jupiter_price"] = {"status": response.status_code}
        response.raise_for_status()
        price_payload = response.json()
        item = price_payload.get(token_address, {}) if isinstance(price_payload, dict) else {}
        if isinstance(item, dict) and _chart_num(item.get("usdPrice")) > 0:
            jupiter_price_data = item
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        provider_diagnostics["jupiter_price"] = {"error": type(exc).__name__}

    if not pair_payload and not candles and not jupiter_price_data:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Live market data is temporarily unavailable from all price providers.",
                "providers": provider_diagnostics,
                "hint": "Check provider HTTP statuses; 429 means rate limiting, while a missing Jupiter price can mean no reliable recent market price.",
            },
        )

    closes = [candle["close"] for candle in candles]
    volumes = [candle["volume_usd"] for candle in candles]
    current_price = _chart_num((pair_payload or {}).get("priceUsd"))
    if current_price <= 0 and jupiter_price_data:
        current_price = _chart_num(jupiter_price_data.get("usdPrice"))
    if current_price <= 0 and closes:
        current_price = closes[-1]
    avg_volume = sum(volumes[-20:]) / len(volumes[-20:]) if volumes else None
    latest_volume = volumes[-1] if volumes else None
    volume_ratio = latest_volume / avg_volume if avg_volume and latest_volume is not None else None
    rsi14 = _rsi(closes, 14)
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    trend = "insufficient_data"
    if ema9 is not None and ema21 is not None:
        trend = "bullish" if ema9 > ema21 else "bearish" if ema9 < ema21 else "neutral"
    signals = []
    if rsi14 is not None:
        if rsi14 >= 70:
            signals.append("RSI overbought zone")
        elif rsi14 <= 30:
            signals.append("RSI oversold zone")
    if volume_ratio is not None and volume_ratio >= 2:
        signals.append("Latest candle volume is at least 2x its recent average")
    if trend == "bullish":
        signals.append("EMA9 is above EMA21")
    elif trend == "bearish":
        signals.append("EMA9 is below EMA21")

    pair = pair_payload or {}
    liquidity = _chart_num((pair.get("liquidity") or {}).get("usd"))
    volume24 = _chart_num(((pair.get("volume") or {}).get("h24")))
    return {
        "token_address": token_address,
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "name": (pair.get("baseToken") or {}).get("name"),
        "mode": "paper",
        "real_trading_enabled": False,
        "as_of_utc": datetime.now(timezone.utc).isoformat(),
        "price_usd": current_price or None,
        "price_change_5m_pct": _chart_num((pair.get("priceChange") or {}).get("m5")) if pair else None,
        "price_change_1h_pct": _chart_num((pair.get("priceChange") or {}).get("h1")) if pair else None,
        "price_change_24h_pct": (_chart_num((pair.get("priceChange") or {}).get("h24")) if pair else (_chart_num(jupiter_price_data.get("priceChange24h")) if jupiter_price_data else None)),
        "liquidity_usd": liquidity or None,
        "volume_24h_usd": volume24 or None,
        "candle_source": candle_source,
        "timeframe": timeframe,
        "aggregate": aggregate,
        "candles_returned": len(candles),
        "candles": candles,
        "indicators": {
            "rsi14": round(rsi14, 2) if rsi14 is not None else None,
            "ema9": ema9,
            "ema21": ema21,
            "trend": trend,
            "latest_candle_volume_usd": latest_volume,
            "average_volume_last_20_candles_usd": avg_volume,
            "latest_volume_vs_average": round(volume_ratio, 2) if volume_ratio is not None else None,
            "signals": signals,
        },
        "chart_data_warning": None if candles else "Price snapshot may be available, but real OHLCV candles are currently unavailable. No synthetic candles were generated.",
        "provider_diagnostics": provider_diagnostics,
    }


@app.get("/paper/status")
def get_paper_status_route():
    return {**paper_status(), "bot": bot_status()}


@app.get("/paper/trades")
def get_paper_trades_route():
    return {"mode": "paper", "trades": list_paper_trades()}


@app.post("/paper/reset")
async def reset_paper_state():
    """Clear all paper trades and positions."""
    try:
        import paper_engine
        paper_engine._open_positions.clear()
        paper_engine._paper_trades.clear()
        paper_engine._realized_pnl_usd = 0.0
        paper_engine._save_paper_state()
        return {"ok": True, "message": "Paper state reset successfully"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/paper-bot/status")
def get_bot_status():
    return bot_status()


@app.post("/paper-bot/start")
async def start_paper_bot():
    return await start_bot()


@app.post("/paper-bot/stop")
async def stop_paper_bot():
    return await stop_bot()


@app.post("/paper/trades")
def add_paper_trade(payload: dict):
    required = {"token_address", "side", "amount_usd", "price_usd"}
    if not required.issubset(payload):
        raise HTTPException(status_code=400, detail=f"Required fields: {sorted(required)}")
    try:
        return record_paper_trade(
            token_address=str(payload["token_address"]),
            side=str(payload["side"]),
            amount_usd=float(payload["amount_usd"]),
            price_usd=float(payload["price_usd"]),
            token_symbol=str(payload.get("token_symbol", "")),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
