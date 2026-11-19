import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .data import fetch_ohlcv
from .indicators import add_indicators, score_latest


def run_symbol(symbol: str, period: str, benchmark=None, threshold: int = 65):
    """Backtest threshold-crossing signals with next-session-open entries.

    A 1-session return exits at the close of the entry session. Only the first
    close crossing above the threshold in each signal episode is counted.
    """
    data = fetch_ohlcv(symbol, period)
    frame = add_indicators(data, benchmark)
    rows = []

    # Need 80 bars for indicator warm-up and up to 10 sessions for exits.
    for i in range(80, len(frame) - 10):
        current = score_latest(frame.iloc[: i + 1])
        previous = score_latest(frame.iloc[:i])
        if current["score"] < threshold or previous["score"] >= threshold:
            continue

        entry = pd.to_numeric(pd.Series([frame["open"].iloc[i + 1]]), errors="coerce").iloc[0]
        if not np.isfinite(entry) or entry <= 0:
            continue

        row = {
            "symbol": symbol,
            "signal_date": str(frame.index[i].date()),
            "entry_date": str(frame.index[i + 1].date()),
            "score": current["score"],
            "threshold": threshold,
            "entry_open": float(entry),
        }

        for horizon in (1, 3, 5, 10):
            exit_close = float(frame["close"].iloc[i + horizon])
            row[f"return_{horizon}d_pct"] = (exit_close / float(entry) - 1) * 100

        rows.append(row)

    return rows


def main():
    parser = argparse.ArgumentParser(description="SPEK RADAR PRO historical signal test")
    parser.add_argument("--symbols", default="DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS")
    parser.add_argument("--period", default="5y")
    parser.add_argument("--benchmark", default="XU100.IS")
    parser.add_argument("--threshold", type=int, default=65)
    args = parser.parse_args()

    benchmark = None
    if args.benchmark:
        try:
            benchmark = fetch_ohlcv(args.benchmark, args.period)["close"]
        except Exception as exc:
            print(f"Benchmark alınamadı; göreceli güç puanı eksik kalabilir: {exc}")

    rows = []
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    for symbol in symbols:
        try:
            symbol_rows = run_symbol(symbol, args.period, benchmark, args.threshold)
            rows.extend(symbol_rows)
            print(f"{symbol}: {len(symbol_rows)} eşik geçiş sinyali")
        except Exception as exc:
            print(f"{symbol}: test hatası: {exc}")

    Path("output").mkdir(exist_ok=True)
    if not rows:
        print("Test sonucu yok; veri, sembol ve tarih aralığını kontrol edin.")
        return

    results = pd.DataFrame(rows).sort_values(["symbol", "signal_date"])
    results.to_csv("output/backtest.csv", index=False)

    print(f"Toplam bağımsız sinyal dönemi: {len(results)}")
    for column in ("return_1d_pct", "return_3d_pct", "return_5d_pct", "return_10d_pct"):
        values = results[column].dropna()
        if values.empty:
            continue
        print(
            f"{column}: ortalama {values.mean():.2f}% | "
            f"medyan {values.median():.2f}% | "
            f"pozitif {((values > 0).mean() * 100):.1f}%"
        )
    print("Rapor: output/backtest.csv")


if __name__ == "__main__":
    main()
