import unittest
from datetime import datetime, timezone
from memecoin_v2.models import TokenMarket
from memecoin_v2.scanner import Scanner, candidate_to_dict
from memecoin_v2.settings import Settings

class FakeProvider:
    def __init__(self, markets): self.markets = markets
    async def discover_solana(self, limit=30): return self.markets[:limit]

def market(address, **changes):
    data = dict(chain="solana", address=address, symbol="T", name="Token", price_usd=0.01,
                liquidity_usd=100000, volume_24h_usd=200000, txns_24h=1000,
                price_change_1h_pct=10, price_change_24h_pct=20,
                pair_created_at_ms=int(datetime.now(timezone.utc).timestamp()*1000)-60000)
    data.update(changes)
    return TokenMarket(**data)

class ScannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_deduplicates_addresses(self):
        result = await Scanner(Settings(), FakeProvider([market("A"), market("A"), market("B", liquidity_usd=1)])).scan()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].market.address, "A")
        self.assertFalse(result[0].risk.eligible)
    async def test_candidate_never_claims_live_trading(self):
        result = await Scanner(Settings(), FakeProvider([market("A")])).scan()
        data = candidate_to_dict(result[0])
        self.assertEqual(data["mode"], "paper")
        self.assertFalse(data["real_trading_enabled"])
        self.assertFalse(data["risk"]["eligible"])
    async def test_limit_is_clamped(self):
        result = await Scanner(Settings(), FakeProvider([market(str(i)) for i in range(60)])).scan(limit=500)
        self.assertLessEqual(len(result), 50)

if __name__ == "__main__": unittest.main()
