"""Historical pattern mining for pre-breakout BIST research."""
from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd
import yfinance as yf

def _features(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    if isinstance(df.columns, pd.MultiIndex):
        # yfinance may return (Price, Ticker) or (Ticker, Price).
        # Select the level that actually contains OHLCV field names.
        price_fields = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
        chosen = None
        for level in range(df.columns.nlevels):
            vals = set(str(v) for v in df.columns.get_level_values(level))
            if {"Close", "Volume"}.issubset(vals):
                chosen = level
                break
        if chosen is None:
            raise ValueError("Yahoo Finance sütunlarında Close/Volume bulunamadı")
        if df.columns.nlevels > 1:
            other_levels = [i for i in range(df.columns.nlevels) if i != chosen]
            if other_levels:
                tickers = list(dict.fromkeys(str(v) for v in df.columns.get_level_values(other_levels[0])))
                if len(tickers) == 1:
                    df = df.xs(tickers[0], axis=1, level=other_levels[0], drop_level=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(chosen if chosen < df.columns.nlevels else 0)
    df.columns = [str(col) for col in df.columns]
    df = df.dropna(subset=["Close", "Volume"]).copy()
    if len(df) < 45:
        return pd.DataFrame()
    c = df["Close"].astype(float)
    v = df["Volume"].astype(float)
    out = pd.DataFrame(index=df.index)
    out["close"] = c
    out["volume"] = v
    out["ret1"] = c.pct_change(1) * 100
    out["ret5"] = c.pct_change(5) * 100
    out["ret20"] = c.pct_change(20) * 100
    out["vol_med20"] = v.shift(1).rolling(20).median()
    out["vol_ratio"] = v / out["vol_med20"].replace(0, np.nan)
    out["vol_accel"] = v.rolling(5).median() / v.shift(5).rolling(20).median().replace(0, np.nan)
    lo = c.shift(1).rolling(20).min()
    hi = c.shift(1).rolling(20).max()
    out["range_pos"] = (c - lo) / (hi - lo).replace(0, np.nan)
    out["future20"] = (c.shift(-1).rolling(20).max().shift(-19) / c - 1) * 100
    out["future40"] = (c.shift(-1).rolling(40).max().shift(-39) / c - 1) * 100
    out["future60"] = (c.shift(-1).rolling(60).max().shift(-59) / c - 1) * 100
    return out.replace([np.inf, -np.inf], np.nan)

def _candidate_score(row: pd.Series) -> int:
    score = 0
    if row.vol_accel >= 2: score += 25
    elif row.vol_accel >= 1.5: score += 18
    elif row.vol_accel >= 1.2: score += 10
    if row.vol_ratio >= 2: score += 20
    elif row.vol_ratio >= 1.5: score += 12
    if -5 <= row.ret5 <= 12: score += 15
    elif -10 <= row.ret5 <= 18: score += 8
    if -5 <= row.range_pos <= 0.65: score += 15
    if abs(row.ret1) <= 4: score += 10
    if row.ret20 <= 20: score += 5
    return min(score, 90)

def analyze_history(symbol: str, period: str = "max") -> dict[str, Any]:
    ticker = symbol.upper().removesuffix(".IS") + ".IS"
    try:
        frame = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False, threads=False)
        if frame.empty: return {"symbol": symbol.upper(), "error": "Geçmiş veri alınamadı."}
        f = _features(frame)
        if f.empty: return {"symbol": symbol.upper(), "error": "Yeterli geçmiş veri yok."}
        # Find the strongest historical forward run-up and inspect the 60 sessions before it.
        valid = f.dropna(subset=["future60", "vol_accel", "vol_ratio", "range_pos"])
        if valid.empty: return {"symbol": symbol.upper(), "error": "Analiz için yeterli veri yok."}
        peak_date = valid["future60"].idxmax()
        peak_gain = float(valid.loc[peak_date, "future60"])
        peak_loc = f.index.get_loc(peak_date)
        # The selected date is the START of the strongest forward 60-session run-up.
        # Also report the actual highest closing-price date inside that forward window.
        future_window = f.iloc[peak_loc + 1: peak_loc + 61]
        runup_peak_date = future_window["close"].idxmax() if not future_window.empty else peak_date
        start_loc = max(0, peak_loc - 60)
        window = f.iloc[start_loc:peak_loc].copy()
        window = window.dropna(subset=["vol_accel", "vol_ratio", "range_pos"])
        if window.empty: return {"symbol": symbol.upper(), "error": "Yükseliş öncesi ölçülebilir dönem bulunamadı."}
        window["score"] = window.apply(_candidate_score, axis=1)
        top = window.sort_values(["score", "vol_accel"], ascending=False).head(12)
        rows = []
        for dt, r in top.sort_index().iterrows():
            rows.append({
                "date": str(dt.date()), "close": round(float(r.close), 4),
                "score": int(r.score), "volume_ratio": round(float(r.vol_ratio), 2),
                "volume_acceleration_5d": round(float(r.vol_accel), 2),
                "change_1d_pct": round(float(r.ret1), 2),
                "change_5d_pct": round(float(r.ret5), 2),
                "change_20d_pct": round(float(r.ret20), 2),
                "range_position_20d": round(float(r.range_pos), 2),
                "forward_max_20d_pct": None if pd.isna(r.future20) else round(float(r.future20), 2),
                "forward_max_40d_pct": None if pd.isna(r.future40) else round(float(r.future40), 2),
                "forward_max_60d_pct": None if pd.isna(r.future60) else round(float(r.future60), 2),
            })
        return {"symbol": ticker.removesuffix(".IS"), "runup_start_date": str(peak_date.date()),
                "runup_peak_date": str(runup_peak_date.date()),
                "peak_forward_60d_date": str(peak_date.date()),
                "peak_forward_60d_return_pct": round(peak_gain, 2),
                "lookback_sessions": 60, "candidate_days": rows,
                "source": "Yahoo Finance; gecikmeli/eksik olabilir"}
    except Exception as exc:
        return {"symbol": symbol.upper(), "error": f"Veri/analiz hatası: {type(exc).__name__}"}

def _extract_ticker_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Extract one ticker from yfinance's grouped MultiIndex batch response."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        return raw
    for level in range(raw.columns.nlevels):
        values = set(str(v) for v in raw.columns.get_level_values(level))
        if ticker in values:
            return raw.xs(ticker, axis=1, level=level, drop_level=True)
    return pd.DataFrame()


def backtest_universe(symbols: list[str], period: str = "2y", limit: int = 40) -> dict[str, Any]:
    results = []
    failures = 0
    symbols_with_data = 0
    normalized = list(dict.fromkeys(s.upper().removesuffix(".IS") for s in symbols))
    # Batch downloads avoid one Yahoo request per ticker, which triggers rate limits.
    batch_size = 25
    for offset in range(0, len(normalized), batch_size):
        batch = normalized[offset:offset + batch_size]
        tickers = [f"{s}.IS" for s in batch]
        try:
            raw = yf.download(
                tickers=tickers, period=period, interval="1d", auto_adjust=True,
                group_by="ticker", threads=True, progress=False, timeout=35,
            )
        except Exception:
            failures += len(batch)
            continue
        if raw is None or raw.empty:
            failures += len(batch)
            continue
        for symbol, ticker in zip(batch, tickers):
            try:
                frame = _extract_ticker_frame(raw, ticker)
                f = _features(frame)
                if f.empty:
                    failures += 1
                    continue
                symbols_with_data += 1
                f = f.dropna(subset=["vol_accel", "vol_ratio", "range_pos", "future20"])
                for dt, r in f.iterrows():
                    score = _candidate_score(r)
                    # Signal is calculated only from data available on that date.
                    if score >= 40 and abs(float(r.ret5)) <= 18 and float(r.ret20) <= 25:
                        results.append({"symbol": symbol, "date": str(dt.date()), "score": score,
                            "close": round(float(r.close), 4), "volume_acceleration_5d": round(float(r.vol_accel), 2),
                            "volume_ratio": round(float(r.vol_ratio), 2), "change_5d_pct": round(float(r.ret5), 2),
                            "forward_max_20d_pct": round(float(r.future20), 2),
                            "outcome_20d": "YÜKSELİŞ" if float(r.future20) >= 30 else ("DÜŞÜŞ" if float(r.future20) <= -15 else "NÖTR")})
            except Exception:
                failures += 1
    results.sort(key=lambda x: (x["score"], x["volume_acceleration_5d"], x["forward_max_20d_pct"]), reverse=True)
    return {"mode":"HISTORICAL_RESEARCH_ONLY","period":period,"symbols_requested":len(normalized),
            "symbols_with_data":symbols_with_data,"data_failures":failures,
            "candidate_count":len(results),"results":results[:limit],
            "note":"YÜKSELİŞ sınıfı, sonraki 20 işlem günündeki en yüksek fiyatın başlangıç kapanışına göre en az %30 yükselmesidir; dönem sonu getirisi değildir. Geriye dönük sonuçlar geleceği garanti etmez. Yahoo Finance verisi gecikmeli/eksik olabilir."}
