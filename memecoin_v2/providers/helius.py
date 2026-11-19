"""Bounded public Solana RPC transaction sample. Heuristic only; incomplete samples stay unknown."""
from __future__ import annotations
import asyncio
import httpx

RPC_URLS = ("https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com")
SIGNATURE_LIMIT = 12
TX_CONCURRENCY = 3

class HeliusProvider:
    """Compatibility name retained; uses public JSON-RPC without requiring a paid key."""
    def __init__(self, timeout: float = 8.0, client: httpx.AsyncClient | None = None):
        self.timeout, self._client = timeout, client

    async def inspect_pool(self, address: str | None) -> dict:
        result = {"provider": "solana_public_rpc_sample", "available": False,
                  "sniper_cluster_detected": None, "transactions_examined": 0,
                  "same_slot_swap_cluster_max": None, "notes": []}
        if not address:
            result["notes"].append("Pool address unavailable")
            return result

        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)

        async def rpc(url: str, method: str, params: list):
            response = await client.post(
                url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("error") or "result" not in data:
                raise ValueError("Invalid RPC response")
            return data["result"]

        try:
            signatures = None
            used_url = None
            for url in RPC_URLS:
                try:
                    signatures = await rpc(
                        url, "getSignaturesForAddress",
                        [address, {"limit": SIGNATURE_LIMIT, "commitment": "confirmed"}]
                    )
                    used_url = url
                    break
                except (httpx.HTTPError, ValueError, TypeError):
                    continue

            if not isinstance(signatures, list) or not signatures:
                result["notes"].append("Public RPC returned no recent sample; gate remains unknown")
                return result

            semaphore = asyncio.Semaphore(TX_CONCURRENCY)

            async def fetch_tx(entry):
                signature = entry.get("signature") if isinstance(entry, dict) else None
                if not signature:
                    return None
                try:
                    async with semaphore:
                        return await rpc(
                            used_url, "getTransaction",
                            [signature, {"encoding": "jsonParsed",
                                         "maxSupportedTransactionVersion": 0,
                                         "commitment": "confirmed"}]
                        )
                except (httpx.HTTPError, ValueError, TypeError):
                    return None

            txs = await asyncio.gather(*(fetch_tx(item) for item in signatures[:SIGNATURE_LIMIT]))
            slots: dict[int, set[str]] = {}
            examined = 0
            for tx in txs:
                if not isinstance(tx, dict):
                    continue
                meta = tx.get("meta") or {}
                logs = " ".join(meta.get("logMessages") or []).lower()
                if "swap" not in logs:
                    continue
                message = ((tx.get("transaction") or {}).get("message") or {})
                keys = message.get("accountKeys") or []
                signer = None
                for key in keys:
                    if isinstance(key, dict) and key.get("signer"):
                        signer = key.get("pubkey")
                        break
                slot = tx.get("slot")
                if isinstance(slot, int) and signer:
                    slots.setdefault(slot, set()).add(signer)
                    examined += 1

            clusters = [len(wallets) for wallets in slots.values()]
            if not clusters:
                result["notes"].append("No classifiable swap in the bounded sample; gate remains unknown")
                return result

            maximum = max(clusters)
            result.update({"available": True, "sniper_cluster_detected": maximum >= 3,
                           "transactions_examined": examined,
                           "same_slot_swap_cluster_max": maximum})
            result["notes"].append(
                f"Public RPC sample: at most {SIGNATURE_LIMIT} recent signatures; heuristic only, not proof of bundle activity"
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            result["notes"].append("Public RPC transaction sample unavailable; gate remains unknown")
        finally:
            if owns:
                await client.aclose()
        return result
