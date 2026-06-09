"""Stratégie Weather Edge (marchés « Highest temperature » de Polymarket).

Principe : la température max du jour d'une station est **prévisible** via les
modèles publics. On calcule une distribution de probabilité (ensemble Open-Meteo
GFS+ICON+ECMWF ≈120 scénarios) **pour la date exacte du marché**, on en déduit
la proba de chaque tranche, on la compare au prix Polymarket, et on **achète les
tranches sous-évaluées** (jamais les favoris chers — la leçon de la crypto).

Garde-fous « béton » :
  - la distribution utilisée est celle de la **date cible du marché** (parsée du
    titre) à la **date locale de la station** — jamais celle d'un autre jour ;
  - le conditionnement sur le max déjà observé n'est appliqué **que si** le
    marché porte sur « aujourd'hui » à la station ;
  - l'achat papier se fait au **vrai meilleur ask du carnet** (pas au dernier
    prix affiché), plafonné par la taille disponible → fills réalistes ;
  - plafond **cumulatif** de tranches détenues par marché (pas par tick) ;
  - sizing Kelly fractionnaire + plafonds de risque + calibration sur les
    résultats réels (kind='weather').

Règlement par le tick principal (marchés résolus le lendemain). 100 % paper.
"""

import time

from backend import config, db
from backend.calibration import Calibrator
from backend.cities import resolve_city
from backend.weather_feed import WeatherFeed
from backend.weather_model import (
    bucket_probabilities,
    ensemble_summary,
    match_date,
    parse_bucket,
    parse_target_date,
)
from backend.strategies.base import Strategy


def _best_ask(book):
    """(meilleur prix ask, taille dispo) ou (None, None)."""
    if not book:
        return None, None
    asks = book.get("asks", [])
    if not asks:
        return None, None
    best = min(asks, key=lambda x: float(x["price"]))
    return float(best["price"]), float(best["size"])


class WeatherEdgeStrategy(Strategy):
    name = "weather"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self.feed = WeatherFeed(cfg)
        self._last_refit = 0.0
        self._warned_cities = set()
        self.calibrator = Calibrator(
            n_bins=cfg.CALIBRATION_BINS,
            prior_strength=cfg.CALIBRATION_PRIOR,
            min_samples=cfg.CALIBRATION_MIN_SAMPLES,
            min_losses=cfg.CALIBRATION_MIN_LOSSES,
        )

    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        now = time.time()

        # Calibration (paris météo réglés uniquement)
        if now - self._last_refit > cfg.CALIBRATION_REFIT_SEC:
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
        positions = {p["token_id"]: p for p in db.get_positions()}
        signals = []

        for ev in events:
            city, coords = resolve_city(ev["title"])
            if not coords:
                if ev["title"] not in self._warned_cities:
                    self._warned_cities.add(ev["title"])
                    ctx.log(f"WEATHER: ville inconnue (à ajouter dans cities.py) : '{ev['title']}'", "WARNING")
                continue
            lat, lon = coords

            buckets = [b for b in ev["buckets"] if not b["closed"]]
            if len(buckets) < 3:
                continue
            unit = "F" if any("°F" in (b["label"] or "") for b in buckets) else "C"
            om_unit = "fahrenheit" if unit == "F" else "celsius"

            # --- Date cible du marché vs dates de prévision (locales station) ---
            target = parse_target_date(ev["title"])
            by_date = await self.feed.ensemble_by_date(lat, lon, om_unit)
            if not by_date:
                continue
            dates = sorted(by_date.keys())
            target_date = match_date(dates, target)
            if not target_date:
                # Marché d'un jour déjà passé à la station (en résolution) ou trop
                # lointain → pas de distribution fiable, on n'y touche pas.
                continue
            members = by_date[target_date]

            # --- Conditionnement intraday UNIQUEMENT si la cible = aujourd'hui là-bas ---
            realized, local_date = await self.feed.realized_today(lat, lon, om_unit)
            is_today = (local_date == target_date)
            realized_used = realized if (is_today and realized is not None) else None

            parsed = [(b["label"], parse_bucket(b["label"])) for b in buckets]
            probs = bucket_probabilities(members, parsed, realized_used)
            summ = ensemble_summary(
                [max(m, realized_used) for m in members] if realized_used is not None else members
            )

            # Tranches déjà détenues sur ce marché (plafond CUMULATIF)
            held_count = sum(
                1 for b in buckets
                if b["yes_token"] in positions and positions[b["yes_token"]]["shares"] > 0
            )
            slots_left = max(0, cfg.WEATHER_MAX_BUCKETS_PER_MARKET - held_count)

            sig = {
                "city": city,
                "title": ev["title"],
                "unit": unit,
                "date": target_date,
                "is_today": is_today,
                "median": round(summ["median"], 1) if summ else None,
                "std": round(summ["std"], 2) if summ else None,
                "spread": [round(summ["min"], 1), round(summ["max"], 1)] if summ else None,
                "realized": round(realized, 1) if (is_today and realized is not None) else None,
                "n": len(members),
                "buckets": [],
                "action": None,
            }

            # --- Construire la vue par tranche + candidats d'achat ---
            candidates = []
            for b in buckets:
                p = probs.get(b["label"])
                pos = positions.get(b["yes_token"])
                held_shares = pos["shares"] if pos and pos["shares"] > 0 else 0.0
                row = {
                    "label": b["label"],
                    "p": round(p, 3) if p is not None else None,
                    "p_cal": None,
                    "price": round(b["yes_price"], 3),
                    "edge": None,
                    "held_shares": round(held_shares, 1),
                    "held_avg": round(pos["avg_price"], 3) if held_shares else None,
                }
                if p is not None:
                    p_cal = self.calibrator.predict(p)
                    edge = p_cal - (b["yes_price"] + ctx.risk.taker_fee(b["yes_price"]))
                    row["p_cal"] = round(p_cal, 3)
                    row["edge"] = round(edge, 3)
                    if (cfg.WEATHER_MIN_BUY_PRICE < b["yes_price"] < cfg.WEATHER_MAX_BUY_PRICE
                            and edge > cfg.WEATHER_EDGE_THRESHOLD
                            and not held_shares and slots_left > 0):
                        candidates.append((b, p, p_cal, edge))
                sig["buckets"].append(row)

            # --- Achats : meilleures tranches d'abord, fill au vrai carnet ---
            candidates.sort(key=lambda c: -c[3])
            bought = 0
            for (b, p, p_cal, edge_disp) in candidates:
                if bought >= slots_left:
                    break
                book = await ctx.client.fetch_book(b["yes_token"])
                ask, ask_size = _best_ask(book)
                if ask is None or ask_size is None or ask_size <= 0:
                    continue   # illiquide → on ne se raconte pas d'histoires
                if not (cfg.WEATHER_MIN_BUY_PRICE < ask < cfg.WEATHER_MAX_BUY_PRICE):
                    continue
                # Edge recalculé au prix d'exécution réel
                edge = p_cal - (ask + ctx.risk.taker_fee(ask))
                if edge <= cfg.WEATHER_EDGE_THRESHOLD:
                    continue
                # Kelly binaire au prix réel
                f = (p_cal - ask) / (1.0 - ask) if ask < 1.0 else 0.0
                if f <= 0:
                    continue
                stake = cfg.WEATHER_KELLY_FRACTION * f * portfolio_value
                stake = min(stake, cfg.WEATHER_STAKE_MAX_USDC,
                            ctx.risk.max_trade_usdc(balance, portfolio_value),
                            ask_size * ask)
                if stake < cfg.WEATHER_STAKE_MIN_USDC:
                    continue
                shares = round(stake / ask, 1)
                if shares < 1.0:
                    continue
                cost = shares * ask
                balance -= cost
                db.update_balance(balance)
                q = f"{ev['title']} {b['label']}"
                ctx.accumulate_position(b["yes_token"], b["market_id"], q, b["yes_outcome"], shares, ask, b["end_date"])
                db.add_trade(b["market_id"], q, b["yes_token"], "BUY", b["yes_outcome"], shares, ask)
                db.log_bet(b["yes_token"], city, b["label"], b["yes_outcome"], True, ask, p, edge,
                           0.0, 0, 0.0, (realized_used or 0.0), shares, cost, kind="weather")
                bought += 1
                sig["action"] = f"BUY {b['label']}"
                # refléter l'achat dans la ligne UI
                for row in sig["buckets"]:
                    if row["label"] == b["label"]:
                        row["held_shares"] = shares
                        row["held_avg"] = round(ask, 3)
                ctx.log(
                    f"WEATHER {city} {target_date} | {b['label']} | P={p:.2f}->{p_cal:.2f} "
                    f"ask={ask:.2f} edge={edge:+.2f} | {shares} parts {cost:.2f}$ "
                    f"(méd {sig['median']}{unit}, réalisé {sig['realized']})",
                    "SUCCESS",
                )

            # tri d'affichage : plus forte proba d'abord
            sig["buckets"].sort(key=lambda x: -(x["p"] or 0))
            signals.append(sig)

        # tri : marchés d'aujourd'hui d'abord, puis par date
        signals.sort(key=lambda s: (not s["is_today"], s["date"]))
        if ctx.ui_state is not None:
            ctx.ui_state["weather"] = signals[: cfg.WEATHER_SIGNALS_MAX]
            ctx.ui_state["updated_at"] = now
            stats = db.get_bet_stats(kind="weather")
            ctx.ui_state["learning"] = {
                "calibrated": self.calibrator.active,
                "samples": stats["settled"],
                "wins": stats["wins"],
                "pnl": round(stats["pnl"], 2),
            }
