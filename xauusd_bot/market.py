import os
import httpx

BASE_URL = "https://api.twelvedata.com/time_series"

async def fetch_candles(symbol: str, interval: str):
    api_key = os.getenv("TWELVE_DATA_API_KEY")
    if not api_key:
        raise RuntimeError("TWELVE_DATA_API_KEY eksik. Render Environment bölümüne Twelve Data API anahtarını ekleyin.")
    params = {"symbol": symbol, "interval": interval, "outputsize": "120", "apikey": api_key, "format": "JSON"}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(BASE_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    if "values" not in payload:
        raise RuntimeError(f"Twelve Data veri hatası: {payload.get('message', payload)}")
    values = list(reversed(payload["values"]))
    candles = []
    for row in values:
        try:
            candles.append({k: float(row[k]) for k in ("open", "high", "low", "close")})
        except (KeyError, TypeError, ValueError):
            continue
    if len(candles) < 60:
        raise RuntimeError(f"{interval} için yeterli mum alınamadı: {len(candles)}")
    return "Twelve Data", candles
