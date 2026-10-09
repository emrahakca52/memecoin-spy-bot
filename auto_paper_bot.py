import asyncio
from datetime import datetime, timezone

import httpx
from signal_engine import get_signal_candidates
from paper_engine import open_paper_position, update_paper_prices, bot_snapshot

POLL_SECONDS = 180
PAPER_BUY_USD = 10.0
MAX_OPEN_POSITIONS = 5
MIN_LIQUIDITY_USD = 10000
MIN_VOLUME_24H_USD = 20000
MIN_BUYS_SELLS_RATIO = 1.0

_task = None
_stop_event = None
_last_run = None
_last_error = None

async def _loop():
    global _last_run, _last_error
    while not _stop_event.is_set():
        try:
            data = await get_signal_candidates(
                min_liquidity_usd=MIN_LIQUIDITY_USD,
                min_volume_24h_usd=MIN_VOLUME_24H_USD,
                min_buys_sells_ratio=MIN_BUYS_SELLS_RATIO,
                limit=20
            )
            candidates = data.get("signals", [])
            # Paper-only heuristic: enter the strongest candidates by liquidity/volume.
            # This is not a proven strategy and is intentionally not connected to a wallet.
            for token in candidates:
                if _stop_event.is_set():
                    break
                price = float(token.get("price_usd") or 0)
                address = token.get("token_address")
                if not address or price <= 0:
                    continue
                result = open_paper_position(
                    token_address=address,
                    token_symbol=token.get("token_symbol", ""),
                    amount_usd=PAPER_BUY_USD,
                    price_usd=price,
                    max_open_positions=MAX_OPEN_POSITIONS,
                    metadata={
                        "liquidity_usd": token.get("liquidity_usd"),
                        "volume_24h_usd": token.get("volume_24h_usd"),
                        "buys_to_sells_ratio": token.get("buys_to_sells_ratio"),
                        "strategy": "simple_candidate_filter_not_validated"
                    }
                )
                # result is ignored if already open or capacity reached.
            await update_paper_prices()
            _last_error = None
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
        except (asyncio.TimeoutError, Exception):
            _task.cancel()
    _task = None
    _stop_event = None
    return bot_status()

def bot_status():
    running = _task is not None and not _task.done()
    return {
        "mode": "paper",
        "running": running,
        "real_trading_enabled": False,
        "poll_interval_seconds": POLL_SECONDS,
        "paper_amount_per_position_usd": PAPER_BUY_USD,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "filters": {
            "min_liquidity_usd": MIN_LIQUIDITY_USD,
            "min_volume_24h_usd": MIN_VOLUME_24H_USD,
            "min_buys_sells_ratio": MIN_BUYS_SELLS_RATIO
        },
        "last_run_utc": _last_run,
        "last_error": _last_error,
        "warning": "Experimental paper simulation only. It does not send real orders and does not prove profitability."
    }
