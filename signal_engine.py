import asyncio, math, os, time
from typing import Any
import httpx

MIN_LIQ = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOL = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))

def _num(v, d=0.0):
    try:
        n = float(v)
        return n if math.isfinite(n) else d
    except:
        return d

async def get_signal_candidates(min_liquidity_usd=None, min_volume_24h_usd=None, min_buys_sells_ratio=None, limit=20):
    min_liq = min_liquidity_usd or MIN_LIQ
    min_vol = min_volume_24h_usd or MIN_VOL
    min_ratio = min_buys_sells_ratio or MIN_RATIO

    candidates = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.dexscreener.com/token-profiles/latest/v1")
            profiles = r.json() if r.status_code == 200 else []
            addrs = [p.get("tokenAddress") for p in profiles if isinstance(p, dict) and p.get("chainId") == "solana"][:25]
            if not addrs:
                return {"signals": [], "checked": 0, "candidate_count": 0}

            r = await client.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(addrs))
            pairs = r.json() if r.status_code == 200 else []

            best = {}
            for p in pairs:
                if not isinstance(p, dict) or p.get("chainId") != "solana":
                    continue
                addr = (p.get("baseToken") or {}).get("address")
                if not addr:
                    continue
                liq = _num((p.get("liquidity") or {}).get("usd"))
                if addr not in best or liq > best[addr][0]:
                    best[addr] = (liq, p)

            for addr, (_, p) in best.items():
                t = p.get("baseToken") or {}
                vol = _num((p.get("volume") or {}).get("h24"))
                tx = (p.get("txns") or {}).get("h24") or {}
                buys = int(_num(tx.get("buys")))
                sells = int(_num(tx.get("sells")))
                change = _num((p.get("priceChange") or {}).get("h24"))
                price = _num(p.get("priceUsd"))
                if vol < min_vol or _num((p.get("liquidity") or {}).get("usd")) < min_liq:
                    continue
                if sells == 0 or buys / sells < min_ratio:
                    continue
                candidates.append({
                    "token_address": addr,
                    "symbol": t.get("symbol") or "UNKNOWN",
                    "price_usd": price,
                    "liquidity_usd": _num((p.get("liquidity") or {}).get("usd")),
                    "volume_24h_usd": vol,
                    "buy_sell_ratio": buys / sells if sells else 0,
                    "trade_count_24h": buys + sells,
                    "price_change_24h_pct": change,
                    "score": round(min(vol/20000, 5) + min(buys/sells if sells else 0, 4), 2),
                })
    except Exception:
        pass

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    return {
        "provider": "DexScreener",
        "checked": len(candidates),
        "candidate_count": len(candidates),
        "signals": candidates[:limit],
        "mode": "paper",
    }

def get_signal_engine_status():
    return {"provider": "DexScreener", "mode": "paper"}
