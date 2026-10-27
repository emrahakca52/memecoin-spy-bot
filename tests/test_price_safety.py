"""Regression tests for extreme quote exit safeguards.

All tests are isolated from production persistence and never send real orders.
"""
import time
import unittest
from unittest.mock import AsyncMock, patch

import paper_engine


class ExtremeQuoteExitGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_positions = paper_engine._open_positions
        self.original_trades = paper_engine._paper_trades
        self.original_realized = paper_engine._realized_pnl_usd
        paper_engine._open_positions = {
            "token-address": {
                "token_address": "token-address",
                "token_symbol": "TEST",
                "entry_price_usd": 0.1,
                "current_price_usd": 0.1,
                "quantity": 100.0,
                "invested_usd": 10.0,
                "unrealized_pnl_usd": 0.0,
                "opened_at": time.time() - 3600,
            }
        }
        paper_engine._paper_trades = []
        paper_engine._realized_pnl_usd = 0.0

    def tearDown(self):
        paper_engine._open_positions = self.original_positions
        paper_engine._paper_trades = self.original_trades
        paper_engine._realized_pnl_usd = self.original_realized

    async def test_extreme_crash_quote_does_not_auto_sell_or_change_mark(self):
        with (
            patch.object(
                paper_engine, "get_token_prices_usd",
                new=AsyncMock(return_value={"token-address": 0.001}),
            ),
            patch.object(paper_engine, "_save_paper_state", return_value=True),
        ):
            result = await paper_engine.update_paper_prices()

        self.assertEqual(result["closed"], 0)
        self.assertIn("token-address", paper_engine._open_positions)
        position = paper_engine._open_positions["token-address"]
        self.assertEqual(position["current_price_usd"], 0.1)
        self.assertEqual(paper_engine._paper_trades, [])
        self.assertEqual(position["price_mark_status"], "unverified_extreme_quote")
        self.assertIn("price_safety_warning", position)

    async def test_extreme_spike_quote_does_not_auto_sell_or_change_mark(self):
        with (
            patch.object(
                paper_engine, "get_token_prices_usd",
                new=AsyncMock(return_value={"token-address": 10.0}),
            ),
            patch.object(paper_engine, "_save_paper_state", return_value=True),
        ):
            result = await paper_engine.update_paper_prices()

        self.assertEqual(result["closed"], 0)
        self.assertIn("token-address", paper_engine._open_positions)
        self.assertEqual(
            paper_engine._open_positions["token-address"]["current_price_usd"], 0.1
        )
        self.assertEqual(paper_engine._paper_trades, [])

    async def test_normal_stop_loss_can_still_close_position(self):
        with (
            patch.object(
                paper_engine, "get_token_prices_usd",
                new=AsyncMock(return_value={"token-address": 0.092}),
            ),
            patch.object(paper_engine, "_save_paper_state", return_value=True),
        ):
            result = await paper_engine.update_paper_prices()

        self.assertEqual(result["closed"], 1)
        self.assertNotIn("token-address", paper_engine._open_positions)
        self.assertEqual(len(paper_engine._paper_trades), 1)
        self.assertEqual(paper_engine._paper_trades[0]["side"], "SELL")
        self.assertAlmostEqual(paper_engine._paper_trades[0]["realized_pnl_usd"], -0.8)


class AccountingQuarantineRegressionTests(unittest.TestCase):
    def test_known_bad_mrbeast_sell_is_excluded_without_mutating_history(self):
        bad_trade = {
            "side": "SELL",
            "token_symbol": "MRBEAST",
            "token_address": "4hqN5vhCG1q13qYUQvBhyMDDnpWKP98DoswDeDCVQaCn",
            "price_usd": 123.45,
            "realized_pnl_usd": 16_100_066.6799,
        }
        valid_trade = {
            "side": "SELL",
            "token_symbol": "SAFE",
            "token_address": "ordinary-token",
            "price_usd": 0.12,
            "realized_pnl_usd": -2.5,
        }
        history = [bad_trade.copy(), valid_trade.copy()]
        before = [trade.copy() for trade in history]

        result = paper_engine._reported_realized_pnl(history)

        self.assertAlmostEqual(result["realized_pnl_usd"], -2.5)
        self.assertEqual(len(result["quarantined_sell_records"]), 1)
        self.assertEqual(history, before)

    def test_non_finite_numeric_values_fall_back_safely(self):
        for value in (float("nan"), float("inf"), float("-inf"), "not-a-number"):
            with self.subTest(value=value):
                self.assertEqual(paper_engine._num(value), 0.0)


if __name__ == "__main__":
    unittest.main()
