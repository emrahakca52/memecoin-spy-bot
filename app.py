from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException, Query

from signal_engine import get_signal_candidates, get_signal_engine_status
from wallet_tracker import get_wallet_stats
from paper_engine import (
    get_paper_status as paper_status,
    get_paper_trades as list_paper_trades,
    record_paper_trade,
    get_price_diagnostics,
    get_token_price_usd,
    load_paper_state,
)
from auto_paper_bot import bot_status, start_bot, stop_bot

logger = logging.getLogger("memecoin_spy")

BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
BIRDEYE_TEST_TOKEN = "So11111111111111111111111111111111111111112"


@asynccontextmanager
async def lifespan(app: FastAPI):
    bot_started = False
    try:
        # Load persisted positions and trade history before the bot can run.
        load_paper_state()
        logger.info("Paper state load completed before bot startup.")
        await start_bot()
        bot_started = True
        logger.info("Paper bot startup requested; real trading disabled.")
    except Exception:
        # Do not start a fresh paper session if the saved database state could not be read.
        logger.exception("Paper state could not be loaded or paper bot failed to start.")
    try:
        yield
    finally:
        if bot_started:
            try:
                await stop_bot()
                logger.info("Paper bot stopped during application shutdown.")
            except Exception:
                logger.exception("Error while stopping paper bot.")


app = FastAPI(title="Memecoin Spy Pro", version="1.3.2", lifespan=lifespan)


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


@app.get("/paper-bot/birdeye-test")
async def birdeye_price_test(
    token_address: str = Query(BIRDEYE_TEST_TOKEN, min_length=32, max_length=64)
):
    """One on-demand diagnostic request; does not change bot settings or trade."""
    api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "provider": "birdeye",
                "result": "api_key_missing",
                "message": "Set BIRDEYE_API_KEY in Render Environment and redeploy.",
            },
        )
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            headers={"Accept": "application/json", "X-API-KEY": api_key, "x-chain": "solana"},
        ) as client:
            response = await client.get(BIRDEYE_PRICE_URL, params={"address": token_address})
    except httpx.TimeoutException:
        return {"provider": "birdeye", "result": "timeout", "token_address": token_address}
    except httpx.HTTPError as exc:
        return {"provider": "birdeye", "result": "network_error", "token_address": token_address, "error_type": type(exc).__name__}

    if response.status_code != 200:
        messages = {
            401: "API key missing or invalid.",
            403: "Access denied; endpoint may not be included in this package.",
            429: "Birdeye rate limit reached; wait before retrying.",
        }
        return {
            "provider": "birdeye", "result": "http_error",
            "http_status": response.status_code, "token_address": token_address,
            "message": messages.get(response.status_code, "Non-200 response."),
        }
    try:
        payload = response.json()
    except ValueError:
        return {"provider": "birdeye", "result": "invalid_json", "http_status": response.status_code, "token_address": token_address}

    data = payload.get("data") if isinstance(payload, dict) else None
    try:
        price = float(data.get("value")) if isinstance(data, dict) else 0.0
    except (TypeError, ValueError, OverflowError):
        price = 0.0
    if price <= 0:
        return {
            "provider": "birdeye", "result": "no_usable_price",
            "http_status": response.status_code, "token_address": token_address,
            "response_success": payload.get("success") if isinstance(payload, dict) else None,
        }
    return {
        "provider": "birdeye", "result": "success", "http_status": response.status_code,
        "token_address": token_address, "price_usd": price,
        "message": "Birdeye returned a usable price; this was a diagnostic request only.",
    }


