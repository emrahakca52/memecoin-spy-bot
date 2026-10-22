"""Pure trader qualification rules; requires verified trade-history metrics."""
from __future__ import annotations
from config import (TRADER_MIN_CLOSED_TRADES, TRADER_MIN_WIN_RATE,
                    TRADER_MAX_SINGLE_TOKEN_PORTFOLIO_SHARE, TRADER_MIN_7D_PNL_USD)

def evaluate_trader(metrics: dict) -> dict:
    """Metrics must come from a trusted trade-history/indexing provider.

    Raw Solana transaction counts are not enough to infer realized PnL/win rate.
    """
    required = ("closed_trades", "win_rate", "max_single_token_portfolio_share", "pnl_7d_usd")
    missing = [key for key in required if metrics.get(key) is None]
    reasons = []
    if not missing:
        try:
            if int(metrics["closed_trades"]) < TRADER_MIN_CLOSED_TRADES: reasons.append("too_few_closed_trades")
            if float(metrics["win_rate"]) < TRADER_MIN_WIN_RATE: reasons.append("win_rate_below_minimum")
            if float(metrics["max_single_token_portfolio_share"]) > TRADER_MAX_SINGLE_TOKEN_PORTFOLIO_SHARE: reasons.append("portfolio_concentration_too_high")
            if float(metrics["pnl_7d_usd"]) < TRADER_MIN_7D_PNL_USD: reasons.append("negative_7d_pnl")
        except (TypeError, ValueError):
            missing.append("invalid_metric_format")
    return {"qualified": not missing and not reasons, "missing_data": missing,
            "rejection_reasons": reasons, "rules": {
                "min_closed_trades": TRADER_MIN_CLOSED_TRADES,
                "min_win_rate": TRADER_MIN_WIN_RATE,
                "max_single_token_portfolio_share": TRADER_MAX_SINGLE_TOKEN_PORTFOLIO_SHARE,
                "min_7d_pnl_usd": TRADER_MIN_7D_PNL_USD}}
