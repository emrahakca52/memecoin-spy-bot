import argparse
from pathlib import Path
import pandas as pd
from .data import fetch_ohlcv, load_symbols_file
from .indicators import add_indicators, score_latest

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--symbols", default="DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS")
    p.add_argument("--symbols-file")
    p.add_argument("--period", default="2y")
    p.add_argument("--benchmark", default="XU100.IS")
    a=p.parse_args()
    symbols=load_symbols_file(a.symbols_file) if a.symbols_file else [s.strip() for s in a.symbols.split(",") if s.strip()]
    benchmark=None
    if a.benchmark:
        try: benchmark=fetch_ohlcv(a.benchmark,a.period).close
        except Exception as e: print(f"UYARI benchmark: {e}")
    rows=[]; errors=[]
    for s in symbols:
        try:
            d=fetch_ohlcv(s,a.period); f=add_indicators(d,benchmark)
            row={"symbol":s,"date":str(f.index[-1].date()),"close":round(float(f.close.iloc[-1]),4)}
            row.update(score_latest(f)); rows.append(row)
            print(f"{s:12} {row['score']:3}/100  {row['signal']}")
        except Exception as e:
            errors.append({"symbol":s,"error":str(e)}); print(f"{s}: VERİ HATASI: {e}")
    Path("output").mkdir(exist_ok=True)
    if rows: pd.DataFrame(rows).sort_values("score",ascending=False).to_csv("output/scan.csv",index=False)
    if errors: pd.DataFrame(errors).to_csv("output/errors.csv",index=False)
    print("Rapor: output/scan.csv")

if __name__=="__main__": main()
