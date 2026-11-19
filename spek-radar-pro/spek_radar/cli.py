import argparse
from pathlib import Path

import pandas as pd

from .data import fetch_ohlcv, load_symbols_file
from .indicators import add_indicators, score_latest
from .universe import fetch_kap_universe


def main():
    parser = argparse.ArgumentParser(description="BIST olağan dışı hareket erken uyarı tarayıcısı")
    parser.add_argument("--symbols", default="DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS")
    parser.add_argument("--symbols-file")
    parser.add_argument("--all-bist", action="store_true",
                         help="KAP'tan Yıldız/Ana/Alt/Yakın İzleme pay evrenini al")
    parser.add_argument("--period", default="2y")
    parser.add_argument("--benchmark", default="XU100.IS")
    parser.add_argument("--top", type=int, default=20,
                        help="Ayrı top-N aday raporu üret (varsayılan: 20)")
    args = parser.parse_args()

    if args.top < 1:
        parser.error("--top en az 1 olmalıdır")

    output = Path("output")
    output.mkdir(exist_ok=True)

    if args.all_bist:
        universe = fetch_kap_universe()
        universe.to_csv(output / "universe_snapshot.csv", index=False)
        symbols = universe["symbol"].tolist()
        market_by_symbol = dict(zip(universe["symbol"], universe["market_segment"]))
        print(f"KAP evreni: {len(symbols)} pay")
    elif args.symbols_file:
        symbols = load_symbols_file(args.symbols_file)
        market_by_symbol = {}
    else:
        symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
        market_by_symbol = {}

    benchmark = None
    if args.benchmark:
        try:
            benchmark = fetch_ohlcv(args.benchmark, args.period).close
        except Exception as exc:
            print(f"UYARI benchmark: {exc}")

    rows, errors = [], []
    total = len(symbols)
    for index, symbol in enumerate(symbols, 1):
        try:
            data = fetch_ohlcv(symbol, args.period)
            features = add_indicators(data, benchmark)
            row = {
                "symbol": symbol,
                "market_segment": market_by_symbol.get(symbol, "BILINMIYOR"),
                "date": str(features.index[-1].date()),
                "close": round(float(features.close.iloc[-1]), 4),
            }
            row.update(score_latest(features))
            rows.append(row)
            turnover = row.get("avg_turnover20_tl")
            turnover_text = f"{turnover:,.0f} TL" if turnover is not None else "ciro bilinmiyor"
            print(
                f"[{index}/{total}] {symbol:12} {row['score']:3}/100  "
                f"{row['signal']} | {row['liquidity']} | {turnover_text}"
            )
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            print(f"[{index}/{total}] {symbol}: VERİ HATASI: {exc}")

    if rows:
        results = pd.DataFrame(rows).sort_values(
            ["score", "avg_turnover20_tl"], ascending=[False, False],
            na_position="last",
        )
        results.to_csv(output / "scan.csv", index=False)
        results.head(args.top).to_csv(output / f"top_{args.top}.csv", index=False)
        print(f"İlk {args.top} aday: output/top_{args.top}.csv")
    else:
        print("Hiçbir sembolde geçerli fiyat verisi alınamadı; aday listesi oluşturulmadı.")

    if errors:
        pd.DataFrame(errors).to_csv(output / "errors.csv", index=False)

    print(f"Tamamlandı: {len(rows)} başarılı, {len(errors)} veri hatası / {total} sembol.")
    print("Raporlar: output/scan.csv, output/top_20.csv, output/errors.csv")


if __name__ == "__main__":
    main()
