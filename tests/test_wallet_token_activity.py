import asyncio
import unittest

from wallet_tracker import get_wallet_token_activity


class TestWalletTokenActivityValidation(unittest.TestCase):
    def test_rejects_invalid_wallet_before_rpc_call(self):
        with self.assertRaises(ValueError):
            asyncio.run(get_wallet_token_activity("not-a-solana-address", limit=5))

    def test_rejects_limit_outside_supported_range(self):
        # Address shape is checked first, so use a base58-shaped 32-character value.
        address = "11111111111111111111111111111111"
        with self.assertRaises(ValueError):
            asyncio.run(get_wallet_token_activity(address, limit=0))
        with self.assertRaises(ValueError):
            asyncio.run(get_wallet_token_activity(address, limit=21))


if __name__ == "__main__":
    unittest.main()
