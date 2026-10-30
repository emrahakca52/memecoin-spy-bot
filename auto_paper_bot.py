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
    COPYFOMO_GATE_ENABLED, PAPER_ENTRIES_ENABLED, MOMENTUM_MIN_SCORE, MOMENTUM_MIN_BUY_SELL_RATIO,
    MOMENTUM_MIN_OBSERVED_CHANGE_PCT, MOMENTUM_MAX_OBSERVED_CHANGE_PCT,
)
from telegram_notify import send_scan_report, send_paper_signal_alert, send_hourly_filter_report
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
_position_monitor_task = None
POSITION_MONITOR_SECONDS = max(10, int(os.getenv("PAPER_POSITION_MONITOR_SECONDS", "30")))
_last_run = None
_last_started = None
_last_error = None
_last_price_update = None
_last_scan = None
_last_skipped = []
_last_copyfomo_checks = []
_last_momentum_observations = []
_last_telegram_report = {"sent": False, "reason": "not_sent_yet", "at_utc": None}
_scan_in_progress = False
_scan_started_monotonic = None
_last_scan_duration_seconds = None
_hourly_rejection_counts = {}
_hourly_scan_count = 0
_hourly_period_started = datetime.now(timezone.utc)

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _agent_desk(token: dict) -> dict:
    """Read-only multi-agent summary; never approves or bypasses safety gates."""
    def metric(*keys):
        for key in keys:
            try:
                value = token.get(key)
                if value is not None and value != "":
                    number = float(value)
                    if math.isfinite(number): return number
            except (TypeError, ValueError, OverflowError):
                pass
        return None
    missing, risks = [], []
    volume = metric("volume_24h_usd")
    ratio = metric("buy_sell_ratio_5m", "buy_sell_ratio_1h", "buy_sell_ratio")
    score = metric("score")
    block_buyers = metric("creation_block_buyer_count", "block0_buyer_count", "creation_block_buyers")
    curve = metric("bonding_curve_pct", "curve_pct", "curve_progress_pct")
    top10 = metric("top10_holder_pct", "top10_holders_pct")
    dev = metric("dev_holder_pct", "developer_holder_pct", "dev_holding_pct")
    sell_pressure = metric("top_holder_sell_pressure_pct", "graduation_sell_pressure_pct")
    if volume is None: missing.append("24h_volume")
    if ratio is None: missing.append("buy_sell_ratio")
    elif ratio < 1: risks.append("sell_pressure")
    if block_buyers is None: missing.append("creation_block_buyers")
    elif block_buyers > 3: risks.append("creation_block_bundle_risk")
    if curve is None: missing.append("bonding_curve_progress"); zone = "UNVERIFIED"
    else:
        curve = max(0.0, min(100.0, curve))
        zone = "0-15_sniper" if curve < 15 else "15-50_discovery" if curve < 50 else "50-85_momentum" if curve < 85 else "85-100_graduation_risk"
    if top10 is None: missing.append("top10_holder_concentration")
    elif top10 > 22: risks.append("top10_above_22pct")
    if dev is None: missing.append("developer_holding")
    elif dev > 4: risks.append("developer_above_4pct")
    if sell_pressure is None: missing.append("graduation_sell_pressure")
    return {"decision": "WATCH" if missing else "RISK_REVIEW" if risks else "ANALYSIS_READY",
            "paper_approval": False, "curve_zone": zone, "curve_pct": curve,
            "creation_block_buyers": block_buyers, "top10_holder_pct": top10,
            "dev_holder_pct": dev, "risk_flags": risks, "missing_data": missing,
            "verified_metrics": sum(v is not None for v in (volume, ratio, score, block_buyers, curve, top10, dev, sell_pressure)),
            "agents": {"scout": "OBSERVED" if volume is not None or score is not None else "UNVERIFIED",
                       "sniper": "UNVERIFIED" if block_buyers is None else "RISK" if block_buyers > 3 else "PASS",
                       "curve": "UNVERIFIED" if curve is None else "OBSERVED",
                       "rug_guard": "UNVERIFIED" if top10 is None and dev is None else "OBSERVED",
                       "graduate": "UNVERIFIED" if sell_pressure is None else "OBSERVED"},
            "note": "Read-only. RugCheck, on-chain and Jupiter checks remain mandatory."}

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

    # Chart-style confirmation from provider's short-window market data.
    # These are optional data fields: never invent candle/RSI data when unavailable.
    chart = {"source": "provider_5m_metrics", "available": False}
    change5m_raw = token.get("price_change_5m_pct")
    vol5m = _positive_finite_float(token.get("volume_5m_usd"))
    vol1h = _positive_finite_float(token.get("volume_1h_usd"))
    change5m = None
    try:
        if change5m_raw is not None and math.isfinite(float(change5m_raw)):
            change5m = float(change5m_raw)
    except (TypeError, ValueError, OverflowError):
        pass
    if change5m is not None:
        chart["available"] = True
        chart["price_change_5m_pct"] = round(change5m, 3)
        if change5m < -0.5:
            return {"passed": False, "reason": "chart_5m_trend_negative", "chart_confirmation": chart}
        if change5m > 20:
            return {"passed": False, "reason": "chart_5m_overheated", "chart_confirmation": chart}
    if vol5m is not None and vol1h is not None and vol1h > 0:
        pace_ratio = (vol5m * 12.0) / vol1h
        chart["available"] = True
        chart["volume_5m_usd"] = vol5m
        chart["volume_1h_usd"] = vol1h
        chart["volume_pace_vs_1h"] = round(pace_ratio, 3)
        if pace_ratio < 0.25:
            return {"passed": False, "reason": "chart_volume_fading", "chart_confirmation": chart}

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
    return {"passed": True, "reason": "observation_passed", "observed_change_pct": round(change_pct, 2), "observed_price_usd": samples[-1], "samples": len(samples), "ratio_window": ratio_window, "ratio": ratio, "trades_5m": trades_5m, "chart_confirmation": chart}


