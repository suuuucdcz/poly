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
from backend.cities import NWS_STATIONS, region_of, resolve_city
from backend.weather_feed import WeatherFeed, station_local_date
from backend.weather_model import weighted_median

# Grille de quantiles (z-scores) pour synthétiser une distribution autour de la
# prévision déterministe de secours (met.no) : 21 scénarios équiprobables.
_ZS = [-1.98, -1.47, -1.18, -0.97, -0.79, -0.64, -0.50, -0.37, -0.24, -0.12,
       0.0, 0.12, 0.24, 0.37, 0.50, 0.64, 0.79, 0.97, 1.18, 1.47, 1.98]
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
    async def _realized_for(self, city, lat, lon, om_unit, target_date, bias=0.0, est=None):
        """(valeur à utiliser pour conditionner, valeur à afficher, source).
        (None, None, None) si la cible n'est pas « aujourd'hui » à la station."""
        grid_val, local_date, offset = await self.feed.realized_today(lat, lon, om_unit)
        if local_date is None and est:
            local_date, offset = est   # Open-Meteo indisponible : fuseau géométrique
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
        skip = {"city": 0, "buckets": 0, "ensemble": 0, "date": 0}
        self.feed.ens_budget = cfg.WEATHER_ENS_BUDGET_PER_TICK

        # Exposition (au coût) par RÉGION météo — une canicule régionale frappe
        # toutes les villes voisines en même temps, on plafonne le panier.
        region_open = {}
        for p in positions.values():
            cname, _ = resolve_city(p["question"])
            r = region_of(cname)
            region_open[r] = region_open.get(r, 0.0) + p["shares"] * p["avg_price"]

        for ev in events:
            city, coords = resolve_city(ev["title"])
            if not coords:
                skip["city"] += 1
                if ev["title"] not in self._warned_cities:
                    self._warned_cities.add(ev["title"])
                    ctx.log(f"WEATHER: ville inconnue (à ajouter dans cities.py) : '{ev['title']}'", "WARNING")
                continue
            lat, lon = coords

            buckets = [b for b in ev["buckets"] if not b["closed"]]
            if len(buckets) < 3:
                skip["buckets"] += 1
                continue
            unit = "F" if any("°F" in (b["label"] or "") for b in buckets) else "C"
            om_unit = "fahrenheit" if unit == "F" else "celsius"

            # --- Distribution de la date cible (locale station) ---
            target = parse_target_date(ev["title"])
            by_date = await self.feed.ensemble_by_date(lat, lon, om_unit)
            model_src = "ensemble"
            if not by_date:
                # SECOURS : Open-Meteo HS (quota épuisé...) -> met.no, distribution
                # synthétique de 21 quantiles autour de la prévision déterministe.
                mx = await self.feed.metno_daily_max_by_date(lat, lon, om_unit)
                if mx:
                    sigma = cfg.WEATHER_SYNTH_SIGMA_F if unit == "F" else cfg.WEATHER_SYNTH_SIGMA_C
                    by_date = {d: [(m + z * sigma, 1.0) for z in _ZS] for d, m in mx.items()}
                    model_src = "metno"
            if not by_date:
                skip["ensemble"] += 1
                continue
            target_date = match_date(sorted(by_date.keys()), target)
            if not target_date:
                skip["date"] += 1
                continue   # jour passé à la station (en résolution) ou trop loin
            # --- Incertitude par ville : les villes au biais instable (std haut)
            # méritent une distribution plus large et des mises plus petites.
            bias_info = self._biases.get(city)
            bias = bias_info[0] if bias_info else 0.0
            city_std = bias_info[1] if bias_info else 0.8
            inflate = min(1.5, cfg.WEATHER_SPREAD_INFLATE + cfg.WEATHER_STD_INFLATE_K * city_std)
            members = inflate_members(by_date[target_date], inflate)
            if bias:
                members = [(v + bias, w) for v, w in members]

            # --- 2e avis : prévision NWS officielle (villes US) ---
            nws_fx = None
            if city in NWS_STATIONS and unit == "F":
                nf = await self.feed.nws_forecast_max(lat, lon)
                v = nf.get(target_date)
                if v is not None:
                    med = weighted_median(members)
                    if abs(v - med) <= 8:
                        shift = cfg.WEATHER_NWS_BLEND * (v - med)
                        members = [(m + shift, w) for m, w in members]
                        nws_fx = v

            # --- Réalisé : capteur officiel (NWS) > grille - marge ---
            # Garde quota : on ne lit le réalisé que si le marché porte sur
            # « aujourd'hui » à la station (fuseau géométrique en estimation).
            est = station_local_date(lon)
            is_today = (est[0] == target_date)
            realized_used, realized_disp, realized_src = (None, None, None)
            if is_today:
                realized_used, realized_disp, realized_src = await self._realized_for(
                    city, lat, lon, om_unit, target_date, bias, est
                )

            parsed = [(b["label"], parse_bucket(b["label"])) for b in buckets]
            bw = cfg.WEATHER_KERNEL_BW_F if unit == "F" else cfg.WEATHER_KERNEL_BW_C
            probs = bucket_probabilities(members, parsed, realized_used, bandwidth=bw)
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
                "model_src": model_src,
                "nws": nws_fx,
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
                        # SORTIES : surpayé / sauvetage / verrouillage
                        book = await ctx.client.fetch_book(b["yes_token"])
                        bid, bid_size = _best(book, "bids")
                        reason = None
                        if bid is not None and bid_size:
                            if bid - p_cal >= cfg.WEATHER_EXIT_EDGE:
                                reason = "surpayé"
                            elif p_cal < cfg.WEATHER_SALVAGE_P and bid >= cfg.WEATHER_SALVAGE_MIN_BID:
                                reason = "sauvetage"
                            elif p_cal >= cfg.WEATHER_LOCK_P and bid >= cfg.WEATHER_LOCK_BID:
                                reason = "verrouillage"
                        if reason:
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
                                    f"WEATHER EXIT [{reason}] {city} {b['label']} | bid {bid:.2f} vs modèle {p_cal:.2f} "
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
                # Qualité de carnet : un bid doit exister, le spread doit être
                # raisonnable, et il faut une vraie profondeur au meilleur ask.
                # (Leçon anti-sélection : un carnet désert = un prix qui ne veut
                # rien dire et une sortie impossible.)
                bid, _bsz = _best(book, "bids")
                if bid is None or (ask - bid) > cfg.WEATHER_MAX_SPREAD:
                    continue
                if ask * ask_size < cfg.WEATHER_MIN_BOOK_USDC:
                    continue
                edge = p_cal - (ask + ctx.risk.taker_fee(ask))
                if edge <= cfg.WEATHER_EDGE_THRESHOLD:
                    continue
                f = (p_cal - ask) / (1.0 - ask) if ask < 1.0 else 0.0
                if f <= 0:
                    continue
                # Kelly amorti par l'incertitude de la ville (#6)
                kelly = cfg.WEATHER_KELLY_FRACTION / (1.0 + cfg.WEATHER_STD_KELLY_DAMP * city_std)
                stake = kelly * f * portfolio_value
                # Plafond régional (#5)
                region = region_of(city)
                region_left = cfg.WEATHER_MAX_REGION_USDC - region_open.get(region, 0.0)
                stake = min(stake, region_left, cfg.WEATHER_STAKE_MAX_USDC,
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
                region_open[region] = region_open.get(region, 0.0) + cost
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

        # Diagnostic : des events trouvés mais aucun signal = problème de données
        if events and not signals:
            ctx.log(
                f"WEATHER DIAG: {len(events)} events mais 0 signal — skips {skip} "
                f"| dernière erreur réseau météo: {self.feed.last_error}",
                "WARNING",
            )

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
            # Diagnostic réseau visible de l'extérieur (cause exacte des trous de données)
            ctx.ui_state["diag"] = {
                "last_net_error": self.feed.last_error,
                "nws_blend_cities": sum(1 for s in signals if s.get("nws") is not None),
                "station_realized": sum(1 for s in signals if s.get("realized_src") == "station"),
            }
