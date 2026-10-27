"""Regression tests for paper-accounting state restoration.

These tests mock persistence entirely; they never connect to or modify a real database.
"""
import unittest
from unittest.mock import MagicMock, patch

import paper_engine


class LoadPaperStateAccountingTests(unittest.TestCase):
    def load_state(self, state):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (state,)
        context = MagicMock()
        context.__enter__.return_value = conn
        context.__exit__.return_value = False
        with (
            patch.object(paper_engine, "_database_url", return_value="postgres://test-only"),
            patch.object(paper_engine, "_connect_database", return_value=context),
            patch.object(paper_engine, "_ensure_state_table"),
            patch.object(paper_engine, "_save_paper_state", return_value=True),
        ):
            return paper_engine.load_paper_state()

    def test_complete_sell_ledger_rebuilds_aggregate(self):
        state = {
            "open_positions": {},
            "paper_trades": [
                {"side": "SELL", "realized_pnl_usd": 12.5},
                {"side": "SELL", "realized_pnl_usd": -2.0},
            ],
            "realized_pnl_usd": 999.0,
        }
        self.load_state(state)
        self.assertAlmostEqual(paper_engine._realized_pnl_usd, 10.5)
        self.assertIsNone(paper_engine._database_status.get("accounting_warning"))

    def test_incomplete_sell_ledger_preserves_stored_aggregate(self):
        state = {
            "open_positions": {},
            "paper_trades": [
                {"side": "SELL", "realized_pnl_usd": 12.5},
                {"side": "SELL", "amount_usd": 40.0},
            ],
            "realized_pnl_usd": 77.25,
        }
        self.load_state(state)
        self.assertAlmostEqual(paper_engine._realized_pnl_usd, 77.25)
        self.assertIn("lack realized_pnl_usd", paper_engine._database_status["accounting_warning"])

    def test_quarantined_history_is_retained_and_excluded_from_reported_total(self):
        state = {
            "open_positions": {},
            "paper_trades": [
                {"side": "SELL", "token_symbol": "SAFE", "realized_pnl_usd": 4.0},
                {
                    "side": "SELL",
                    "token_symbol": "MRBEAST",
                    "token_address": "4hqN5vhCG1q13qYUQvBhyMDDnpWKP98DoswDeDCVQaCn",
                    "price_usd": 123.0,
                    "realized_pnl_usd": 16000000.0,
                },
            ],
            "realized_pnl_usd": 16000004.0,
        }
        self.load_state(state)
        self.assertEqual(len(paper_engine._paper_trades), 2)
        self.assertTrue(paper_engine._paper_trades[1]["accounting_quarantined"])
        self.assertAlmostEqual(paper_engine._realized_pnl_usd, 4.0)
        self.assertEqual(len(paper_engine._reported_realized_pnl(paper_engine._paper_trades)["quarantined_sell_records"]), 1)


if __name__ == "__main__":
    unittest.main()
