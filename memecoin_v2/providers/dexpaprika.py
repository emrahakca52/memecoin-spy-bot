"""DexPaprika public new-pool discovery adapter for Solana.

Uses the documented pools/search endpoint ordered by pool creation time.
This is a discovery feed only; risk checks remain in the independent pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
import httpx

BASE = "https://api.dexpaprika.com"


def _number(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _created_ms(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


class DexPaprikaProvider:
    def __init__(self, timeout: float = 12.0, client: httpx.AsyncClient | None = None):
        self.timeout = timeout
        self._client = client

    async def discover_solana(self, limit: int = 30):
        from ..models import TokenMarket

        owns = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.timeout,
            headers={"accept": "application/json", "user-agent": "MemecoinSpyV2/2.0"},
        )
        try:
            response = await client.get(
                f"{BASE}/networks/solana/pools/search",
                params={"order_by": "created_at", "sort": "desc",
                        "limit": max(1, min(int(limit), 100)), "detailed": "true"},
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("results", []) if isinstance(payload, dict) else []
            results, seen = [], set()
            for pool in rows:
                if not isinstance(pool, dict):
                    continue
                tokens = pool.get("tokens") or []
                if not isinstance(tokens, list) or len(tokens) < 2:
                    continue
                # Solana pool search returns token addresses in the detailed token objects.
                # Prefer the first token unless the pool exposes a quote/base designation.
                base = next((t for t in tokens if isinstance(t, dict) and
                             str(t.get("role", "")).lower() == "base"), None)
                if base is None:
                    base = next((t for t in tokens if isinstance(t, dict)), {})
                address = str(base.get("address") or base.get("id") or "")
                if not address or address in seen:
                    continue
                seen.add(address)
                quote = next((t for t in tokens if isinstance(t, dict) and
                              (t is not base)), {})
                txns = pool.get("transactions_24h")
                results.append(TokenMarket(
                    chain="solana", address=address,
                    symbol=str(base.get("symbol") or "UNKNOWN"),
                    name=str(base.get("name") or "Unknown"),
                    price_usd=_number(base.get("price_usd") or pool.get("price_usd")),
                    liquidity_usd=_number(pool.get("liquidity_usd")),
                    volume_24h_usd=_number(pool.get("volume_usd_24h")),
                    txns_24h=int(txns) if txns is not None else None,
                    price_change_1h_pct=_number(pool.get("price_change_1h")),
                    price_change_24h_pct=_number(pool.get("price_change_24h")),
                    pair_created_at_ms=_created_ms(pool.get("created_at")),
                    pair_address=str(pool.get("id") or "") or None,
                    source="dexpaprika",
                    raw={"dex_name": pool.get("dex_name"), "quote_token": quote,
                         "pool": pool},
                ))
                if len(results) >= max(1, min(int(limit), 50)):
                    break
            return results
        finally:
            if owns:
                await client.aclose()
