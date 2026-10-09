import asyncio
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI

app = FastAPI(title="Memecoin Spy Pro")

DEX_URL = "https://api.dexscreener.com/token-profiles/latest/v1"

# İstekleri sıklaştırmamak için basit önbellek
CACHE_SECONDS = 30
cached_data = None
cached_at = 0
request_lock = asyncio.Lock()


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
    global cached_data, cached_at

    async with request_lock:
        now = time.monotonic()

        # Son 30 saniyedeki veriyi yeniden kullan
        if cached_data is not None and now - cached_at < CACHE_SECONDS:
            return cached_data

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(DEX_URL)

                if response.status_code == 429:
                    return {
                        "status": "rate_limited",
                        "message": (
                            "DexScreener istek sınırı uyguladı. "
                            "Bir süre sonra yeniden dene."
                        ),
                        "mode": "paper",
                    }

                response.raise_for_status()
                data = response.json()

            if not isinstance(data, list):
                return {
                    "status": "error",
                    "message": "Beklenmeyen API yanıtı.",
                }

            tokens = [
                token for token in data
                if isinstance(token, dict)
                and token.get("chainId") == "solana"
            ]

            result = {
                "mode": "paper",
                "count": len(tokens),
                "signals": tokens[:20],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            cached_data = result
            cached_at = time.monotonic()

            return result

        except httpx.HTTPError as exc:
            return {
                "status": "error",
                "message": str(exc),
                "mode": "paper",
            }

        except Exception as exc:
            return {
                "status": "error",
                "message": str(exc),
                "mode": "paper",
            }
