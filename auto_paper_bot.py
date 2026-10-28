import asyncio
import logging
import math
import os
from datetime import datetime, timezone

from signal_engine import get_signal_candidates
from check import evaluate_candidate
from config import (
    PAPER_AMOUNT_USD, MAX_OPEN_POSITIONS,
    MIN_LIQUIDITY_USD, MIN_VOLUME_24H_USD, MIN_BUYS_SELLS_RATIO,
    TAKE_PROFIT_PCT, STOP_LOSS_PCT, MAX_DAILY_LOSS_USD,
    MAX_GROK_CHECKS_PER_SCAN, GROK_CONFIDENCE_THRESHOLD,
    COPYFOMO_GATE_ENABLED,
)
from telegram_notify import send_scan_report
from paper_engine import (
    get_token_prices_usd,
    open_paper_position,
    update_paper_prices,
    get_price_diagnostics,
    get_paper_status,
)

logger = logging.getLogger(__name__)
logger.info("AUTO_PAPER_BOT LOADED - NEW VERSION")

POLL_SECONDS = max(30, int(os.getenv("PAPER_BOT_POLL_SECONDS", "180")))
MAX_ENTRY_PRICE_DEVIATION_PCT = 10.0

_task = None
_stop_event = None
_last_run = None
_last_started = None
_last_error = None
_last_price_update = None
_last_scan = None
_last_skipped = []
_last_copyfomo_checks = []

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _positive_finite_float(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None

def _daily_loss_exceeded() -> bool:
    try:
        status = get_paper_status()
        realized = status.get("paper_realized_pnl_usd", 0)
        return realized <= -MAX_DAILY_LOSS_USD
    except Exception:
        return False

async def _loop(stop_event):
    global _last_run, _last_started, _last_error, _last_price_update, _last_scan, _last_skipped, _last_copyfomo_checks

    while not stop_event.is_set():
        _last_started = _utc_now()
        scan_checked = 0
        scan_candidate_count = 0
        scan_approved_count = 0
        scan_opened_count = 0
        scan_daily_loss_limit = False
        scan_error = None
        try:
            _last_error = None
            _last_skipped = []
            logger.info("Scan cycle started")

            try:
                _last_price_update = await asyncio.wait_for(update_paper_prices(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Price update timed out")
                _last_price_update = {"error": "timeout"}

            if _daily_loss_exceeded():
                scan_daily_loss_limit = True
                _last_skipped.append({"reason": "daily_loss_limit_reached"})
                _last_scan = {
                    "provider": "paused",
                    "checked": 0,
                    "candidate_count": 0,
                    "filter_diagnostics": {},
                    "signal_diagnostics": {"reason": "daily_loss_limit_reached"},
                }

            if not scan_daily_loss_limit:
                try:
                    data = await asyncio.wait_for(
                        get_signal_candidates(
                            min_liquidity_usd=MIN_LIQUIDITY_USD,
                            min_volume_24h_usd=MIN_VOLUME_24H_USD,
                            min_buys_sells_ratio=MIN_BUYS_SELLS_RATIO,
                            limit=20,
                        ),
                        timeout=40,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Signal fetch timed out")
                    scan_error = "signal_fetch_timeout"
                    data = {"signals": [], "checked": 0}

                signals = data.get("signals", []) or []
                scan_checked = int(data.get("checked", 0) or 0)
                scan_candidate_count = len(signals)
                _last_scan = {
                    "provider": data.get("provider"),
                    "checked": data.get("checked", 0),
                    "candidate_count": len(signals),
                    "filter_diagnostics": data.get("filter_diagnostics", {}),
                    "signal_diagnostics": data.get("diagnostics", {}),
                }
                logger.info("Signals found: %s", len(signals))

                eligible_signals = signals
                if COPYFOMO_GATE_ENABLED and signals:
                    ordered = sorted(signals, key=lambda x: float(x.get("score") or 0), reverse=True)
                    checked = []
                    eligible_signals = []
                    for token in ordered[:MAX_GROK_CHECKS_PER_SCAN]:
                        if stop_event.is_set():
                            break
                        try:
                            gate = await asyncio.wait_for(evaluate_candidate(token), timeout=20)
                            checked.append({"symbol": token.get("symbol"), **gate})
                            if gate.get("passed"):
                                eligible_signals.append(token)
                            else:
                                rejection_reasons = gate.get("rejection_reasons") or []
                                rug = gate.get("rugcheck") if isinstance(gate.get("rugcheck"), dict) else {}
                                # Preserve the concrete fail-closed reason in status and Telegram reports.
                                if not rejection_reasons:
                                    rejection_reasons = rug.get("missing_data") or []
                                if not rejection_reasons and rug.get("error"):
                                    rejection_reasons = [str(rug.get("error"))]
                                detail = "; ".join(str(reason) for reason in rejection_reasons[:4] if reason)
                                skipped_item = {"symbol": token.get("symbol"), "reason": "rugcheck_rejected"}
                                if detail:
                                    skipped_item["detail"] = detail[:220]
                                if rug.get("http_status") is not None:
                                    skipped_item["http_status"] = rug.get("http_status")
                                _last_skipped.append(skipped_item)
                        except asyncio.TimeoutError:
                            _last_skipped.append({"symbol": token.get("symbol"), "reason": "rugcheck_timeout"})
                    _last_copyfomo_checks = checked

                scan_approved_count = len(eligible_signals)
                valid_signals = []
                for token in eligible_signals:
                    address = token.get("token_address")
                    symbol = token.get("symbol") or ""
                    signal_price = _positive_finite_float(token.get("price_usd"))
                    if not address or signal_price is None:
                        continue
                    valid_signals.append((token, address, symbol, signal_price))

                prices = {}
                if valid_signals:
                    try:
                        prices = await asyncio.wait_for(
                            get_token_prices_usd([a for _, a, _, _ in valid_signals]),
                            timeout=25,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Price fetch timed out")

                for token, address, symbol, signal_price in valid_signals:
                    if stop_event.is_set():
                        break
                    live_price = prices.get(address)
                    if not live_price or live_price <= 0:
                        _last_skipped.append({"symbol": symbol, "reason": "live_price_unavailable"})
                        continue
                    deviation = abs(live_price / signal_price - 1) * 100
                    if deviation > MAX_ENTRY_PRICE_DEVIATION_PCT:
                        _last_skipped.append({"symbol": symbol, "reason": "price_deviation", "deviation_pct": round(deviation, 2)})
                        continue

                    result = open_paper_position(
                        token_address=address,
                        token_symbol=symbol,
                        amount_usd=PAPER_AMOUNT_USD,
                        price_usd=live_price,
                        max_open_positions=MAX_OPEN_POSITIONS,
                    )
                    if result.get("skipped"):
                        _last_skipped.append({"symbol": symbol, "reason": result.get("reason")})
                    else:
                        scan_opened_count += 1
                        logger.info("Opened position: %s", symbol)

        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            scan_error = _last_error
            logger.exception("Scan failed")
        finally:
            _last_run = _utc_now()
            if isinstance(_last_scan, dict):
                reason_counts = {}
                for item in _last_skipped:
                    reason = str(item.get("reason") or "unknown")
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                _last_scan["processing"] = {
                    "security_approved": scan_approved_count,
                    "paper_positions_opened": scan_opened_count,
                    "skipped_count": len(_last_skipped),
                    "skip_reason_counts": reason_counts,
                    "daily_loss_limit": scan_daily_loss_limit,
                    "scan_error": scan_error,
                }
            try:
                await send_scan_report(
                    checked=scan_checked,
                    candidates=scan_candidate_count,
                    approved=scan_approved_count,
                    opened=scan_opened_count,
                    skipped=_last_skipped,
                    error=scan_error,
                    daily_loss_limit=scan_daily_loss_limit,
                )
            except Exception:
                logger.exception("Telegram scan report unexpectedly failed")
            logger.info("Scan cycle finished")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass

async def start_bot():
    global _task, _stop_event
    if _task is not None and not _task.done():
        return bot_status()
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop(_stop_event))
    return bot_status()

async def stop_bot():
    global _task, _stop_event
    if _stop_event:
        _stop_event.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except asyncio.TimeoutError:
            _task.cancel()
    _task = None
    _stop_event = None
    return bot_status()

def bot_status():
    copyfomo_summary = []
    for item in _last_copyfomo_checks[-8:]:
        gate = item.get("gate") if isinstance(item.get("gate"), dict) else item
        ai = gate.get("grok") if isinstance(gate.get("grok"), dict) else {}
        copyfomo_summary.append({
            "symbol": item.get("symbol"),
            "passed": gate.get("passed"),
            "stage": gate.get("stage"),
            "decision": gate.get("decision"),
            "rejection_reasons": gate.get("rejection_reasons", []),
            "ai_provider": ai.get("provider", "not_called"),
            "ai_model": ai.get("model"),
            "ai_api_ok": ai.get("api_ok", False),
            "ai_decision": ai.get("decision", "NOT_RUN"),
            "ai_confidence": ai.get("confidence"),
            "ai_reasons": ai.get("reasons", []),
            "ai_http_status": ai.get("http_status"),
        })
    return {
        "mode": "paper",
        "running": _task is not None and not _task.done(),
        "real_trading_enabled": False,
        "poll_interval_seconds": POLL_SECONDS,
        "paper_amount_per_position_usd": PAPER_AMOUNT_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "last_run_utc": _last_run,
        "last_started_utc": _last_started,
        "last_error": _last_error,
        "last_scan": _last_scan,
        "last_price_update": _last_price_update,
        "last_copyfomo_checks": copyfomo_summary,
        "last_skipped": _last_skipped[-8:],
        "copyfomo_gate_enabled": COPYFOMO_GATE_ENABLED,
        "ai_provider": os.getenv("AI_PROVIDER", "grok").strip().lower(),
        "ai_model": (os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() if os.getenv("AI_PROVIDER", "grok").strip().lower() == "gemini" else GROK_MODEL),
        "ai_api_key_configured": bool((os.getenv("GEMINI_API_KEY", "").strip() if os.getenv("AI_PROVIDER", "grok").strip().lower() == "gemini" else os.getenv("XAI_API_KEY", "").strip())),
        "grok_api_key_configured": bool(os.getenv("XAI_API_KEY", "").strip()),
        "gemini_api_key_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "database_configured": bool(os.getenv("DATABASE_URL", "").strip()),
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip()),
        "warning": "Paper only.",
    }
