from __future__ import annotations

import os
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .paper import status as paper_status, trade as paper_trade
from .scanner import configured_symbols, scan_symbols, universe_status
from .historical import analyze_history, backtest_universe

app = FastAPI(title="BIST Spek Radar", version="0.2.0",
              description="BIST anomaly screening and paper portfolio only")
BACKTEST_JOBS: dict[str, dict[str, Any]] = {}
ACTIVE_BACKTEST_JOB: str | None = None

class PaperOrder(BaseModel):
    symbol: str = Field(min_length=2, max_length=12)
    side: str
    quantity: float = Field(gt=0)
    price: float = Field(gt=0)

def _run_backtest_job(job_id: str, period: str, limit: int, symbols: list[str]) -> None:
    global ACTIVE_BACKTEST_JOB
    BACKTEST_JOBS[job_id]["status"] = "running"
    BACKTEST_JOBS[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()
    try:
        result = backtest_universe(symbols, period=period, limit=limit)
        result["universe"] = universe_status()
        BACKTEST_JOBS[job_id].update({"status": "completed", "result": result})
    except Exception as exc:
        BACKTEST_JOBS[job_id].update({"status": "failed", "error": type(exc).__name__})
    BACKTEST_JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    if ACTIVE_BACKTEST_JOB == job_id:
        ACTIVE_BACKTEST_JOB = None

@app.get("/health")
def health():
    return {"status":"ok","project":"BIST Spek Radar","mode":"PAPER_ONLY"}

@app.get("/api/universe")
def universe():
    symbols = configured_symbols()
    return {"symbols":symbols,"count":len(symbols),"universe":universe_status()}

@app.get("/api/scan")
def scan():
    return {"mode":"RESEARCH_ONLY","source":"Yahoo Finance; indicative/delayed, not exchange real-time",
            "results":scan_symbols()}

@app.get("/api/history/{symbol}")
def history(symbol: str):
    if not symbol.replace(".", "").isalnum() or len(symbol) > 12:
        raise HTTPException(status_code=400, detail="Geçersiz hisse kodu")
    return analyze_history(symbol)

@app.get("/api/backtest/start")
def start_backtest(background_tasks: BackgroundTasks, period: str = "2y", limit: int = 100):
    global ACTIVE_BACKTEST_JOB
    if period not in {"1y", "2y", "5y", "max"}:
        raise HTTPException(status_code=400, detail="period 1y, 2y, 5y veya max olabilir")
    if ACTIVE_BACKTEST_JOB and BACKTEST_JOBS.get(ACTIVE_BACKTEST_JOB, {}).get("status") in {"queued", "running"}:
        active = BACKTEST_JOBS[ACTIVE_BACKTEST_JOB]
        return {"message": "Tarihsel tarama zaten çalışıyor.", **active,
                "status_url": f"/api/backtest/status/{ACTIVE_BACKTEST_JOB}"}
    symbols = configured_symbols()
    job_id = uuid4().hex[:12]
    ACTIVE_BACKTEST_JOB = job_id
    BACKTEST_JOBS[job_id] = {
        "job_id": job_id, "status": "queued", "period": period,
        "symbols_requested": len(symbols), "created_at": datetime.now(timezone.utc).isoformat()
    }
    background_tasks.add_task(_run_backtest_job, job_id, period, max(1, min(limit, 100)), symbols)
    return {"message": "Tarihsel tarama arka planda başlatıldı.", "job_id": job_id,
            "status": "queued", "symbols_requested": len(symbols),
            "status_url": f"/api/backtest/status/{job_id}"}

@app.get("/api/backtest/current")
def current_backtest():
    if not ACTIVE_BACKTEST_JOB:
        return {"status": "idle"}
    return BACKTEST_JOBS.get(ACTIVE_BACKTEST_JOB, {"status": "idle"})

@app.get("/api/backtest/status/{job_id}")
def backtest_status(job_id: str):
    job = BACKTEST_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Tarama kimliği bulunamadı veya servis yeniden başladı.")
    return job

@app.get("/api/backtest")
def backtest(period: str = "2y", limit: int = 40):
    if period not in {"1y", "2y", "5y", "max"}:
        raise HTTPException(status_code=400, detail="period 1y, 2y, 5y veya max olabilir")
    symbols = configured_symbols()
    result = backtest_universe(symbols, period=period, limit=max(1, min(limit, 100)))
    result["universe"] = universe_status()
    return result

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
<button onclick="scan()">Şimdi Tara</button> <button id="backtestBtn" onclick="startBacktest()">517 Hisse Tarihsel Araştırma</button> <button onclick="paper()">Sanal Portföyü Yenile</button>
<div id="out" class="card">Tarama bekleniyor.</div><div id="ledger" class="card">Sanal portföy yüklenmedi.</div>
<script>
async function scan(){let o=document.getElementById('out');o.textContent='Veri alınıyor…';
try{let d=await(await fetch('/api/scan')).json();let rows=d.results||[];
o.innerHTML='<h2>Tarama sonuçları ('+rows.length+')</h2><table><tr><th>Hisse</th><th>Skor</th><th>Son kapanış</th><th>1 gün</th><th>5 gün</th><th>Hacim katı</th><th>Sinyal nedenleri</th></tr>'+
rows.map(x=>'<tr><td>'+x.symbol+'</td><td>'+x.score+'</td><td>'+x.last_close+'</td><td>'+x.change_1d_pct+'%</td><td>'+x.change_5d_pct+'%</td><td>'+x.volume_ratio+'x</td><td>'+x.reasons.join(' · ')+'</td></tr>').join('')+'</table><p class="muted">'+d.source+'</p>'}
catch(e){o.textContent='Tarama hatası: '+e}}
async function startBacktest(){let o=document.getElementById('out');let b=document.getElementById('backtestBtn');b.disabled=true;b.textContent='Tarama başlatılıyor…';o.textContent='517 sembol için tarihsel tarama başlatılıyor…';
try{let j=await(await fetch('/api/backtest/start?period=2y&limit=100')).json();o.textContent='Tarama kimliği: '+j.job_id+' · '+j.symbols_requested+' sembol. Sonuç bekleniyor…';pollBacktest(j.job_id)}
catch(e){o.textContent='Başlatma hatası: '+e;b.disabled=false;b.textContent='Yeniden Dene'}}
async function pollBacktest(id){let o=document.getElementById('out');
try{let j=await(await fetch('/api/backtest/status/'+id)).json();
if(j.status==='completed'){let b=document.getElementById('backtestBtn');b.disabled=false;b.textContent='517 Hisse Tarihsel Araştırma';let d=j.result;let rows=d.results||[];o.innerHTML='<h2>Tarihsel sonuçlar · '+d.symbols_with_data+'/'+d.symbols_requested+' hissede veri · '+d.candidate_count+' aday</h2><p>Veri hatası: '+d.data_failures+'</p><table><tr><th>Hisse</th><th>Tarih</th><th>Skor</th><th>Hacim</th><th>5g değişim</th><th>Sonraki 20g max</th><th>Sınıf</th></tr>'+rows.map(x=>'<tr><td>'+x.symbol+'</td><td>'+x.date+'</td><td>'+x.score+'</td><td>'+x.volume_ratio+'x</td><td>'+x.change_5d_pct+'%</td><td>'+x.forward_max_20d_pct+'%</td><td>'+x.outcome_20d+'</td></tr>').join('')+'</table><p class="muted">'+d.note+'</p>';return}
if(j.status==='failed'){let b=document.getElementById('backtestBtn');b.disabled=false;b.textContent='Yeniden Dene';o.textContent='Tarama başarısız: '+j.error;return}
o.textContent='Tarihsel tarama: '+j.status+' · '+j.symbols_requested+' sembol. Sayfa açık kalabilir.';setTimeout(()=>pollBacktest(id),5000)}
catch(e){let b=document.getElementById('backtestBtn');b.disabled=false;b.textContent='Durumu Yenile';o.textContent='Durum alınamadı: '+e}}
async function paper(){let d=await(await fetch('/api/paper')).json();document.getElementById('ledger').innerHTML='<h2>Sanal Portföy</h2><p>Mod: '+d.mode+' · Nakit: '+d.cash.toLocaleString('tr-TR')+' TL</p><p>Açık pozisyon: '+d.positions.length+' · Son işlem: '+d.recent_trades.length+'</p>'}
paper();
</script></main></html>""")
