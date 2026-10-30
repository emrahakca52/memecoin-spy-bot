from statistics import mean

def ema(values, period):
    if len(values) < period:
        raise ValueError("EMA için yetersiz mum")
    alpha = 2 / (period + 1)
    current = mean(values[:period])
    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
    return current

def rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]
    avg_gain, avg_loss = mean(gains), mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)

def macd_histogram(values):
    fast, slow = ema(values, 12), ema(values, 26)
    # Signal line is approximated from the recent MACD series.
    series = []
    for i in range(35, len(values) + 1):
        part = values[:i]
        series.append(ema(part, 12) - ema(part, 26))
    signal = ema(series, 9) if len(series) >= 9 else series[-1]
    return fast - slow - signal

def build_signal(data):
    closes = {tf: [c["close"] for c in rows] for tf, rows in data.items()}
    c1, c5, c15 = closes["1min"], closes["5min"], closes["15min"]
    trend_up = ema(c15, 20) > ema(c15, 50)
    trend_down = ema(c15, 20) < ema(c15, 50)
    confirm_up = ema(c5, 20) > ema(c5, 50) and macd_histogram(c5) > 0
    confirm_down = ema(c5, 20) < ema(c5, 50) and macd_histogram(c5) < 0
    momentum = rsi(c1)
    last = c1[-1]
    if trend_up and confirm_up and 50 <= momentum <= 68:
        direction, reason = "BUY", "M15 yükseliş trendi, M5 teyidi ve M1 RSI uyumlu"
    elif trend_down and confirm_down and 32 <= momentum <= 50:
        direction, reason = "SELL", "M15 düşüş trendi, M5 teyidi ve M1 RSI uyumlu"
    else:
        direction, reason = "WAIT", "Zaman dilimleri aynı yönde yeterli teyit vermiyor"
    recent = data["5min"][-20:]
    support = min(x["low"] for x in recent)
    resistance = max(x["high"] for x in recent)
    # ATR-like range proxy for conservative demo levels.
    ranges = [x["high"] - x["low"] for x in data["5min"][-14:]]
    volatility = mean(ranges)
    if direction == "BUY":
        sl, tp = last - 1.5 * volatility, last + 2.0 * volatility
    elif direction == "SELL":
        sl, tp = last + 1.5 * volatility, last - 2.0 * volatility
    else:
        sl = tp = None
    return {
        "direction": direction, "entry": round(last, 3),
        "stop_loss": round(sl, 3) if sl is not None else None,
        "take_profit": round(tp, 3) if tp is not None else None,
        "rsi_m1": round(momentum, 2),
        "trend_m15": "UP" if trend_up else "DOWN" if trend_down else "FLAT",
        "support_m5": round(support, 3), "resistance_m5": round(resistance, 3),
        "reason": reason, "risk_notice": "Eğitsel teknik sinyal; broker fiyatı ve spread farklı olabilir. Emir göndermez."
    }
