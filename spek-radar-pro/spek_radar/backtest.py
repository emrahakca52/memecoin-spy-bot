import argparse
from pathlib import Path
import pandas as pd
from .data import fetch_ohlcv
from .indicators import add_indicators, score_latest

def run_symbol(symbol, period, benchmark):
    d=fetch_ohlcv(symbol,period); f=add_indicators(d,benchmark); rows=[]
    # Signal at close; simulated entry is next session open (no same-bar look-ahead).
    for i in range(80,len(f)-11):
        s=score_latest(f.iloc[:i+1])
        if s["score"] < 65: continue
        entry=float(f.open.iloc[i+1])
        row={"symbol":symbol,"signal_date":str(f.index[i].date()),"entry_date":str(f.index[i+1].date()),"score":s["score"]}
        for h in (1,3,5,10):
            row[f"return_{h}d_pct"]=(float(f.close.iloc[i+1+h])/entry-1)*100
        rows.append(row)
    return rows

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--symbols",default="DSTKF.IS,KTLEV.IS,TERA.IS,PEKGY.IS")
    p.add_argument("--period",default="5y")
    p.add_argument("--benchmark",default="XU100.IS")
    a=p.parse_args(); b=None
    if a.benchmark:
        try: b=fetch_ohlcv(a.benchmark,a.period).close
        except Exception as e: print(f"Benchmark yok: {e}")
    rows=[]
    for s in [x.strip() for x in a.symbols.split(",") if x.strip()]:
        try: rows.extend(run_symbol(s,a.period,b)); print(s,"test tamam")
        except Exception as e: print(s,"test hatası:",e)
    Path("output").mkdir(exist_ok=True)
    if not rows: print("Test sonucu yok; veri ve sembolleri kontrol edin."); return
    df=pd.DataFrame(rows); df.to_csv("output/backtest.csv",index=False)
    print("Sinyal sayısı:",len(df))
    for c in ["return_1d_pct","return_3d_pct","return_5d_pct","return_10d_pct"]:
        print(c, "ortalama",round(df[c].mean(),2),"% | pozitif",round((df[c]>0).mean()*100,1),"%")
    print("Rapor: output/backtest.csv")

if __name__=="__main__": main()
