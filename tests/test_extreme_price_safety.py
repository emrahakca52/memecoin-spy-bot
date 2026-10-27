"""Regression tests for extreme paper-price safety.

All tests use in-memory state and mocked prices; they never touch production DB.
"""
import unittest
from unittest.mock import AsyncMock, patch

import paper_engine


class ExtremePaperPriceSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_positions = paper_engine._open_positions
        self.old_trades = paper_engine._paper_trades
        self.old_realized = paper_engine._realized_pnl_usd
        self.old_cache = paper_engine._price_cache
        self.old_pending = paper_engine._pending_price_jumps
        self.old_cooldown = paper_engine._provider_cooldown_until
        self.old_reason = paper_engine._provider_cooldown_reason
        self.old_diag = paper_engine._price_diagnostics
        paper_engine._open_positions = {
            "token-1": {
                "token_address": "token-1",
                "token_symbol": "TEST",
                "entry_price_usd": 1.0,
                "current_price_usd": 1.0,
                "quantity": 10.0,
                "invested_usd": 10.0,
                "unrealized_pnl_usd": 0.0,
                "opened_at": 0,
            }
        }
        paper_engine._paper_trades = []
        paper_engine._realized_pnl_usd = 0.0
        paper_engine._price_cache = {}
        paper_engine._pending_price_jumps = {}
        paper_engine._provider_cooldown_until = {}
        paper_engine._provider_cooldown_reason = {}
        paper_engine._price_diagnostics = {"birdeye": {"requests": 0}}

    def tearDown(self):
        paper_engine._open_positions = self.old_positions
        paper_engine._paper_trades = self.old_trades
        paper_engine._realized_pnl_usd = self.old_realized
        paper_engine._price_cache = self.old_cache
        paper_engine._pending_price_jumps = self.old_pending
        paper_engine._provider_cooldown_until = self.old_cooldown
        paper_engine._provider_cooldown_reason = self.old_reason
        paper_engine._price_diagnostics = self.old_diag

    async def test_extreme_down_quote_does_not_mark_or_auto_sell(self):
        with (
            patch.object(paper_engine, "get_token_prices_usd", new=AsyncMock(return_value={"token-1": 0.01})),
            patch.object(paper_engine, "_save_paper_state", return_value=True),
        ):
            result = await paper_engine.update_paper_prices()

        self.assertEqual(result["closed"], 0)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(paper_engine._paper_trades), 0)
        self.assertIn("token-1", paper_engine._open_positions)
        position = paper_engine._open_positions["token-1"]
        self.assertEqual(position["current_price_usd"], 1.0)
        self.assertEqual(position["unrealized_pnl_usd"], 0.0)
        self.assertEqual(position["price_mark_status"], "unverified_extreme_quote")
        self.assertIn("last_suspect_price_usd", position)

    async def test_normal_stop_loss_quote_closes_position(self):
        with (
            patch.object(paper_engine, "get_token_prices_usd", new=AsyncMock(return_value={"token-1": 0.94})),
            patch.object(paper_engine, "_save_paper_state", return_value=True),
        ):
            result = await paper_engine.update_paper_prices()

        self.assertEqual(result["closed"], 1)
        self.assertNotIn("token-1", paper_engine._open_positions)
        self.assertEqual(len(paper_engine._paper_trades), 1)
        self.assertEqual(paper_engine._paper_trades[0]["side"], "SELL")
        self.assertEqual(paper_engine._paper_trades[0]["reason"], "stop_loss_5pct")


if __name__ == "__main__":
    unittest.main()
