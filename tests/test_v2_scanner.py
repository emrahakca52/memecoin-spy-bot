import unittest
from datetime import datetime,timezone
from memecoin_v2.models import TokenMarket
from memecoin_v2.scanner import Scanner,candidate_to_dict
from memecoin_v2.settings import Settings

class FakeProvider:
    def __init__(self,markets): self.markets=markets
    async def discover_solana(self,limit=30): return self.markets[:limit]

class EmptyProvider:
    async def discover_solana(self,limit=30): return []

class FakeOnchainProvider:
    async def inspect_mint(self,mint):
        return {"authority_check":True,"mint_authority_present":False,
                "freeze_authority_present":False,"holder_sample_complete":False}

class FakeAiProvider:
    async def __call__(self,token,rugcheck_result):
        return {"provider":"test","api_ok":True,"passed":True,"decision":"COPY"}

class FakeRugCheckProvider:
    async def inspect_mint(self,mint,market_context=None):
        return {"available":True,"score_normalised":12,"top10_holders_pct":18,
                "lp_locked_pct":80,"mint_authority_active":False,
                "freeze_authority_active":False,"insider_network_count":0,"dangerous_risks":[]}

def market(address,**changes):
    data=dict(chain="solana",address=address,symbol="T",name="Token",price_usd=0.01,
              liquidity_usd=100000,volume_24h_usd=200000,txns_24h=1000,
              price_change_1h_pct=10,price_change_24h_pct=20,
              pair_created_at_ms=int(datetime.now(timezone.utc).timestamp()*1000)-60000)
    data.update(changes)
    return TokenMarket(**data)

class ScannerTests(unittest.IsolatedAsyncioTestCase):
    def scanner(self,markets,onchain=None,rug=None):
        return Scanner(Settings(),FakeProvider(markets),onchain or FakeOnchainProvider(),rug or FakeRugCheckProvider(),ai_provider=FakeAiProvider())
    async def test_gecko_fallback_used_when_primary_is_empty(self):
        class Fallback:
            def __init__(self): self.called=False
            async def discover_solana(self,limit=30):
                self.called=True
                return [market("GECKO",source="geckoterminal")]
        fallback=Fallback()
        scanner=Scanner(Settings(),EmptyProvider(),FakeOnchainProvider(),FakeRugCheckProvider(),ai_provider=FakeAiProvider(),fallback_provider=fallback,paprika_provider=EmptyProvider())
        result=await scanner.scan()
        self.assertTrue(fallback.called)
        self.assertEqual(result[0].market.source,"geckoterminal")

    async def test_deduplicates_addresses(self):
        result=await self.scanner([market("A"),market("A"),market("B",liquidity_usd=1)]).scan()
        self.assertEqual(len(result),2)
        self.assertEqual(result[0].market.address,"A")
        self.assertFalse(result[0].risk.eligible)
    async def test_candidate_never_claims_live_trading(self):
        result=await self.scanner([market("A")]).scan()
        data=candidate_to_dict(result[0])
        self.assertEqual(data["mode"],"paper")
        self.assertFalse(data["real_trading_enabled"])
        self.assertFalse(data["risk"]["eligible"])
        self.assertIn("security",data)
        self.assertIn("bundle_sniper_activity",data["risk"]["missing"])
    async def test_limit_is_clamped(self):
        result=await self.scanner([market(str(i)) for i in range(60)]).scan(limit=500)
        self.assertLessEqual(len(result),50)
    async def test_rpc_and_rugcheck_are_bounded_to_ten_candidates(self):
        class CountingRpc:
            def __init__(self): self.calls=0
            async def inspect_mint(self,mint): self.calls+=1; return {}
        class CountingRug:
            def __init__(self): self.calls=0
            async def inspect_mint(self,mint,market_context=None): self.calls+=1; return {}
        rpc,rug=CountingRpc(),CountingRug()
        await self.scanner([market(str(i)) for i in range(15)],rpc,rug).scan(limit=15)
        self.assertEqual(rpc.calls,10)
        self.assertEqual(rug.calls,10)

if __name__=="__main__": unittest.main()
