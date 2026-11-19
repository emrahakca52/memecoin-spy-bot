"""Live BIST equity universe from KAP's public market page.

Only equity-market segments are included: Yildiz, Ana, Alt and Yakin Izleme.
Funds, warrants, debt instruments and pre-market platform are excluded.
"""
from __future__ import annotations

from io import StringIO
import re
import requests
import pandas as pd

KAP_MARKETS_URL = "https://www.kap.org.tr/tr/Pazarlar"
INCLUDED_MARKETS = {
    "YILDIZ PAZAR": "YILDIZ",
    "ANA PAZAR": "ANA",
    "ALT PAZAR": "ALT",
    "YAKIN İZLEME PAZARI": "YAKIN_IZLEME",
}
CODE_RE = re.compile(r"^[A-Z0-9]{3,8}$")


def fetch_kap_universe() -> pd.DataFrame:
    """Fetch and validate current listed equity codes from KAP.

    Fails closed if KAP's page structure changes or too few codes are parsed;
    it never silently falls back to a partial/hard-coded list.
    """
    response = requests.get(
        KAP_MARKETS_URL,
        headers={"User-Agent": "SPEK-RADAR-PRO/1.0 (market-universe reader)"},
        timeout=30,
    )
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    rows: dict[str, dict] = {}

    for table in tables:
        market = None
        for values in table.itertuples(index=False, name=None):
            cells = [str(v).strip() for v in values if pd.notna(v)]
            if not cells:
                continue
            joined = " ".join(cells).upper()
            for label, segment in INCLUDED_MARKETS.items():
                if label in joined:
                    market = segment
                    break
            if market is None or len(cells) < 2:
                continue

            # KAP table rows are generally: sequence, ticker, company name.
            code = cells[1].upper().strip()
            if code.isdigit() and len(cells) >= 3:
                code = cells[1].upper().strip()
            if CODE_RE.fullmatch(code):
                name = cells[2] if len(cells) >= 3 else ""
                rows[code] = {
                    "symbol": f"{code}.IS",
                    "code": code,
                    "company": name,
                    "market_segment": market,
                    "source": "KAP",
                }

    result = pd.DataFrame(rows.values())
    if len(result) < 100:
        raise RuntimeError(
            f"KAP evreni doğrulanamadı: yalnızca {len(result)} pay ayrıştırıldı. "
            "KAP sayfa yapısı değişmiş olabilir; eksik listeyle tarama yapılmadı."
        )
    return result.sort_values(["market_segment", "code"]).reset_index(drop=True)
