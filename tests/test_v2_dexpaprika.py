import unittest
from memecoin_v2.providers.dexpaprika import DexPaprikaProvider


class FakeResponse:
    def raise_for_status(self): return None
    def json(self):
        return {"results": [{
            "id": "pool-123", "created_at": "2026-11-19T10:00:00Z",
            "volume_usd_24h": 80000, "transactions_24h": 75,
            "liquidity_usd": 45000, "dex_name": "Raydium",
            "tokens": [
                {"address": "mint123", "symbol": "EX", "name": "Example", "role": "base"},
                {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL", "name": "Wrapped SOL"}
            ]
        }]}


class FakeClient:
    async def get(self, url, params=None):
        self.url, self.params = url, params
        return FakeResponse()


class DexPaprikaTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_solana_pools_are_normalized(self):
        client = FakeClient()
        rows = await DexPaprikaProvider(client=client).discover_solana(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].address, "mint123")
        self.assertEqual(rows[0].symbol, "EX")
        self.assertEqual(rows[0].source, "dexpaprika")
        self.assertEqual(rows[0].liquidity_usd, 45000)
        self.assertEqual(rows[0].txns_24h, 75)
        self.assertIn("/networks/solana/pools/search", client.url)
        self.assertEqual(client.params["order_by"], "created_at")


if __name__ == "__main__": unittest.main()
