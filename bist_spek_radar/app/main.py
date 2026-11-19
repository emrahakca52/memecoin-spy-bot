from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .paper import status as paper_status, trade as paper_trade
from .scanner import configured_symbols, scan_symbols

app = FastAPI(title="BIST Spek Radar", version="0.1.0",
              description="BIST anomaly screening and paper portfolio only")

class PaperOrder(BaseModel):
    symbol: str = Field(min_length=2, max_length=12)
    side: str
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)

@app.get("/health")
def health():
    return {"status":"ok","project":"BIST Spek Radar","mode":"PAPER_ONLY"}

@app.get("/api/universe")
def universe():
    return {"symbols":configured_symbols(),"count":len(configured_symbols())}

@app.get("/api/scan")
def scan():
    return {"mode":"RESEARCH_ONLY","source":"Yahoo Finance; indicative/delayed, not exchange real-time",
            "results":scan_symbols()}

@app.get("/api/paper")
def paper():
    return paper_status()

@app.post("/api/paper/trade")
def paper_order(order: PaperOrder):
    try:
        return paper_trade(order.symbol, order.side, order.quantity, order.price)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse("""<!doctype html><html lang="tr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>BIST Spek Radar</title>
<style>body{font:16px system-ui;background:#101827;color:#e8eef8;margin:0;padding:24px}
main{max-width:1100px;margin:auto}h1{margin-bottom:4px}.muted{color:#9fb0c8}
button{background:#2563eb;color:white;border:0;border-radius:8px;padding:11px 16px;font-weight:700}
.card{background:#1b293d;border:1px solid #31435e;border-radius:12px;padding:16px;margin:14px 0}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #34445b}
.good{color:#7dd3a7}.warn{color:#fbbf24}</style><main>
<h1>BIST Spek Radar</h1><div class="muted">Anormal fiyat/hacim hareketi taraması · Sanal portföy</div>
<div class="card"><b class="warn">Veri uyarısı:</b> Yahoo Finance verisi gecikmeli veya eksik olabilir. Bu ekran manipülasyon kanıtı ya da yatırım tavsiyesi üretmez.</div>
<button onclick="scan()">Şimdi Tara</button> <button onclick="paper()">Sanal Portföyü Yenile</button>
<div id="out" class="card">Tarama bekleniyor.</div><div id="ledger" class="card">Sanal portföy yüklenmedi.</div>
<script>
async function scan(){let o=document.getElementById('out');o.textContent='Veri alınıyor…';
try{let d=await(await fetch('/api/scan')).json();let rows=d.results||[];
o.innerHTML='<h2>Tarama sonuçları ('+rows.length+')</h2><table><tr><th>Hisse</th><th>Skor</th><th>Son kapanış</th><th>1 gün</th><th>5 gün</th><th>Hacim katı</th><th>Sinyal nedenleri</th></tr>'+
rows.map(x=>'<tr><td>'+x.symbol+'</td><td>'+x.score+'</td><td>'+x.last_close+'</td><td>'+x.change_1d_pct+'%</td><td>'+x.change_5d_pct+'%</td><td>'+x.volume_ratio+'x</td><td>'+x.reasons.join(' · ')+'</td></tr>').join('')+'</table><p class="muted">'+d.source+'</p>'}
catch(e){o.textContent='Tarama hatası: '+e}}
async function paper(){let d=await(await fetch('/api/paper')).json();document.getElementById('ledger').innerHTML='<h2>Sanal Portföy</h2><p>Mod: '+d.mode+' · Nakit: '+d.cash.toLocaleString('tr-TR')+' TL</p><p>Açık pozisyon: '+d.positions.length+' · Son işlem: '+d.recent_trades.length+'</p>'}
paper();
</script></main></html>""")
