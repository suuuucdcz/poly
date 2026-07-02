"""Balayage de résolution (« sweep ») — acheter le gagnant DÉJÀ CONNU.

LE CONSTAT
----------
En fin de soirée locale (et jusqu'à la fermeture, le lendemain matin), le max du
jour est FINAL : la tranche gagnante est une donnée acquise, lisible dans le corps
des METAR de la station de résolution (validé : 8/8 d'accord avec les marchés
convergés). Pourtant certains gagnants se paient encore 0.90-0.97 — dernière prime
de doute que quelqu'un doit encaisser. C'est le trade à plus haute probabilité de
tout le système : pas de prévision, pas de vitesse, une donnée déjà réalisée.

HUMILITÉ (leçon 0/504)
----------------------
On n'achète QUE si le marché est déjà d'accord (ask >= SWEEP_MIN_PRICE = 0.80) :
on encaisse les derniers centimes de convergence, on ne prend jamais parti CONTRE
le marché. Si notre « gagnant certain » se paie 0.30, c'est nous qui avons tort
(donnée révisée, station différente, règle mal lue) -> on passe.

Positions taguées [SWEEP] et tenues jusqu'à la résolution (comme l'arbitrage) ;
le moteur convergence les ignore. 100 % paper.
"""

import time

from backend import config, db
from backend.weather_model import parse_bucket, _bucket_bounds
from backend.strategies.base import Strategy
from backend.strategies.weather_convergence import _local_clock

SWEEP_TAG = "[SWEEP]"


def _best_ask(book):
    if not book:
        return None, None
    rows = book.get("asks", [])
    if not rows:
        return None, None
    b = min(rows, key=lambda x: float(x["price"]))
    return float(b["price"]), float(b["size"])


class ResolutionSweepStrategy(Strategy):
    name = "sweep"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self.feed = None      # WeatherFeed injecté/partagé par le bot
        self.stats = {"candidates": 0, "bought": 0}

    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        if not getattr(cfg, "SWEEP_ENABLED", True) or self.feed is None:
            return
        now = time.time()
        try:
            events = await ctx.client.find_temperature_events()   # cache partagé
        except Exception:
            return

        balance = db.get_portfolio()["balance"]
        positions = db.get_positions()
        held = {p["token_id"] for p in positions if p["shares"] > 0}
        open_cost = sum(p["shares"] * p["avg_price"] for p in positions
                        if p["shares"] > 0 and str(p.get("question", "")).startswith(SWEEP_TAG))

        for ev in events:
            icao = ev.get("icao")
            if not icao:
                continue                      # sans station prouvée, pas de certitude
            offset, local_date, local_hour = _local_clock(ev.get("game_start"), ev.get("event_date"))
            if offset is None:
                continue
            event_date = ev.get("event_date")
            # Fenêtre : fin de soirée du jour J (max quasi final) OU lendemain matin
            # (max FINAL, marché pas encore fermé). Au-delà de midi J+1, les METAR
            # de J sortent de notre fenêtre de 30 h -> on ne certifie plus rien.
            if event_date == local_date:
                if local_hour is None or local_hour < cfg.SWEEP_EVENING_HOUR:
                    continue
            elif local_date is not None and event_date is not None and local_date > event_date:
                if local_hour is None or local_hour > 12.0:
                    continue
            else:
                continue

            buckets = [b for b in ev["buckets"] if not b["closed"] and b.get("accepting", True)]
            if len(buckets) < 3:
                continue
            unit = "F" if any("°F" in (b["label"] or "") for b in buckets) else "C"
            om_unit = "fahrenheit" if unit == "F" else "celsius"
            R = await self.feed.metar_max_today(icao, event_date, int(offset), om_unit)
            if R is None:
                continue

            # La tranche gagnante = celle qui contient R (convention arrondi)
            winner = None
            for b in buckets:
                p = parse_bucket(b["label"])
                if not p:
                    continue
                lo, hi = _bucket_bounds(p)
                if (lo is None or R >= lo) and (hi is None or R < hi):
                    winner = b
                    break
            if winner is None or winner["yes_token"] in held:
                continue
            # garde de bord : à moins de 0.15° du demi-degré, l'arrondi peut basculer
            frac = abs(R - round(R))
            if abs(frac - 0.5) < 0.15:
                continue

            book = await ctx.client.fetch_book(winner["yes_token"])
            ask, size = _best_ask(book)
            if ask is None or not size:
                continue
            # HUMILITÉ : le marché doit déjà être d'accord ; on rafle les centimes
            if not (cfg.SWEEP_MIN_PRICE <= ask <= cfg.SWEEP_MAX_PRICE):
                continue
            self.stats["candidates"] += 1
            edge = 1.0 - ask - ctx.risk.taker_fee(ask)
            if edge < cfg.SWEEP_MIN_EDGE:
                continue
            min_shares = max(1.0, float(winner.get("min_size") or 5))
            stake = min(cfg.SWEEP_STAKE_MAX_USDC,
                        cfg.SWEEP_MAX_TOTAL_USDC - open_cost,
                        balance * 0.9, size * ask)
            shares = float(int(stake / ask))
            if shares < min_shares:
                continue
            eff = ask + ctx.risk.taker_fee(ask)
            cost = shares * eff
            balance -= cost
            db.update_balance(balance)
            q = f"{SWEEP_TAG} {ev['title']} {winner['label']}"
            ctx.accumulate_position(winner["yes_token"], winner["market_id"], q,
                                    winner["yes_outcome"], shares, eff, winner["end_date"])
            db.add_trade(winner["market_id"], q, winner["yes_token"], "BUY",
                         winner["yes_outcome"], shares, ask)
            held.add(winner["yes_token"])
            open_cost += cost
            self.stats["bought"] += 1
            ctx.log(
                f"SWEEP {ev['title']} | gagnant {winner['label']} (R={R:.1f}{unit} FINAL) "
                f"acheté {ask:.2f} -> 1.00 à la résolution | +{edge * shares:.2f}$ quasi certain",
                "SUCCESS",
            )

        if ctx.ui_state is not None:
            ctx.ui_state["sweep"] = {
                "candidates": self.stats["candidates"],
                "bought": self.stats["bought"],
                "open_cost": round(open_cost, 2),
                "ts": now,
            }
