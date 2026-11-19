"""Fetch current BIST equity market members from KAP's public markets page."""
from __future__ import annotations

import re
import unicodedata
from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup

KAP_MARKETS_URL = "https://www.kap.org.tr/tr/Pazarlar"
MARKET_LABELS = {
    "YILDIZ PAZAR": "YILDIZ",
    "ANA PAZAR": "ANA",
    "ALT PAZAR": "ALT",
    "YAKIN IZLEME PAZARI": "YAKIN_IZLEME",
}
CODE_RE = re.compile(r"^[A-Z0-9]{3,8}$")


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.upper())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(value.split())


def _market_from_text(value: str) -> str | None:
    text = _normalise(value)
    for label, segment in MARKET_LABELS.items():
        if text.startswith(label):
            return segment
    return None


def parse_kap_markets(html: str) -> pd.DataFrame:
    """Parse KAP's market sections; accepts row-based or table-based markup."""
    soup = BeautifulSoup(html, "html.parser")
    rows: dict[str, dict] = {}
    current_market = None

    for tr in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
        row_text = " ".join(cells).strip()
        detected = _market_from_text(row_text)
        if detected:
            current_market = detected
            continue
        if not current_market:
            continue

        # KAP company rows link both the ticker and company name.
        anchors = [a.get_text(" ", strip=True).upper().strip()
                   for a in tr.find_all("a")]
        code = next((a for a in anchors if CODE_RE.fullmatch(a)), None)
        if not code:
            # Fallback for rows without anchors: sequence | ticker | company.
            code = next((c.upper() for c in cells if CODE_RE.fullmatch(c.upper())), None)
        if not code:
            continue
        name = next((a.get_text(" ", strip=True) for a in tr.find_all("a")
                     if a.get_text(" ", strip=True).upper().strip() != code), "")
        if not name and len(cells) >= 3:
            name = cells[2]
        # Exclude fund products even if KAP groups them with pay-market issuers.
        if any(token in _normalise(name) for token in ("YATIRIM FONU", "BORSA YATIRIM FONU", "ETF")):
            continue
        rows[code] = {
            "symbol": f"{code}.IS",
            "code": code,
            "company": name,
            "market_segment": current_market,
            "source": "KAP",
        }

    result = pd.DataFrame(rows.values())
    if len(result) < 100:
        # Some KAP releases render the market header outside the data table.
        # Try table-level parsing as a conservative fallback, never return a
        # suspiciously small or unclassified universe.
        table_rows: dict[str, dict] = {}
        market = None
        for table in pd.read_html(StringIO(html)):
            for values in table.itertuples(index=False, name=None):
                cells = [str(v).strip() for v in values if pd.notna(v)]
                joined = " ".join(cells)
                detected = _market_from_text(joined)
                if detected:
                    market = detected
                    continue
                if market:
                    code = next((c.upper() for c in cells if CODE_RE.fullmatch(c.upper())), None)
                    if code:
                        table_rows[code] = {
                            "symbol": f"{code}.IS", "code": code,
                            "company": cells[-1] if len(cells) > 1 else "",
                            "market_segment": market, "source": "KAP",
                        }
        if len(table_rows) > len(rows):
            result = pd.DataFrame(table_rows.values())

    if len(result) < 100:
        raise RuntimeError(
            f"KAP evreni doğrulanamadı: {len(result)} pay ayrıştırıldı. "
            "Eksik/yanlış listeyle tarama yapılmadı."
        )
    return result.drop_duplicates("code").sort_values(
        ["market_segment", "code"]
    ).reset_index(drop=True)


def fetch_kap_universe() -> pd.DataFrame:
    response = requests.get(
        KAP_MARKETS_URL,
        headers={"User-Agent": "Mozilla/5.0 SPEK-RADAR-PRO/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    return parse_kap_markets(response.text)
