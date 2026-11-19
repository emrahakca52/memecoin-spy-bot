"""Standalone v2 read-only discovery API; not the production v1 entry point."""
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from memecoin_v2.scanner import Scanner, candidate_to_dict
from memecoin_v2.settings import Settings

app = FastAPI(title="Memecoin Spy Bot V2", version="2.0.0",
              description="Read-only Solana token discovery and risk triage. Paper mode only.")
scanner = Scanner(Settings.from_env())

@app.get("/health")
async def health():
    return {"ok": True, "service": "memecoin-spy-bot-v2", "mode": "paper",
            "real_trading_enabled": False, "utc": datetime.now(timezone.utc).isoformat()}

@app.get("/v2/scan")
async def scan(limit: int = Query(default=20, ge=1, le=50)):
    try:
        candidates = await scanner.scan(limit=limit)
        return {"ok": True, "mode": "paper", "real_trading_enabled": False,
                "count": len(candidates),
                "eligible_count": sum(1 for c in candidates if c.risk.eligible),
                "candidates": [candidate_to_dict(c) for c in candidates]}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Market discovery provider unavailable") from exc
