"""Broad-universe, explainable BIST anomaly screening.

Universe source: public KAP-derived symbol CSV (fallback list if unavailable).
Market bars: Yahoo Finance; indicative/delayed and not exchange-authoritative.
This flags statistical outliers; it does not prove manipulation or intent.
"""
from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf

UNIVERSE_URL = os.getenv(
    "BIST_UNIVERSE_URL",
    "https://raw.githubusercontent.com/ahmeterenodaci/Istanbul-Stock-Exchange--BIST--including-symbols-and-logos/main/without_logo.csv",
)
FALLBACK = "THYAO,ASELS,TUPRS,EREGL,AKBNK,GARAN,ISCTR,SISE,SAHOL,KCHOL,TCELL,BIMAS,PGSUS,TOASO,FROTO,KOZAL,ASTOR,HEKTS,MIATK,DSTKF,KNFRT,VEYAS,BLCYT,KRTEK"
UNIVERSE_STATUS: dict[str, Any] = {"source": "not_loaded", "is_full_universe": False, "warning": "Evren henüz yüklenmedi."}


@dataclass
class Signal:
    symbol: str
    name: str
    score: int
    last_close: float
    change_1d_pct: float
    change_5d_pct: float
    volume_ratio: float
    turnover_try: float
    volatility_20d_pct: float
    short_term_score: int
    swing_score: int
    exit_risk_score: int
    phase: str
    exit_reasons: list[str]
    reasons: list[str]
    data_date: str
    data_source: str = "Yahoo Finance (indicative; may be delayed)"


def configured_symbols() -> list[str]:
    global UNIVERSE_STATUS
    raw = os.getenv("BIST_SYMBOLS", "").strip()
    if raw:
        symbols = list(dict.fromkeys(s.strip().upper().removesuffix(".IS") for s in raw.split(",") if s.strip()))
        UNIVERSE_STATUS = {"source": "BIST_SYMBOLS environment variable", "is_full_universe": False, "warning": "Özel sembol listesi kullanılıyor; tüm BIST kapsamı olduğu doğrulanmadı."}
        return symbols
    try:
        response = requests.get(UNIVERSE_URL, timeout=12, headers={"User-Agent": "BIST-Spek-Radar/1.0"})
        response.raise_for_status()
        table = pd.read_csv(io.StringIO(response.text))
        col = next((c for c in table.columns if c.lower() in {"symbol", "ticker", "code"}), None)
        if col:
            symbols = [str(s).strip().upper().removesuffix(".IS") for s in table[col].dropna()]
            symbols = [s for s in symbols if s.isalnum() and 3 <= len(s) <= 6]
            if len(symbols) >= 100:
                symbols = list(dict.fromkeys(symbols))
                UNIVERSE_STATUS = {"source": UNIVERSE_URL, "is_full_universe": True, "warning": "Sembol listesi harici CSV kaynağından alındı; borsa resmî evreniyle ayrıca karşılaştırılmalı."}
                return symbols
    except Exception as exc:
        UNIVERSE_STATUS = {"source": "fallback", "is_full_universe": False, "warning": f"Harici sembol listesi alınamadı ({type(exc).__name__}); yalnızca yedek liste kullanılıyor."}
    else:
        UNIVERSE_STATUS = {"source": "fallback", "is_full_universe": False, "warning": "Harici CSV beklenen sembol sütununu veya yeterli sayıda hisseyi içermedi; yalnızca yedek liste kullanılıyor."}
    return FALLBACK.split(",")


