import unittest

from signal_engine import _jupiter_candidates


class JupiterSchemaTests(unittest.TestCase):
    def test_maps_documented_stats24h_fields(self):
        candidates = _jupiter_candidates([{
            "id": "mint-1",
            "name": "Example",
            "symbol": "EX",
            "usdPrice": 0.001,
            "liquidity": 50000,
            "mcap": 100000,
            "stats24h": {
                "priceChange": 0.12,
                "buyVolume": 14000,
                "sellVolume": 11000,
                "numBuys": 120,
                "numSells": 60,
                "numTraders": 95,
            },
        }])
        self.assertEqual(len(candidates), 1)
        token = candidates[0]
        self.assertEqual(token["token_address"], "mint-1")
        self.assertEqual(token["volume_24h_usd"], 25000)
        self.assertEqual(token["buys_24h"], 120)
        self.assertEqual(token["sells_24h"], 60)
        self.assertEqual(token["trade_count_24h"], 95)
        self.assertAlmostEqual(token["price_change_24h_pct"], 12.0)
        self.assertTrue(token["price_change_24h_present"])

    def test_missing_price_change_is_not_marked_present(self):
        candidates = _jupiter_candidates([{
            "id": "mint-2",
            "symbol": "MISS",
            "stats24h": {"buyVolume": 100, "sellVolume": 100},
        }])
        self.assertFalse(candidates[0]["price_change_24h_present"])


if __name__ == "__main__":
    unittest.main()
