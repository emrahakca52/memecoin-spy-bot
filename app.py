import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI

app = FastAPI(title="Memecoin Spy Pro")

DEX_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
import asyncio

@app.get("/")
def home():
    return {
        "name": "Memecoin Spy Pro",
        "status": "running",
        "mode": "paper",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/signals")
async def signals():
    try:
        async with httpx.AsyncClient(timeout=20) as client:
    await asyncio.sleep(2)
    response = await client.get(DEX_URL)

    if response.status_code == 429:
        await asyncio.sleep(10)
        response = await client.get(DEX_URL)

    response.raise_for_status()
    data = response.json()

        tokens = [
            token for token in data
            if token.get("chainId") == "solana"
        ]

        return {
            "mode": "paper",
            "count": len(tokens),
            "signals": tokens[:20],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }
