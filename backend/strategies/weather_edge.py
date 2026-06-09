"""Stratégie 5 — Weather Edge (marchés « Highest temperature » de Polymarket).

Principe : la température max du jour d'une station est **prévisible** via les
modèles publics. On calcule une distribution de probabilité (ensemble Open-Meteo
GFS+ICON+ECMWF), on en déduit la proba de chaque bucket du marché, on la compare
au prix Polymarket, et on **achète les tranches sous-évaluées** (jamais les
favoris chers — la leçon de la crypto). Beaucoup de villes/dates indépendantes →
la variance s'annule (la vraie version de « multiplier les petits gains »).

Réutilise tout le moteur : calibration (corrige le biais grille↔station),
sizing Kelly, journal `bet_log`, persistance. Règlement par le tick principal
(les buckets se règlent le lendemain). 100 % paper.
"""

import time

from backend import config, db
from backend.calibration import Calibrator
from backend.cities import resolve_city
from backend.weather_feed import WeatherFeed
from backend.weather_model import bucket_probabilities, ensemble_summary, parse_bucket
from backend.strategies.base import Strategy


class WeatherEdgeStrategy(Strategy):
    name = "weather"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self.feed = WeatherFeed(cfg)
        self._last_refit = 0.0
        self.calibrator = Calibrator(
            n_bins=cfg.CRYPTO_CALIBRATION_BINS,
            prior_strength=cfg.CRYPTO_CALIBRATION_PRIOR,
            min_samples=cfg.CRYPTO_CALIBRATION_MIN_SAMPLES,
            min_losses=cfg.CRYPTO_CALIBRATION_MIN_LOSSES,
        )

    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        now = time.time()

        # Calibration (sur les paris MÉTÉO uniquement)
        if now - self._last_refit > cfg.CRYPTO_CALIBRATION_REFIT_SEC:
            self._last_refit = now
            try:
                self.calibrator.fit(db.get_bet_samples(3000, kind="weather"))
            except Exception:
                pass

        try:
            events = await ctx.client.find_temperature_events()
        except Exception as e:
            ctx.log(f"WEATHER: erreur récupération marchés: {e}", "WARNING")
            return

        balance = db.get_portfolio()["balance"]
        signals = []

        for ev in events:
            city, coords = resolve_city(ev["title"])
            if not coords:
                continue   # ville hors registre cities.py
            lat, lon = coords
            buckets = [b for b in ev["buckets"] if not b["closed"]]
            if len(buckets) < 3:
                continue
            unit = "F" if any("°F" in (b["label"] or "") for b in buckets) else "C"
            om_unit = "fahrenheit" if unit == "F" else "celsius"

            members = await self.feed.ensemble_max(lat, lon, om_unit)
            if not members:
                continue
            realized = await self.feed.realized_max_today(lat, lon, om_unit)

            parsed = [(b["label"], parse_bucket(b["label"])) for b in buckets]
            probs = bucket_probabilities(members, parsed, realized)
            summ = ensemble_summary(members)

            sig = {
                "city": city, "title": ev["title"], "unit": unit,
                "median": round(summ["median"], 1) if summ else None,
                "std": round(summ["std"], 2) if summ else None,
                "realized": round(realized, 1) if realized is not None else None,
                "n": len(members), "buckets": [], "action": None,
            }

            candidates = []
            for b in buckets:
                p = probs.get(b["label"])
                if p is None:
                    continue
                price = b["yes_price"]
                p_cal = self.calibrator.predict(p)
                fee = ctx.risk.taker_fee(price)
                edge = p_cal - (price + fee)
                sig["buckets"].append({
                    "label": b["label"], "p": round(p, 3), "p_cal": round(p_cal, 3),
                    "price": round(price, 3), "edge": round(edge, 3),
                })
                held = db.get_position(b["yes_token"])
                already = bool(held and held["shares"] > 0)
                if (cfg.WEATHER_MIN_BUY_PRICE < price < cfg.WEATHER_MAX_BUY_PRICE
                        and edge > cfg.WEATHER_EDGE_THRESHOLD and not already):
                    candidates.append((b, p, p_cal, price, edge))

            # On garde les meilleures tranches par edge (diversification)
            candidates.sort(key=lambda c: -c[4])
            for (b, p, p_cal, price, edge) in candidates[: cfg.WEATHER_MAX_BUCKETS_PER_MARKET]:
                f = (p_cal - price) / (1.0 - price) if price < 1.0 else 0.0   # Kelly binaire
                if f <= 0:
                    continue
                stake = cfg.WEATHER_KELLY_FRACTION * f * portfolio_value
                stake = min(stake, cfg.WEATHER_STAKE_MAX_USDC,
                            ctx.risk.max_trade_usdc(balance, portfolio_value))
                if stake < cfg.WEATHER_STAKE_MIN_USDC:
                    continue
                shares = round(stake / price, 1)
                if shares < 1.0:
                    continue
                cost = shares * price
                balance -= cost
                db.update_balance(balance)
                q = f"{ev['title']} {b['label']}"
                ctx.accumulate_position(b["yes_token"], b["market_id"], q, b["yes_outcome"], shares, price, b["end_date"])
                db.add_trade(b["market_id"], q, b["yes_token"], "BUY", b["yes_outcome"], shares, price)
                db.log_bet(b["yes_token"], city, b["label"], b["yes_outcome"], True, price, p, edge,
                           0.0, 0, 0.0, (realized or 0.0), shares, cost, kind="weather")
                sig["action"] = f"BUY {b['label']}"
                ctx.log(
                    f"WEATHER {city} {unit} | {b['label']} | P={p:.2f}->{p_cal:.2f} prix={price:.2f} "
                    f"edge={edge:+.2f} | {shares} parts {cost:.2f}$ "
                    f"(méd {sig['median']}{unit} réalisé {sig['realized']})",
                    "SUCCESS",
                )

            sig["buckets"].sort(key=lambda x: -x["p"])
            signals.append(sig)

        if ctx.crypto_state is not None:
            ctx.crypto_state["weather"] = signals[: cfg.WEATHER_SIGNALS_MAX]
            ctx.crypto_state["updated_at"] = now
            stats = db.get_bet_stats(kind="weather")
            ctx.crypto_state["learning"] = {
                "calibrated": self.calibrator.active,
                "samples": stats["settled"],
                "wins": stats["wins"],
                "pnl": round(stats["pnl"], 2),
            }
