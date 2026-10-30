import os
import httpx

async def send_telegram(signal, symbol):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    message = (
        f"{symbol} | {signal['direction']}\n"
        f"Giriş referansı: {signal['entry']}\n"
        f"Stop Loss: {signal['stop_loss']}\n"
        f"Take Profit: {signal['take_profit']}\n"
        f"RSI M1: {signal['rsi_m1']} | M15 trend: {signal['trend_m15']}\n"
        f"Destek: {signal['support_m5']} | Direnç: {signal['resistance_m5']}\n"
        f"{signal['reason']}\n"
        "Yalnızca DEMO sinyalidir; otomatik emir gönderilmez."
    )
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message}
        )
        response.raise_for_status()
        return bool(response.json().get("ok"))
