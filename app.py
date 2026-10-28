import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

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
from config import MIN_LIQUIDITY_USD, MIN_VOLUME_24H_USD, MIN_BUYS_SELLS_RATIO
from grok import evaluate_token
from telegram_positions import positions_dashboard_loop

logger = logging.getLogger("memecoin_spy")

BIRDEYE_PRICE_URL = "https://public-api.birdeye.so/defi/price"
BIRDEYE_TEST_TOKEN = "So11111111111111111111111111111111111111112"


@asynccontextmanager
async def lifespan(app: FastAPI):
    dashboard_stop = asyncio.Event()
    dashboard_task = asyncio.create_task(positions_dashboard_loop(dashboard_stop))
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
