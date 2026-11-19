import argparse
from pathlib import Path
import pandas as pd
from .data import fetch_ohlcv, load_symbols_file
from .indicators import add_indicators, score_latest
from .universe import fetch_kap_universe


def main():
    p = argparse.ArgumentParser(description="BIST olağan dışı hareket erken uyarı tarayıcısı")
    p.add_argument("--symbols", default="DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS")
    p.add_argument("--symbols-file")
    p.add_argument("--all-bist", action="store_true",
                   help="KAP'tan Yıldız/Ana/Alt/Yakın İzleme pay evrenini al")
    p.add_argument("--period", default="2y")
    p.add_argument("--benchmark", default="XU100.IS")
    a = p.parse_args()

    Path("output").mkdir(exist_ok=True)
    if a.all_bist:
        universe = fetch_kap_universe()
        universe.to_csv("output/universe_snapshot.csv", index=False)
        symbols = universe["symbol"].tolist()
        market_by_symbol = dict(zip(universe["symbol"], universe["market_segment"]))
        print(f"KAP evreni: {len(symbols)} pay")
    elif a.symbols_file:
        symbols = load_symbols_file(a.symbols_file)
        market_by_symbol = {}
    else:
        symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
        market_by_symbol = {}

    benchmark = None
    if a.benchmark:
        try:
            benchmark = fetch_ohlcv(a.benchmark, a.period).close
        except Exception as e:
            print(f"UYARI benchmark: {e}")

    rows, errors = [], []
    total = len(symbols)
    for i, symbol in enumerate(symbols, 1):
        try:
            data = fetch_ohlcv(symbol, a.period)
            features = add_indicators(data, benchmark)
            row = {
                "symbol": symbol,
                "market_segment": market_by_symbol.get(symbol, "BILINMIYOR"),
                "date": str(features.index[-1].date()),
                "close": round(float(features.close.iloc[-1]), 4),
            }
            row.update(score_latest(features))
            rows.append(row)
            print(f"[{i}/{total}] {symbol:12} {row['score']:3}/100  {row['signal']}")
        except Exception as e:
            errors.append({"symbol": symbol, "error": str(e)})
            print(f"[{i}/{total}] {symbol}: VERİ HATASI: {e}")

    if rows:
        pd.DataFrame(rows).sort_values("score", ascending=False).to_csv(
            "output/scan.csv", index=False
        )
    if errors:
        pd.DataFrame(errors).to_csv("output/errors.csv", index=False)
    print(f"Tamamlandı: {len(rows)} başarılı, {len(errors)} veri hatası / {total} sembol.")
    print("Rapor: output/scan.csv | Hatalar: output/errors.csv")


if __name__ == "__main__":
    main()
