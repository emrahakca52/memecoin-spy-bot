import asyncio
import logging
import math
import os
from datetime import datetime, timezone

from signal_engine import (
    get_signal_candidates,
    MAX_PRICE_CHANGE_24H_PCT,
    MAX_NEGATIVE_PRICE_CHANGE_24H_PCT as SIGNAL_MAX_NEGATIVE_PRICE_CHANGE_24H_PCT,
    MIN_TRADES_24H,
)
from check import evaluate_candidate
from config import MAX_GROK_CHECKS_PER_SCAN, GROK_CONFIDENCE_THRESHOLD
from paper_engine import (
    get_token_prices_usd,
    open_paper_position,
    update_paper_prices,
    get_price_diagnostics,
)

logger = logging.getLogger(__name__)

# Paper-only settings. This module never submits real orders.
POLL_SECONDS = max(30, int(os.getenv("PAPER_BOT_POLL_SECONDS", "180")))
PAPER_BUY_USD = 10.0
MAX_OPEN_POSITIONS = 5
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "10000"))
MIN_VOLUME_24H_USD = float(os.getenv("MIN_VOLUME_24H_USD", "20000"))
MIN_BUYS_SELLS_RATIO = float(os.getenv("MIN_BUYS_SELLS_RATIO", "1.0"))

# Keep status aligned with the actual signal_engine filters.
MAX_ABSOLUTE_PRICE_CHANGE_24H_PCT = MAX_PRICE_CHANGE_24H_PCT

# Reflect the actual negative-only threshold enforced by signal_engine.py.
MAX_NEGATIVE_PRICE_CHANGE_24H_PCT = SIGNAL_MAX_NEGATIVE_PRICE_CHANGE_24H_PCT

MAX_ENTRY_PRICE_DEVIATION_PCT = 10.0

_task = None
_stop_event = None
_last_run = None
_last_error = None
_last_price_update = None
_last_scan = None
_last_skipped = []
_last_copyfomo_checks = []

