"""Standalone V2 read-only discovery API; not the production V1 entry point."""
from datetime import datetime,timezone
import asyncio
import os
import time
from fastapi import FastAPI,HTTPException,Query,Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel,Field
import hmac
from memecoin_v2.scanner import Scanner,candidate_to_dict
from memecoin_v2.settings import Settings
from wallet_tracker import get_wallet_token_activity
from telegram_notify import send_scan_report
from memecoin_v2 import paper_ledger

app=FastAPI(title="Memecoin Spy Bot V2",version="2.0.0",
            description="Solana discovery, fail-closed security screening and authenticated paper ledger. No live execution.")
scanner=Scanner(Settings.from_env())
_telegram_lock=asyncio.Lock()
_last_telegram_report=0.0

@app.get("/health")
async def health():
    return {"ok":True,"service":"memecoin-spy-bot-v2","mode":"paper",
            "real_trading_enabled":False,"utc":datetime.now(timezone.utc).isoformat(),
            "integrations":{"dexscreener":True,"solana_public_rpc":True,
                            "rugcheck":True,"ai_screening":bool(os.getenv("GEMINI_API_KEY") if os.getenv("AI_PROVIDER","grok").lower()=="gemini" else os.getenv("XAI_API_KEY")),"helius_transaction_indexing":bool(os.getenv("HELIUS_API_KEY")),"paper_ledger":bool(os.getenv("DATABASE_URL")),
                            "live_execution":False}}

@app.get("/v2/scan")
async def scan(limit:int=Query(default=20,ge=1,le=50)):
    global _last_telegram_report
    try:
        candidates=await scanner.scan(limit=limit)
        eligible=sum(1 for c in candidates if c.risk.eligible)
        report={"ok":True,"mode":"paper","real_trading_enabled":False,
                "count":len(candidates),"eligible_count":eligible,
                "candidates":[candidate_to_dict(c) for c in candidates]}
        if os.getenv("V2_TELEGRAM_REPORTS_ENABLED","false").strip().lower() in {"1","true","yes","on"}:
            async with _telegram_lock:
                now=time.monotonic()
                if now-_last_telegram_report>=900:
                    skipped=[{"symbol":c.market.symbol,"reason":",".join(c.risk.missing or c.risk.reasons)}
                             for c in candidates if not c.risk.eligible][:10]
                    await send_scan_report(checked=len(candidates),candidates=len(candidates),
                                           approved=eligible,opened=0,skipped=skipped)
                    _last_telegram_report=now
        return report
    except Exception as exc:
        raise HTTPException(status_code=502,detail="Market or security data provider unavailable") from exc

@app.get("/v2/wallet/{wallet_address}")
async def wallet_activity(wallet_address:str,limit:int=Query(default=10,ge=1,le=20)):
    try:
        result=await get_wallet_token_activity(wallet_address,limit=limit)
        result["service"]="memecoin-spy-bot-v2"
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502,detail="Solana wallet activity provider unavailable") from exc


class PaperBuyRequest(BaseModel):
    mint: str = Field(min_length=32, max_length=64)
    symbol: str = Field(min_length=1, max_length=20)
    price_usd: float = Field(gt=0, le=1_000_000_000)
    amount_usd: float = Field(gt=0, le=100_000)

class PaperSellRequest(BaseModel):
    mint: str = Field(min_length=32, max_length=64)
    price_usd: float = Field(gt=0, le=1_000_000_000)
    quantity: float = Field(gt=0, le=1e18)

def _require_paper_key(provided: str | None):
    expected=os.getenv("V2_PAPER_API_KEY","").strip()
    if not expected:
        raise HTTPException(status_code=503,detail="V2 paper write API is disabled: configure V2_PAPER_API_KEY")
    if not provided or not hmac.compare_digest(provided,expected):
        raise HTTPException(status_code=401,detail="Invalid paper API key")

@app.get("/v2/paper/status")
async def paper_status():
    try:
        paper_ledger.initialize()
        return paper_ledger.status()
    except Exception as exc:
        raise HTTPException(status_code=503,detail="V2 paper ledger unavailable; check DATABASE_URL") from exc

@app.post("/v2/paper/buy")
async def paper_buy(payload:PaperBuyRequest,x_paper_api_key:str|None=Header(default=None)):
    _require_paper_key(x_paper_api_key)
    try:
        paper_ledger.initialize()
        return paper_ledger.buy(payload.mint,payload.symbol,payload.price_usd,payload.amount_usd,
                                Settings.from_env().max_daily_loss_usd)
    except paper_ledger.LedgerError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503,detail="V2 paper ledger unavailable") from exc

@app.post("/v2/paper/sell")
async def paper_sell(payload:PaperSellRequest,x_paper_api_key:str|None=Header(default=None)):
    _require_paper_key(x_paper_api_key)
    try:
        paper_ledger.initialize()
        return paper_ledger.sell(payload.mint,payload.price_usd,payload.quantity)
    except paper_ledger.LedgerError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503,detail="V2 paper ledger unavailable") from exc


