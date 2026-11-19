import unittest
from datetime import datetime,timezone
from memecoin_v2.models import TokenMarket
from memecoin_v2.risk import evaluate_market
from memecoin_v2.scoring import score_market
from memecoin_v2.settings import Settings

class V2CoreTests(unittest.TestCase):
    def market(self,**overrides):
        values=dict(chain="solana",address="mint",symbol="TEST",name="Test",
                    price_usd=0.001,liquidity_usd=80000,volume_24h_usd=150000,
                    txns_24h=500,price_change_1h_pct=8,price_change_24h_pct=30,
                    pair_created_at_ms=int(datetime.now(timezone.utc).timestamp()*1000)-3600000,
                    onchain={"rpc":{"mint_authority_present":False,"freeze_authority_present":False},
                             "rugcheck":{"available":True,"score_normalised":12,
                                         "top10_holders_pct":18,"lp_locked_pct":80,
                                         "mint_authority_active":False,"freeze_authority_active":False,
                                         "dangerous_risks":[]}})
        values.update(overrides)
        return TokenMarket(**values)
    def test_market_score_is_bounded(self):
        score,signals=score_market(self.market())
        self.assertGreaterEqual(score,0); self.assertLessEqual(score,100)
        self.assertIn("active_volume",signals)
    def test_missing_bundle_data_never_approves(self):
        result=evaluate_market(self.market(),Settings())
        self.assertFalse(result.eligible)
        self.assertIn("bundle_sniper_activity",result.missing)
    def test_missing_security_data_never_approves(self):
        result=evaluate_market(self.market(onchain={}),Settings())
        self.assertFalse(result.eligible)
        self.assertIn("rugcheck_report",result.missing)
    def test_high_holder_concentration_is_rejected(self):
        data=self.market().onchain
        data["rugcheck"]["top10_holders_pct"]=40
        result=evaluate_market(self.market(onchain=data),Settings())
        self.assertIn("top10_holder_concentration_above_limit",result.reasons)
    def test_active_authority_is_rejected(self):
        data=self.market().onchain
        data["rpc"]["mint_authority_present"]=True
        result=evaluate_market(self.market(onchain=data),Settings())
        self.assertIn("mint_authority_enabled",result.reasons)
    def test_low_lp_lock_is_rejected(self):
        data=self.market().onchain
        data["rugcheck"]["lp_locked_pct"]=10
        result=evaluate_market(self.market(onchain=data),Settings())
        self.assertIn("lp_lock_below_minimum",result.reasons)

    def test_ai_and_helius_positive_gates_are_required_and_can_pass(self):
        data=self.market().onchain
        data["ai"]={"api_ok":True,"passed":True}
        data["helius"]={"available":True,"sniper_cluster_detected":False}
        result=evaluate_market(self.market(onchain=data),Settings())
        self.assertTrue(result.eligible)
    def test_ai_rejection_blocks_candidate(self):
        data=self.market().onchain
        data["ai"]={"api_ok":True,"passed":False}
        data["helius"]={"available":True,"sniper_cluster_detected":False}
        result=evaluate_market(self.market(onchain=data),Settings())
        self.assertIn("ai_screening_rejected",result.reasons)
    def test_helius_cluster_blocks_candidate(self):
        data=self.market().onchain
        data["ai"]={"api_ok":True,"passed":True}
        data["helius"]={"available":True,"sniper_cluster_detected":True}
        result=evaluate_market(self.market(onchain=data),Settings())
        self.assertIn("bundle_sniper_cluster_detected",result.reasons)

    def test_real_trading_is_disabled(self):
        self.assertFalse(Settings.from_env().real_trading_enabled)

if __name__=="__main__": unittest.main()
