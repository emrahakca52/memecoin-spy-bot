import unittest
from datetime import datetime, timezone
from memecoin_v2.models import TokenMarket
from memecoin_v2.risk import evaluate_market
from memecoin_v2.scoring import score_market
from memecoin_v2.settings import Settings


class V2CoreTests(unittest.TestCase):
    def market(self, **overrides):
        values = dict(chain="solana", address="mint", symbol="TEST", name="Test",
                      price_usd=0.001, liquidity_usd=80_000, volume_24h_usd=150_000,
                      txns_24h=500, price_change_1h_pct=8, price_change_24h_pct=30,
                      pair_created_at_ms=int(datetime.now(timezone.utc).timestamp()*1000)-3_600_000)
        values.update(overrides)
        return TokenMarket(**values)

    def test_market_score_is_bounded(self):
        score, signals = score_market(self.market())
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIn("active_volume", signals)

    def test_missing_onchain_data_never_approves(self):
        result = evaluate_market(self.market(), Settings())
        self.assertFalse(result.eligible)
        self.assertIn("holder_concentration", result.missing)
        self.assertIn("bundle_sniper_activity", result.missing)

    def test_low_liquidity_is_rejected(self):
        result = evaluate_market(self.market(liquidity_usd=100), Settings())
        self.assertFalse(result.eligible)
        self.assertIn("liquidity_below_minimum", result.reasons)

    def test_real_trading_is_disabled(self):
        self.assertFalse(Settings.from_env().real_trading_enabled)


if __name__ == "__main__":
    unittest.main()
