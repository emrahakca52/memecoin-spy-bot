"""Single-message Telegram dashboard for live paper-position prices."""
import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
import paper_engine

logger = logging.getLogger("memecoin_spy.telegram_positions")
REFRESH_SECONDS = max(15, int(os.getenv("TELEGRAM_POSITIONS_REFRESH_SECONDS", "15")))
_message_id = None


def _fmt_price(value):
    if value is None:
        return "veri bekleniyor"
    try:
        value = float(value)
        if value >= 1:
            return f"${value:,.6f}"
        return f"${value:.10g}"
    except (TypeError, ValueError):
        return "veri bekleniyor"


def _render():
    positions = list(getattr(paper_engine, "_open_positions", {}).values())
    wallet = paper_engine.get_paper_status()
    invested = 0.0
    current_total = 0.0
    lines = [
        "📊 AÇIK POZİSYONLARIM",
        f"🕒 Güncelleme: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}",
        "",
        "💼 SANAL KASA",
        f"🏦 Başlangıç kasası: {wallet.get('paper_starting_balance_usd', 0):.2f}$",
        f"💵 Serbest bakiye: {wallet.get('paper_cash_available_usd', 0):.2f}$",
        f"🔒 Bağlı sermaye: {wallet.get('paper_cash_committed_usd', 0):.2f}$",
        f"📈 Günlük gerçekleşmiş K/Z: {wallet.get('paper_daily_realized_pnl_usd', 0):+.2f}$",
        f"🛑 Günlük zarar limiti: -{wallet.get('paper_daily_loss_limit_usd', 0):.2f}$",
    ]
    equity = wallet.get("paper_total_equity_usd")
    if equity is not None:
        lines.append(f"💎 Toplam sanal özsermaye: {equity:.2f}$")
    else:
        lines.append("⚠️ Toplam özsermaye: fiyat verisi eksik; hesaplanamadı")
    lines.append("")
    for pos in positions:
        symbol = str(pos.get("token_symbol") or "TOKEN")[:32]
        entry = float(pos.get("entry_price_usd") or 0)
        current = pos.get("current_price_usd")
        quantity = float(pos.get("quantity") or 0)
        amount = float(pos.get("invested_usd") or 0)
        invested += amount
        if current is not None:
            current = float(current)
            value = quantity * current
            pnl = value - amount
            pct = (current / entry - 1) * 100 if entry > 0 else 0
            current_total += value
            icon = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{icon} {symbol} | {_fmt_price(current)} | {pnl:+.3f}$ ({pct:+.2f}%)")
            lines.append(f"   Giriş: {_fmt_price(entry)} | Değer: {value:.2f}$")
        else:
            lines.append(f"⚪ {symbol} | Güncel fiyat alınamadı")
            lines.append(f"   Giriş: {_fmt_price(entry)} | Yatırım: {amount:.2f}$")
    if not positions:
        lines.append("Şu anda açık paper pozisyonu yok.")
    total_pnl = current_total - invested
    lines += ["", f"📌 Açık işlem: {len(positions)}", f"💵 Yatırılan: {invested:.2f}$"]
    if positions and all(p.get("current_price_usd") is not None for p in positions):
        lines += [f"💰 Güncel değer: {current_total:.2f}$", f"📈 Toplam açık K/Z: {total_pnl:+.2f}$"]
    lines += ["", "🧪 PAPER MODU — gerçek alım/satım kapalı"]
    return "\n".join(lines)[:3900]


async def refresh_open_positions_dashboard():
    global _message_id
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("Telegram position dashboard disabled: credentials not configured")
        return {"sent": False, "reason": "not_configured"}
    api = f"https://api.telegram.org/bot{token}"
    async with httpx.AsyncClient(timeout=12) as client:
        text = _render()
        if _message_id is not None:
            response = await client.post(f"{api}/editMessageText", json={
                "chat_id": chat_id, "message_id": _message_id, "text": text,
                "disable_web_page_preview": True,
            })
            payload = response.json()
            if payload.get("ok"):
                return {"sent": True, "updated": True, "message_id": _message_id}
            description = str(payload.get("description", "")).lower()
            if "message is not modified" in description:
                return {"sent": True, "updated": False, "message_id": _message_id}
            _message_id = None
        response = await client.post(f"{api}/sendMessage", json={
            "chat_id": chat_id, "text": text, "disable_web_page_preview": True,
        })
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram sendMessage failed"))
        _message_id = payload["result"]["message_id"]
        return {"sent": True, "updated": False, "message_id": _message_id}


async def positions_dashboard_loop(stop_event):
    while not stop_event.is_set():
        try:
            # Refresh marks and preserve the existing paper-only TP/SL behavior.
            await paper_engine.update_paper_prices()
            await refresh_open_positions_dashboard()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram open-position dashboard refresh failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REFRESH_SECONDS)
        except asyncio.TimeoutError:
            pass
