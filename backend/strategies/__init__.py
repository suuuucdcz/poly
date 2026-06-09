"""Stratégies de trading du bot.

Chaque stratégie implémente `Strategy.run(ctx, markets, balance, portfolio_value)`
et partage l'état/les services via un `TradeContext`.
"""

from backend.strategies.arbitrage import ArbitrageStrategy
from backend.strategies.base import Strategy, TradeContext
from backend.strategies.crypto_direction import CryptoDirectionStrategy
from backend.strategies.momentum import MomentumStrategy
from backend.strategies.value import ValueStrategy
from backend.strategies.weather_edge import WeatherEdgeStrategy

__all__ = [
    "Strategy",
    "TradeContext",
    "ArbitrageStrategy",
    "MomentumStrategy",
    "ValueStrategy",
    "CryptoDirectionStrategy",
    "WeatherEdgeStrategy",
]
