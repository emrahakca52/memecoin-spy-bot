import unittest

from signal_engine import _has_finite_number, _jupiter_candidates, _normalize


class JupiterDiscoveryTests(unittest.TestCase):
    def test_maps_token_v2_recent_stats(self):
        tokens = [{
            "id": "mint-123",
            "symbol": "TEST",
            "name": "Test Token",
            "usdPrice": 0.001,
            "liquidity": 25000,
            "mcap": 100000,
            "stats24h": {
                "numBuys": 120,
                "numSells": 60,
                "buyVolume": 18000,
                "sellVolume": 12000,
                "priceChange": 0.125,
                "numTraders": 150,
            },
        }]
        result = _jupiter_candidates(tokens)[0]
        self.assertEqual(result["token_address"], "mint-123")
        self.assertEqual(result["volume_24h_usd"], 30000)
        self.assertEqual(result["buys_24h"], 120)
        self.assertEqual(result["sells_24h"], 60)
        self.assertEqual(result["trade_count_24h"], 150)
        self.assertEqual(result["price_change_24h_pct"], 12.5)
        self.assertTrue(result["price_change_24h_present"])

    def test_missing_price_change_is_not_valid(self):
        result = _jupiter_candidates([{"id": "mint-456", "symbol": "NOCHANGE"}])[0]
        self.assertFalse(result["price_change_24h_present"])

    def test_percentage_field_under_one_is_not_multiplied(self):
        result = _jupiter_candidates([{
            "id": "mint-percent",
            "stats24h": {"priceChangePercent": 0.5},
        }])[0]
        self.assertEqual(result["price_change_24h_pct"], 0.5)
        self.assertTrue(result["price_change_24h_present"])

    def test_token_price_change_field_is_already_percentage(self):
        result = _jupiter_candidates([{
            "id": "mint-token-change",
            "priceChange24h": -0.4,
        }])[0]
        self.assertEqual(result["price_change_24h_pct"], -0.4)
        self.assertTrue(result["price_change_24h_present"])

    def test_normalize_marks_non_finite_change_missing(self):
        result = _normalize({
            "address": "mint-789",
            "price": 1,
            "price_change_24h_percent": "nan",
        })
        self.assertFalse(result["price_change_24h_present"])

    def test_finite_metric_validation(self):
        self.assertTrue(_has_finite_number("12.5"))
        self.assertFalse(_has_finite_number("inf"))


if __name__ == "__main__":
    unittest.main()
