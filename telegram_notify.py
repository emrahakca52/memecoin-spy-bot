"""Optional Telegram scan reports. Disabled safely unless both env vars are configured."""
import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def send_scan_report(
    *,
    checked: int = 0,
    candidates: int = 0,
    approved: int = 0,
    opened: int = 0,
    skipped: list | None = None,
    error: str | None = None,
    daily_loss_limit: bool = False,
) -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.info("Telegram scan report skipped: Telegram credentials are not configured")
        return {"sent": False, "reason": "not_configured"}

    skipped = skipped or []
    lines = [
        "🤖 MEMECOIN BOT — TARMA RAPORU",
        f"🔎 Taranan token: {checked}",
        f"📊 Filtrelerden geçen aday: {candidates}",
        f"🛡️ Güvenlikten geçen: {approved}",
        f"🧪 Açılan sanal işlem: {opened}",
    ]
    if daily_loss_limit:
        lines.append("⛔ Günlük sanal zarar limiti nedeniyle yeni işlem değerlendirmesi durduruldu.")
    elif opened:
        lines.append("✅ Sanal pozisyon açıldı.")
    elif approved:
        lines.append("ℹ️ Güvenlik kontrolü geçti; işlem açılma koşulları ayrıca kontrol edildi.")
    else:
        lines.append("📭 Yeni sinyal yok; uygun aday bulunmadı veya aday güvenlik kontrolünden geçmedi.")
    if skipped:
        reason_counts = {}
        for item in skipped:
            reason = str(item.get("reason") or "elenmiş")
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        lines.append("Eleme sayıları: " + ", ".join(f"{key}={value}" for key, value in sorted(reason_counts.items())))
        reasons = []
        for item in skipped[-3:]:
            symbol = item.get("symbol") or "Token"
            reason = item.get("reason") or "elenmiş"
            detail = str(item.get("detail") or "").strip()
            status = item.get("http_status")
            suffix = f" — {detail}" if detail else ""
            if status is not None:
                suffix += f" (HTTP {status})"
            reasons.append(f"{symbol}: {reason}{suffix}"[:260])
        lines.append("Son eleme nedenleri: " + "; ".join(reasons))
    if error:
        lines.append(f"⚠️ Tarama hatası: {error[:180]}")
    lines.extend(["💵 Mod: PAPER (sanal)", "🔒 Gerçek işlem: KAPALI"])

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json={"chat_id": chat_id, "text": "\n".join(lines), "disable_web_page_preview": True},
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                logger.warning("Telegram rejected scan report: %s", payload.get("description", "unknown error"))
                return {"sent": False, "reason": "telegram_rejected"}
        return {"sent": True}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Telegram scan report failed: %s", exc)
        return {"sent": False, "reason": "send_failed"}
