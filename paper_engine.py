from datetime import datetime, timezone
from threading import Lock

_lock = Lock()
_trades = []
_next_id = 1

def paper_status():
    with _lock:
        return {
            "mode": "paper",
            "real_trading_enabled": False,
            "simulated_trade_count": len(_trades),
            "note": "No wallet is connected and no real orders are sent."
        }

def list_paper_trades():
    with _lock:
        return list(_trades)

def record_paper_trade(token_address, side, amount_usd, price_usd):
    global _next_id
    side = side.strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'.")
    if not token_address.strip():
        raise ValueError("token_address cannot be empty.")
    if amount_usd <= 0 or price_usd <= 0:
        raise ValueError("amount_usd and price_usd must be greater than zero.")
    with _lock:
        trade = {
            "id": _next_id,
            "token_address": token_address.strip(),
            "side": side,
            "amount_usd": round(amount_usd, 2),
            "price_usd": price_usd,
            "simulated_token_quantity": round(amount_usd / price_usd, 10),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "paper"
        }
        _next_id += 1
        _trades.append(trade)
        return trade
