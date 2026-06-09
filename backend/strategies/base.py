"""Interface de stratégie et contexte d'exécution."""

from backend import db


class TradeContext:
    """Services et état partagés fournis à la stratégie pour un tick.

    - `client`          : PolymarketClient (accès API)
    - `risk`            : RiskManager (frais, dimensionnement)
    - `log`             : fonction de journalisation du bot
    - `portfolio_value` : valeur totale du portefeuille (pour le sizing)
    - `ui_state`        : dict où la stratégie publie ses signaux pour le dashboard
    """

    def __init__(self, client, risk, log, portfolio_value, ui_state=None):
        self.client = client
        self.risk = risk
        self.log = log
        self.portfolio_value = portfolio_value
        self.ui_state = ui_state

    def accumulate_position(self, token_id, market_id, question, outcome,
                            new_shares, new_price, end_date=None):
        return db.accumulate_position(token_id, market_id, question, outcome,
                                      new_shares, new_price, end_date)


class Strategy:
    """Interface commune. Les sous-classes implémentent `run`."""

    name = "base"

    async def run(self, ctx, markets, balance, portfolio_value):
        raise NotImplementedError
