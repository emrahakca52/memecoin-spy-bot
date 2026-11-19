"""Helius Enhanced Transactions adapter for conservative same-slot swap clustering."""
from __future__ import annotations
import os
import httpx

class HeliusProvider:
    def __init__(self, timeout: float=12.0, client: httpx.AsyncClient|None=None):
        self.timeout,self._client=timeout,client
        self.api_key=os.getenv("HELIUS_API_KEY","").strip()

    async def inspect_pool(self, address: str|None) -> dict:
        result={"provider":"helius","available":False,"sniper_cluster_detected":None,
                "transactions_examined":0,"same_slot_swap_cluster_max":None,"notes":[]}
        if not self.api_key:
            result["notes"].append("HELIUS_API_KEY is not configured")
            return result
        if not address:
            result["notes"].append("Pool/pair address is unavailable")
            return result
        owns=self._client is None
        client=self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            url=f"https://api.helius.xyz/v0/addresses/{address}/transactions"
            response=await client.get(url,params={"api-key":self.api_key,"limit":100})
            response.raise_for_status()
            payload=response.json()
            if not isinstance(payload,list):
                result["notes"].append("Unexpected Helius response shape")
                return result
            swaps=[tx for tx in payload if isinstance(tx,dict) and str(tx.get("type","")).upper()=="SWAP"]
            slot_signers={}
            for tx in swaps:
                slot=tx.get("slot")
                signer=tx.get("feePayer")
                if isinstance(slot,int) and isinstance(signer,str) and signer:
                    slot_signers.setdefault(slot,set()).add(signer)
            clusters=[len(signers) for signers in slot_signers.values()]
            if not clusters:
                result["notes"].append("No classifiable same-slot swap sample; gate remains unknown")
                return result
            maximum=max(clusters)
            result.update({"available":True,"sniper_cluster_detected":maximum>=3,
                           "transactions_examined":len(payload),"same_slot_swap_cluster_max":maximum})
            result["notes"].append("Heuristic only: three or more distinct swap fee-payers in one slot; not proof of coordinated sniping")
        except (httpx.HTTPError,ValueError,TypeError):
            result["notes"].append("Helius transaction lookup failed; gate remains unknown")
        finally:
            if owns: await client.aclose()
        return result
