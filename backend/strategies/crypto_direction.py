"""Stratégie 4 — Crypto Direction v3 (marchés « Up/Down » courts de Polymarket).

Modèle : P(up) = Φ( delta / (sigma·√t) ), delta = spot/open_ref - 1.

v2 → v3 (couche d'apprentissage) :
  - CALIBRATION : la proba du modèle est corrigée par les résultats réels passés
    (cf. backend/calibration.py). Sans données → identité (= v2). Avec données →
    les probas annoncées collent aux fréquences de gain observées.
  - SIZING PAR EDGE (Kelly fractionnaire) : on mise plus quand l'edge est grand,
    moins quand il est petit — au lieu d'un montant fixe.
  - PLAFOND DE CORRÉLATION : exposition simultanée bornée sur le panier crypto
    (BTC/ETH/SOL… bougent ensemble), pour ne pas empiler des paris corrélés.
  - JOURNALISATION : chaque pari (situation → résultat) est loggé pour nourrir la
    calibration (« plus de données → plus précis », une fois cette boucle en place).
  - Règlement proactif inchangé (encaisse dès la fin de fenêtre).
"""

import math
import time

from backend import config, db
from backend.calibration import Calibrator
from backend.indicators import normal_cdf
from backend.strategies.base import Strategy


def _best_ask(book):
    if not book:
        return None, None
    asks = book.get("asks", [])
    if not asks:
        return None, None
    best = min(asks, key=lambda x: float(x["price"]))
    return float(best["price"]), float(best["size"])


def _flow_confirms(flow, is_up, veto):
    """Vrai si le flux agresseur ne contredit pas FORTEMENT le côté visé."""
    return flow > -veto if is_up else flow < veto


