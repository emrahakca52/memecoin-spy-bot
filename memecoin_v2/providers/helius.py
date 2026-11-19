"""No-key Solana RPC transaction sample. Heuristic only; incomplete samples stay unknown."""
from __future__ import annotations
import httpx

RPC_URLS = ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")

class HeliusProvider:
    """Compatibility name retained; uses public JSON-RPC when no Helius key exists."""
    def __init__(self, timeout: float=12.0, client: httpx.AsyncClient|None=None):
        self.timeout, self._client = timeout, client
        self.api_key = ""

    async def inspect_pool(self, address: str|None) -> dict:
        result = {"provider":"solana_public_rpc_sample","available":False,
                  "sniper_cluster_detected":None,"transactions_examined":0,
                  "same_slot_swap_cluster_max":None,"notes":[]}
        if not address:
            result["notes"].append("Pool address unavailable")
            return result
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        async def rpc(url, method, params):
            response = await client.post(url, json={"jsonrpc":"2.0","id":1,"method":method,"params":params})
            response.raise_for_status()
            data = response.json()
            if data.get("error") or "result" not in data:
                raise ValueError("Invalid RPC response")
            return data["result"]
        try:
            signatures = None
            used_url = None
            for url in RPC_URLS:
                try:
                    signatures = await rpc(url, "getSignaturesForAddress",
                                           [address, {"limit":40,"commitment":"confirmed"}])
                    used_url = url
                    break
                except (httpx.HTTPError, ValueError):
                    continue
            if not isinstance(signatures, list) or not signatures:
                result["notes"].append("Public RPC returned no transaction sample; gate remains unknown")
                return result
            slots = {}
            examined = 0
            for entry in signatures[:40]:
                sig = entry.get("signature") if isinstance(entry,dict) else None
                if not sig:
                    continue
                try:
                    tx = await rpc(used_url, "getTransaction",
                                   [sig, {"encoding":"jsonParsed","maxSupportedTransactionVersion":0,"commitment":"confirmed"}])
                except (httpx.HTTPError, ValueError):
                    continue
                if not isinstance(tx,dict):
                    continue
                meta = tx.get("meta") or {}
                logs = " ".join(meta.get("logMessages") or []).lower()
                # Only classify transactions with explicit swap-like program logs.
                if "swap" not in logs:
                    continue
                message = ((tx.get("transaction") or {}).get("message") or {})
                keys = message.get("accountKeys") or []
                signer = None
                for key in keys:
                    if isinstance(key,dict) and key.get("signer"):
                        signer = key.get("pubkey")
                        break
                    if isinstance(key,str):
                        signer = key
                        break
                slot = tx.get("slot")
                if isinstance(slot,int) and signer:
                    slots.setdefault(slot,set()).add(signer)
                    examined += 1
            clusters = [len(x) for x in slots.values()]
            if not clusters:
                result["notes"].append("No classifiable swap sample; gate remains unknown")
                return result
            maximum = max(clusters)
            result.update({"available":True,"sniper_cluster_detected":maximum>=3,
                           "transactions_examined":examined,"same_slot_swap_cluster_max":maximum})
            result["notes"].append("Public RPC recent sample only; incomplete heuristic, not proof of bundle activity")
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            result["notes"].append("Public RPC transaction sample unavailable; gate remains unknown")
        finally:
            if owns:
                await client.aclose()
        return result
