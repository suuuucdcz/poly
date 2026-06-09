"""Modèles Pydantic pour les requêtes de l'API FastAPI."""

from pydantic import BaseModel


class ResetRequest(BaseModel):
    budget: float


class TradeRequest(BaseModel):
    market_id: str
    token_id: str
    action: str  # BUY ou SELL
    outcome: str
    amount_usdc: float


class BotConfigRequest(BaseModel):
    strategy: str
    tick_interval: int
    max_markets_to_scan: int
