"""Stratégie « Convergence intraday » — le vrai métier de trader sur les marchés
« Highest temperature in {Ville} on {Date} » de Polymarket.

IDÉE MAÎTRESSE
--------------
Le max d'une journée n'est pas une météo à DEVINER, c'est un thermomètre à LIRE.
Il est monotone croissant : à l'heure locale `h`, on a déjà observé un max courant
`R` (au capteur officiel), et le max final `M ≥ R`. Passé le pic (~15h locale) la
température retombe et `M ≈ R` : la tranche gagnante est CONNUE avant la résolution.

Le edge = être plus rapide/juste que le marché sur une donnée PUBLIQUE connue.
Quand le capteur lit 34° à 15h30 et que le marché cote encore « 34°C » à 0,55, on
achète : ça converge vers ~0,95. Ce n'est pas un pari, c'est un rattrapage de lag.

DISCIPLINE (ce qui manquait au modèle précédent, 0/504)
-------------------------------------------------------
  - on ACHÈTE la tranche qui contient le max réel (le futur favori), pas celles
    que le marché a déjà exclues ;
  - on ENCAISSE la convergence par PALIERS (scale-out) : on ne garde JAMAIS
    jusqu'à la résolution binaire ;
  - on COUPE immédiatement une tranche qui diverge (le max l'a dépassée / sa proba
    s'effondre) tant qu'un acheteur existe — on n'attend pas le zéro ;
  - liquidation totale calée sur l'heure LOCALE réelle de chaque station.

PRUDENCE
--------
Entrées limitées aux villes NWS US (`CONV_ENTRIES_NWS_ONLY`) : là on lit le capteur
EXACT qui résout le marché. Ailleurs la grille Open-Meteo ≠ la station de
résolution (c'est ce qui a coulé l'ancien modèle) -> on se contente d'y GÉRER les
sorties. 100 % paper.
"""

import datetime as _dt
import time

from backend import config, db
from backend.cities import METAR_STATIONS, region_of, resolve_city
from backend.weather_feed import WeatherFeed
from backend.weather_model import (
    _bucket_bounds,
    bucket_probabilities,
    parse_bucket,
    parse_target_date,
    weighted_median,
)
from backend.strategies.base import Strategy


def _best(book, side):
    """(meilleur prix, taille) du côté demandé, ou (None, None)."""
    if not book:
        return None, None
    rows = book.get(side, [])
    if not rows:
        return None, None
    b = min(rows, key=lambda x: float(x["price"])) if side == "asks" \
        else max(rows, key=lambda x: float(x["price"]))
    return float(b["price"]), float(b["size"])


def _local_clock(game_start, event_date):
    """Horloge ANCRÉE sur le marché lui-même. `game_start` (gameStartTime Polymarket)
    est le minuit LOCAL du jour de résolution exprimé en UTC ; `event_date` est ce
    jour local (YYYY-MM-DD). Renvoie (offset_seconds, local_date, local_hour) avec le
    fuseau EXACT (DST et demi-fuseaux type Inde +5:30 compris), sans dépendre
    d'Open-Meteo ni de l'approximation lon/15. (None, None, None) si non parsable."""
    if not game_start or not event_date:
        return None, None, None
    try:
        gs = _dt.datetime.fromisoformat(str(game_start).replace(" ", "T").replace("Z", "+00:00"))
        if gs.tzinfo is None:
            gs = gs.replace(tzinfo=_dt.timezone.utc)
        ev_midnight_utc = _dt.datetime.fromisoformat(str(event_date) + "T00:00:00+00:00")
        offset = (ev_midnight_utc - gs).total_seconds()   # ex. Dallas -18000 (UTC-5)
        local_now = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=offset)
        return offset, local_now.strftime("%Y-%m-%d"), local_now.hour + local_now.minute / 60.0
    except Exception:
        return None, None, None


