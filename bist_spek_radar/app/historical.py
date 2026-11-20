"""Leakage-resistant historical BIST signal research.

All signals use information available at the signal-day close only.
Forward returns are computed solely for evaluation, never for ranking/signalling.
Yahoo Finance data may be delayed, incomplete, or adjusted.
"""
from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd
import yfinance as yf


def _features(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    if isinstance(df.columns, pd.MultiIndex):
        chosen = None
        for level in range(df.columns.nlevels):
            vals = set(str(v) for v in df.columns.get_level_values(level))
            if {"Close", "Volume"}.issubset(vals):
                chosen = level
                break
        if chosen is None:
            raise ValueError("Yahoo Finance sütunlarında Close/Volume bulunamadı")
        other = [i for i in range(df.columns.nlevels) if i != chosen]
        if other:
            tickers = list(dict.fromkeys(str(v) for v in df.columns.get_level_values(other[0])))
            if len(tickers) == 1:
                df = df.xs(tickers[0], axis=1, level=other[0], drop_level=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
    df.columns = [str(x) for x in df.columns]
    if not {"Close", "Volume"}.issubset(df.columns):
        return pd.DataFrame()
    df = df.dropna(subset=["Close", "Volume"]).copy()
    if len(df) < 65:
        return pd.DataFrame()
    c, v = df["Close"].astype(float), df["Volume"].astype(float)
    h = df["High"].astype(float) if "High" in df else c
    l = df["Low"].astype(float) if "Low" in df else c
    f = pd.DataFrame(index=df.index)
    f["close"], f["volume"] = c, v
    f["high"], f["low"] = h, l
    f["ret1"], f["ret5"], f["ret20"] = c.pct_change()*100, c.pct_change(5)*100, c.pct_change(20)*100
    f["vol_ratio"] = v / v.rolling(20).mean().replace(0, np.nan)
    f["vol_accel"] = v.rolling(5).mean() / v.rolling(20).mean().shift(5).replace(0, np.nan)
    f["ema9"] = c.ewm(span=9, adjust=False).mean()
    f["ema21"] = c.ewm(span=21, adjust=False).mean()
    f["ema50"] = c.ewm(span=50, adjust=False).mean()
    f["trend_strong"] = (c > f["ema21"]) & (f["ema9"] > f["ema21"]) & (f["ema21"] > f["ema50"])
    f["trend_improving"] = (f["ema9"] > f["ema21"]) & (f["ema9"] > f["ema9"].shift(3))
    bar_range = (h-l).replace(0, np.nan)
    mfm = ((c-l) - (h-c)) / bar_range
    mfv = mfm.fillna(0) * v
    f["cmf"] = mfv.rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)
    f["prior_high"] = h.shift(1).rolling(20).max()
    f["prior_low"] = l.shift(1).rolling(20).min()
    f["range_pos"] = (c-f["prior_low"]) / (f["prior_high"]-f["prior_low"]).replace(0,np.nan)
    f["near_breakout"] = (c >= f["prior_high"]*0.97) & (c <= f["prior_high"]*1.03)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    f["rsi"] = 100 - 100/(1 + gain/loss.replace(0,np.nan))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    f["macd_hist"] = macd-signal
    f["momentum_strong"] = (f["rsi"] >= 52) & (f["rsi"] <= 72) & (f["macd_hist"] > 0) & (f["macd_hist"] >= f["macd_hist"].shift(1))
    f["not_extended"] = (f["ret1"] < 8) & (f["ret5"] < 18) & (f["ret20"] < 30)
    for n in (5, 10, 20):
        f[f"future_max{n}"] = c.shift(-1).rolling(n).max().shift(-(n-1))
        f[f"future_min{n}"] = c.shift(-1).rolling(n).min().shift(-(n-1))
        f[f"future_close{n}"] = c.shift(-n)
        f[f"max_return{n}"] = (f[f"future_max{n}"]/c-1)*100
        f[f"min_return{n}"] = (f[f"future_min{n}"]/c-1)*100
        f[f"close_return{n}"] = (f[f"future_close{n}"]/c-1)*100
    return f.replace([np.inf,-np.inf],np.nan)


def _score_row(r: pd.Series) -> tuple[int, list[str]]:
    score, reasons = 0, []
    if r.vol_ratio >= 2.0: score += 15; reasons.append("Hacim ortalamanın 2 katı")
    elif r.vol_ratio >= 1.5: score += 10; reasons.append("Hacim ortalamanın 1,5 katı")
    elif r.vol_ratio >= 1.2: score += 5; reasons.append("Hacim artıyor")
    if r.vol_accel >= 2.0: score += 10; reasons.append("Hacim ivmesi çok güçlü")
    elif r.vol_accel >= 1.5: score += 7; reasons.append("Hacim ivmesi güçlü")
    elif r.vol_accel >= 1.2: score += 4; reasons.append("Hacim ivmesi artıyor")
    if bool(r.trend_strong): score += 20; reasons.append("Ana trend yukarı")
    elif bool(r.trend_improving): score += 12; reasons.append("Kısa trend güçleniyor")
    if r.cmf > 0.15: score += 20; reasons.append("Para akışı güçlü pozitif")
    elif r.cmf > 0.05: score += 14; reasons.append("Para akışı pozitif")
    elif r.cmf > 0: score += 7; reasons.append("Para akışı sıfırın üzerinde")
    if bool(r.near_breakout): score += 15; reasons.append("20 günlük tepeye yakın")
    elif r.range_pos >= 0.65 and r.range_pos < 0.97: score += 10; reasons.append("20 günlük aralığın üst bölgesinde")
    elif r.range_pos >= 0.45: score += 5; reasons.append("Fiyat aralık içinde güçleniyor")
    if bool(r.momentum_strong): score += 15; reasons.append("RSI ve MACD momentumu güçlü")
    elif r.rsi >= 48 and r.macd_hist > 0: score += 8; reasons.append("Momentum pozitif")
    if bool(r.not_extended): score += 5; reasons.append("Henüz aşırı yükselmemiş")
    return min(int(score),100), reasons


def _extract_ticker_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        return raw
    for level in range(raw.columns.nlevels):
        if ticker in set(str(x) for x in raw.columns.get_level_values(level)):
            return raw.xs(ticker, axis=1, level=level, drop_level=True)
    return pd.DataFrame()


def _download_features(symbol: str, period: str) -> pd.DataFrame:
    ticker = symbol.upper().removesuffix(".IS") + ".IS"
    frame = yf.download(ticker, period=period, interval="1d", auto_adjust=True,
                        progress=False, threads=False, timeout=25)
    return _features(frame) if frame is not None and not frame.empty else pd.DataFrame()


def analyze_history(symbol: str, period: str = "max") -> dict[str, Any]:
    try:
        f = _download_features(symbol, period)
        if f.empty:
            return {"symbol": symbol.upper(), "error": "Yeterli geçmiş veri alınamadı."}
        valid = f.dropna(subset=["vol_accel", "vol_ratio", "range_pos", "max_return20"])
        if valid.empty:
            return {"symbol": symbol.upper(), "error": "Analiz için yeterli veri yok."}
        peak_date = valid["max_return20"].idxmax()
        peak_gain = float(valid.loc[peak_date, "max_return20"])
        start = max(0, f.index.get_loc(peak_date) - 60)
        window = f.iloc[start:f.index.get_loc(peak_date)].copy()
        window = window.dropna(subset=["vol_accel", "vol_ratio", "range_pos"])
        rows = []
        for dt, r in window.iterrows():
            score, reasons = _score_row(r)
            rows.append({"date": str(dt.date()), "close": round(float(r.close), 4),
                         "score": score, "volume_ratio": round(float(r.vol_ratio), 2),
                         "volume_acceleration_5d": round(float(r.vol_accel), 2),
                         "change_1d_pct": round(float(r.ret1), 2),
                         "change_5d_pct": round(float(r.ret5), 2),
                         "reasons": reasons})
        rows.sort(key=lambda x: (x["score"], x["date"]), reverse=True)
        return {"symbol": symbol.upper().removesuffix(".IS"),
                "strongest_forward_runup_start": str(peak_date.date()),
                "strongest_forward_max20_pct": round(peak_gain, 2),
                "candidate_days": rows[:20],
                "note": "Gelecek getiri yalnızca tarihsel değerlendirmede kullanılmıştır; sinyal skoruna katılmaz."}
    except Exception as exc:
        return {"symbol": symbol.upper(), "error": f"Veri/analiz hatası: {type(exc).__name__}"}


def backtest_universe(symbols: list[str], period: str = "2y", limit: int = 100, progress_callback=None) -> dict[str, Any]:
    normalized = list(dict.fromkeys(s.upper().removesuffix(".IS") for s in symbols))
    all_events: list[dict[str, Any]] = []
    failures, with_data, processed = 0, 0, 0
    batch_size = 30
    total_batches = (len(normalized) + batch_size - 1) // batch_size
    for batch_no, offset in enumerate(range(0, len(normalized), batch_size), start=1):
        batch = normalized[offset:offset + batch_size]
        tickers = [f"{s}.IS" for s in batch]
        try:
            raw = yf.download(tickers=tickers, period=period, interval="1d", auto_adjust=True,
                              group_by="ticker", threads=True, progress=False, timeout=45)
        except Exception:
            raw = pd.DataFrame()
        if raw is None or raw.empty:
            failures += len(batch)
        else:
            for symbol, ticker in zip(batch, tickers):
                try:
                    frame = _extract_ticker_frame(raw, ticker)
                    f = _features(frame)
                    if f.empty:
                        failures += 1
                        continue
                    with_data += 1
                    required = ["vol_accel", "vol_ratio", "range_pos", "ret1", "ret5", "ret20", "cmf", "macd_hist",
                                "max_return5", "max_return10", "max_return20",
                                "min_return5", "min_return10", "min_return20",
                                "close_return5", "close_return10", "close_return20"]
                    eligible = f.dropna(subset=required)
                    last_signal_loc = -999
                    for loc, (dt, r) in enumerate(eligible.iterrows()):
                        score, reasons = _score_row(r)
                        # One signal per share per 5 sessions to avoid duplicate spam.
                        if score < 80 or loc - last_signal_loc < 5:
                            continue
                        # A stretched/limit-up day is not an early-entry setup.
                        if r.ret1 >= 8 or r.ret5 > 18 or r.ret20 > 30 or r.cmf <= 0 or not bool(r.trend_improving) or not bool(r.momentum_strong):
                            continue
                        last_signal_loc = loc
                        all_events.append({
                            "symbol": symbol, "date": str(dt.date()), "score": score,
                            "close": round(float(r.close), 4),
                            "volume_ratio": round(float(r.vol_ratio), 2),
                            "volume_acceleration_5d": round(float(r.vol_accel), 2),
                            "change_1d_pct": round(float(r.ret1), 2),
                            "change_5d_pct": round(float(r.ret5), 2),
                            "change_20d_pct": round(float(r.ret20), 2),
                            "range_position_20d": round(float(r.range_pos), 2),\n                            "cmf": round(float(r.cmf), 3),
                            "forward_max_5d_pct": round(float(r.max_return5), 2),
                            "forward_max_10d_pct": round(float(r.max_return10), 2),
                            "forward_max_20d_pct": round(float(r.max_return20), 2),
                            "forward_min_5d_pct": round(float(r.min_return5), 2),
                            "forward_min_10d_pct": round(float(r.min_return10), 2),
                            "forward_min_20d_pct": round(float(r.min_return20), 2),
                            "forward_close_5d_pct": round(float(r.close_return5), 2),
                            "forward_close_10d_pct": round(float(r.close_return10), 2),
                            "forward_close_20d_pct": round(float(r.close_return20), 2),
                            "reasons": reasons,
                        })
                except Exception:
                    failures += 1
        processed += len(batch)
        if progress_callback:
            progress_callback(processed, len(normalized), with_data, failures, batch_no, total_batches)

    def metrics(events: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
        if not events:
            return {"signals": 0, "positive_close_rate_pct": None, "hit_5pct_max_rate_pct": None,
                    "hit_10pct_max_rate_pct": None, "avg_close_return_pct": None,
                    "median_close_return_pct": None, "avg_max_adverse_pct": None}
        close = np.array([x[f"forward_close_{horizon}d_pct"] for x in events], dtype=float)
        maxr = np.array([x[f"forward_max_{horizon}d_pct"] for x in events], dtype=float)
        adverse = np.array([x[f"forward_min_{horizon}d_pct"] for x in events], dtype=float)
        return {"signals": len(events),
                "positive_close_rate_pct": round(float(np.mean(close > 0) * 100), 1),
                "hit_5pct_max_rate_pct": round(float(np.mean(maxr >= 5) * 100), 1),
                "hit_10pct_max_rate_pct": round(float(np.mean(maxr >= 10) * 100), 1),
                "avg_close_return_pct": round(float(np.mean(close)), 2),
                "median_close_return_pct": round(float(np.median(close)), 2),
                "avg_max_adverse_pct": round(float(np.mean(adverse)), 2)}

    all_events.sort(key=lambda x: (x["date"], x["symbol"]))
    return {
        "mode": "HISTORICAL_RESEARCH_NO_LOOKAHEAD_SIGNAL",
        "period": period, "symbols_requested": len(normalized),
        "symbols_processed": processed, "symbols_with_data": with_data,
        "data_failures": failures, "signal_count": len(all_events),
        "evaluation": {"5_sessions": metrics(all_events, 5),
                       "10_sessions": metrics(all_events, 10),
                       "20_sessions": metrics(all_events, 20)},
        "signals": all_events[-max(1, min(limit, 500)):],
        "note": ("Sinyal skoru yalnızca sinyal günündeki ve önceki OHLCV verileriyle hesaplanır. "
                 "Gelecek 5/10/20 seansın en yüksek/düşük ve kapanış getirileri yalnızca sonuç ölçümüdür. "
                 "Aynı hissede sinyaller arasında en az 5 seans vardır. "
                 "Komisyon, kayma, devre kesici, likidite ve hayatta kalma yanlılığı tam modellenmemiştir. "
                 "Yahoo Finance verisi gecikmeli/eksik olabilir; sonuçlar yatırım tavsiyesi değildir.")
    }
