import math
import unittest

from signal_engine import _has_finite_number, _normalize


class TestPriceChangeMetricValidation(unittest.TestCase):
    def test_missing_and_non_finite_price_change_are_invalid(self):
        for value in (None, "not-a-number", "nan", "inf", "-inf", float("nan"), float("inf"), True):
            with self.subTest(value=repr(value)):
                self.assertFalse(_has_finite_number(value))

    def test_finite_numeric_price_change_is_valid(self):
        for value in (0, 0.0, "12.5", -30):
            with self.subTest(value=repr(value)):
                self.assertTrue(_has_finite_number(value))

    def test_normalize_does_not_mark_nan_price_change_as_present(self):
        token = _normalize({
            "address": "test-token",
            "symbol": "TEST",
            "price": 1,
            "liquidity": 20000,
            "volume_24h_usd": 30000,
            "buy_24h": 100,
            "sell_24h": 50,
            "trade_24h_count": 150,
            "price_change_24h_percent": "nan",
        })
        self.assertFalse(token["price_change_24h_present"])
        self.assertTrue(math.isfinite(token["price_change_24h_pct"]))


if __name__ == "__main__":
    unittest.main()
