import asyncio
from datetime import datetime, timezone

from signal_engine import get_signal_candidates
from paper_engine import (
    get_token_price_usd,
    open_paper_position,
    update_paper_prices,
)

POLL_SECONDS = 180
PAPER_BUY_USD = 10.0
MAX_OPEN_POSITIONS = 5
MIN_LIQUIDITY_USD = 10000
MIN_VOLUME_24H_USD = 20000
MIN_BUYS_SELLS_RATIO = 1.0
MAX_ENTRY_PRICE_DEVIATION_PCT = 10.0

_task = None
_stop_event = None
_last_run = None
_last_error = None
_last_price_update = None
_last_scan = None
_last_skipped = []


async def _loop():
    global _last_run, _last_error, _last_price_update, _last_scan, _last_skipped
    while not _stop_event.is_set():
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
            _last_scan = {
                "provider": data.get("provider"),
                "checked": data.get("checked", 0),
                "candidate_count": data.get("candidate_count", len(data.get("signals", []))),
                "note": data.get("note"),
            }

            for token in data.get("signals", []):
                if _stop_event.is_set():
                    break
                address = token.get("token_address")
                try:
                    signal_price = float(token.get("price_usd") or 0)
                except (TypeError, ValueError):
                    continue
                if not address or signal_price <= 0:
                    _last_skipped.append({"symbol": token.get("token_symbol"), "reason": "invalid_signal_price"})
                    continue

                # Never open a position from the scanner quote alone.
                live_price = await get_token_price_usd(address)
                if live_price is None or live_price <= 0:
                    _last_skipped.append({"symbol": token.get("token_symbol"), "reason": "live_price_unavailable"})
                    continue

                deviation = abs(live_price / signal_price - 1) * 100
                if deviation > MAX_ENTRY_PRICE_DEVIATION_PCT:
                    _last_skipped.append({
                        "symbol": token.get("token_symbol"),
                        "reason": "signal_live_price_deviation",
                        "deviation_pct": round(deviation, 2),
                    })
                    continue

                open_paper_position(
                    token_address=address,
                    token_symbol=token.get("token_symbol", ""),
                    amount_usd=PAPER_BUY_USD,
                    price_usd=live_price,
                    max_open_positions=MAX_OPEN_POSITIONS,
                    metadata={
                        "pair_address": token.get("pair_address"),
                        "dex_id": token.get("dex_id"),
                        "liquidity_usd": token.get("liquidity_usd"),
                        "volume_24h_usd": token.get("volume_24h_usd"),
                        "buys_to_sells_ratio": token.get("buys_to_sells_ratio"),
                        "strategy": "experimental_candidate_filter_not_validated",
                        "signal_price_usd": signal_price,
                        "entry_price_deviation_pct": round(deviation, 4),
                        "take_profit_pct": 10,
                        "stop_loss_pct": -5,
                    },
                )
            _last_run = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            _last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def start_bot():
    global _task, _stop_event
    if _task is not None and not _task.done():
        return bot_status()
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop())
    return bot_status()


async def stop_bot():
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except Exception:
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
        "paper_amount_per_position_usd": PAPER_BUY_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_entry_price_deviation_pct": MAX_ENTRY_PRICE_DEVIATION_PCT,
        "filters": {
            "min_liquidity_usd": MIN_LIQUIDITY_USD,
            "min_volume_24h_usd": MIN_VOLUME_24H_USD,
            "min_buys_sells_ratio": MIN_BUYS_SELLS_RATIO,
        },
        "last_run_utc": _last_run,
        "last_error": _last_error,
        "last_price_update": _last_price_update,
        "last_scan": _last_scan,
        "last_skipped": _last_skipped[-10:],
        "warning": (
            "Experimental paper simulation only. No real orders are sent. "
            "Entry quotes are checked against a second provider call; large price jumps are rejected. "
            "Take-profit +10% and stop-loss -5% are unvalidated simulation rules."
        ),
    }
