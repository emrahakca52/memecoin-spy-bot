"""Public RugCheck report adapter. Unknown fields remain unknown; no missing data is zero-filled."""
from __future__ import annotations
import math
import httpx

BASE_URL = "https://api.rugcheck.xyz/v1"

def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None

def _authority(meta: dict, key: str):
    if key not in meta:
        return None
    value = meta.get(key)
    return bool(str(value).strip() and str(value).strip().lower() not in {"none","null","0","false"})

def _top10_pct(report: dict):
    holders = report.get("topHolders") or report.get("top_holders")
    if not isinstance(holders, list) or not holders:
        return None
    known = report.get("knownAccounts") or report.get("known_accounts") or {}
    lockers = report.get("lockers") or report.get("lpLockers") or report.get("lp_lockers") or {}
    excluded = set()
    if isinstance(lockers, dict):
        for key, item in lockers.items():
            if isinstance(key, str): excluded.add(key)
            if isinstance(item, dict):
                for field in ("owner","tokenAccount","token_account"):
                    if isinstance(item.get(field), str): excluded.add(item[field])
    selected = []
    for item in holders:
        if not isinstance(item, dict): continue
        addresses = [item.get("address"), item.get("owner")]
        if any(address in excluded for address in addresses if isinstance(address,str)): continue
        is_special = False
        if isinstance(known,dict):
            for address in addresses:
                info = known.get(address) if isinstance(address,str) else None
                if isinstance(info,dict) and str(info.get("type","")).upper().replace("-","_") in {
                    "AMM","LOCKER","BURN","BURNED","BONDING_CURVE","POOL","LP","VAULT"
                }:
                    is_special = True
        if not is_special: selected.append(item)
    if not selected: return None
    values=[]
    for item in selected[:10]:
        pct=_number(item.get("pct",item.get("percentage")))
        if pct is None: return None
        values.append(pct)
    total=sum(values)
    return total*100 if total <= 1 else total

def _lp_locked_pct(report: dict):
    direct=_number(report.get("lpLockedPct",report.get("lp_locked_pct")))
    if direct is not None: return direct*100 if 0 <= direct <= 1 else direct
    values=[]
    markets=report.get("markets")
    if isinstance(markets,list):
        for market in markets:
            lp=market.get("lp") if isinstance(market,dict) else None
            if isinstance(lp,dict):
                value=_number(lp.get("lpLockedPct",lp.get("lockedPct",lp.get("locked_pct"))))
                if value is not None: values.append(value*100 if 0 <= value <= 1 else value)
    return min(values) if values else None

class RugCheckProvider:
    def __init__(self, timeout: float=12.0, client: httpx.AsyncClient|None=None):
        self.timeout,self._client=timeout,client

    async def inspect_mint(self, mint: str, market_context: dict|None=None) -> dict:
        owns=self._client is None
        client=self._client or httpx.AsyncClient(timeout=self.timeout,headers={"accept":"application/json"})
        result={"provider":"rugcheck","available":False,"score_normalised":None,
                "top10_holders_pct":None,"lp_locked_pct":None,
                "mint_authority_active":None,"freeze_authority_active":None,
                "insider_network_count":None,"dangerous_risks":[],"warnings":[],"error":None}
        try:
            response=await client.get(f"{BASE_URL}/tokens/{mint}/report")
            if response.status_code != 200:
                result["error"]=f"http_{response.status_code}"
                return result
            report=response.json()
            if not isinstance(report,dict):
                result["error"]="invalid_report_shape"
                return result
            meta=report.get("tokenMeta") or report.get("token_meta") or {}
            if not isinstance(meta,dict): meta={}
            result.update({
                "available":True,
                "score_normalised":_number(report.get("score_normalised",report.get("scoreNormalised"))),
                "top10_holders_pct":_top10_pct(report),
                "lp_locked_pct":_lp_locked_pct(report),
                "mint_authority_active":_authority(meta,"mintAuthority") if "mintAuthority" in meta else _authority(report,"mintAuthority"),
                "freeze_authority_active":_authority(meta,"freezeAuthority") if "freezeAuthority" in meta else _authority(report,"freezeAuthority"),
            })
            insider=report.get("insiderNetworks",report.get("insider_networks"))
            if isinstance(insider,list): result["insider_network_count"]=len(insider)
            else: result["insider_network_count"]=_number(insider)
            for risk in report.get("risks") or []:
                if not isinstance(risk,dict): continue
                level=str(risk.get("level","")).lower()
                name=str(risk.get("name","unknown_risk"))
                normalized=name.lower().replace("_"," ").replace("-"," ")
                market_managed=any(term in normalized for term in ("top 10 holder","top10 holder","holder concentration","lp lock","liquidity lock"))
                if level in {"danger","critical","high"} and not market_managed:
                    result["dangerous_risks"].append(name)
            if result["insider_network_count"] is None:
                result["warnings"].append("insider_network_data_unavailable")
            if result["lp_locked_pct"] is None:
                result["warnings"].append("lp_lock_data_unavailable")
            result["report_market_context"]=market_context or {}
        except (httpx.HTTPError,ValueError,TypeError):
            result["error"]="provider_unavailable"
        finally:
            if owns: await client.aclose()
        return result
