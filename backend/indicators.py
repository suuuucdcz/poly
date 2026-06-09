"""Indicateurs techniques purs (sans état, sans dépendance externe)."""

import math


def sma(values, period):
    """Moyenne mobile simple sur les `period` dernières valeurs."""
    window = values[-period:]
    if not window:
        return 0.0
    return sum(window) / len(window)


def ema(values, period):
    """Moyenne mobile exponentielle (seed = SMA sur la première fenêtre)."""
    if not values:
        return 0.0
    if len(values) < period:
        return sum(values) / len(values)
    k = 2.0 / (period + 1)
    ema_val = sum(values[:period]) / period  # seed avec une SMA
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def rsi(values, period=14):
    """Relative Strength Index (0-100). Neutre (50) si données insuffisantes."""
    if len(values) < period + 1:
        return 50.0
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    recent = deltas[-period:]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0001
    avg_loss = sum(losses) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def normal_cdf(x):
    """Fonction de répartition de la loi normale centrée réduite Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
