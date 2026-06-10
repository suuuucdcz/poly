"""Stratégie Weather Edge (marchés « Highest temperature » de Polymarket).

La température max du jour d'une station est prévisible via les modèles publics.
On calcule une distribution de probabilité (ensemble Open-Meteo GFS+ICON+ECMWF,
pondérée — ECMWF pèse plus — et légèrement élargie car les ensembles sont trop
confiants) **pour la date exacte du marché**, on en déduit la proba de chaque
tranche (troncature officielle), on compare au prix et on achète les tranches
sous-évaluées (jamais les favoris chers).

Garde-fous « béton » :
  - distribution de la DATE CIBLE du marché, en date locale de la station ;
  - max réalisé : lu sur le **capteur officiel NWS** pour les villes US (la
    donnée même qui résout le marché) ; sinon grille Open-Meteo MOINS une marge
    de sécurité (la grille peut surestimer le capteur → éviter d'éliminer une
    tranche à tort) ; appliqué seulement si cible = aujourd'hui là-bas ;
  - achat au vrai meilleur ask du carnet, taille plafonnée ;
  - plafond cumulatif de tranches par marché ; Kelly fractionnaire ; calibration ;
  - SORTIE ANTICIPÉE : si le marché paie nettement plus que la valeur modèle
    (prévision retournée ou marché euphorique), on vend au bid — prise de
    profit / coupe de perte. Ces sorties sont EXCLUES de la calibration.

Règlement final par le tick principal. 100 % paper.
"""

import time

from backend import config, db, station_bias
from backend.calibration import Calibrator
from backend.cities import NWS_STATIONS, resolve_city
from backend.weather_feed import WeatherFeed
from backend.weather_model import (
    bucket_probabilities,
    ensemble_summary,
    inflate_members,
    match_date,
    parse_bucket,
    parse_target_date,
)
from backend.strategies.base import Strategy


def _best(book, side):
    """(meilleur prix, taille) du côté demandé, ou (None, None)."""
    if not book:
        return None, None
    rows = book.get(side, [])
    if not rows:
        return None, None
    if side == "asks":
        b = min(rows, key=lambda x: float(x["price"]))
    else:
        b = max(rows, key=lambda x: float(x["price"]))
    return float(b["price"]), float(b["size"])


