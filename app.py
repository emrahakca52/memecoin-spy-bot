from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query

from signal_engine import get_signal_candidates
from wallet_tracker import get_wallet_stats
from paper_engine import (
    paper_status,
    list_paper_trades,
    record_paper_trade,
    portfolio_status,
)
from auto_paper_bot import bot_status, start_bot, stop_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Paper bot is OFF by default.
    yield
    await stop_bot()


app = FastAPI(
    title="Memecoin Spy Pro",
    version="1.1.1",
    lifespan=lifespan,
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
    }


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
        if exc.response.status_code == 429:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Market data provider rate limit. "
                    "Please wait before retrying."
                ),
            )

        raise HTTPException(
            status_code=502,
            detail=(
                "Market data provider returned HTTP "
                f"{exc.response.status_code}."
            ),
        )

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not reach market data provider: "
                f"{type(exc).__name__}"
            ),
        )


@app.get("/wallet/{wallet_address}")
async def wallet(
    wallet_address: str,
    limit: int = Query(20, ge=1, le=100),
):
    try:
        return await get_wallet_stats(
            wallet_address,
            limit=limit,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Could not reach the Solana RPC provider.",
        )


@app.get("/paper/status")
def get_paper_status():
    return {
        **paper_status(),
        **portfolio_status(),
        "bot": bot_status(),
    }


@app.get("/paper/trades")
def get_paper_trades():
    return {
        "mode": "paper",
        "trades": list_paper_trades(),
    }


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
    required = {
        "token_address",
        "side",
        "amount_usd",
        "price_usd",
    }

    if not required.issubset(payload):
        raise HTTPException(
            status_code=400,
            detail=f"Required fields: {sorted(required)}",
        )

    try:
        return record_paper_trade(
            token_address=str(payload["token_address"]),
            side=str(payload["side"]),
            amount_usd=float(payload["amount_usd"]),
            price_usd=float(payload["price_usd"]),
            token_symbol=str(payload.get("token_symbol", "")),
            source="manual",
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )
