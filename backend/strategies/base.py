"""Interface de stratégie et contexte d'exécution partagé."""

from backend import db


class TradeContext:
    """Porte les services et l'état partagé fournis à chaque stratégie pour un tick.

    - `client`  : PolymarketClient (accès API)
    - `risk`    : RiskManager (frais, filtres, dimensionnement)
    - `feed`    : CryptoPriceFeed (utilisé par la stratégie crypto)
    - `log`     : fonction de journalisation du bot
    - `price_histories`, `peak_prices` : état mutable partagé (porté par le bot)
    - `max_markets_to_scan` : limite de scan (réglable à chaud)
    - `portfolio_value`     : snapshot de la valeur totale pour le dimensionnement
    - `crypto_state`        : dict où la stratégie crypto publie ses derniers signaux
    """

    def __init__(
        self,
        client,
        risk,
        feed,
        log,
        price_histories,
        peak_prices,
        max_markets_to_scan,
        portfolio_value,
        crypto_state=None,
    ):
        self.client = client
        self.risk = risk
        self.feed = feed
        self.log = log
        self.price_histories = price_histories
        self.peak_prices = peak_prices
        self.max_markets_to_scan = max_markets_to_scan
        self.portfolio_value = portfolio_value
        self.crypto_state = crypto_state

    def accumulate_position(
        self,
        token_id,
        market_id,
        question,
        outcome,
        new_shares,
        new_price,
        end_date=None,
        payout_multiplier=None,
    ):
        """Accumule des parts avec un prix moyen pondéré (PRU).

        Comportement identique à l'ancien `TradingBot._accumulate_position` :
        initialise le pic de prix (trailing stop) lors de la création d'une position.
        """
        existing = db.get_position(token_id)
        if existing and existing["shares"] > 0.0001:
            old_shares = existing["shares"]
            old_avg = existing["avg_price"]
            total_shares = old_shares + new_shares
            weighted_avg = ((old_shares * old_avg) + (new_shares * new_price)) / total_shares
            if end_date is None:
                end_date = existing.get("end_date")
            db.save_position(
                token_id, market_id, question, outcome,
                total_shares, weighted_avg, new_price, end_date, payout_multiplier,
            )
            return total_shares, weighted_avg
        else:
            db.save_position(
                token_id, market_id, question, outcome,
                new_shares, new_price, new_price, end_date, payout_multiplier,
            )
            self.peak_prices[token_id] = new_price
            return new_shares, new_price


class Strategy:
    """Interface commune. Les sous-classes implémentent `run`."""

    name = "base"

    async def run(self, ctx, markets, balance, portfolio_value):
        raise NotImplementedError
