import unittest

import numpy as np
import pandas as pd

from spek_radar.indicators import add_indicators, score_latest


class IndicatorTests(unittest.TestCase):
    def _frame(self, volume):
        close = pd.Series(np.linspace(10, 12, len(volume)))
        return pd.DataFrame({
            "open": close * .999,
            "high": close * 1.01,
            "low": close * .99,
            "close": close,
            "volume": volume,
        })

    def test_score_bounds(self):
        result = score_latest(add_indicators(self._frame([100000.0] * 120)))
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertIn("signal", result)

    def test_relative_volume_uses_previous_sessions(self):
        volume = [100000.0] * 39 + [300000.0]
        features = add_indicators(self._frame(volume))
        self.assertAlmostEqual(features["rel_volume"].iloc[-1], 3.0)

    def test_obv_pressure_is_normalized_to_average_daily_volume(self):
        volume = [100000.0] * 39 + [300000.0]
        features = add_indicators(self._frame(volume))
        self.assertTrue(np.isfinite(features["obv_change20"].iloc[-1]))


if __name__ == "__main__":
    unittest.main()
