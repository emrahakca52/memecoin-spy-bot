"""Regression tests for paper-status execution-cost completeness diagnostics."""
import unittest
from unittest.mock import patch

import paper_engine


class PaperStatusCostDiagnosticsTests(unittest.TestCase):
    def test_missing_sell_and_open_entry_costs_are_counted_and_net_is_incomplete(self):
        positions = {
            "TOKEN_A": {
                "token_symbol": "A",
                "quantity": 10.0,
                "invested_usd": 10.0,
                "entry_price_usd": 1.0,
                "current_price_usd": 1.0,
                "estimated_entry_cost_usd": None,
                "last_price_update_utc": "2026-01-01T00:00:00+00:00",
            }
        }
        trades = [
            {"side": "SELL", "realized_pnl_usd": 1.0},
            {
                "side": "SELL",
                "realized_pnl_usd": -1.0,
                "estimated_transaction_cost_usd": 0.03,
                "accounting_quarantined": True,
            },
        ]
        with (
            patch.object(paper_engine, "_open_positions", positions),
            patch.object(paper_engine, "_paper_trades", trades),
            patch.object(paper_engine, "_realized_pnl_usd", 1.0),
            patch.object(paper_engine, "_price_mark_age_seconds", return_value=10.0),
            patch.object(paper_engine, "_stale_price_threshold_seconds", return_value=540),
        ):
            status = paper_engine.get_paper_status()

        self.assertEqual(status["paper_sell_records_missing_costs"], 1)
        self.assertEqual(status["paper_quarantined_sell_cost_records"], 1)
        self.assertEqual(status["paper_open_positions_missing_entry_costs"], 1)
        self.assertTrue(status["paper_estimated_net_pnl_incomplete"])
        self.assertIsNone(status["paper_estimated_net_total_pnl_usd"])
        self.assertTrue(status["paper_total_pnl_incomplete"])
        self.assertEqual(len(paper_engine._paper_trades), 2)

    def test_complete_cost_data_allows_net_estimate(self):
        positions = {
            "TOKEN_B": {
                "token_symbol": "B",
                "quantity": 10.0,
                "invested_usd": 10.0,
                "entry_price_usd": 1.0,
                "current_price_usd": 1.1,
                "estimated_entry_cost_usd": 0.03,
                "last_price_update_utc": "2026-01-01T00:00:00+00:00",
            }
        }
        trades = [
            {
                "side": "SELL",
                "realized_pnl_usd": 1.0,
                "estimated_transaction_cost_usd": 0.03,
            }
        ]
        with (
            patch.object(paper_engine, "_open_positions", positions),
            patch.object(paper_engine, "_paper_trades", trades),
            patch.object(paper_engine, "_realized_pnl_usd", 1.0),
            patch.object(paper_engine, "_price_mark_age_seconds", return_value=10.0),
            patch.object(paper_engine, "_stale_price_threshold_seconds", return_value=540),
        ):
            status = paper_engine.get_paper_status()

        self.assertEqual(status["paper_sell_records_missing_costs"], 0)
        self.assertEqual(status["paper_quarantined_sell_cost_records"], 0)
        self.assertEqual(status["paper_open_positions_missing_entry_costs"], 0)
        self.assertFalse(status["paper_estimated_net_pnl_incomplete"])
        self.assertIsNotNone(status["paper_estimated_net_total_pnl_usd"])
        self.assertFalse(status["paper_total_pnl_incomplete"])


if __name__ == "__main__":
    unittest.main()
