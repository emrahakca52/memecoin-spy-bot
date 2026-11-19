import unittest
from memecoin_v2.providers.helius import HeliusProvider

class FakeResponse:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return {"result": self.payload}

class FakeClient:
    async def post(self, url, json):
        method = json["method"]
        if method == "getSignaturesForAddress":
            return FakeResponse([{"signature": f"sig-{i}"} for i in range(4)])
        i = int(json["params"][0].split("-")[1])
        return FakeResponse({
            "slot": 100 if i < 3 else 101,
            "meta": {"logMessages": ["Program log: swap"]},
            "transaction": {"message": {"accountKeys": [
                {"pubkey": f"wallet-{i}", "signer": True}
            ]}}
        })

class HeliusTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_key_public_rpc_detects_same_slot_cluster(self):
        provider = HeliusProvider(client=FakeClient())
        result = await provider.inspect_pool("pool")
        self.assertTrue(result["available"])
        self.assertTrue(result["sniper_cluster_detected"])
        self.assertEqual(result["same_slot_swap_cluster_max"], 3)
        self.assertEqual(result["transactions_examined"], 4)

    async def test_missing_pool_fails_closed(self):
        result = await HeliusProvider(client=FakeClient()).inspect_pool(None)
        self.assertFalse(result["available"])
        self.assertIsNone(result["sniper_cluster_detected"])

    async def test_unavailable_rpc_fails_closed(self):
        class BrokenClient:
            async def post(self, url, json):
                raise RuntimeError("offline")
        result = await HeliusProvider(client=BrokenClient()).inspect_pool("pool")
        self.assertFalse(result["available"])
        self.assertIsNone(result["sniper_cluster_detected"])

if __name__ == "__main__":
    unittest.main()
