"""Best-effort public Solana RPC diagnostics; partial holder data never counts as complete."""
from __future__ import annotations
import base64
import httpx

RPC_URL = "https://api.mainnet-beta.solana.com"

class SolanaRpcProvider:
    def __init__(self, timeout: float = 10.0, client: httpx.AsyncClient | None = None):
        self.timeout, self._client = timeout, client

    async def inspect_mint(self, mint: str) -> dict:
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        result = {"provider":"solana_public_rpc","authority_check":False,
                  "mint_authority_present":None,"freeze_authority_present":None,
                  "largest_token_accounts_pct":None,"top10_token_accounts_pct":None,
                  "top10_wallets_observed_pct":None,"holder_sample_accounts":0,
                  "holder_sample_complete":False,"notes":[]}
        async def rpc(method, params):
            response = await client.post(RPC_URL, json={"jsonrpc":"2.0","id":1,"method":method,"params":params})
            response.raise_for_status()
            payload = response.json()
            if payload.get("error") or "result" not in payload:
                raise ValueError("Invalid RPC result")
            return payload["result"]
        try:
            supply = await rpc("getTokenSupply",[mint,{"commitment":"confirmed"}])
            largest = await rpc("getTokenLargestAccounts",[mint,{"commitment":"confirmed"}])
            mint_info = await rpc("getAccountInfo",[mint,{"encoding":"jsonParsed","commitment":"confirmed"}])
            supply_value = int(supply["value"]["amount"])
            accounts = largest.get("value") or []
            data = (mint_info.get("value") or {}).get("data") or {}
            parsed = data.get("parsed",{}).get("info",{}) if isinstance(data,dict) else {}
            if parsed:
                result["authority_check"] = True
                result["mint_authority_present"] = parsed.get("mintAuthority") is not None
                result["freeze_authority_present"] = parsed.get("freezeAuthority") is not None
            else:
                result["notes"].append("Mint authority format not verified")
            if supply_value > 0 and accounts:
                amounts = [int(a.get("amount","0")) for a in accounts]
                result["largest_token_accounts_pct"] = round(max(amounts,default=0)/supply_value*100,4)
                result["top10_token_accounts_pct"] = round(sum(sorted(amounts,reverse=True)[:10])/supply_value*100,4)
                addresses = [a["address"] for a in accounts if a.get("address")]
                multi = await rpc("getMultipleAccounts",[addresses,{"encoding":"base64","commitment":"confirmed"}])
                owner_amounts = {}
                for token_account, account in zip(accounts,multi.get("value") or []):
                    try:
                        raw = base64.b64decode(account["data"][0])
                        if len(raw) < 72: continue
                        owner = base64.b58encode(raw[32:64]).decode() if hasattr(base64,"b58encode") else raw[32:64].hex()
                        owner_amounts[owner] = owner_amounts.get(owner,0) + int(token_account.get("amount","0"))
                    except (TypeError,ValueError,KeyError,IndexError):
                        continue
                if owner_amounts:
                    result["top10_wallets_observed_pct"] = round(sum(sorted(owner_amounts.values(),reverse=True)[:10])/supply_value*100,4)
                result["holder_sample_accounts"] = len(owner_amounts)
                result["notes"].append("Top-20 token-account sample only; not a complete wallet index")
            else:
                result["notes"].append("Supply or largest-account data unavailable")
        except (httpx.HTTPError,ValueError,KeyError,TypeError,OverflowError):
            result["notes"].append("Public RPC inspection unavailable or rate-limited")
        finally:
            if owns: await client.aclose()
        return result
