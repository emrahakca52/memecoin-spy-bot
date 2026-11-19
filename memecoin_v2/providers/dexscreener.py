"""DexScreener public discovery adapter for Solana token profiles."""
from __future__ import annotations
import time
import asyncio
import httpx
from ..models import TokenMarket

BASE = "https://api.dexscreener.com"


def _number(value):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


class DexScreenerProvider:
    def __init__(self, timeout: float = 12.0, client: httpx.AsyncClient | None = None):
        self.timeout = timeout
        self._client = client

    async def discover_solana(self, limit: int = 30) -> list[TokenMarket]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout, headers={"accept": "application/json"})
        try:
            response = None
            # Respect public API throttling. Retry with bounded backoff and
            # Retry-After when supplied; if still rate-limited, return no
            # candidates rather than turning a provider throttle into HTTP 502.
            for attempt in range(3):
                response = await client.get(f"{BASE}/token-profiles/latest/v1")
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    break
                if attempt < 2:
                    retry_after = response.headers.get("retry-after")
                    try:
                        delay = min(max(float(retry_after), 0.5), 8.0) if retry_after else (1.5 ** (attempt + 1))
                    except ValueError:
                        delay = 1.5 ** (attempt + 1)
                    await asyncio.sleep(delay)
            if response is not None and response.status_code == 429:
                return []
            response.raise_for_status()
            profiles = response.json()
            if not isinstance(profiles, list): return []
            addresses = []
            for item in profiles:
                if isinstance(item, dict) and str(item.get("chainId", "")).lower() == "solana":
                    address = item.get("tokenAddress")
                    if isinstance(address, str) and address and address not in addresses:
                        addresses.append(address)
                if len(addresses) >= max(1, min(limit, 100)): break
            results: list[TokenMarket] = []
            for address in addresses:
                try:
                    pair_response = await client.get(f"{BASE}/token-pairs/v1/solana/{address}")
                    if pair_response.status_code in {408, 429, 500, 502, 503, 504}:
                        # Keep already collected candidates; stop before the
                        # provider rate limit turns a partial scan into a crash.
                        break
                    pair_response.raise_for_status()
                    pairs = pair_response.json()
                    if not isinstance(pairs, list) or not pairs: continue
                    pair = max(pairs, key=lambda p: _number((p.get("liquidity") or {}).get("usd")) or 0)
                    base = pair.get("baseToken") or {}
                    volume = pair.get("volume") or {}
                    txns = pair.get("txns") or {}
                    h24 = txns.get("h24") or {}
                    changes = pair.get("priceChange") or {}
                    created = pair.get("pairCreatedAt")
                    results.append(TokenMarket(
                        chain="solana", address=address,
                        symbol=str(base.get("symbol") or "UNKNOWN"), name=str(base.get("name") or "Unknown"),
                        price_usd=_number(pair.get("priceUsd")),
                        liquidity_usd=_number((pair.get("liquidity") or {}).get("usd")),
                        volume_24h_usd=_number(volume.get("h24")),
                        txns_24h=(int(h24.get("buys", 0) or 0) + int(h24.get("sells", 0) or 0)),
                        price_change_1h_pct=_number(changes.get("h1")),
                        price_change_24h_pct=_number(changes.get("h24")),
                        pair_created_at_ms=int(created) if created else None,
                        pair_address=pair.get("pairAddress"), source="dexscreener", raw=pair,
                    ))
                except (httpx.HTTPError, ValueError, TypeError):
                    continue
                await asyncio_sleep_briefly()
            return results
        finally:
            if owns_client: await client.aclose()


async def asyncio_sleep_briefly():
    # Respect public API limits while walking a batch of token profiles.
    import asyncio
    await asyncio.sleep(0.15)
