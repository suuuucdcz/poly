"""Stratégies du bot — météo uniquement."""

from backend.strategies.base import Strategy, TradeContext
from backend.strategies.weather_edge import WeatherEdgeStrategy
from backend.strategies.weather_convergence import WeatherConvergenceStrategy
from backend.strategies.weather_negrisk import NegRiskArbStrategy
from backend.strategies.weather_sweep import ResolutionSweepStrategy

__all__ = ["Strategy", "TradeContext", "WeatherEdgeStrategy",
           "WeatherConvergenceStrategy", "NegRiskArbStrategy",
           "ResolutionSweepStrategy"]
