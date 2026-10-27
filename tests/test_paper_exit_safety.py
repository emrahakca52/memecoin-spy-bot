"""Regression tests for the final paper-position close safety boundary."""

import paper_engine


def _reset(monkeypatch, position):
    monkeypatch.setattr(paper_engine, "_paper_trades", [])
    monkeypatch.setattr(paper_engine, "_open_positions", {"token": position})
    monkeypatch.setattr(paper_engine, "_realized_pnl_usd", 0.0)
    monkeypatch.setattr(paper_engine, "_save_paper_state", lambda: True)


def test_extreme_stop_loss_quote_cannot_close_position(monkeypatch):
    position = {
        "token_address": "token",
        "token_symbol": "TEST",
        "entry_price_usd": 1.0,
        "current_price_usd": 0.98,
        "invested_usd": 10.0,
        "quantity": 10.0,
    }
    _reset(monkeypatch, position)

    closed = paper_engine._close_position(
        "token", position, 0.000001, "stop_loss_5pct", -99.9999
    )

    assert closed is False
    assert "token" in paper_engine._open_positions
    assert paper_engine._paper_trades == []
    assert position["price_mark_status"] == "unverified_extreme_quote"
    assert "close refused" in position["price_safety_warning"]


def test_extreme_take_profit_quote_cannot_close_position(monkeypatch):
    position = {
        "token_address": "token",
        "token_symbol": "TEST",
        "entry_price_usd": 1.0,
        "current_price_usd": 1.0,
        "invested_usd": 10.0,
        "quantity": 10.0,
    }
    _reset(monkeypatch, position)

    closed = paper_engine._close_position(
        "token", position, 100.0, "take_profit_10pct", 9900.0
    )

    assert closed is False
    assert "token" in paper_engine._open_positions
    assert paper_engine._paper_trades == []
    assert position["price_mark_status"] == "unverified_extreme_quote"


def test_plausible_stop_loss_can_still_close_position(monkeypatch):
    position = {
        "token_address": "token",
        "token_symbol": "TEST",
        "entry_price_usd": 1.0,
        "current_price_usd": 1.0,
        "invested_usd": 10.0,
        "quantity": 10.0,
    }
    _reset(monkeypatch, position)

    closed = paper_engine._close_position(
        "token", position, 0.94, "stop_loss_5pct", -6.0
    )

    assert closed is True
    assert "token" not in paper_engine._open_positions
    assert len(paper_engine._paper_trades) == 1
    assert paper_engine._paper_trades[0]["realized_pnl_usd"] == -0.6



def test_status_withholds_unverified_quote_from_marked_value_and_pnl(monkeypatch):
    verified = {
        "token_address": "verified",
        "token_symbol": "SAFE",
        "entry_price_usd": 1.0,
        "current_price_usd": 0.99,
        "invested_usd": 10.0,
        "quantity": 10.0,
        "unrealized_pnl_usd": -0.1,
    }
    unverified = {
        "token_address": "unverified",
        "token_symbol": "XTO",
        "entry_price_usd": 1.0,
        "current_price_usd": 0.0000025,
        "invested_usd": 10.0,
        "quantity": 10.0,
        "unrealized_pnl_usd": -9.999975,
        "price_mark_status": "unverified_extreme_quote",
        "last_suspect_price_usd": 0.0000025,
        "price_safety_warning": "suspect quote",
    }
    monkeypatch.setattr(
        paper_engine, "_open_positions",
        {"verified": verified, "unverified": unverified},
    )
    monkeypatch.setattr(paper_engine, "_paper_trades", [])
    monkeypatch.setattr(paper_engine, "_realized_pnl_usd", 0.0)

    status = paper_engine.get_paper_status()
    by_symbol = {p["token_symbol"]: p for p in status["open_positions"]}

    assert by_symbol["XTO"]["current_price_usd"] is None
    assert by_symbol["XTO"]["unrealized_pnl_usd"] is None
    assert by_symbol["XTO"]["last_suspect_price_usd"] == 0.0000025
    assert status["paper_invested_usd"] == 20.0
    assert status["paper_current_value_usd"] == 9.9
    assert status["paper_unrealized_pnl_usd"] == -0.1
    assert status["unverified_position_count"] == 1
    assert status["unverified_position_invested_usd"] == 10.0
    assert status["paper_total_pnl_incomplete"] is True


def test_accepted_normal_quote_clears_previous_unverified_mark(monkeypatch):
    import asyncio
    import time

    position = {
        "token_address": "token",
        "token_symbol": "XTO",
        "entry_price_usd": 1.0,
        "current_price_usd": 1.0,
        "invested_usd": 10.0,
        "quantity": 10.0,
        "unrealized_pnl_usd": 0.0,
        "opened_at": time.time() - 60,
        "price_mark_status": "unverified_extreme_quote",
        "last_suspect_price_usd": 0.0000025,
        "price_safety_warning": "suspect quote",
    }
    monkeypatch.setattr(paper_engine, "_open_positions", {"token": position})
    monkeypatch.setattr(paper_engine, "_save_paper_state", lambda: True)

    async def accepted_prices(addresses, reference_prices=None):
        return {"token": 0.99}

    monkeypatch.setattr(paper_engine, "get_token_prices_usd", accepted_prices)
    result = asyncio.run(paper_engine.update_paper_prices())

    assert result["updated"] == 1
    assert position["current_price_usd"] == 0.99
    assert position["unrealized_pnl_usd"] == -0.1
    assert "price_mark_status" not in position
    assert "price_safety_warning" not in position
    assert position["last_suspect_price_usd"] == 0.0000025
