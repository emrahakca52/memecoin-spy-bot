"""Regression checks for conservative extreme-exit guard configuration."""
import unittest

import paper_engine


class ExtremeExitGuardConfigTests(unittest.TestCase):
    def test_stop_loss_overshoot_is_capped_at_ten_percentage_points(self):
        self.assertGreaterEqual(paper_engine.MAX_STOP_LOSS_OVERSHOOT_PCT, 5.0)
        self.assertLessEqual(paper_engine.MAX_STOP_LOSS_OVERSHOOT_PCT, 10.0)

    def test_take_profit_overshoot_is_capped_at_fifty_percentage_points(self):
        self.assertGreaterEqual(paper_engine.MAX_TAKE_PROFIT_OVERSHOOT_PCT, 10.0)
        self.assertLessEqual(paper_engine.MAX_TAKE_PROFIT_OVERSHOOT_PCT, 50.0)


if __name__ == "__main__":
    unittest.main()
