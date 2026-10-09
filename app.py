import os
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
import httpx

from signal_engine import get_signal_candidates
from wallet_tracker import get_wallet_stats
from paper_engine import paper_status, record_paper_trade, list_paper_trades

app = FastAPI(title="Memecoin Spy Pro", version="1.0.0")

@app.get("/")
def home():
    return {
        "name": "Memecoin Spy Pro",
        "status": "running",
        "mode": "paper",
        "note": "No real orders are sent.",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper"}

@app.get("/signals")
async def signals(
    min_liquidity_usd: float = Query(10000, ge=0),
    min_volume_24h_usd: float = Query(20000, ge=0),
    min_buys_sells_ratio: float = Query(1.0, ge=0),
    limit: int = Query(20, ge=1, le=50)
):
    """Discover and filter Solana token pairs using public DexScreener data."""
    try:
        result = await get_signal_candidates(
            min_liquidity_usd=min_liquidity_usd,
            min_volume_24h_usd=min_volume_24h_usd,
            min_buys_sells_ratio=min_buys_sells_ratio,
            limit=limit
        )
        return result
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 429:
            raise HTTPException(status_code=503, detail="DexScreener rate limit. Wait before retrying.")
        raise HTTPException(status_code=502, detail=f"Market data provider returned HTTP {status}.")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not reach market data provider.")

@app.get("/wallet/{wallet_address}")
async def wallet(wallet_address: str, limit: int = Query(20, ge=1, le=100)):
    """Summarize public Solana transaction activity; this is not a profitability score."""
    try:
        return await get_wallet_stats(wallet_address, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not reach the Solana RPC provider.")

@app.get("/paper/status")
def get_paper_status():
    return paper_status()

@app.get("/paper/trades")
def get_paper_trades():
    return {"mode": "paper", "trades": list_paper_trades()}

@app.post("/paper/trades")
def add_paper_trade(payload: dict):
    """Manually record a simulated trade only. Does not connect to a wallet."""
    required = {"token_address", "side", "amount_usd", "price_usd"}
    if not required.issubset(payload):
        raise HTTPException(status_code=400, detail=f"Required fields: {sorted(required)}")
    try:
        return record_paper_trade(
            token_address=str(payload["token_address"]),
            side=str(payload["side"]),
            amount_usd=float(payload["amount_usd"]),
            price_usd=float(payload["price_usd"])
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
