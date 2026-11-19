import unittest
from memecoin_v2.providers.helius import HeliusProvider
from memecoin_v2.models import TokenMarket
from memecoin_v2.risk import evaluate_market
from memecoin_v2.settings import Settings
from datetime import datetime,timezone

class FakeResponse:
    def __init__(self,payload): self.payload=payload
    def raise_for_status(self): pass
    def json(self): return self.payload

class FakeClient:
    async def get(self,url,params):
        return FakeResponse([
            {"type":"SWAP","slot":100,"feePayer":"wallet-a"},
            {"type":"SWAP","slot":100,"feePayer":"wallet-b"},
            {"type":"SWAP","slot":100,"feePayer":"wallet-c"},
            {"type":"SWAP","slot":101,"feePayer":"wallet-d"},
        ])

class HeliusTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_fails_closed(self):
        provider=HeliusProvider(client=FakeClient())
        provider.api_key=""
        result=await provider.inspect_pool("pool")
        self.assertFalse(result["available"])
        self.assertIsNone(result["sniper_cluster_detected"])

    async def test_same_slot_cluster_is_flagged(self):
        provider=HeliusProvider(client=FakeClient())
        provider.api_key="test-key"
        result=await provider.inspect_pool("pool")
        self.assertTrue(result["available"])
        self.assertTrue(result["sniper_cluster_detected"])
        self.assertEqual(result["same_slot_swap_cluster_max"],3)

    async def test_known_clean_sample_is_not_flagged(self):
        class CleanClient:
            async def get(self,url,params):
                return FakeResponse([{"type":"SWAP","slot":1,"feePayer":"a"},
                                     {"type":"SWAP","slot":2,"feePayer":"b"}])
        provider=HeliusProvider(client=CleanClient())
        provider.api_key="test-key"
        result=await provider.inspect_pool("pool")
        self.assertFalse(result["sniper_cluster_detected"])

if __name__=="__main__": unittest.main()
