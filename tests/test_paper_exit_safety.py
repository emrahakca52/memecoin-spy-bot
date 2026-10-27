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
