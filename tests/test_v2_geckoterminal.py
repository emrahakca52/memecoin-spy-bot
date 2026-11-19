import unittest
from memecoin_v2.providers.geckoterminal import GeckoTerminalProvider


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "data": [{
                "id": "solana_pool123",
                "attributes": {
                    "name": "TOKEN / SOL",
                    "base_token_price_usd": "0.01",
                    "reserve_in_usd": "45000",
                    "volume_usd": {"h24": "80000"},
                    "transactions": {"h24": {"buys": 40, "sells": 35}},
                    "price_change_percentage": {"h1": "2.5", "h24": "15"},
                    "pool_created_at": "2026-11-19T10:00:00Z"
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_token123"}},
                    "quote_token": {"data": {"id": "solana_sol"}}
                }
            }],
            "included": [{
                "id": "solana_token123",
                "type": "token",
                "attributes": {"address": "token123", "name": "Example", "symbol": "EX"}
            }]
        }


class FakeClient:
    async def get(self, url, params=None):
        self.url = url
        self.params = params
        return FakeResponse()


class GeckoTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_solana_pools_are_normalized(self):
        client = FakeClient()
        rows = await GeckoTerminalProvider(client=client).discover_solana(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].address, "token123")
        self.assertEqual(rows[0].symbol, "EX")
        self.assertEqual(rows[0].source, "geckoterminal")
        self.assertEqual(rows[0].liquidity_usd, 45000)
        self.assertEqual(rows[0].txns_24h, 75)
        self.assertIn("/networks/solana/new_pools", client.url)

    async def test_empty_feed_returns_empty_list(self):
        class EmptyResponse(FakeResponse):
            def json(self):
                return {"data": [], "included": []}
        class EmptyClient(FakeClient):
            async def get(self, url, params=None):
                return EmptyResponse()
        self.assertEqual(await GeckoTerminalProvider(client=EmptyClient()).discover_solana(), [])


if __name__ == "__main__":
    unittest.main()