class WeatherConvergenceStrategy(Strategy):
    name = "convergence"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self.feed = WeatherFeed(cfg)
        self._warned = set()
        # Confirmation du pic : (city, date) -> (dernier R vu, ts de la dernière HAUSSE).
        # Une entrée n'est permise que si R n'a plus monté depuis CONV_STABLE_SEC.
        self._r_seen = {}
        # Scale-out one-shot : tokens déjà allégés (sinon le TP se re-déclenche à
        # chaque tick et grignote la position en payant des frais à chaque vente).
        self._tp_done = set()

    # ------------------------------------------------------------
    # Max courant `R` (running max) + heure locale + date locale
    # ------------------------------------------------------------
    async def _running_max(self, icao, lat, lon, om_unit, local_date, offset):
        """Max courant `R` lu au CAPTEUR (la VALEUR seule ; l'horloge, elle, vient du
        marché via gameStartTime). CORPS des METAR horaires de LA station de
        résolution (icao extrait de la description du marché) — la donnée même que
        Wunderground convertit pour résoudre. SURTOUT PAS les observations
        infra-horaires d'api.weather.gov : elles lisent 1-2°F plus chaud que le
        METAR horaire (Dallas 01/07 : 96.8 lu vs 95.0 officiel -> pari perdu) et
        leur jitter en dixièmes réarmait sans cesse le chrono de stabilité.
        Sinon grille Open-Meteo pour ce jour local. (None, None) si indisponible."""
        if icao:
            m = await self.feed.metar_max_today(icao, local_date, int(offset), om_unit)
            if m is not None:
                return m, "metar"
        grid_R, grid_date, _off = await self.feed.realized_today(lat, lon, om_unit)
        if grid_R is not None and grid_date == local_date:
            return grid_R, "grille"
        return None, None

    # ------------------------------------------------------------
    # Distribution du max FINAL, conditionnée à (R, heure)
    # ------------------------------------------------------------
    def _final_max_dist(self, R, local_hour, forecast_med, unit):
        """Renvoie (members, sigma, realized_floor, M_hat) décrivant la loi du max
        FINAL. La tranche gagnante = round(M).

        APRÈS le pic : le max est ~figé. σ est en DEGRÉS-MARCHÉ (PAS de ×1.8 pour les
        °F : une tranche fait 1° dans les deux unités, et l'incertitude post-pic est
        une affaire d'arrondi/lecture ~0.1-0.2°, pas une erreur physique de prévision).
        Distribution SYMÉTRIQUE autour de R -> on désigne round(R) avec confiance
        (93.4 -> « 93°F », 93.6 -> « 94°F »). CONV_MIN_FAIR écarte les cas au bord du
        demi-degré (93.5) où c'est vraiment 50/50.

        AVANT le pic : la chauffe à venir est PHYSIQUE -> σ scalé par l'unité, centré
        sur max(R, prévision) et plancher à R (le max ne redescend pas)."""
        cfg = self.cfg
        h = local_hour if local_hour is not None else 0.0
        if h >= cfg.CONV_PEAK_HOUR:
            sigma = max(0.10,
                        cfg.CONV_SIGMA_PASTPEAK - cfg.CONV_SIGMA_EVENING_DECAY * (h - cfg.CONV_PEAK_HOUR))
            return [(R, 1.0)], sigma, None, R
        uf = 1.8 if unit == "F" else 1.0
        M_hat = max(R, forecast_med) if forecast_med is not None else R
        frac = min(1.0, max(0.0, (cfg.CONV_PEAK_HOUR - h) / 8.0))  # 0 au pic, ~1 tôt
        sigma = (cfg.CONV_SIGMA_PRE_C * (0.4 + 0.6 * frac)) * uf
        return [(M_hat, 1.0)], sigma, R, M_hat

    # ------------------------------------------------------------
    # Boucle principale
    # ------------------------------------------------------------
    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        now = time.time()
        try:
            events = await ctx.client.find_temperature_events()
        except Exception as e:
            ctx.log(f"CONV: récupération marchés KO: {e}", "WARNING")
            return

        balance = db.get_portfolio()["balance"]
        # Les paniers d'arbitrage [ARB] sont EXCLUS des sorties : ils se tiennent
        # jusqu'à la résolution (c'est elle qui paie) — nos sorties du soir les
        # détruiraient. Mais leurs tokens sont BLOQUÉS à l'achat : acheter un token
        # déjà [ARB] fusionnerait les deux positions (clé = token_id) et notre
        # question écraserait le tag -> le flatten du soir vendrait une patte du
        # panier et casserait la garantie de l'arbitrage.
        from backend.strategies.weather_negrisk import is_arb_position
        all_pos = db.get_positions()
        arb_tokens = {p["token_id"] for p in all_pos
                      if is_arb_position(p) and p["shares"] > 0}
        positions = {p["token_id"]: p for p in all_pos if not is_arb_position(p)}
        self.feed.ens_budget = cfg.WEATHER_ENS_BUDGET_PER_TICK
        signals = []

        # Exposition au coût par région (une canicule régionale = un seul pari)
        region_open = {}
        for p in positions.values():
            cname, _ = resolve_city(p["question"])
            region_open[region_of(cname)] = region_open.get(region_of(cname), 0.0) \
                + p["shares"] * p["avg_price"]

        for ev in events:
            city, coords = resolve_city(ev["title"])
            if not coords:
                continue
            lat, lon = coords
            buckets = [b for b in ev["buckets"] if not b["closed"]]
            if len(buckets) < 3:
                continue
            unit = "F" if any("°F" in (b["label"] or "") for b in buckets) else "C"
            om_unit = "fahrenheit" if unit == "F" else "celsius"

            # --- Horloge ANCRÉE sur le marché (gameStartTime) : fuseau EXACT ---
            offset, local_date, local_hour = _local_clock(ev.get("game_start"), ev.get("event_date"))
            event_date = ev.get("event_date")
            if event_date is None:            # repli : date reconstruite depuis le titre
                td = parse_target_date(ev["title"])
                if td and local_date:
                    event_date = f"{local_date[:4]}-{td[0]:02d}-{td[1]:02d}"
            # La convergence ne se joue QUE le jour LOCAL de résolution (max courant dispo).
            is_today = (local_date is not None and event_date == local_date)

            held_here = [b for b in buckets
                         if b["yes_token"] in positions and positions[b["yes_token"]]["shares"] > 0]

            # --- max courant R au capteur (VALEUR seule ; horloge déjà fixée) ---
            # UNIQUEMENT le jour de résolution : après minuit local, local_date pointe
            # sur le lendemain -> lire R donnerait le max d'un AUTRE jour. Une position
            # tenue au-delà se liquide via la règle 'jour passé' ci-dessous.
            # Station de résolution : celle que LE MARCHÉ déclare (description),
            # sinon le registre local (vérifié villes US + EGLC/ZGGG). NYC nous a
            # appris la leçon : le registre disait Central Park, le marché résout
            # à LaGuardia — 2°F d'écart, certitude fausse.
            icao = ev.get("icao") or METAR_STATIONS.get(city)
            R, R_src = (None, None)
            if is_today and local_date is not None and offset is not None:
                R, R_src = await self._running_max(icao, lat, lon, om_unit, local_date, offset)

            # --- Confirmation du pic : R doit avoir cessé de monter ---
            r_stable = False
            if R is not None:
                key = (city, local_date)
                prev = self._r_seen.get(key)
                if prev is None or R > prev[0] + 0.05:
                    self._r_seen[key] = (R, now)      # R monte (ou 1re lecture) -> chrono reparti
                else:
                    r_stable = (now - prev[1]) >= cfg.CONV_STABLE_SEC
                if len(self._r_seen) > 1000:          # hygiène mémoire (dates passées)
                    self._r_seen = {k: v for k, v in self._r_seen.items() if k[1] == local_date}

            # --- prévision (utile seulement avant le pic) ---
            forecast_med = None
            if is_today and local_hour is not None and local_hour < cfg.CONV_PEAK_HOUR:
                by_date = await self.feed.ensemble_by_date(lat, lon, om_unit)
                if by_date and local_date in by_date:
                    forecast_med = weighted_median(by_date[local_date])

            # --- probabilités conditionnées par tranche ---
            parsed = [(b["label"], parse_bucket(b["label"])) for b in buckets]
            probs, M_hat, sigma = {}, None, None
            if is_today and R is not None:
                members, sigma, floor, M_hat = self._final_max_dist(R, local_hour, forecast_med, unit)
                probs = bucket_probabilities(members, parsed, realized=floor, bandwidth=sigma)

            past_flatten = (local_hour is not None and local_hour >= cfg.CONV_FLATTEN_LOCAL_HOUR)
            # ENTRÉES UNIQUEMENT APRÈS LE PIC : c'est là que le max est ~lu au capteur
            # (M ≈ R) et que l'edge est réel (rattraper le lag du marché). AVANT le pic,
            # le max final DÉPEND de la chauffe restante à prévoir — même avec une
            # prévision, si elle est déjà dépassée par R (cas Miami 11h : R=89.6 >
            # prévision) on ancrerait sur une tranche trop basse. Deviner la chauffe
            # = exactement ce qui a fait 0/504. Pré-pic -> on observe, on n'entre pas.
            post_peak = (local_hour is not None and local_hour >= cfg.CONV_PEAK_HOUR)
            # + r_stable : le pic réel varie (LA/Phoenix ~16-17h). Entrer à 15h05
            # pendant que ça chauffe encore = achats de round(R) coupés 1h après
            # (Denver 01/07 : acheté 88-89 à 16h43, gagnant 90-91 ; Atlanta 02/07 :
            # +1.8°F APRÈS 45 min de plateau).
            # Entrées : UNIQUEMENT si on lit la station de résolution elle-même
            # (R_src == "metar"). Une lecture grille ≈ 10-25 km de maille ne prouve
            # rien sur le capteur officiel.
            entries_ok = (is_today and R is not None and post_peak and r_stable
                          and not past_flatten
                          and ((not cfg.CONV_ENTRIES_NWS_ONLY) or R_src == "metar"))

            held_count = len(held_here)
            slots_left = max(0, cfg.CONV_MAX_BUCKETS_PER_MARKET - held_count)

            sig = {
                "city": city, "title": ev["title"], "unit": unit, "date": event_date,
                "is_today": bool(is_today), "median": round(M_hat, 1) if M_hat is not None else None,
                "std": round(sigma, 2) if sigma is not None else None, "spread": None,
                "realized": round(R, 1) if R is not None else None, "realized_src": R_src,
                "stable": bool(r_stable), "bias": None, "model_src": "convergence", "nws": None,
                "local_hour": round(local_hour, 1) if local_hour is not None else None,
                "n": 0, "buckets": [], "action": None,
            }

            candidates = []
            for b in buckets:
                parsed_b = parse_bucket(b["label"])
                fair = probs.get(b["label"])
                pos = positions.get(b["yes_token"])
                held = pos["shares"] if pos and pos["shares"] > 0 else 0.0
                row = {
                    "label": b["label"], "p": round(fair, 3) if fair is not None else None,
                    "p_cal": None, "price": round(b["yes_price"], 3), "edge": None,
                    "held_shares": round(held, 1),
                    "held_avg": round(pos["avg_price"], 3) if held else None,
                }
                # ---------------- SORTIES (toujours actives, même hors NWS) ------------
                # Les sorties PRIX/HEURE (lock, TP, clôture) ne dépendent PAS de la
                # prévision -> elles marchent même si le flux météo est en panne (429).
                # Seules 'coupe' (tranche dépassée) et 'sortie' (proba effondrée) ont
                # besoin de R/fair.
                if held:
                    book = await ctx.client.fetch_book(b["yes_token"])
                    bid, bid_size = _best(book, "bids")
                    if bid is not None and bid_size:
                        bounds = _bucket_bounds(parsed_b) if parsed_b else (None, None)
                        upper = bounds[1]
                        # tranche MORTE : le max observé a dépassé sa borne haute
                        dead = (upper is not None and R is not None and upper <= R - 1e-6)
                        # Liquidation calée sur l'heure LOCALE (gameStartTime), PAS sur
                        # endDate : ce dernier (12:00 UTC) est nominal — les marchés
                        # restent tradables jusqu'à ~00h30 locale (publication de la
                        # donnée du lendemain). On flatten le soir (20h), bien avant ; et
                        # si le jour de résolution est PASSÉ (position pas soldée, ex.
                        # aucun acheteur à 20h), on dump avant la résolution binaire.
                        flatten = past_flatten or not is_today
                        reason, sell_frac = None, 1.0
                        if dead:
                            reason, sell_frac = "coupe", 1.0            # perdant : on coupe
                        elif flatten:
                            reason, sell_frac = "clôture", 1.0          # liquidation soir
                        elif (bid <= cfg.CONV_MARKET_VETO
                                and pos["avg_price"] >= 0.25):
                            # VETO MARCHÉ : on a payé cher, le marché dit ~zéro.
                            # L'historique (0/504, 0/4) dit que c'est lui qui a
                            # raison (notre donnée peut être fantôme) -> on sauve
                            # ce qui peut l'être au lieu de rouler jusqu'à 0.
                            reason, sell_frac = "veto-marché", 1.0
                        elif bid >= cfg.CONV_TP2:
                            reason, sell_frac = "lock", 1.0             # convergence faite
                        elif fair is not None and fair < cfg.CONV_FAIR_CUT:
                            reason, sell_frac = "sortie", 1.0           # notre tranche décroche
                        elif (bid >= cfg.CONV_TP1
                                and bid >= pos["avg_price"] + cfg.CONV_TP_MIN_PROFIT
                                and b["yes_token"] not in self._tp_done):
                            # scale-out partiel : EN PROFIT uniquement (entrée possible
                            # jusqu'à 0.90 -> sans ce garde, TP à 0.80 vendrait à perte)
                            # et UNE SEULE FOIS (sinon re-déclenché à chaque tick).
                            reason, sell_frac = "TP", cfg.CONV_TP1_FRAC
                        if reason and bid >= 0.01:
                            qty = round(min(held, bid_size) * sell_frac, 1)
                            if qty >= 1.0:
                                if reason == "TP":
                                    self._tp_done.add(b["yes_token"])
                                # revenu NET des frais taker (5 % · bid · (1−bid) par part)
                                revenue = qty * (bid - ctx.risk.taker_fee(bid))
                                pnl = revenue - qty * pos["avg_price"]
                                balance += revenue
                                db.update_balance(balance)
                                remaining = round(held - qty, 1)
                                if remaining < 1.0:
                                    self._tp_done.discard(b["yes_token"])   # position soldée
                                db.save_position(b["yes_token"], b["market_id"], pos["question"],
                                                 pos["outcome"], remaining, pos["avg_price"], bid)
                                db.add_trade(b["market_id"], pos["question"], b["yes_token"],
                                             "SELL", pos["outcome"], qty, bid, pnl)
                                if remaining < 1.0:
                                    db.settle_bet(b["yes_token"], None, pnl)  # exit -> hors calibration
                                row["held_shares"] = round(remaining, 1)
                                if not remaining:
                                    row["held_avg"] = None
                                ctx.log(
                                    f"CONV EXIT [{reason}] {city} {b['label']} | bid {bid:.2f} "
                                    f"| R={R} {unit} h={local_hour:.1f} | vendu {qty} PnL {pnl:+.2f}$",
                                    "SUCCESS" if pnl >= 0 else "WARNING",
                                )

                # ---------------- ENTRÉES (villes NWS, gagnant connu sous-coté) --------
                elif (entries_ok and slots_left > 0 and fair is not None
                        and fair >= cfg.CONV_MIN_FAIR and b.get("accepting", True)
                        and b["yes_token"] not in arb_tokens):
                    ask_disp = b["yes_price"]
                    edge = fair - ask_disp - ctx.risk.taker_fee(ask_disp)   # net des frais d'entrée
                    row["edge"] = round(edge, 3)
                    if (cfg.CONV_MIN_ENTRY <= ask_disp <= cfg.CONV_MAX_ENTRY
                            and edge >= cfg.CONV_EDGE):
                        candidates.append((b, fair))
                if fair is not None and row["edge"] is None:
                    row["edge"] = round(fair - b["yes_price"], 3)
                sig["buckets"].append(row)

            # --- Exécution des achats : meilleures tranches d'abord, fill au carnet ---
            candidates.sort(key=lambda c: -c[1])
            bought = 0
            for (b, fair) in candidates:
                if bought >= slots_left:
                    break
                if not b.get("accepting", True):
                    continue
                book = await ctx.client.fetch_book(b["yes_token"])
                ask, ask_size = _best(book, "asks")
                bid, _bsz = _best(book, "bids")
                if ask is None or not ask_size or bid is None:
                    continue
                if not (cfg.CONV_MIN_ENTRY <= ask <= cfg.CONV_MAX_ENTRY):
                    continue
                if (ask - bid) > cfg.WEATHER_MAX_SPREAD or ask * ask_size < cfg.WEATHER_MIN_BOOK_USDC:
                    continue
                fee_ps = ctx.risk.taker_fee(ask)          # frais taker d'entrée par part
                edge = fair - ask - fee_ps                # edge NET de frais
                if edge < cfg.CONV_EDGE:
                    continue
                f = edge / (1.0 - ask) if ask < 1.0 else 0.0
                if f <= 0:
                    continue
                stake = cfg.CONV_KELLY * f * portfolio_value
                region = region_of(city)
                region_left = cfg.WEATHER_MAX_REGION_USDC - region_open.get(region, 0.0)
                stake = min(stake, region_left, cfg.CONV_STAKE_MAX_USDC,
                            ctx.risk.max_trade_usdc(balance, portfolio_value), ask_size * ask)
                if stake < cfg.CONV_STAKE_MIN_USDC:
                    continue
                # Taille d'ordre minimale imposée par le CLOB (orderMinSize, souvent 5)
                min_shares = max(1.0, float(b.get("min_size") or 5))
                shares = round(stake / ask, 1)
                if shares < min_shares:
                    continue
                # Base de coût EFFECTIVE = prix de fill + frais taker -> le P&L de sortie
                # (net des frais de sortie aussi) reflète l'économie réelle, frais compris.
                eff = ask + fee_ps
                cost = shares * eff
                balance -= cost
                db.update_balance(balance)
                q = f"{ev['title']} {b['label']}"
                ctx.accumulate_position(b["yes_token"], b["market_id"], q, b["yes_outcome"],
                                        shares, eff, b["end_date"])
                db.add_trade(b["market_id"], q, b["yes_token"], "BUY", b["yes_outcome"], shares, ask)
                db.log_bet(b["yes_token"], city, b["label"], b["yes_outcome"], True, ask, fair, edge,
                           0.0, 0, 0.0, (R or 0.0), shares, cost, kind="weather_conv")
                region_open[region] = region_open.get(region, 0.0) + cost
                bought += 1
                sig["action"] = f"BUY {b['label']}"
                for row in sig["buckets"]:
                    if row["label"] == b["label"]:
                        row["held_shares"] = shares
                        row["held_avg"] = round(ask, 3)
                ctx.log(
                    f"CONV BUY {city} {b['label']} | juste {fair:.2f} vs ask {ask:.2f} edge {edge:+.2f} "
                    f"| R={R} {unit} h={local_hour:.1f} ({R_src}) | {shares} parts {cost:.2f}$",
                    "SUCCESS",
                )

            sig["buckets"].sort(key=lambda x: -(x["p"] or 0))
            sig["held_any"] = any(r["held_shares"] > 0 for r in sig["buckets"])
            signals.append(sig)

        signals.sort(key=lambda s: (not s.get("held_any"), not s["is_today"], s["date"] or "9999"))
        if ctx.ui_state is not None:
            ctx.ui_state["weather"] = signals[: cfg.WEATHER_SIGNALS_MAX]
            ctx.ui_state["updated_at"] = now
            # P&L RÉEL depuis la table trades (vérité comptable) et non depuis bet_log,
            # qui ne settle que le dernier morceau d'un scale-out (sous-comptage) et
            # laisse 'wins' à 0 sur les sorties anticipées. wins = sorties gagnantes.
            tot = db.get_trade_totals()
            ctx.ui_state["learning"] = {
                "calibrated": False,
                "samples": tot["closed"],
                "wins": tot["wins"],
                "pnl": tot["pnl_total"],
            }
            ctx.ui_state["diag"] = {
                "last_net_error": self.feed.last_error,
                "engine": "convergence",
                "station_realized": sum(1 for s in signals if s.get("realized_src") == "station"),
            }