DASHBOARD_HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memecoin Spy V2</title><style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141c30;--muted:#91a0bb;--line:#26334d;--green:#37d6a0;--red:#ff7185;--amber:#f6c76b}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#0b1020,#101a2d);color:#edf3ff;font:15px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:auto;padding:26px 18px 60px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}
h1{margin:0;font-size:clamp(25px,5vw,38px);letter-spacing:-1px}.sub,.muted{color:var(--muted)}.pill{border:1px solid var(--line);border-radius:99px;padding:7px 12px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:24px 0}.card{background:#141c30;border:1px solid var(--line);border-radius:16px;padding:17px;min-width:0}.metric{font-size:24px;font-weight:750;margin-top:7px}.label{font-size:12px;color:var(--muted)}
.actions{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0}button{background:#2bdfaa;color:#061b16;border:0;border-radius:10px;padding:11px 15px;font-weight:750;cursor:pointer}button.secondary{background:#263650;color:#edf3ff}button:disabled{opacity:.5}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:12px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.good{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--amber)}.section{margin-top:18px}.scroll{overflow:auto}.small{font-size:12px;color:var(--muted)}
input{background:#0c1425;border:1px solid var(--line);border-radius:9px;padding:11px;color:#fff;min-width:0;flex:1}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:18px 12px}.card{padding:13px}table{min-width:700px}}
</style></head><body><main>
<div class="top"><div><h1>MEMECOIN SPY <span style="color:#37d6a0">V2</span></h1><div class="sub">Solana tarama · Güvenlik filtreleri · Sanal portföy</div></div><div class="pill">● PAPER MODE — GERÇEK İŞLEM KAPALI</div></div>
<div class="grid">
<div class="card"><div class="label">Servis</div><div class="metric good" id="service">Kontrol ediliyor</div></div>
<div class="card"><div class="label">Son tarama adedi</div><div class="metric" id="count">—</div></div>
<div class="card"><div class="label">Filtreyi geçen</div><div class="metric" id="eligible">—</div></div>
<div class="card"><div class="label">Sanal bakiye</div><div class="metric" id="cash">Bağlantı bekliyor</div></div>
</div>
<div class="actions"><button id="scanBtn" onclick="scan()">Şimdi tara</button><button class="secondary" onclick="refresh()">Durumu yenile</button></div>
<div class="card"><div class="top"><div><b>En son tarama sonuçları</b><div class="small">Risk kapılarından geçmeyen tokenlar uygun kabul edilmez.</div></div><span class="small" id="scanTime">Henüz tarama yok</span></div>
<div class="scroll"><table><thead><tr><th>Token</th><th>Fiyat</th><th>Likidite</th><th>Hacim 24s</th><th>Risk</th><th>Karar</th><th>Neden</th></tr></thead><tbody id="rows"><tr><td colspan="7" class="muted">Tarama başlatılmadı.</td></tr></tbody></table></div></div>
<div class="card section"><b>Servis ve API durumu</b><div id="integrations" class="small" style="margin-top:12px">Yükleniyor…</div></div>
<div class="card section"><b>Cüzdan hareketi sorgula</b><div class="small" style="margin:5px 0 12px">Solana cüzdan adresini gir. Bu ekran yalnızca okuma yapar.</div><div class="actions"><input id="wallet" placeholder="Solana cüzdan adresi"><button class="secondary" onclick="walletLookup()">Sorgula</button></div><pre id="walletResult" class="small" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre></div>
<div class="small section">V2 yalnızca sanal işlem modundadır. Yatırım tavsiyesi değildir. Veriler sağlayıcı erişimine bağlıdır.</div>
</main><script>
const el=id=>document.getElementById(id);const fmt=v=>v==null?'—':(Number(v).toLocaleString('en-US',{maximumFractionDigits:6}));
async function refresh(){try{const h=await fetch('/health').then(r=>r.json());el('service').textContent=h.ok?'ONLINE':'OFFLINE';el('service').className='metric '+(h.ok?'good':'bad');el('integrations').innerHTML=Object.entries(h.integrations||{}).map(([k,v])=>'<div style="padding:5px 0">'+k.replaceAll('_',' ')+' — <b class="'+(v?'good':'warn')+'">'+(v?'Hazır':'Anahtar/bağlantı bekliyor')+'</b></div>').join('');}catch(e){el('service').textContent='Bağlantı yok';el('service').className='metric bad'}
try{const p=await fetch('/v2/paper/status').then(r=>r.ok?r.json():null);el('cash').textContent=p?('$'+fmt(p.cash_usd)):'DB bekliyor';}catch(e){el('cash').textContent='DB bekliyor'}}
async function scan(){const b=el('scanBtn');b.disabled=true;b.textContent='Taranıyor…';try{const r=await fetch('/v2/scan?limit=20');const d=await r.json();if(!r.ok)throw Error(d.detail||'Tarama başarısız');el('count').textContent=d.count;el('eligible').textContent=d.eligible_count;el('scanTime').textContent=new Date().toLocaleString('tr-TR');el('rows').innerHTML='';for(const c of d.candidates||[]){const m=c||{},risk=c.risk||{};const tr=document.createElement('tr');const vals=[m.symbol||m.name||'—',fmt(m.price_usd),fmt(m.liquidity_usd),fmt(m.volume_24h_usd),m.market_score??risk.score??'—',risk.eligible?'UYGUN':'ELE',((risk.missing||[]).concat(risk.reasons||[]).join(', ')||'—')];for(let i=0;i<vals.length;i++){const td=document.createElement('td');td.textContent=String(vals[i]);if(i===5)td.className=risk.eligible?'good':'bad';tr.appendChild(td)}el('rows').appendChild(tr)}if(!d.candidates?.length)el('rows').innerHTML='<tr><td colspan="7">Sonuç bulunamadı.</td></tr>';}catch(e){el('rows').innerHTML='<tr><td colspan="7" class="bad">'+e.message+'</td></tr>'}finally{b.disabled=false;b.textContent='Şimdi tara';await refresh()}}
async function walletLookup(){const a=el('wallet').value.trim();if(!a)return;el('walletResult').textContent='Sorgulanıyor…';try{const r=await fetch('/v2/wallet/'+encodeURIComponent(a)+'?limit=10');const d=await r.json();el('walletResult').textContent=JSON.stringify(d,null,2)}catch(e){el('walletResult').textContent='Sorgu başarısız: '+e.message}}
refresh();
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML
