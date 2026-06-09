"""Frais Polymarket et dimensionnement des mises."""

from backend import config, db


class RiskManager:
    def __init__(self, cfg=config):
        self.fee_rate = cfg.FEE_RATE
        self.fee_rebate = cfg.FEE_REBATE
        self.max_exposure_pct = cfg.MAX_EXPOSURE_PCT
        self.max_position_pct = cfg.MAX_POSITION_PCT
        self.max_trade_cap = cfg.MAX_TRADE_USDC_CAP

    def taker_fee(self, price):
        """Frais taker : rate * price * (1 - price) * (1 - rebate).

        Les marchés météo sont aujourd'hui sans frais ; on garde cette retenue
        comme marge de sécurité conservatrice (exige un edge un peu plus grand).
        """
        return self.fee_rate * price * (1.0 - price) * (1.0 - self.fee_rebate)

    def max_trade_usdc(self, balance, portfolio_value):
        """Taille max d'une mise, en respectant les limites d'exposition."""
        positions = db.get_positions()
        positions_value = sum(p["shares"] * p["current_price"] for p in positions)
        current_exposure = positions_value / portfolio_value if portfolio_value > 0 else 0

        remaining_exposure = max(0, self.max_exposure_pct - current_exposure)
        max_by_exposure = remaining_exposure * portfolio_value
        max_by_position = self.max_position_pct * portfolio_value
        max_by_balance = balance * 0.25

        return min(max_by_exposure, max_by_position, max_by_balance, self.max_trade_cap)
