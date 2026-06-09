"""Gestion du risque et calcul des frais Polymarket.

Extrait à l'identique de l'ancien `bot.py` (méthodes `_taker_fee`,
`_total_arb_fees`, `_market_closing_too_soon`, `_get_max_trade_usdc`,
`_spread_too_wide`) pour préserver le comportement des stratégies existantes.
"""

from datetime import datetime, timezone

from backend import config, db


class RiskManager:
    def __init__(self, cfg=config):
        self.fee_rate = cfg.FEE_RATE
        self.fee_rebate = cfg.FEE_REBATE
        self.max_exposure_pct = cfg.MAX_EXPOSURE_PCT
        self.max_position_pct = cfg.MAX_POSITION_PCT
        self.min_market_hours = cfg.MIN_MARKET_HOURS
        self.max_spread_pct = cfg.MAX_SPREAD_PCT
        self.max_trade_cap = cfg.MAX_TRADE_USDC_CAP

    # ----- Frais -----
    def taker_fee(self, price):
        """Frais taker Polymarket : rate * price * (1 - price) * (1 - rebate)."""
        return self.fee_rate * price * (1.0 - price) * (1.0 - self.fee_rebate)

    def total_arb_fees(self, prices):
        """Somme des frais taker sur une liste de prix d'achat."""
        return sum(self.taker_fee(p) for p in prices)

    # ----- Filtres -----
    def market_closing_too_soon(self, end_date_str):
        """Vrai si le marché clôture dans moins de min_market_hours."""
        if not end_date_str:
            return False  # Pas de date de fin = pas de filtre
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_left = (end_dt - now).total_seconds() / 3600
            return hours_left < self.min_market_hours
        except Exception:
            return False

    def max_trade_usdc(self, balance, portfolio_value):
        """Taille max de trade respectant les limites d'exposition."""
        positions = db.get_positions()
        positions_value = sum(p["shares"] * p["current_price"] for p in positions)
        current_exposure = positions_value / portfolio_value if portfolio_value > 0 else 0

        remaining_exposure = max(0, self.max_exposure_pct - current_exposure)
        max_by_exposure = remaining_exposure * portfolio_value
        max_by_position = self.max_position_pct * portfolio_value
        max_by_balance = balance * 0.25

        return min(max_by_exposure, max_by_position, max_by_balance, self.max_trade_cap)

    def spread_too_wide(self, book):
        """Vrai si le spread bid-ask est trop large (> max_spread_pct)."""
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        if not asks or not bids:
            return True
        best_ask = min(float(a["price"]) for a in asks)
        best_bid = max(float(b["price"]) for b in bids)
        mid = (best_ask + best_bid) / 2
        if mid <= 0:
            return True
        spread = (best_ask - best_bid) / mid
        return spread > self.max_spread_pct
