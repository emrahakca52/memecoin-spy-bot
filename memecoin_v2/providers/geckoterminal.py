"""GeckoTerminal public API fallback for newly created Solana pools.

The public API is rate-limited and beta; this adapter is a discovery source only.
All candidate safety decisions remain in the existing independent risk pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone
import httpx

BASE = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"
ACCEPT = "application/json;version=20230302"


def _number(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _included_tokens(payload: dict) -> dict:
    result = {}
    for item in payload.get("included") or []:
        if not isinstance(item, dict) or item.get("type") != "token":
            continue
        attrs = item.get("attributes") or {}
        address = attrs.get("address")
        if address:
            result[str(address)] = attrs
    return result


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


class GeckoTerminalProvider:
    """Fetch latest Solana pools and normalize them to TokenMarket records."""

    def __init__(self, timeout: float = 12.0, client: httpx.AsyncClient | None = None):
        self.timeout = timeout
        self._client = client

    async def discover_solana(self, limit: int = 30):
        from ..models import TokenMarket

        owns = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self.timeout,
            headers={"accept": ACCEPT, "user-agent": "MemecoinSpyV2/2.0"},
        )
        try:
            response = await client.get(
                f"{BASE}/networks/{NETWORK}/new_pools",
                params={"include": "base_token,quote_token,dex", "page": 1},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                return []
            tokens = _included_tokens(payload)
            results = []
            seen = set()
            for pool in payload["data"]:
                if not isinstance(pool, dict):
                    continue
                attrs = pool.get("attributes") or {}
                rel = pool.get("relationships") or {}
                base_data = ((rel.get("base_token") or {}).get("data") or {})
                quote_data = ((rel.get("quote_token") or {}).get("data") or {})
                base_id = str(base_data.get("id") or "")
                quote_id = str(quote_data.get("id") or "")
                base_addr = base_id.split("_", 1)[1] if "_" in base_id else ""
                quote_addr = quote_id.split("_", 1)[1] if "_" in quote_id else ""
                token_attrs = tokens.get(base_addr, {})
                # The new pool feed is ordered by creation time. Use the base token
                # as the candidate and never infer token identity from the pool name.
                address = base_addr or str(token_attrs.get("address") or "")
                if not address or address in seen:
                    continue
                seen.add(address)
                txns = attrs.get("transactions") or {}
                h24 = txns.get("h24") or {}
                volume = attrs.get("volume_usd") or {}
                changes = attrs.get("price_change_percentage") or {}
                name = str(token_attrs.get("name") or attrs.get("name") or "Unknown")
                symbol = str(token_attrs.get("symbol") or "UNKNOWN")
                results.append(TokenMarket(
                    chain="solana",
                    address=address,
                    symbol=symbol,
                    name=name,
                    price_usd=_number(attrs.get("base_token_price_usd")),
                    liquidity_usd=_number(attrs.get("reserve_in_usd")),
                    volume_24h_usd=_number(volume.get("h24")),
                    txns_24h=(int(h24.get("buys", 0) or 0) + int(h24.get("sells", 0) or 0)),
                    price_change_1h_pct=_number(changes.get("h1")),
                    price_change_24h_pct=_number(changes.get("h24")),
                    pair_created_at_ms=_created_ms(attrs.get("pool_created_at")),
                    pair_address=str(pool.get("id") or "").split("_", 1)[-1] or None,
                    source="geckoterminal",
                    raw={"pool_id": pool.get("id"), "quote_token": quote_addr,
                         "pool_attributes": attrs},
                ))
                if len(results) >= max(1, min(int(limit), 50)):
                    break
            return results
        finally:
            if owns:
                await client.aclose()
