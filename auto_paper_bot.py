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
from paper_engine import (
    get_token_prices_usd,
    open_paper_position,
    update_paper_prices,
    get_price_diagnostics,
    get_paper_status,
)

logger = logging.getLogger(__name__)

POLL_SECONDS = max(30, int(os.getenv("PAPER_BOT_POLL_SECONDS", "180")))
MAX_ENTRY_PRICE_DEVIATION_PCT = 10.0

_task = None
_stop_event = None
_last_run = None
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
    global _last_run, _last_error, _last_price_update, _last_scan, _last_skipped, _last_copyfomo_checks

    while not stop_event.is_set():
        _last_run = _utc_now()
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
                _last_skipped.append({"reason": "daily_loss_limit_reached"})
                await asyncio.sleep(POLL_SECONDS)
                continue

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
                data = {"signals": [], "checked": 0}

            signals = data.get("signals", []) or []
            _last_scan = {
                "provider": data.get("provider"),
                "checked": data.get("checked", 0),
                "candidate_count": len(signals),
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
                            _last_skipped.append({"symbol": token.get("symbol"), "reason": "rugcheck_rejected"})
                    except asyncio.TimeoutError:
                        _last_skipped.append({"symbol": token.get("symbol"), "reason": "rugcheck_timeout"})
                _last_copyfomo_checks = checked

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
                    logger.info("Opened position: %s", symbol)

        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            logger.exception("Scan failed")
        finally:
            _last_run = _utc_now()
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
        "last_error": _last_error,
        "last_skipped": _last_skipped[-8:],
        "copyfomo_gate_enabled": COPYFOMO_GATE_ENABLED,
        "warning": "Paper only.",
    }
