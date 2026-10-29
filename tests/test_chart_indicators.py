import math
import unittest

from app import _chart_num, _ema, _rsi


class ChartIndicatorRegressionTests(unittest.TestCase):
    def test_chart_number_rejects_non_finite_values(self):
        self.assertEqual(_chart_num("nan", 7.0), 7.0)
        self.assertEqual(_chart_num(float("inf"), 7.0), 7.0)
        self.assertEqual(_chart_num("not-a-number", 7.0), 7.0)
        self.assertEqual(_chart_num("12.5"), 12.5)

    def test_ema_requires_enough_points_and_returns_finite_value(self):
        self.assertIsNone(_ema([1.0, 2.0], 3))
        result = _ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertIsNotNone(result)
        self.assertTrue(math.isfinite(result))
        self.assertGreater(result, 3.0)

    def test_rsi_handles_rising_flat_and_insufficient_series(self):
        self.assertIsNone(_rsi([1.0] * 14, 14))
        self.assertEqual(_rsi([float(i) for i in range(1, 17)], 14), 100.0)
        self.assertEqual(_rsi([3.0] * 16, 14), 50.0)

    def test_rsi_stays_in_valid_range(self):
        values = [100.0, 98.0, 99.0, 96.0, 97.0, 95.0, 98.0, 99.0, 97.0,
                  100.0, 101.0, 98.0, 97.0, 96.0, 99.0, 102.0, 101.0]
        result = _rsi(values, 14)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 0.0)
        self.assertLessEqual(result, 100.0)


if __name__ == "__main__":
    unittest.main()
