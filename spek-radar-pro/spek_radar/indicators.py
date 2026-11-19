from __future__ import annotations

import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame, benchmark: pd.Series | None = None) -> pd.DataFrame:
    """Add daily technical features using only information available at each close."""
    x = df.copy().sort_index()
    prev = x.close.shift(1)
    tr = pd.concat(
        [x.high - x.low, (x.high - prev).abs(), (x.low - prev).abs()],
        axis=1,
    ).max(axis=1)
    x["atr14"] = tr.rolling(14).mean()
    x["atr_pct"] = x.atr14 / x.close.replace(0, np.nan)

    # Compare today's ATR with the preceding 20 sessions, not a window
    # containing today's observation.
    atr_baseline = x.atr_pct.shift(1).rolling(20).median()
    x["atr_compression"] = x.atr_pct / atr_baseline.replace(0, np.nan)

    # Relative volume must use the preceding 20 sessions to avoid diluting
    # today's unusual volume with the observation being evaluated.
    x["vol_avg20"] = x.volume.shift(1).rolling(20).mean()
    x["rel_volume"] = x.volume / x.vol_avg20.replace(0, np.nan)

    # Approximate traded value; adjusted close * volume is not official turnover.
    x["turnover20_tl"] = (x.close * x.volume).rolling(20).mean()

    direction = np.sign(x.close.diff()).fillna(0)
    x["obv"] = (direction * x.volume).cumsum()
    # Express the 20-session OBV change in units of average daily volume.
    x["obv_change20"] = x.obv.diff(20) / x.vol_avg20.replace(0, np.nan)

    bar_range = (x.high - x.low).replace(0, np.nan)
    x["close_location"] = ((x.close - x.low) / bar_range).clip(0, 1)
    x["return5"] = x.close.pct_change(5)
    x["return20"] = x.close.pct_change(20)
    x["high20"] = x.high.rolling(20).max().shift(1)
    x["distance_high20"] = x.close / x.high20 - 1

    if benchmark is not None:
        b = benchmark.reindex(x.index).ffill()
        x["relative_strength20"] = x.close.pct_change(20) - b.pct_change(20)
    else:
        x["relative_strength20"] = np.nan
    return x


def _num(value, digits):
    return None if pd.isna(value) else round(float(value), digits)


def score_latest(x: pd.DataFrame) -> dict:
    r = x.iloc[-1]
    rv = r.get("rel_volume", np.nan)
    comp = r.get("atr_compression", np.nan)
    obv = r.get("obv_change20", np.nan)
    loc = r.get("close_location", np.nan)
    rs = r.get("relative_strength20", np.nan)

    parts = {
        "relative_volume": 100 if pd.notna(rv) and rv >= 3 else 75 if pd.notna(rv) and rv >= 2 else 45 if pd.notna(rv) and rv >= 1.4 else 10,
        "volatility_compression": 90 if pd.notna(comp) and comp <= .75 else 65 if pd.notna(comp) and comp <= .9 else 35 if pd.notna(comp) and comp <= 1.05 else 10,
        "obv_pressure": 85 if pd.notna(obv) and obv > 1.5 else 65 if pd.notna(obv) and obv > .5 else 35 if pd.notna(obv) and obv > 0 else 10,
        "close_strength": 85 if pd.notna(loc) and loc >= .8 else 60 if pd.notna(loc) and loc >= .6 else 25,
        "relative_strength": 75 if pd.notna(rs) and rs >= .08 else 60 if pd.notna(rs) and rs > .02 else 35 if pd.notna(rs) and rs >= 0 else 10,
    }
    weights = {
        "relative_volume": .25,
        "volatility_compression": .15,
        "obv_pressure": .20,
        "close_strength": .15,
        "relative_strength": .25,
    }
    score = round(sum(parts[key] * weights[key] for key in weights))
    label = (
        "GÜÇLÜ ERKEN UYARI" if score >= 80
        else "YAKIN TAKİP" if score >= 65
        else "İZLEME" if score >= 50
        else "SİNYAL ZAYIF"
    )
    turnover = r.get("turnover20_tl", np.nan)
    liquidity = (
        "ÇOK DÜŞÜK" if pd.notna(turnover) and turnover < 5_000_000
        else "DÜŞÜK" if pd.notna(turnover) and turnover < 20_000_000
        else "NORMAL" if pd.notna(turnover)
        else "BİLİNMİYOR"
    )
    return {
        "score": score,
        "signal": label,
        "avg_turnover20_tl": _num(turnover, 0),
        "liquidity": liquidity,
        "rel_volume": _num(rv, 2),
        "atr_compression": _num(comp, 2),
        "obv_change20": _num(obv, 2),
        "close_location": _num(loc, 2),
        "relative_strength20": _num(rs, 3),
        **{f"score_{key}": value for key, value in parts.items()},
    }
