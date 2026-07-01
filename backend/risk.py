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
        """Frais taker Polymarket PAR PART : feeRate * price * (1 - price).

        Formule officielle : frais = C · feeRate · p · (1−p). Catégorie « weather »
        -> feeRate = 0.05 (confirmé sur la doc + `taker_base_fee` du CLOB). La cloche
        p(1−p) rend les frais maximaux à 0.50 et faibles aux extrêmes (ex. 0.05·0.9·0.1
        ≈ 0,45¢/part à 0.90). Le « rebate » précédent (−25 %) était fictif : SUPPRIMÉ,
        il sous-estimait les frais réels d'un quart.
        """
        return self.fee_rate * price * (1.0 - price)

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