def _ai_quota_fallback_allowed(gate: dict) -> bool:
    """Paper-only fallback when AI is rate-limited, never when a safety gate fails.

    RugCheck, independent on-chain authority checks, and Jupiter round-trip
    must all have passed. This prevents a temporary AI quota error from silently
    blocking every otherwise-verified paper candidate, without weakening hard
    token-safety checks or enabling real trading.
    """
    if not isinstance(gate, dict) or gate.get("passed"):
        return False
    rug = gate.get("rugcheck") if isinstance(gate.get("rugcheck"), dict) else {}
    onchain = gate.get("onchain_safety") if isinstance(gate.get("onchain_safety"), dict) else {}
    route = gate.get("round_trip") if isinstance(gate.get("round_trip"), dict) else {}
    ai = gate.get("grok") if isinstance(gate.get("grok"), dict) else {}
    if not (rug.get("passed") is True and onchain.get("passed") is True and route.get("passed") is True):
        return False
    status = str(ai.get("http_status") or ai.get("status_code") or "")
    error = str(ai.get("error") or ai.get("provider_status") or "").lower()
    message = str(ai.get("message") or ai.get("provider_message") or "").lower()
    quota_limited = status == "429" or "resource_exhausted" in error or "quota" in error or "quota" in message
    return quota_limited and ai.get("api_ok") is False


def _daily_loss_exceeded() -> bool:
    # A zero/non-positive limit disables the aggregate daily cap in PAPER mode.
    if MAX_DAILY_LOSS_USD <= 0:
        return False
    try:
        status = get_paper_status()
        # Use today's realized PnL, not lifetime PnL, so a historical loss
        # does not permanently disable future paper scans.
        daily_realized = status.get("paper_daily_realized_pnl_usd", 0)
        return daily_realized <= -abs(MAX_DAILY_LOSS_USD)
    except Exception:
        logger.exception("Could not evaluate daily paper loss limit; fail closed for this scan.")
        return True