COPYFOMO_GATE_ENABLED = (
    os.getenv("COPYFOMO_GATE_ENABLED", "false").strip().lower()
    in {"1", "true", "yes", "on"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_finite_float(value):
    """Return a positive finite float, or None for malformed/unsafe values."""
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


async def _loop(stop_event):
    global _last_run, _last_error, _last_price_update
    global _last_scan, _last_skipped, _last_copyfomo_checks

    while not stop_event.is_set():
        try:
            _last_error = None
            _last_skipped = []
            _last_price_update = await update_paper_prices()

            data = await get_signal_candidates(
                min_liquidity_usd=MIN_LIQUIDITY_USD,
                min_volume_24h_usd=MIN_VOLUME_24H_USD,
                min_buys_sells_ratio=MIN_BUYS_SELLS_RATIO,
                limit=20,
            )
            signals = data.get("signals", []) or []
            diagnostics = data.get("diagnostics") or {}
            _last_scan = {
                "provider": data.get("provider"),
                "checked": data.get("checked", 0),
                "candidate_count": data.get("candidate_count", len(signals)),
                "note": data.get("note"),
                "last_error": data.get("last_error"),
                "filters": diagnostics.get("filters", {}),
                "additional_filters": diagnostics.get("additional_filters", {}),
                "rejection_counts": diagnostics.get("rejection_counts", {}),
            }

            # Optional CopyFomo gate: RugCheck first, then Grok.
            _last_copyfomo_checks = []
            eligible_signals = signals
            if COPYFOMO_GATE_ENABLED:
                ordered = sorted(
                    signals,
                    key=lambda item: float(item.get("score") or 0),
                    reverse=True,
                )
                checked = []
                eligible_signals = []
                for token in ordered[:MAX_GROK_CHECKS_PER_SCAN]:
                    if stop_event.is_set():
                        break
                    gate = await evaluate_candidate(token)
                    checked.append({
                        "symbol": token.get("symbol"),
                        "token_address": token.get("token_address"),
                        **gate,
                    })
                    if gate.get("passed"):
                        eligible_signals.append(token)
                    else:
                        _last_skipped.append({
                            "symbol": token.get("symbol"),
                            "reason": "copyfomo_gate_rejected",
                            "stage": gate.get("stage"),
                            "details": gate.get("rejection_reasons", []),
                        })
                _last_copyfomo_checks = checked
                _last_scan["copyfomo_gate"] = {
                    "enabled": True,
                    "checked": len(checked),
                    "approved": len(eligible_signals),
                    "max_checks_per_scan": MAX_GROK_CHECKS_PER_SCAN,
                    "grok_confidence_threshold": GROK_CONFIDENCE_THRESHOLD,
                    "note": (
                        "Only top-ranked candidates are checked per scan; "
                        "other candidates are not approved this cycle."
                    ),
                }
            else:
                _last_scan["copyfomo_gate"] = {
                    "enabled": False,
                    "note": (
                        "Set COPYFOMO_GATE_ENABLED=true after configuring "
                        "XAI_API_KEY to enable RugCheck + Grok gating."
                    ),
                }

            valid_signals = []
            for token in eligible_signals:
                address = token.get("token_address")
                symbol = token.get("symbol") or token.get("token_symbol") or ""
                signal_price = _positive_finite_float(token.get("price_usd"))

                if not address or signal_price is None:
                    _last_skipped.append({
                        "symbol": symbol,
                        "reason": "invalid_signal_price",
                    })
                    continue
                valid_signals.append((token, address, symbol, signal_price))

            prices = {}
            if valid_signals and not stop_event.is_set():
                # Compare the live quote against the discovery/signal quote.
                # This is especially important on first entry, when no cached
                # accepted price exists yet. A repeated Birdeye quote cannot
                # override a large disagreement with this reference.
                prices = await get_token_prices_usd(
                    [address for _, address, _, _ in valid_signals],
                    reference_prices={
                        address: signal_price
                        for _, address, _, signal_price in valid_signals
                    },
                )

            for token, address, symbol, signal_price in valid_signals:
                if stop_event.is_set():
                    break

                live_price = prices.get(address)
                if live_price is None or live_price <= 0:
                    _last_skipped.append({
                        "symbol": symbol,
                        "reason": "live_price_unavailable",
                    })
                    continue

                deviation = abs(live_price / signal_price - 1) * 100
                if deviation > MAX_ENTRY_PRICE_DEVIATION_PCT:
                    _last_skipped.append({
                        "symbol": symbol,
                        "reason": "signal_live_price_deviation",
                        "deviation_pct": round(deviation, 2),
                    })
                    continue

                result = open_paper_position(
                    token_address=address,
                    token_symbol=symbol,
                    amount_usd=PAPER_BUY_USD,
                    price_usd=live_price,
                    max_open_positions=MAX_OPEN_POSITIONS,
                    metadata={
                        "pair_address": token.get("pool_address") or token.get("pair_address"),
                        "liquidity_usd": token.get("liquidity_usd"),
                        "volume_24h_usd": token.get("volume_24h_usd"),
                        "buy_sell_ratio": token.get(
                            "buy_sell_ratio", token.get("buys_to_sells_ratio")
                        ),
                        "strategy": "experimental_candidate_filter_not_validated",
                        "signal_price_usd": signal_price,
                        "entry_price_deviation_pct": round(deviation, 4),
                        "take_profit_pct": 10,
                        "stop_loss_pct": -5,
                        "copyfomo_gate_enabled": COPYFOMO_GATE_ENABLED,
                        "copyfomo_strategy_profile": (
                            "grok_rugcheck_gate_paper_only"
                            if COPYFOMO_GATE_ENABLED
                            else "legacy_candidate_filter"
                        ),
                    },
                )
                if result.get("skipped"):
                    _last_skipped.append({
                        "symbol": symbol,
                        "reason": result.get("reason"),
                    })

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.exception("Paper bot scan cycle failed: %s", _last_error)
        finally:
            # Timestamp is recorded even when a cycle raises an exception.
            _last_run = _utc_now()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def start_bot():
    global _task, _stop_event
    if _task is not None and not _task.done():
        return bot_status()
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop(_stop_event), name="paper-bot-loop")
    return bot_status()


async def stop_bot():
    global _task, _stop_event
    task = _task
    event = _stop_event
    if event is not None:
        event.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _task = None
    _stop_event = None
    return bot_status()


def bot_status():
    return {
        "mode": "paper",
        "running": _task is not None and not _task.done(),
        "real_trading_enabled": False,
        "poll_interval_seconds": POLL_SECONDS,
        "paper_amount_per_position_usd": PAPER_BUY_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_entry_price_deviation_pct": MAX_ENTRY_PRICE_DEVIATION_PCT,
        "filters": {
            "min_liquidity_usd": MIN_LIQUIDITY_USD,
            "min_volume_24h_usd": MIN_VOLUME_24H_USD,
            "min_buys_sells_ratio": MIN_BUYS_SELLS_RATIO,
            "min_trades_24h": MIN_TRADES_24H,
            "max_absolute_price_change_24h_pct": MAX_ABSOLUTE_PRICE_CHANGE_24H_PCT,
            "max_negative_price_change_24h_pct": MAX_NEGATIVE_PRICE_CHANGE_24H_PCT,
        },
        "filter_notes": {
            "price_change_filter_source": "signal_engine.py",
            "max_absolute_price_change_24h_pct_is_effective": True,
            "max_negative_price_change_24h_pct_is_separate_effective_filter": True,
            "note": (
                "signal_engine.py enforces both the absolute 24h price-change "
                "limit and the separate maximum-negative-change limit. "
                "Tokens below the negative threshold are rejected."
            ),
        },
        "last_run_utc": _last_run,
        "last_error": _last_error,
        "last_price_update": _last_price_update,
        "last_scan": _last_scan,
        "last_skipped": _last_skipped[-10:],
        "copyfomo_gate_enabled": COPYFOMO_GATE_ENABLED,
        "copyfomo_checks": _last_copyfomo_checks[-MAX_GROK_CHECKS_PER_SCAN:],
        "price_diagnostics": get_price_diagnostics(),
        "warning": (
            "Experimental paper simulation only. No real orders are sent. "
            "Entry quotes are checked against a second provider call; large price "
            "jumps are rejected. Take-profit +10% and stop-loss -5% are "
            "unvalidated simulation rules."
        ),
    }
