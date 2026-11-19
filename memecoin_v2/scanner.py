"""Orchestrates discovery, public RPC and RugCheck security review."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from .models import RankedCandidate
from .providers.dexscreener import DexScreenerProvider
from .providers.rugcheck import RugCheckProvider
from .providers.solana_rpc import SolanaRpcProvider
from .risk import evaluate_market
from .scoring import score_market
from .settings import Settings

class Scanner:
    def __init__(self, settings: Settings|None=None, provider=None, onchain_provider=None, rugcheck_provider=None):
        self.settings=settings or Settings.from_env()
        self.provider=provider or DexScreenerProvider(timeout=self.settings.request_timeout_seconds)
        self.onchain_provider=onchain_provider or SolanaRpcProvider(timeout=self.settings.request_timeout_seconds)
        self.rugcheck_provider=rugcheck_provider or RugCheckProvider(timeout=self.settings.request_timeout_seconds)

    async def scan(self, limit: int=30) -> list[RankedCandidate]:
        markets=await self.provider.discover_solana(limit=max(1,min(int(limit),50)))
        ranked,seen=[],set()
        for index,market in enumerate(markets):
            key=market.address.lower()
            if not key or key in seen: continue
            seen.add(key)
            if index < 10 and market.chain.lower()=="solana":
                context={"liquidity_usd":market.liquidity_usd,"volume_24h_usd":market.volume_24h_usd,
                         "price_change_24h_pct":market.price_change_24h_pct}
                rpc_task=self.onchain_provider.inspect_mint(market.address)
                rug_task=self.rugcheck_provider.inspect_mint(market.address,context)
                rpc_result,rug_result=await asyncio.gather(rpc_task,rug_task,return_exceptions=True)
                if isinstance(rpc_result,BaseException):
                    rpc_result={"provider":"solana_public_rpc","notes":["On-chain inspection unavailable"]}
                if isinstance(rug_result,BaseException):
                    rug_result={"provider":"rugcheck","available":False,"error":"provider_unavailable"}
                market=replace(market,onchain={"rpc":rpc_result,"rugcheck":rug_result})
            score,signals=score_market(market)
            risk=evaluate_market(market,self.settings)
            ranked.append(RankedCandidate(market,score,risk,signals))
        return sorted(ranked,key=lambda item:(item.risk.eligible,item.score),reverse=True)

def candidate_to_dict(item: RankedCandidate) -> dict:
    m=item.market
    return {
        "chain":m.chain,"address":m.address,"symbol":m.symbol,"name":m.name,
        "price_usd":m.price_usd,"liquidity_usd":m.liquidity_usd,
        "volume_24h_usd":m.volume_24h_usd,"txns_24h":m.txns_24h,
        "price_change_1h_pct":m.price_change_1h_pct,"price_change_24h_pct":m.price_change_24h_pct,
        "pair_created_at_ms":m.pair_created_at_ms,"pair_address":m.pair_address,
        "source":m.source,"market_score":item.score,
        "risk":{"eligible":item.risk.eligible,"score":item.risk.score,
                "reasons":list(item.risk.reasons),"missing":list(item.risk.missing)},
        "security":m.onchain,
        "signals":list(item.signals),"mode":"paper","real_trading_enabled":False,
    }