async def _loop(stop_event):
    global _last_run, _last_started, _last_error, _last_price_update, _last_scan, _last_skipped, _last_copyfomo_checks, _last_momentum_observations, _last_telegram_report, _scan_in_progress, _scan_started_monotonic, _last_scan_duration_seconds

    while not stop_event.is_set():
        _last_started = _utc_now()
        _scan_started_monotonic = asyncio.get_running_loop().time()
        _scan_in_progress = True
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

            if not PAPER_ENTRIES_ENABLED:
                scan_daily_loss_limit = True
                _last_skipped.append({"reason": "new_entries_paused"})
                _last_scan = {
                    "provider": "paused",
                    "checked": 0,
                    "candidate_count": 0,
                    "filter_diagnostics": {},
                    "signal_diagnostics": {"reason": "new_entries_paused"},
                }
            elif _daily_loss_exceeded():
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
                            desk = _agent_desk(token)
                            gate = await asyncio.wait_for(evaluate_candidate(token), timeout=75)
                            checked.append({"symbol": token.get("symbol"), "agent_desk": desk, **gate})
                            if gate.get("passed"):
                                eligible_signals.append(token)
                            elif _ai_quota_fallback_allowed(gate):
                                # PAPER ONLY: use deterministic hard checks when AI provider
                                # quota is exhausted; never bypass RugCheck/on-chain/route gates.
                                gate["paper_fallback"] = "ai_quota_hard_checks_only"
                                gate["passed"] = True
                                gate["decision"] = "PAPER_ONLY_FALLBACK"
                                checked[-1] = {"symbol": token.get("symbol"), "agent_desk": desk, **gate}
                                eligible_signals.append(token)
                                logger.warning(
                                    "AI quota fallback allowed for PAPER candidate %s after RugCheck, on-chain and Jupiter checks passed",
                                    token.get("symbol"),
                                )
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
                    # The observation price is the freshest quote from the same USD-price
                    # pipeline used during the 20-second momentum window. Prefer it over
                    # the earlier bulk quote, which may be stale by the time observation ends.
                    # Do not compare a Jupiter discovery snapshot against a DexScreener/
                    # GeckoTerminal live quote: cross-provider price differences caused
                    # false rejections (e.g. a 30.8% discrepancy) even after a valid
                    # bounded momentum observation. The observation window itself enforces
                    # the configured -0.5% to +8% movement guard.
                    live_price = _positive_finite_float((observation or {}).get("observed_price_usd"))
                    if not live_price:
                        live_price = _positive_finite_float(prices.get(address))
                    if not live_price or live_price <= 0:
                        _last_skipped.append({"symbol": symbol, "reason": "live_price_unavailable"})
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
            if _scan_started_monotonic is not None:
                _last_scan_duration_seconds = round(max(0.0, asyncio.get_running_loop().time() - _scan_started_monotonic), 2)
            _scan_in_progress = False
            _scan_started_monotonic = None
            reason_counts = {}
            if isinstance(_last_scan, dict):
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
            # Aggregate rejection reasons across scan cycles; report once per hour.
            global _hourly_rejection_counts, _hourly_scan_count, _hourly_period_started
            _hourly_scan_count += 1
            for reason, count in reason_counts.items():
                _hourly_rejection_counts[reason] = _hourly_rejection_counts.get(reason, 0) + count
            elapsed = (datetime.now(timezone.utc) - _hourly_period_started).total_seconds()
            if elapsed >= 3600:
                try:
                    await send_hourly_filter_report(
                        counts=dict(_hourly_rejection_counts),
                        scans=_hourly_scan_count,
                        started_at=_hourly_period_started.isoformat(),
                    )
                except Exception:
                    logger.exception("Hourly filter report unexpectedly failed")
                _hourly_rejection_counts = {}
                _hourly_scan_count = 0
                _hourly_period_started = datetime.now(timezone.utc)
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

async def _position_monitor_loop(stop_event):
    """Refresh open paper positions independently of the slower discovery scan."""
    global _last_price_update
    while not stop_event.is_set():
        try:
            _last_price_update = await asyncio.wait_for(update_paper_prices(), timeout=25)
        except asyncio.TimeoutError:
            _last_price_update = {"error": "position_monitor_timeout"}
            logger.warning("Position monitor timed out")
        except Exception as exc:
            _last_price_update = {"error": type(exc).__name__}
            logger.exception("Position monitor failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POSITION_MONITOR_SECONDS)
        except asyncio.TimeoutError:
            pass


async def start_bot():
    global _task, _stop_event, _position_monitor_task
    if _task is not None and not _task.done():
        return bot_status()
    _stop_event = asyncio.Event()
    _position_monitor_task = asyncio.create_task(_position_monitor_loop(_stop_event))
    _task = asyncio.create_task(_loop(_stop_event))
    return bot_status()

async def stop_bot():
    global _task, _stop_event, _position_monitor_task
    if _stop_event:
        _stop_event.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except asyncio.TimeoutError:
            _task.cancel()
    if _position_monitor_task:
        try:
            await asyncio.wait_for(_position_monitor_task, timeout=5)
        except asyncio.TimeoutError:
            _position_monitor_task.cancel()
    _task = None
    _position_monitor_task = None
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
        "position_monitor_interval_seconds": POSITION_MONITOR_SECONDS,
        "position_monitor_running": _position_monitor_task is not None and not _position_monitor_task.done(),
        "paper_amount_per_position_usd": PAPER_AMOUNT_USD,
        "paper_entries_enabled": PAPER_ENTRIES_ENABLED,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "take_profit_pct": TAKE_PROFIT_PCT,
        "stop_loss_pct": STOP_LOSS_PCT,
        "last_run_utc": _last_run,
        "last_started_utc": _last_started,
        "scan_in_progress": _scan_in_progress,
        "last_scan_duration_seconds": _last_scan_duration_seconds,
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