@app.get("/paper-bot/birdeye-listing-test")
async def birdeye_listing_test():
    """Diagnostic only: test Birdeye new-token discovery; never creates a trade."""
    api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="BIRDEYE_API_KEY is not configured.")

    url = "https://public-api.birdeye.so/defi/v2/tokens/new_listing"
    headers = {"accept": "application/json", "X-API-KEY": api_key, "x-chain": "solana"}
    params = {"limit": 10, "meme_platform_enabled": "true"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            response = await client.get(url, params=params, headers=headers)
        status_code = response.status_code
        if status_code != 200:
            return {
                "provider": "birdeye", "result": "error", "endpoint": url,
                "http_status": status_code, "response_preview": response.text[:500],
                "mode": "paper", "real_trading_enabled": False,
                "message": "Discovery diagnostic only; no trade or position was created.",
            }
        payload = response.json()
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = next((data.get(k) for k in ("items", "tokens", "list", "result") if isinstance(data.get(k), list)), [])
        else:
            items = []
        preview = []
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            preview.append({
                key: item.get(key)
                for key in (
                    "address", "token_address", "symbol", "name", "decimals", "price", "priceUsd",
                    "liquidity", "liquidity_usd", "volume24h", "volume_24h_usd", "market_cap",
                    "marketCap", "listedAt", "listTime", "blockUnixTime",
                )
                if item.get(key) is not None
            })
        return {
            "provider": "birdeye",
            "result": "success" if payload.get("success", True) and items else ("empty" if payload.get("success", True) else "api_error"),
            "endpoint": url, "http_status": status_code,
            "api_success": payload.get("success") if isinstance(payload, dict) else None,
            "data_keys": list(data.keys())[:30] if isinstance(data, dict) else None,
            "items_found": len(items), "sample_tokens": preview,
            "mode": "paper", "real_trading_enabled": False,
            "message": "Discovery diagnostic only; no trade or position was created.",
        }
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Birdeye listing request failed: {type(exc).__name__}") from exc


@app.get("/paper-bot/birdeye-engine-test")
async def birdeye_engine_test(token_address: str = Query(BIRDEYE_TEST_TOKEN, min_length=32, max_length=64)):
    """Calls the paper engine price function; never opens a position or places an order."""
    try:
        price = await get_token_price_usd(token_address)
    except Exception as exc:
        logger.exception("Birdeye engine diagnostic failed.")
        return {"provider": "birdeye", "result": "engine_error", "error_type": type(exc).__name__, "diagnostics": get_price_diagnostics()}
    return {
        "provider": "birdeye", "result": "success" if price is not None and price > 0 else "no_usable_price",
        "token_address": token_address, "price_usd": price, "diagnostics": get_price_diagnostics(),
        "message": "Called the paper engine price function; no trade or position was created.",
    }


@app.get("/paper-bot/signal-engine-status")
def signal_engine_status():
    """Safe discovery diagnostics; never places trades."""
    return get_signal_engine_status()


@app.get("/signals")
async def signals(
    min_liquidity_usd: float = Query(10000, ge=0),
    min_volume_24h_usd: float = Query(20000, ge=0),
    min_buys_sells_ratio: float = Query(1.0, ge=0),
    limit: int = Query(20, ge=1, le=50),
):
    try:
        return await get_signal_candidates(
            min_liquidity_usd=min_liquidity_usd,
            min_volume_24h_usd=min_volume_24h_usd,
            min_buys_sells_ratio=min_buys_sells_ratio,
            limit=limit,
        )
    except httpx.HTTPStatusError as exc:
        status = 503 if exc.response.status_code == 429 else 502
        raise HTTPException(status_code=status, detail="Market data provider is temporarily unavailable.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach market data provider: {type(exc).__name__}") from exc


@app.get("/wallet/{wallet_address}")
async def wallet(wallet_address: str, limit: int = Query(20, ge=1, le=100)):
    try:
        return await get_wallet_stats(wallet_address, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach the Solana RPC provider.") from exc


@app.get("/paper/status")
def get_paper_status_route():
    return {**paper_status(), "bot": bot_status()}


@app.get("/paper/trades")
def get_paper_trades_route():
    return {"mode": "paper", "trades": list_paper_trades()}


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
