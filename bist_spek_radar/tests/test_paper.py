import pytest
from app import paper


def test_buy_sell_and_cash(monkeypatch, tmp_path):
    monkeypatch.setattr(paper, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(paper, "STARTING_CASH", 10000.0)
    bought = paper.trade("THYAO", "BUY", 10, 100)
    assert bought["cash"] == 9000
    sold = paper.trade("THYAO", "SELL", 4, 110)
    assert sold["cash"] == 9440
    assert paper.status()["positions"][0]["quantity"] == 6


def test_reject_overspend(monkeypatch, tmp_path):
    monkeypatch.setattr(paper, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(paper, "STARTING_CASH", 1000.0)
    with pytest.raises(ValueError):
        paper.trade("THYAO", "BUY", 11, 100)


def test_reject_oversell(monkeypatch, tmp_path):
    monkeypatch.setattr(paper, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(paper, "STARTING_CASH", 1000.0)
    with pytest.raises(ValueError):
        paper.trade("THYAO", "SELL", 1, 100)
