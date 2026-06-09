"""Modèles Pydantic pour les requêtes de l'API FastAPI."""

from pydantic import BaseModel


class ResetRequest(BaseModel):
    budget: float


class SellRequest(BaseModel):
    token_id: str


class BotConfigRequest(BaseModel):
    tick_interval: int