class WeatherEdgeStrategy(Strategy):
    name = "weather"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self.feed = WeatherFeed(cfg)
        self._last_refit = 0.0
        self._last_bias_harvest = 0.0
        self._biases = db.get_city_biases()   # ville -> (bias, std, n)
        self._warned_cities = set()
        self.calibrator = Calibrator(
            n_bins=cfg.CALIBRATION_BINS,
            prior_strength=cfg.CALIBRATION_PRIOR,
            min_samples=cfg.CALIBRATION_MIN_SAMPLES,
            min_losses=cfg.CALIBRATION_MIN_LOSSES,
        )

    # ------------------------------------------------------------
    # Max réalisé : capteur officiel d'abord, grille avec marge sinon
    # ------------------------------------------------------------
    async def _realized_for(self, city, lat, lon, om_unit, target_date, bias=0.0):
        """(valeur à utiliser pour conditionner, valeur à afficher, source).
        (None, None, None) si la cible n'est pas « aujourd'hui » à la station."""
        grid_val, local_date, offset = await self.feed.realized_today(lat, lon, om_unit)
        if local_date != target_date:
            return None, None, None
        station = NWS_STATIONS.get(city)
        if station:
            nws = await self.feed.nws_max_today(station, local_date, offset, om_unit)
            if nws is not None:
                return nws, nws, "station"     # capteur officiel : pas de marge
        if grid_val is None:
            return None, None, None
        # Repli grille : borne BASSE prudente du max officiel. Si la station lit
        # plus froid que la grille (biais négatif), il faut l'intégrer, sinon on
        # éliminerait des tranches à tort.
        margin = self.cfg.WEATHER_REALIZED_MARGIN_C
        if om_unit == "fahrenheit":
            margin *= 1.8
        return grid_val + min(0.0, bias) - margin, grid_val, "grille"

    # ------------------------------------------------------------
    # Boucle
    # ------------------------------------------------------------
    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        now = time.time()

        if now - self._last_refit > cfg.CALIBRATION_REFIT_SEC:
            self._last_refit = now
            try:
                self.calibrator.fit(db.get_bet_samples(3000, kind="weather"))
            except Exception:
                pass

        # Calibration du biais grille↔station (marchés résolus), toutes les 12 h
        if now - self._last_bias_harvest > cfg.BIAS_REFRESH_HOURS * 3600:
            self._last_bias_harvest = now
            try:
                await station_bias.harvest(self.feed, ctx.client, ctx.log)
                self._biases = db.get_city_biases()
            except Exception as e:
                ctx.log(f"BIAS: harvest impossible: {e}", "WARNING")

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

            # --- Distribution de la date cible (locale station) ---
            target = parse_target_date(ev["title"])
            by_date = await self.feed.ensemble_by_date(lat, lon, om_unit)
            if not by_date:
                continue
            target_date = match_date(sorted(by_date.keys()), target)
            if not target_date:
                continue   # jour passé à la station (en résolution) ou trop loin
            members = inflate_members(by_date[target_date], cfg.WEATHER_SPREAD_INFLATE)

            # --- Biais grille↔station appris des marchés résolus ---
            bias_info = self._biases.get(city)
            bias = bias_info[0] if bias_info else 0.0
            if bias:
                members = [(v + bias, w) for v, w in members]

            # --- Réalisé : capteur officiel (NWS) > grille - marge ---
            realized_used, realized_disp, realized_src = await self._realized_for(
                city, lat, lon, om_unit, target_date, bias
            )
            is_today = realized_src is not None

            parsed = [(b["label"], parse_bucket(b["label"])) for b in buckets]
            probs = bucket_probabilities(members, parsed, realized_used)
            summ = ensemble_summary(
                [(max(v, realized_used), w) for v, w in members] if realized_used is not None else members
            )

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
                "realized": round(realized_disp, 1) if realized_disp is not None else None,
                "realized_src": realized_src,
                "bias": round(bias, 1) if bias_info else None,
                "n": len(members),
                "buckets": [],
                "action": None,
            }

            # --- Vue par tranche + candidats d'achat + SORTIES anticipées ---
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

                    if held_shares:
                        # SORTIE : le marché paie nettement plus que la valeur modèle
                        book = await ctx.client.fetch_book(b["yes_token"])
                        bid, bid_size = _best(book, "bids")
                        if (bid is not None and bid_size
                                and bid - p_cal >= cfg.WEATHER_EXIT_EDGE):
                            qty = round(min(held_shares, bid_size), 1)
                            if qty >= 1.0:
                                revenue = qty * bid
                                pnl = revenue - qty * pos["avg_price"]
                                balance += revenue
                                db.update_balance(balance)
                                remaining = held_shares - qty
                                db.save_position(b["yes_token"], b["market_id"], pos["question"],
                                                 pos["outcome"], remaining, pos["avg_price"], bid)
                                db.add_trade(b["market_id"], pos["question"], b["yes_token"],
                                             "SELL", pos["outcome"], qty, bid, pnl)
                                if remaining < 1.0:
                                    db.settle_bet(b["yes_token"], None, pnl)  # exclu de la calibration
                                row["held_shares"] = round(remaining, 1)
                                if not remaining:
                                    row["held_avg"] = None
                                ctx.log(
                                    f"WEATHER EXIT {city} {b['label']} | bid {bid:.2f} >> modèle {p_cal:.2f} "
                                    f"| vendu {qty} | PnL {pnl:+.2f}$",
                                    "SUCCESS" if pnl >= 0 else "WARNING",
                                )
                    elif (cfg.WEATHER_MIN_BUY_PRICE < b["yes_price"] < cfg.WEATHER_MAX_BUY_PRICE
                            and edge > cfg.WEATHER_EDGE_THRESHOLD and slots_left > 0):
                        candidates.append((b, p, p_cal, edge))
                sig["buckets"].append(row)

            # --- Achats : meilleures tranches d'abord, fill au vrai carnet ---
            candidates.sort(key=lambda c: -c[3])
            bought = 0
            for (b, p, p_cal, edge_disp) in candidates:
                if bought >= slots_left:
                    break
                book = await ctx.client.fetch_book(b["yes_token"])
                ask, ask_size = _best(book, "asks")
                if ask is None or not ask_size:
                    continue
                if not (cfg.WEATHER_MIN_BUY_PRICE < ask < cfg.WEATHER_MAX_BUY_PRICE):
                    continue
                edge = p_cal - (ask + ctx.risk.taker_fee(ask))
                if edge <= cfg.WEATHER_EDGE_THRESHOLD:
                    continue
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
                for row in sig["buckets"]:
                    if row["label"] == b["label"]:
                        row["held_shares"] = shares
                        row["held_avg"] = round(ask, 3)
                ctx.log(
                    f"WEATHER {city} {target_date} | {b['label']} | P={p:.2f}->{p_cal:.2f} "
                    f"ask={ask:.2f} edge={edge:+.2f} | {shares} parts {cost:.2f}$ "
                    f"(méd {sig['median']}{unit}, réalisé {sig['realized']} {realized_src or ''})",
                    "SUCCESS",
                )

            sig["buckets"].sort(key=lambda x: -(x["p"] or 0))
            sig["held_any"] = any(r["held_shares"] > 0 for r in sig["buckets"])
            signals.append(sig)

        # Priorité d'affichage : marchés où l'on a parié, puis ceux du jour
        signals.sort(key=lambda s: (not s["held_any"], not s["is_today"], s["date"]))
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
