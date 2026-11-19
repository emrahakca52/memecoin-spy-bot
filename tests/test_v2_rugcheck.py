import unittest
from memecoin_v2.providers.rugcheck import _top10_pct,_lp_locked_pct,_authority

class RugCheckParserTests(unittest.TestCase):
    def test_top_holders_sum_and_exclude_known_pool(self):
        report={"topHolders":[{"address":"pool","pct":50},{"address":"a","pct":10},{"address":"b","pct":8}],
                "knownAccounts":{"pool":{"type":"AMM"}}}
        self.assertEqual(_top10_pct(report),18)
    def test_lp_lock_uses_most_conservative_market(self):
        report={"markets":[{"lp":{"lpLockedPct":90}},{"lp":{"lpLockedPct":60}}]}
        self.assertEqual(_lp_locked_pct(report),60)
    def test_missing_is_unknown_and_null_authority_is_revoked(self):
        self.assertIsNone(_top10_pct({}))
        self.assertIsNone(_authority({},"mintAuthority"))
        self.assertFalse(_authority({"mintAuthority":None},"mintAuthority"))
        self.assertTrue(_authority({"mintAuthority":"SomeAddress"},"mintAuthority"))

if __name__=="__main__": unittest.main()
