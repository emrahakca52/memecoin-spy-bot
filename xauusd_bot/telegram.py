import os
import httpx

async def send_telegram(signal, symbol, source):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    text = (
        f"XAUUSD {signal['direction']}\n"
        f"Kaynak: {source}\n"
        f"Giriş referansı: {signal['entry']}\n"
        f"Stop Loss: {signal['stop_loss']}\n"
        f"Take Profit: {signal['take_profit']}\n"
        f"RSI M1: {signal['rsi_m1']} | M15 trend: {signal['trend_m15']}\n"
        f"{signal['reason']}\n"
        "Sadece sinyal; otomatik emir gönderilmez."
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json={"chat_id": chat_id, "text": text})
        response.raise_for_status()
        return bool(response.json().get("ok"))
