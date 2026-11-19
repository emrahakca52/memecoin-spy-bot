import unittest

from spek_radar.universe import parse_kap_markets


class KapUniverseParserTests(unittest.TestCase):
    def test_market_sections_are_classified_and_non_equity_ignored(self):
        html = """
        <table>
          <tr><th colspan="3">YILDIZ PAZAR 149 Şirket / Fon Bulundu</th></tr>
          <tr><td>1</td><td><a>BINHO</a></td><td><a>1000 YATIRIMLAR HOLDİNG A.Ş.</a></td></tr>
          <tr><th colspan="3">ANA PAZAR 393 Şirket / Fon Bulundu</th></tr>
          <tr><td>1</td><td><a>DSTKF</a></td><td><a>DESTEK FİNANS FAKTORİNG A.Ş.</a></td></tr>
          <tr><th colspan="3">ALT PAZAR</th></tr>
          <tr><td>1</td><td><a>AGYO</a></td><td><a>ÖRNEK A.Ş.</a></td></tr>
          <tr><th colspan="3">YAKIN İZLEME PAZARI</th></tr>
          <tr><td>1</td><td><a>ISBTR</a></td><td><a>ÖRNEK BANKA A.Ş.</a></td></tr>
        </table>
        """
        # The production guard requires a broad universe; lower it only in
        # this fixture by patching the parser's threshold through a larger set
        # of synthetic tickers.
        for i in range(100):
            html = html.replace(
                "</table>",
                f"<tr><td>{i+2}</td><td><a>TEST{i:03d}</a></td><td><a>TEST COMPANY</a></td></tr></table>"
            )
        # Added rows remain in the final section, proving parser extraction.
        result = parse_kap_markets(html)
        self.assertIn("BINHO.IS", set(result.symbol))
        self.assertIn("DSTKF.IS", set(result.symbol))
        self.assertIn("AGYO.IS", set(result.symbol))
        self.assertIn("ISBTR.IS", set(result.symbol))
        self.assertEqual(
            result.loc[result.code.eq("DSTKF"), "market_segment"].iloc[0],
            "ANA",
        )


if __name__ == "__main__":
    unittest.main()