def _analyze(symbol: str, frame: pd.DataFrame) -> Signal | None:
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        # yf.download(group_by="ticker") returns ticker at level 0.
        if symbol in frame.columns.get_level_values(0):
            frame = frame[symbol]
        else:
            frame.columns = frame.columns.get_level_values(-1)
    if not {"Close", "Volume"}.issubset(frame.columns):
        return None
    df = frame.dropna(subset=["Close", "Volume"]).copy()
    if len(df) < 25:
        return None
    close, volume = df["Close"].astype(float), df["Volume"].astype(float)
    last = float(close.iloc[-1])
    if not np.isfinite(last) or last <= 0:
        return None
    baseline = volume.iloc[-21:-1]
    vol_base = float(baseline.median())
    vol_ratio = float(volume.iloc[-1] / vol_base) if vol_base > 0 else 0.0
    change_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
    change_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
    returns = close.pct_change().dropna()
    vol20 = float(returns.iloc[-20:].std(ddof=1) * np.sqrt(20) * 100)
    turnover = float(last * volume.iloc[-1])
    change_20d = float((close.iloc[-1] / close.iloc[-21] - 1) * 100)
    high20 = float(close.iloc[-21:].max())
    distance_from_high20 = float((last / high20 - 1) * 100) if high20 > 0 else 0.0
    previous_volume = float(volume.iloc[-2])
    volume_fade = previous_volume > 0 and float(volume.iloc[-1]) < previous_volume * 0.55

    # Separate early-move scores from exhaustion/exit-risk scores.
    short_score, swing_score, exit_score = 0, 0, 0
    short_reasons, swing_reasons, exit_reasons = [], [], []
    if vol_ratio >= 3 and abs(change_1d) <= 4:
        short_score += 30; short_reasons.append("Fiyat henüz sınırlı hareket ederken hacim belirgin arttı")
    elif vol_ratio >= 2:
        short_score += 18; short_reasons.append("Günlük hacim 20 günlük medyanın en az 2 katı")
    if -2 <= change_1d <= 4 and -4 <= change_5d <= 10:
        short_score += 15; short_reasons.append("Kısa vadeli fiyat henüz aşırı yükselmedi")
    if change_1d > 0 and vol_ratio >= 1.5:
        short_score += 15; short_reasons.append("Pozitif günlük hareket hacimle destekleniyor")
    if vol_ratio >= 1.5 and change_20d <= 18:
        swing_score += 20; swing_reasons.append("Hacim artışı 20 günlük aşırı yükselişten önce/erken aşamada")
    if 0 <= change_5d <= 15:
        swing_score += 15; swing_reasons.append("5 günlük yükseliş henüz aşırı hızlanmamış")
    if change_20d <= 25:
        swing_score += 10; swing_reasons.append("20 günlük fiyat uzaması sınırlı")
    if change_1d <= -5 and vol_ratio >= 2:
        exit_score += 35; exit_reasons.append("Yüksek hacimle sert günlük düşüş")
    if change_5d >= 20:
        exit_score += 20; exit_reasons.append("5 işlem gününde sert fiyat uzaması")
    if change_20d >= 40:
        exit_score += 20; exit_reasons.append("20 işlem gününde aşırı yükseliş")
    if volume_fade and change_5d >= 10:
        exit_score += 15; exit_reasons.append("Yükseliş sonrası günlük hacimde belirgin sönme")
    if distance_from_high20 <= -8 and change_20d >= 15:
        exit_score += 20; exit_reasons.append("20 günlük zirveden belirgin geri çekilme")
    short_score, swing_score, exit_score = min(short_score, 100), min(swing_score, 100), min(exit_score, 100)
    phase = "ÇIKIŞ RİSKİ" if exit_score >= 40 else ("HIZLI HAREKET ADAYI" if short_score >= 40 else ("ORTA VADE ADAYI" if swing_score >= 35 else "İZLEME"))
    # General anomaly score remains as a separate context score.
    score, reasons = 0, []
    if vol_ratio >= 5:
        score += 30; reasons.append(f"Hacim 20 günlük medyanın {vol_ratio:.1f} katı")
    elif vol_ratio >= 3:
        score += 24; reasons.append(f"Hacim 20 günlük medyanın {vol_ratio:.1f} katı")
    elif vol_ratio >= 2:
        score += 15; reasons.append(f"Hacim artışı {vol_ratio:.1f} kat")
    if abs(change_1d) >= 8:
        score += 18; reasons.append(f"Günlük fiyat hareketi %{change_1d:.1f}")
    elif abs(change_1d) >= 4:
        score += 10; reasons.append(f"Günlük fiyat hareketi %{change_1d:.1f}")
    if abs(change_5d) >= 20:
        score += 18; reasons.append(f"5 günlük hareket %{change_5d:.1f}")
    elif abs(change_5d) >= 10:
        score += 10; reasons.append(f"5 günlük hareket %{change_5d:.1f}")
    if vol20 >= 15:
        score += 12; reasons.append(f"20 günlük oynaklık %{vol20:.1f}")
    elif vol20 >= 8:
        score += 6; reasons.append(f"20 günlük oynaklık %{vol20:.1f}")
    # Turnover is a liquidity context field, not a manipulation signal.
    if turnover >= 100_000_000:
        score += 5; reasons.append("Günlük işlem tutarı 100 milyon TL üzerinde")
    # Very high volume with muted price can indicate absorption/rotation.
    if vol_ratio >= 2 and abs(change_1d) < 1.5:
        score += 12; reasons.append("Hacim yüksek, fiyat tepkisi sınırlı")
    return Signal(
        symbol=symbol, name=symbol, score=min(score, 100), last_close=round(last, 4),
        change_1d_pct=round(change_1d, 2), change_5d_pct=round(change_5d, 2),
        volume_ratio=round(vol_ratio, 2), turnover_try=round(turnover, 2),
        volatility_20d_pct=round(vol20, 2), short_term_score=short_score, swing_score=swing_score,\n        exit_risk_score=exit_score, phase=phase, exit_reasons=exit_reasons, reasons=reasons,
        data_date=str(df.index[-1].date()),
    )


def scan_symbols(symbols: list[str] | None = None, period: str = "3mo") -> list[dict[str, Any]]:
    universe = symbols or configured_symbols()
    tickers = [f"{s.removesuffix('.IS')}.IS" for s in universe]
    try:
        raw = yf.download(
            tickers=tickers, period=period, interval="1d", auto_adjust=True,
            group_by="ticker", threads=True, progress=False, timeout=8,
        )
    except Exception:
        raw = pd.DataFrame()
    results: list[dict[str, Any]] = []
    if raw.empty:
        return results
    for symbol in universe:
        ticker = f"{symbol.removesuffix('.IS')}.IS"
        try:
            frame = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(0) else raw
            signal = _analyze(symbol.removesuffix(".IS"), frame)
            if signal and signal.score > 0:
                results.append(asdict(signal))
        except Exception:
            continue
    return sorted(results, key=lambda row: (row["score"], row["volume_ratio"], row["turnover_try"]), reverse=True)


def universe_status() -> dict[str, Any]:
    return dict(UNIVERSE_STATUS)
