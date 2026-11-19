import pandas as pd
from app.scanner import _analyze


def test_unusual_volume_and_price_move_are_explained():
    dates = pd.date_range("2025-01-01", periods=30, freq="B")
    close = [100.0] * 29 + [108.0]
    volume = [1000.0] * 29 + [5000.0]
    frame = pd.DataFrame({"Close": close, "Volume": volume}, index=dates)
    result = _analyze("TEST", frame)
    assert result is not None
    assert result.score >= 50
    assert result.volume_ratio == 5.0
    assert any("Hacim" in reason for reason in result.reasons)


def test_insufficient_history_is_ignored():
    dates = pd.date_range("2025-01-01", periods=10, freq="B")
    frame = pd.DataFrame({"Close": [10.0] * 10, "Volume": [100.0] * 10}, index=dates)
    assert _analyze("TEST", frame) is None
