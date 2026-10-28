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
    COPYFOMO_GATE_ENABLED, MOMENTUM_MIN_SCORE, MOMENTUM_MIN_BUY_SELL_RATIO,
    MOMENTUM_MIN_OBSERVED_CHANGE_PCT, MOMENTUM_MAX_OBSERVED_CHANGE_PCT,
)
from telegram_notify import send_scan_report, send_paper_signal_alert
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
_last_momentum_observations = []
_last_telegram_report = {"sent": False, "reason": "not_sent_yet", "at_utc": None}

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _positive_finite_float(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None

async def _observe_candidate(token: dict) -> dict:
    """20-second paper-only price observation inspired by early-momentum bots.

    Our current feeds do not expose a reliable per-trade wallet stream, so this
    samples normalized USD prices rather than pretending to count unique buyers.
    """
    address = str(token.get("token_address") or "")
    if not address:
        return {"passed": False, "reason": "missing_address"}
    trades_5m = int(token.get("trade_count_5m") or 0)
    ratio_5m = _positive_finite_float(token.get("buy_sell_ratio_5m"))
    # Prefer the recent 5-minute tape when it has enough activity; otherwise
    # fall back to the provider's 1h/24h ratio instead of treating missing data as zero.
    if trades_5m >= 5 and ratio_5m is not None:
        ratio = ratio_5m
        ratio_window = "5m"
    else:
        ratio = _positive_finite_float(token.get("buy_sell_ratio_1h")) or _positive_finite_float(token.get("buy_sell_ratio")) or 0.0
        ratio_window = "1h_or_24h"
    trades = int(token.get("trade_count_1h") or token.get("trade_count_24h") or 0)
    score = _positive_finite_float(token.get("score")) or 0.0
    if score < MOMENTUM_MIN_SCORE:
        return {"passed": False, "reason": "momentum_score_below_threshold", "score": score, "min_score": MOMENTUM_MIN_SCORE}
    if ratio < MOMENTUM_MIN_BUY_SELL_RATIO:
        return {"passed": False, "reason": "buy_sell_ratio_below_momentum_threshold", "ratio": ratio, "ratio_window": ratio_window, "trades_5m": trades_5m, "min_ratio": MOMENTUM_MIN_BUY_SELL_RATIO}
    if trades < 20:
        return {"passed": False, "reason": "trade_count_below_20", "trades": trades}

    samples = []
    for index in range(3):
        try:
            found = await asyncio.wait_for(get_token_prices_usd([address]), timeout=8)
            price = _positive_finite_float(found.get(address)) if isinstance(found, dict) else None
            if price:
                samples.append(price)
        except Exception:
            pass
        if index < 2:
            await asyncio.sleep(10)

    if len(samples) < 2:
        return {"passed": False, "reason": "observation_price_samples_missing", "samples": len(samples)}
    change_pct = (samples[-1] / samples[0] - 1) * 100
    if change_pct < MOMENTUM_MIN_OBSERVED_CHANGE_PCT:
        return {"passed": False, "reason": "observation_momentum_below_threshold", "observed_change_pct": round(change_pct, 2), "observed_price_usd": samples[-1], "min_change_pct": MOMENTUM_MIN_OBSERVED_CHANGE_PCT, "samples": len(samples)}
    if change_pct > MOMENTUM_MAX_OBSERVED_CHANGE_PCT:
        return {"passed": False, "reason": "observation_overheated", "observed_change_pct": round(change_pct, 2), "observed_price_usd": samples[-1], "max_change_pct": MOMENTUM_MAX_OBSERVED_CHANGE_PCT, "samples": len(samples)}
    return {"passed": True, "reason": "observation_passed", "observed_change_pct": round(change_pct, 2), "observed_price_usd": samples[-1], "samples": len(samples), "ratio_window": ratio_window, "ratio": ratio, "trades_5m": trades_5m}


def _daily_loss_exceeded() -> bool:
    try:
        status = get_paper_status()
        realized = status.get("paper_realized_pnl_usd", 0)
        return realized <= -MAX_DAILY_LOSS_USD
    except Exception:
        return False

async def _loop(stop_event):
    global _last_run, _last_started, _last_error, _last_price_update, _last_scan, _last_skipped, _last_copyfomo_checks, _last_momentum_observations, _last_telegram_report

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
            _last_momentum_observations = []
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
                            gate = await asyncio.wait_for(evaluate_candidate(token), timeout=75)
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

                # Observe only a small, highest-ranked batch to limit provider load.
                # Observation is concurrent so the 20-second window is shared, not serial.
                observation_results = {}
                observation_batch = sorted(
                    valid_signals, key=lambda row: float(row[0].get("score") or 0), reverse=True
                )[:5]
                if observation_batch:
                    observed = await asyncio.gather(
                        *[_observe_candidate(row[0]) for row in observation_batch],
                        return_exceptions=True,
                    )
                    for row, outcome in zip(observation_batch, observed):
                        address = row[1]
                        token = row[0]
                        if isinstance(outcome, Exception):
                            observation_results[address] = {"passed": False, "reason": type(outcome).__name__}
                        else:
                            observation_results[address] = outcome
                        _last_momentum_observations.append({
                            "symbol": token.get("symbol"),
                            "token_address": address,
                            "score": token.get("score"),
                            "buy_sell_ratio_5m": token.get("buy_sell_ratio_5m"),
                            "trade_count_5m": token.get("trade_count_5m"),
                            "result": observation_results[address],
                        })

                observed_addresses = set(observation_results)
                for token, address, symbol, signal_price in valid_signals:
                    if stop_event.is_set():
                        break
                    observation = observation_results.get(address)
                    if address not in observed_addresses:
                        _last_skipped.append({"symbol": symbol, "reason": "observation_batch_limit"})
                        continue
                    if observation is not None and not observation.get("passed"):
                        _last_skipped.append({
                            "symbol": symbol,
                            "reason": "momentum_observation_rejected",
                            "detail": observation.get("reason"),
                            "observed_change_pct": observation.get("observed_change_pct"),
                        })
                        continue
                    live_price = prices.get(address) or (observation or {}).get("observed_price_usd")
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
                        try:
                            alert_result = await send_paper_signal_alert(
                                symbol=symbol,
                                address=address,
                                price_usd=live_price,
                                amount_usd=PAPER_AMOUNT_USD,
                            )
                            if not alert_result.get("sent"):
                                logger.warning("Telegram paper signal alert not sent: %s", alert_result)
                        except Exception:
                            logger.exception("Telegram paper signal alert unexpectedly failed for %s", symbol)

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
                checks = _last_copyfomo_checks if isinstance(_last_copyfomo_checks, list) else []
                # Each check is flattened when appended: {"symbol": ..., **gate}.
                # Read the gate fields from the item itself, not item["gate"].
                rugcheck_passed_count = sum(
                    1 for item in checks
                    if isinstance(item.get("rugcheck"), dict)
                    and item["rugcheck"].get("passed") is True
                )
                ai_called_count = sum(
                    1 for item in checks
                    if isinstance(item.get("grok"), dict)
                    and item["grok"].get("provider") not in (None, "not_called")
                )
                ai_api_success_count = sum(
                    1 for item in checks
                    if isinstance(item.get("grok"), dict)
                    and item["grok"].get("api_ok") is True
                )
                _last_scan["momentum_observations"] = list(_last_momentum_observations)
                _last_scan["processing"] = {
                    "security_approved": scan_approved_count,
                    "rugcheck_passed": rugcheck_passed_count,
                    "ai_called": ai_called_count,
                    "ai_api_success": ai_api_success_count,
                    "paper_positions_opened": scan_opened_count,
                    "skipped_count": len(_last_skipped),
                    "skip_reason_counts": reason_counts,
                    "daily_loss_limit": scan_daily_loss_limit,
                    "scan_error": scan_error,
                }
            try:
                telegram_result = await send_scan_report(
                    checked=scan_checked,
                    candidates=scan_candidate_count,
                    approved=scan_approved_count,
                    opened=scan_opened_count,
                    skipped=_last_skipped,
                    error=scan_error,
                    daily_loss_limit=scan_daily_loss_limit,
                )
                _last_telegram_report = {**(telegram_result if isinstance(telegram_result, dict) else {"sent": False, "reason": "invalid_send_result"}), "at_utc": _utc_now()}
                if not _last_telegram_report.get("sent"):
                    logger.warning("Telegram scan report not sent: %s", _last_telegram_report)
            except Exception as exc:
                _last_telegram_report = {"sent": False, "reason": type(exc).__name__, "at_utc": _utc_now()}
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
    for item in _last_copyfomo_checks[-10:]:
        gate = item.get("gate") if isinstance(item.get("gate"), dict) else item
        ai = gate.get("grok") if isinstance(gate.get("grok"), dict) else {}
        rug = gate.get("rugcheck") if isinstance(gate.get("rugcheck"), dict) else {}
        onchain = gate.get("onchain_safety") if isinstance(gate.get("onchain_safety"), dict) else {}
        round_trip = gate.get("round_trip") if isinstance(gate.get("round_trip"), dict) else {}
        copyfomo_summary.append({
            "symbol": item.get("symbol"),
            "passed": gate.get("passed"),
            "stage": gate.get("stage"),
            "decision": gate.get("decision"),
            "rejection_reasons": gate.get("rejection_reasons", []),
            "rugcheck_score": rug.get("score_normalised", rug.get("score")),
            "rugcheck_raw_score": rug.get("raw_score"),
            "top10_holders_pct": rug.get("top10_holders_pct"),
            "lp_locked_pct": rug.get("lp_locked_pct"),
            "mint_authority_active": rug.get("mint_authority_active"),
            "freeze_authority_active": rug.get("freeze_authority_active"),
            "insider_network_count": rug.get("insider_network_count"),
            "risk_flags": rug.get("risk_flags", []),
            "missing_data": rug.get("missing_data", []),
            "rugcheck_error": rug.get("error"),
            "rugcheck_http_status": rug.get("http_status"),
            "onchain_safety_passed": onchain.get("passed"),
            "onchain_safety_reasons": onchain.get("reasons", []),
            "onchain_mint_authority_active": onchain.get("mint_authority_active"),
            "onchain_freeze_authority_active": onchain.get("freeze_authority_active"),
            "onchain_rpc_error": onchain.get("error"),
            "round_trip_passed": round_trip.get("passed"),
            "round_trip_reasons": round_trip.get("reasons", []),
            "round_trip_sell_price_impact_pct": round_trip.get("sell_price_impact_pct"),
            "round_trip_error": round_trip.get("error"),
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
        "last_telegram_report": _last_telegram_report,
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
