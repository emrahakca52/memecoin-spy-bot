"""Standalone V2 read-only discovery API; not the production V1 entry point."""
from datetime import datetime,timezone
import asyncio
import os
import time
from fastapi import FastAPI,HTTPException,Query
from memecoin_v2.scanner import Scanner,candidate_to_dict
from memecoin_v2.settings import Settings
from wallet_tracker import get_wallet_token_activity
from telegram_notify import send_scan_report

app=FastAPI(title="Memecoin Spy Bot V2",version="2.0.0",
            description="Read-only Solana discovery and security triage. Paper mode only.")
scanner=Scanner(Settings.from_env())
_telegram_lock=asyncio.Lock()
_last_telegram_report=0.0

@app.get("/health")
async def health():
    return {"ok":True,"service":"memecoin-spy-bot-v2","mode":"paper",
            "real_trading_enabled":False,"utc":datetime.now(timezone.utc).isoformat(),
            "integrations":{"dexscreener":True,"solana_public_rpc":True,
                            "rugcheck":True,"transaction_indexing":False,
                            "live_execution":False}}

@app.get("/v2/scan")
async def scan(limit:int=Query(default=20,ge=1,le=50)):
    global _last_telegram_report
    try:
        candidates=await scanner.scan(limit=limit)
        eligible=sum(1 for c in candidates if c.risk.eligible)
        report={"ok":True,"mode":"paper","real_trading_enabled":False,
                "count":len(candidates),"eligible_count":eligible,
                "candidates":[candidate_to_dict(c) for c in candidates]}
        if os.getenv("V2_TELEGRAM_REPORTS_ENABLED","false").strip().lower() in {"1","true","yes","on"}:
            async with _telegram_lock:
                now=time.monotonic()
                if now-_last_telegram_report>=900:
                    skipped=[{"symbol":c.market.symbol,"reason":",".join(c.risk.missing or c.risk.reasons)}
                             for c in candidates if not c.risk.eligible][:10]
                    await send_scan_report(checked=len(candidates),candidates=len(candidates),
                                           approved=eligible,opened=0,skipped=skipped)
                    _last_telegram_report=now
        return report
    except Exception as exc:
        raise HTTPException(status_code=502,detail="Market or security data provider unavailable") from exc

@app.get("/v2/wallet/{wallet_address}")
async def wallet_activity(wallet_address:str,limit:int=Query(default=10,ge=1,le=20)):
    try:
        result=await get_wallet_token_activity(wallet_address,limit=limit)
        result["service"]="memecoin-spy-bot-v2"
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502,detail="Solana wallet activity provider unavailable") from exc
