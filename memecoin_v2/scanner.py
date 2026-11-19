"""Orchestrates discovery, public RPC and RugCheck security review."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from .models import RankedCandidate
from .providers.dexscreener import DexScreenerProvider
from .providers.rugcheck import RugCheckProvider
from .providers.solana_rpc import SolanaRpcProvider
from .providers.helius import HeliusProvider
from .providers.geckoterminal import GeckoTerminalProvider
from .providers.dexpaprika import DexPaprikaProvider
from grok import evaluate_token
from .local_screening import evaluate_local
import os
import logging
from .risk import evaluate_market
from .scoring import score_market
from .settings import Settings

logger=logging.getLogger(__name__)

class Scanner:
    def __init__(self, settings: Settings|None=None, provider=None, onchain_provider=None, rugcheck_provider=None, helius_provider=None, ai_provider=None, fallback_provider=None, paprika_provider=None):
        self.settings=settings or Settings.from_env()
        self.provider=provider or DexScreenerProvider(timeout=self.settings.request_timeout_seconds)
        self.fallback_provider=fallback_provider or GeckoTerminalProvider(timeout=self.settings.request_timeout_seconds)
        self.paprika_provider=paprika_provider or DexPaprikaProvider(timeout=self.settings.request_timeout_seconds)
        self.onchain_provider=onchain_provider or SolanaRpcProvider(timeout=self.settings.request_timeout_seconds)
        self.rugcheck_provider=rugcheck_provider or RugCheckProvider(timeout=self.settings.request_timeout_seconds)
        self.helius_provider=helius_provider or HeliusProvider(timeout=self.settings.request_timeout_seconds)
        self.ai_provider=ai_provider or (evaluate_token if (os.getenv("GEMINI_API_KEY","").strip() if os.getenv("AI_PROVIDER","grok").lower()=="gemini" else os.getenv("XAI_API_KEY","").strip()) else None)

    async def scan(self, limit: int=30) -> list[RankedCandidate]:
        bounded_limit=max(1,min(int(limit),50))
        primary_error=None
        try:
            markets=await self.provider.discover_solana(limit=bounded_limit)
        except Exception as exc:
            markets=[]
            primary_error=type(exc).__name__
            logger.warning("Primary discovery unavailable provider=dexscreener error=%s",primary_error)
        primary_count=len(markets)
        if markets:
            logger.info("Discovery provider=dexscreener candidate_count=%s",primary_count)
        else:
            paprika_error=None
            try:
                markets=await self.paprika_provider.discover_solana(limit=bounded_limit)
                logger.info("Discovery fallback provider=dexpaprika primary_count=%s primary_error=%s fallback_count=%s",primary_count,primary_error,len(markets))
            except Exception as exc:
                paprika_error=type(exc).__name__
                logger.warning("Discovery fallback unavailable provider=dexpaprika error=%s",paprika_error)
                markets=[]
            if not markets:
                try:
                    markets=await self.fallback_provider.discover_solana(limit=bounded_limit)
                    logger.info("Discovery fallback provider=geckoterminal primary_count=%s paprika_error=%s fallback_count=%s",primary_count,paprika_error,len(markets))
                except Exception as exc:
                    logger.warning("Discovery fallback unavailable provider=geckoterminal error=%s",type(exc).__name__)
                    markets=[]
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
                helius_task=self.helius_provider.inspect_pool(market.pair_address)
                token_for_ai={"token_address":market.address,"symbol":market.symbol,"name":market.name,
                              "price_usd":market.price_usd,"liquidity_usd":market.liquidity_usd,
                              "volume_24h_usd":market.volume_24h_usd,"txns_24h":market.txns_24h,
                              "price_change_24h_pct":market.price_change_24h_pct}
                rpc_result,rug_result,helius_result=await asyncio.gather(
                    rpc_task,rug_task,helius_task,return_exceptions=True)
                if isinstance(rpc_result,BaseException):
                    rpc_result={"provider":"solana_public_rpc","notes":["On-chain inspection unavailable"]}
                if isinstance(rug_result,BaseException):
                    rug_result={"provider":"rugcheck","available":False,"error":"provider_unavailable"}
                if isinstance(helius_result,BaseException):
                    helius_result={"provider":"helius","available":False,"sniper_cluster_detected":None,"notes":["provider_unavailable"]}
                try:
                    ai_result=(await self.ai_provider(token_for_ai,rug_result) if self.ai_provider else evaluate_local(token_for_ai,rug_result))
                except Exception:
                    ai_result={"provider":"AI","api_ok":False,"passed":False,"reasons":["provider_unavailable"]}
                if isinstance(ai_result,BaseException):
                    ai_result={"provider":"AI","api_ok":False,"passed":False,"reasons":["provider_unavailable"]}
                market=replace(market,onchain={"rpc":rpc_result,"rugcheck":rug_result,"helius":helius_result,"ai":ai_result})
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
