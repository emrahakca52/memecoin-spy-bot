import unittest
import numpy as np
import pandas as pd
from spek_radar.indicators import add_indicators, score_latest

class Tests(unittest.TestCase):
    def test_score_bounds(self):
        n=120
        c=pd.Series(np.linspace(10,12,n))
        d=pd.DataFrame({"open":c*.999,"high":c*1.01,"low":c*.99,"close":c,"volume":100000.0})
        result=score_latest(add_indicators(d))
        self.assertGreaterEqual(result["score"],0)
        self.assertLessEqual(result["score"],100)
        self.assertIn("signal",result)

if __name__=="__main__": unittest.main()
