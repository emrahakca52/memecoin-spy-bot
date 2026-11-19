from __future__ import annotations
import pandas as pd

def fetch_ohlcv(symbol: str, period: str = "2y") -> pd.DataFrame:
    # Prototype only: replace with licensed BIST market-data adapter for production.
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance eksik; pip install -r requirements.txt çalıştırın.") from exc
    raw = yf.download(symbol, period=period, interval="1d", auto_adjust=True,
                      progress=False, threads=False)
    if raw is None or raw.empty:
        raise ValueError(f"{symbol}: veri alınamadı veya sembol desteklenmiyor.")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.columns = [str(c).lower().replace(" ", "_") for c in raw.columns]
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in raw.columns]
    if missing:
        raise ValueError(f"{symbol}: eksik kolonlar: {missing}")
    out = raw[required].copy()
    out.index = pd.to_datetime(out.index)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    for col in required:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna()
    out = out[(out.close > 0) & (out.volume >= 0)]
    if len(out) < 80:
        raise ValueError(f"{symbol}: yeterli veri yok ({len(out)} bar).")
    return out

def load_symbols_file(path: str) -> list[str]:
    df = pd.read_csv(path)
    col = "symbol" if "symbol" in df.columns else df.columns[0]
    return sorted(set(df[col].dropna().astype(str).str.strip().tolist()))
