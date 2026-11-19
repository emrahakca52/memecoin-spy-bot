"""Explainable anomaly screening for Borsa Istanbul daily bars.

Yahoo Finance data is indicative and may be delayed/incomplete. This module
flags statistical outliers; it does not infer intent or prove manipulation.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf


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
    reasons: list[str]
    data_date: str
    data_source: str = "Yahoo Finance (indicative; may be delayed)"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def configured_symbols() -> list[str]:
    raw = os.getenv("BIST_SYMBOLS", "THYAO,ASELS,TUPRS,EREGL,AKBNK,GARAN,ISCTR,SISE,SAHOL,KCHOL,TCELL,BIMAS,PGSUS,TOASO,FROTO,KOZAL,ASTOR,HEKTS,MIATK,DSTKF")
    return list(dict.fromkeys(s.strip().upper().removesuffix(".IS") for s in raw.split(",") if s.strip()))


def _analyze(symbol: str, frame: pd.DataFrame) -> Signal | None:
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    needed = {"Close", "Volume"}
    if not needed.issubset(frame.columns):
        return None
    df = frame.dropna(subset=["Close", "Volume"]).copy()
    if len(df) < 25:
        return None
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    last = float(close.iloc[-1])
    if not np.isfinite(last) or last <= 0:
        return None
    vol_base = float(volume.iloc[-21:-1].median())
    vol_ratio = float(volume.iloc[-1] / vol_base) if vol_base > 0 else 0.0
    change_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100)
    change_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100)
    returns = close.pct_change().dropna()
    vol20 = float(returns.iloc[-20:].std(ddof=1) * np.sqrt(20) * 100)
    turnover = float(last * volume.iloc[-1])
    score = 0
    reasons: list[str] = []
    if vol_ratio >= 3:
        score += 30
        reasons.append(f"Hacim, önceki 20 gün medyanının {vol_ratio:.1f} katı")
    elif vol_ratio >= 2:
        score += 18
        reasons.append(f"Hacim artışı: {vol_ratio:.1f} kat")
    if abs(change_1d) >= 7:
        score += 25
        reasons.append(f"Günlük fiyat hareketi %{change_1d:.1f}")
    elif abs(change_1d) >= 4:
        score += 15
        reasons.append(f"Belirgin günlük hareket: %{change_1d:.1f}")
    if abs(change_5d) >= 15:
        score += 20
        reasons.append(f"5 günlük hareket %{change_5d:.1f}")
    elif abs(change_5d) >= 8:
        score += 10
        reasons.append(f"5 günlük hareket %{change_5d:.1f}")
    if vol20 >= 12:
        score += 15
        reasons.append(f"Yüksek 20 günlük oynaklık: %{vol20:.1f}")
    elif vol20 >= 7:
        score += 8
        reasons.append(f"Artan oynaklık: %{vol20:.1f}")
    if turnover >= 100_000_000:
        score += 10
        reasons.append("Günlük işlem tutarı 100 milyon TL üzerinde (yaklaşık)")
    return Signal(
        symbol=symbol, name=symbol, score=min(score, 100), last_close=round(last, 4),
        change_1d_pct=round(change_1d, 2), change_5d_pct=round(change_5d, 2),
        volume_ratio=round(vol_ratio, 2), turnover_try=round(turnover, 2),
        volatility_20d_pct=round(vol20, 2), reasons=reasons,
        data_date=str(df.index[-1].date()),
    )


def scan_symbols(symbols: list[str] | None = None, period: str = "6mo") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for symbol in (symbols or configured_symbols()):
        ticker = symbol if symbol.endswith(".IS") else f"{symbol}.IS"
        try:
            history = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
            signal = _analyze(symbol.removesuffix(".IS"), history)
            if signal and signal.score > 0:
                results.append(signal.to_dict())
        except Exception:
            continue
    return sorted(results, key=lambda row: (row["score"], row["turnover_try"]), reverse=True)