class CryptoDirectionStrategy(Strategy):
    name = "crypto_direction"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self._open_bets = {}            # token_id -> infos du pari ouvert
        self._last_refit = 0.0
        self.calibrator = Calibrator(
            n_bins=cfg.CRYPTO_CALIBRATION_BINS,
            prior_strength=cfg.CRYPTO_CALIBRATION_PRIOR,
            min_samples=cfg.CRYPTO_CALIBRATION_MIN_SAMPLES,
            min_losses=cfg.CRYPTO_CALIBRATION_MIN_LOSSES,
        )

    # ------------------------------------------------------------
    # Helpers sizing / exposition
    # ------------------------------------------------------------
    def _kelly_stake(self, p, ask, bankroll):
        """Mise (USDC) ∝ edge, via Kelly fractionnaire pour un token binaire
        acheté au prix `ask` (gagne 1, perd `ask`)."""
        if ask <= 0.0 or ask >= 1.0 or bankroll <= 0:
            return 0.0
        f = (p - ask) / (1.0 - ask)   # fraction de Kelly
        if f <= 0.0:
            return 0.0
        return self.cfg.CRYPTO_KELLY_FRACTION * f * bankroll

    @staticmethod
    def _crypto_exposure():
        """Exposition USDC actuelle sur l'ensemble des marchés Up/Down (corrélés)."""
        total = 0.0
        for p in db.get_positions():
            if "Up or Down" in (p.get("question") or ""):
                total += p["shares"] * p["avg_price"]
        return total

    # ------------------------------------------------------------
    # Règlement proactif
    # ------------------------------------------------------------
    async def _settle_ended_bets(self, ctx):
        cfg = self.cfg
        now = time.time()
        balance = db.get_portfolio()["balance"]

        for token, bet in list(self._open_bets.items()):
            if now < bet["end_ts"] + cfg.CRYPTO_SETTLE_GRACE_SEC:
                continue
            pos = db.get_position(token)
            if not pos or pos["shares"] <= 0:
                self._open_bets.pop(token, None)
                continue
            close = await ctx.feed.reference_price(bet["asset"], bet["end_ts"])
            if close is None:
                continue
            outcome_up = close >= bet["open_ref"]
            won = (bet["is_up"] == outcome_up)
            final = 1.0 if won else 0.0
            payout = pos["shares"] * final
            pnl = payout - pos["shares"] * pos["avg_price"]
            balance += payout
            db.update_balance(balance)
            db.add_trade(bet["market_id"], pos["question"], token, "RESOLVE", bet["side"], pos["shares"], final, pnl)
            db.save_position(token, bet["market_id"], pos["question"], bet["side"], 0.0, 0.0, 0.0)
            db.settle_bet(token, won, pnl)        # <-- nourrit l'apprentissage
            ctx.peak_prices.pop(token, None)
            ctx.log(
                f"CRYPTO SETTLE {bet['asset']} {bet['window']} | close {close:.2f} vs open {bet['open_ref']:.2f} "
                f"-> {'UP' if outcome_up else 'DOWN'} | {bet['side']} {'WON' if won else 'LOST'} | PnL {pnl:+.2f}$",
                "SUCCESS" if pnl >= 0 else "WARNING",
            )
            self._open_bets.pop(token, None)

    # ------------------------------------------------------------
    # Boucle
    # ------------------------------------------------------------
    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        now = time.time()

        # Refit périodique du calibrateur depuis le journal des paris
        if cfg.CRYPTO_CALIBRATION_ENABLED and (now - self._last_refit > cfg.CRYPTO_CALIBRATION_REFIT_SEC):
            self._last_refit = now
            try:
                self.calibrator.fit(db.get_bet_samples(3000, kind="crypto"))
            except Exception as e:
                ctx.log(f"CRYPTO: refit calibration impossible: {e}", "WARNING")

        # Règlement proactif des fenêtres terminées
        if cfg.CRYPTO_PROACTIVE_SETTLE and self._open_bets:
            await self._settle_ended_bets(ctx)
            balance = db.get_portfolio()["balance"]

        # Marchés courants
        assets = list(cfg.CRYPTO_ASSETS.keys())
        try:
            updown = await ctx.client.find_updown_events(assets, cfg.CRYPTO_WINDOWS, cfg.CRYPTO_WINDOW_SECONDS)
        except Exception as e:
            ctx.log(f"CRYPTO: erreur récupération des marchés Up/Down: {e}", "WARNING")
            return

        now = time.time()
        signals = []

        for mk in updown:
            if mk["closed"]:
                continue
            t_left = mk["end_ts"] - now
            if t_left <= 0:
                continue

            asset = mk["asset"]
            spot = await ctx.feed.spot(asset)
            if spot is None:
                continue
            open_ref = await ctx.feed.reference_price(asset, mk["open_ts"], live_spot=spot)
            if not open_ref or open_ref <= 0:
                continue
            sigma = await ctx.feed.volatility(asset)

            delta = spot / open_ref - 1.0
            denom = sigma * math.sqrt(max(t_left, 1.0))
            p_up = normal_cdf(delta / denom) if denom > 0 else (1.0 if delta >= 0 else 0.0)
            p_down = 1.0 - p_up

            up_token = mk["up_token"]
            down_token = mk["down_token"]
            held_up = db.get_position(up_token)
            held_down = db.get_position(down_token)
            already_held = bool(
                (held_up and held_up["shares"] > 0) or (held_down and held_down["shares"] > 0)
            )

            signal = {
                "asset": asset, "window": mk["window"], "question": mk["question"],
                "spot": spot, "open_ref": open_ref, "delta_pct": delta * 100.0,
                "t_left": int(t_left), "p_up": p_up,
                "up_price": mk["up_price"], "down_price": mk["down_price"],
                "held": already_held, "flow": None,
                "edge_up": None, "edge_down": None, "action": None,
            }

            in_band = cfg.CRYPTO_ENTRY_MIN_SECONDS_LEFT <= t_left <= cfg.CRYPTO_ENTRY_MAX_SECONDS_LEFT

            if in_band and not already_held:
                book_up = await ctx.client.fetch_book(up_token)
                book_down = await ctx.client.fetch_book(down_token)
                up_ask, up_size = _best_ask(book_up)
                down_ask, down_size = _best_ask(book_down)

                flow = await ctx.feed.trade_flow(asset) if cfg.CRYPTO_USE_ORDERFLOW else 0.0
                signal["flow"] = flow

                # Candidats avec proba CALIBRÉE + gating de confiance.
                # tuple: (side, token, ask, size, edge, is_up, p_raw, p_cal)
                candidates = []
                if up_ask is not None and cfg.CRYPTO_MIN_PRICE < up_ask < cfg.CRYPTO_MAX_PRICE:
                    p_cal = self.calibrator.predict(p_up)
                    edge_up = p_cal - (up_ask + ctx.risk.taker_fee(up_ask))
                    signal["edge_up"] = edge_up
                    if p_cal >= cfg.CRYPTO_MIN_CONFIDENCE:
                        candidates.append((mk["up_outcome"], up_token, up_ask, up_size, edge_up, True, p_up, p_cal))
                if down_ask is not None and cfg.CRYPTO_MIN_PRICE < down_ask < cfg.CRYPTO_MAX_PRICE:
                    p_cal = self.calibrator.predict(p_down)
                    edge_down = p_cal - (down_ask + ctx.risk.taker_fee(down_ask))
                    signal["edge_down"] = edge_down
                    if p_cal >= cfg.CRYPTO_MIN_CONFIDENCE:
                        candidates.append((mk["down_outcome"], down_token, down_ask, down_size, edge_down, False, p_down, p_cal))

                tradeable = [c for c in candidates if c[4] > cfg.CRYPTO_EDGE_THRESHOLD]
                if cfg.CRYPTO_USE_ORDERFLOW and tradeable:
                    veto = cfg.CRYPTO_FLOW_VETO
                    tradeable = [c for c in tradeable if _flow_confirms(flow, c[5], veto)]

                if tradeable:
                    side, token, ask, size, edge, is_up, p_raw, p_cal = max(tradeable, key=lambda c: c[4])

                    # --- SIZING PAR EDGE (Kelly) + plafonds risque + corrélation + carnet ---
                    stake = self._kelly_stake(p_cal, ask, portfolio_value)
                    stake = min(stake, cfg.CRYPTO_STAKE_MAX_USDC,
                                ctx.risk.max_trade_usdc(balance, portfolio_value))
                    avail_bucket = max(0.0, cfg.CRYPTO_MAX_BUCKET_USDC - self._crypto_exposure())
                    stake = min(stake, avail_bucket)
                    if size and size > 0:
                        stake = min(stake, size * ask)   # ne pas dépasser le carnet

                    if stake >= cfg.CRYPTO_STAKE_MIN_USDC and ask > 0:
                        shares = round(stake / ask, 1)
                        if shares >= 1.0:
                            cost = shares * ask
                            balance -= cost
                            db.update_balance(balance)
                            ctx.accumulate_position(token, mk["market_id"], mk["question"], side, shares, ask, mk["end_date"])
                            db.add_trade(mk["market_id"], mk["question"], token, "BUY", side, shares, ask)
                            db.log_bet(token, asset, mk["window"], side, is_up, ask, p_raw, edge,
                                       delta * 100.0, int(t_left), sigma, flow, shares, cost,
                                       kind="crypto")
                            self._open_bets[token] = {
                                "asset": asset, "window": mk["window"],
                                "open_ts": mk["open_ts"], "end_ts": mk["end_ts"],
                                "open_ref": open_ref, "side": side, "is_up": is_up,
                                "market_id": mk["market_id"],
                            }
                            signal["action"] = f"BUY {side}"
                            signal["held"] = True
                            cal_tag = "cal" if self.calibrator.active else "raw"
                            ctx.log(
                                f"CRYPTO {asset} {mk['window']} | Δ {delta*100:+.3f}% t={int(t_left)}s | "
                                f"P={p_raw:.2f}->{p_cal:.2f}({cal_tag}) flow={flow:+.2f} | "
                                f"{side}@{ask:.3f} edge={edge:+.3f} | {shares} parts {cost:.2f}$",
                                "SUCCESS",
                            )

            signals.append(signal)

        # Snapshot pour le frontend + statut d'apprentissage
        signals.sort(key=lambda s: s["t_left"])
        if ctx.crypto_state is not None:
            ctx.crypto_state["signals"] = signals[: cfg.CRYPTO_SIGNALS_MAX]
            ctx.crypto_state["updated_at"] = now
            stats = db.get_bet_stats(kind="crypto")
            ctx.crypto_state["learning"] = {
                "calibrated": self.calibrator.active,
                "samples": stats["settled"],
                "wins": stats["wins"],
                "pnl": round(stats["pnl"], 2),
            }
