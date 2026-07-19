"""Orchestrateur du bot — stratégie unique : Weather Edge (météo).

Boucle de tick : règle les marchés résolus, rafraîchit le prix courant des
positions ouvertes (PnL latent vivant), snapshot d'equity, puis délègue à la
stratégie météo. 100 % paper trading.
"""

import asyncio
import json
import traceback
from datetime import datetime

from backend import config, db
from backend.polymarket_client import PolymarketClient
from backend.risk import RiskManager
from backend.strategies import (
    NegRiskArbStrategy,
    ResolutionSweepStrategy,
    TradeContext,
    WeatherConvergenceStrategy,
    WeatherEdgeStrategy,
)


class TradingBot:
    def __init__(self):
        self.is_running = False
        self.strategy = config.DEFAULT_STRATEGY        # "weather"
        self.tick_interval = config.DEFAULT_TICK_INTERVAL
        self.logs = []
        self.logs_max_size = 150
        self.active_task = None
        self.lock = asyncio.Lock()

        self.client = PolymarketClient()
        self.client.set_logger(self.log)
        self.risk = RiskManager()
        # Moteur actif : "convergence" (intraday, le vrai edge) ou "edge" (ancien
        # modèle prévision, conservé pour référence). Voir config.STRATEGY_ENGINE.
        if getattr(config, "STRATEGY_ENGINE", "convergence") == "convergence":
            self.weather = WeatherConvergenceStrategy()
        else:
            self.weather = WeatherEdgeStrategy()
        # Arbitrage de panier NegRisk : edge structurel, tourne EN PLUS de la
        # convergence (mêmes events via le cache de découverte, positions [ARB]).
        self.arb = NegRiskArbStrategy()
        # Balayage de résolution : achète le gagnant déjà connu (soirée/lendemain).
        # Partage le feed du moteur météo (cache METAR commun -> zéro appel en plus).
        self.sweep = ResolutionSweepStrategy()
        self.sweep.feed = getattr(self.weather, "feed", None)

        # État exposé au frontend (signaux météo + apprentissage)
        self.ui_state = {"weather": [], "updated_at": 0, "learning": {}}

    # ============================================================
    # LOGS / ÉTAT
    # ============================================================
    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append({"time": timestamp, "level": level, "message": message})
        if len(self.logs) > self.logs_max_size:
            self.logs.pop(0)
        print(f"[{timestamp}] [{level}] {message}")

    def get_logs(self):
        return self.logs

    def get_signals(self):
        return self.ui_state

    # ============================================================
    # CONTRÔLE
    # ============================================================
    def start(self):
        if self.is_running:
            return False
        self.is_running = True
        self.log("Starting PolyQuant Weather Bot...")
        try:
            self.active_task = asyncio.create_task(self.loop())
        except RuntimeError as e:
            # Appelé hors de la boucle asyncio (ex. endpoint FastAPI synchrone,
            # exécuté dans un thread) : create_task échoue -> SANS ce rollback,
            # is_running restait True avec AUCUNE boucle = bot zombie (30 h de
            # gel constatées le 08-09/07 après un clic Stop/Start).
            self.is_running = False
            self.active_task = None
            self.log(f"Démarrage impossible (pas de boucle asyncio ici): {e}", "ERROR")
            return False
        return True

    def stop(self):
        if not self.is_running:
            return False
        self.is_running = False
        self.log("Stopping bot...")
        if self.active_task:
            self.active_task.cancel()
            self.active_task = None
        return True

    async def loop(self):
        while self.is_running:
            try:
                async with self.lock:
                    await self.tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log(f"Error in tick loop: {e}", "ERROR")
                self.log(traceback.format_exc(), "DEBUG")
            await asyncio.sleep(self.tick_interval)

    # ============================================================
    # TICK PRINCIPAL
    # ============================================================
    async def tick(self):
        # 1. Marchés des positions ouvertes : règlement si résolus,
        #    sinon mise à jour du prix courant (PnL latent vivant).
        portfolio = db.get_portfolio()
        balance = portfolio["balance"]
        positions = db.get_positions()

        market_ids = list(set(pos["market_id"] for pos in positions))
        if market_ids:
            tasks = [self.client.fetch_market(mid) for mid in market_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            details = {
                mid: res for mid, res in zip(market_ids, results)
                if res and not isinstance(res, Exception)
            }
            for mid, m in details.items():
                try:
                    question = m.get("question", "Unknown Market")
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    is_closed = m.get("closed", False) or not m.get("active", True)
                    for pos in [p for p in positions if p["market_id"] == mid]:
                        try:
                            idx = outcomes.index(pos["outcome"])
                            price = float(prices[idx])
                        except (ValueError, IndexError):
                            price = 0.0
                        if is_closed:
                            settle_value = pos["shares"] * price
                            pnl = settle_value - (pos["shares"] * pos["avg_price"])
                            balance += settle_value
                            db.update_balance(balance)
                            # question de la POSITION (conserve le tag [ARB]/[SWEEP]
                            # dans le journal), pas celle du marché
                            db.add_trade(mid, pos["question"] or question, pos["token_id"],
                                         "RESOLVE", pos["outcome"], pos["shares"], price, pnl)
                            db.settle_bet(pos["token_id"], 1 if price >= 0.5 else 0, pnl)
                            db.save_position(pos["token_id"], mid, question, pos["outcome"], 0.0, 0.0, 0.0)
                            self.log(f"RESOLVED: '{question}' -> {price:.2f}. PnL: {pnl:+.2f}$", "SUCCESS" if pnl >= 0 else "WARNING")
                        else:
                            db.update_position_price(pos["token_id"], price)
                except Exception as e:
                    self.log(f"Error checking market {mid}: {e}", "WARNING")

        # 2. Snapshot d'equity + écrémage éventuel
        portfolio = db.get_portfolio()
        balance = portfolio["balance"]
        positions = db.get_positions()
        positions_value = sum(p["shares"] * p["current_price"] for p in positions)
        portfolio_value = balance + positions_value

        # Écrémage automatique AVANT le snapshot ; la courbe enregistre la
        # RICHESSE TOTALE (equity + banque) -> jamais de fausse falaise le jour
        # d'un écrémage.
        bank = db.get_bank()
        wealth = portfolio_value + bank["total"]
        try:
            skimmed = self._bank_check(balance, portfolio_value, bank, wealth)
            if skimmed:
                portfolio_value -= skimmed
                balance -= skimmed
        except Exception as e:
            self.log(f"BANQUE: erreur écrémage: {e}", "WARNING")
        db.update_bank_hwm(wealth)
        db.record_equity_snapshot(portfolio_value + db.get_bank()["total"])

        self.log(f"Tick (Val: {portfolio_value:.2f}$ | Cash: {balance:.2f}$ | Pos: {len(positions)})")

        # 3. Stratégie météo
        ctx = TradeContext(
            client=self.client,
            risk=self.risk,
            log=self.log,
            portfolio_value=portfolio_value,
            ui_state=self.ui_state,
        )
        await self.weather.run(ctx, [], balance, portfolio_value)

        # 4. Arbitrage de panier (NegRisk) — après la convergence, cache partagé
        try:
            await self.arb.run(ctx, [], balance, portfolio_value)
        except Exception as e:
            self.log(f"ARB: erreur scanner: {e}", "WARNING")

        # 5. Balayage de résolution — gagnants déjà connus payés < 1.00
        try:
            await self.sweep.run(ctx, [], balance, portfolio_value)
        except Exception as e:
            self.log(f"SWEEP: erreur: {e}", "WARNING")

    # ============================================================
    # ÉCRÉMAGE DES PROFITS (règle bancaire — voir config)
    # ============================================================
    def _bank_check(self, balance, equity, bank, wealth):
        """Écrème les profits vers la banque si TOUTES les conditions sont
        réunies : (1) pas déjà écrémé ce mois calendaire, (2) richesse totale
        au PLUS HAUT historique (jamais en plein creux), (3) montant ≥ minimum
        (jamais de miettes), (4) le capital de travail reste intact et on ne
        prend que du CASH disponible. Renvoie le montant écrémé (0 sinon)."""
        if not getattr(config, "BANK_AUTO", False):
            return 0.0
        now = datetime.utcnow()
        if (bank["last_skim"] or "")[:7] == now.strftime("%Y-%m"):
            return 0.0                          # déjà écrémé ce mois-ci
        if wealth < bank["hwm"] - 1e-9:
            return 0.0                          # pas au plus haut -> on attend
        skim = round(min(balance, equity - config.BANK_KEEP_WORKING), 2)
        if skim < config.BANK_MIN_SKIM:
            return 0.0
        db.bank_skim(skim, wealth)
        self.log(
            f"ÉCRÉMAGE: {skim:.2f}$ mis en banque (banque totale: {bank['total'] + skim:.2f}$ | "
            f"capital de travail conservé: {equity - skim:.2f}$)",
            "SUCCESS",
        )
        return skim

    # ============================================================
    # PURGE DES PARIS DE L'ANCIEN MODÈLE (action unique, sur demande)
    # Vend au bid ce qui a un acheteur, passe le reste en perte (paper) :
    # libère les plafonds régionaux et rend l'equity 100 % « modèle V2 ».
    # ============================================================
    async def purge_old_positions(self, cutoff):
        async with self.lock:
            sold_value = 0.0
            written_off_cost = 0.0
            closed = 0
            for pos in db.get_positions():
                if (pos.get("opened_at") or "9999") >= cutoff:
                    continue
                token = pos["token_id"]
                remaining = pos["shares"]
                if remaining <= 0:
                    continue
                realized = 0.0
                # vendre au bid, jusqu'à 4 niveaux de carnet
                for _ in range(4):
                    if remaining < 1:
                        break
                    book = await self.client.fetch_book(token)
                    bids = (book or {}).get("bids", [])
                    if not bids:
                        break
                    best = max(bids, key=lambda x: float(x["price"]))
                    price, size = float(best["price"]), float(best["size"])
                    if price < 0.01:
                        break
                    qty = round(min(remaining, size), 1)
                    if qty < 1:
                        break
                    rev = qty * price
                    pnl = rev - qty * pos["avg_price"]
                    db.update_balance(db.get_portfolio()["balance"] + rev)
                    remaining = round(remaining - qty, 1)
                    db.save_position(token, pos["market_id"], pos["question"], pos["outcome"], remaining, pos["avg_price"], price)
                    db.add_trade(pos["market_id"], pos["question"], token, "SELL", pos["outcome"], qty, price, pnl)
                    sold_value += rev
                    realized += pnl
                if remaining >= 0.1:
                    # pas d'acheteur : on acte la perte (paper) et on libère le plafond
                    pnl = -remaining * pos["avg_price"]
                    db.add_trade(pos["market_id"], pos["question"], token, "SELL", pos["outcome"], remaining, 0.0, pnl)
                    db.save_position(token, pos["market_id"], pos["question"], pos["outcome"], 0.0, 0.0, 0.0)
                    written_off_cost += remaining * pos["avg_price"]
                    realized += pnl
                db.settle_bet(token, None, realized)   # sortie hors-calibration
                closed += 1
            self.log(
                f"PURGE ancien modèle: {closed} positions fermées | récupéré {sold_value:.2f}$ au bid | "
                f"passé en perte {written_off_cost:.2f}$ de coût",
                "INFO",
            )
            return {"closed": closed, "recovered": round(sold_value, 2), "written_off": round(written_off_cost, 2)}

    # ============================================================
    # VENTE MANUELLE (« Vendre tout » du dashboard)
    # ============================================================
    async def sell_position(self, token_id):
        async with self.lock:
            existing = db.get_position(token_id)
            if not existing or existing["shares"] <= 0:
                raise Exception("Aucune position sur ce token.")
            book = await self.client.fetch_book(token_id)
            bids = (book or {}).get("bids", [])
            if not bids:
                raise Exception("Pas d'acheteur dans le carnet pour l'instant.")
            best = max(bids, key=lambda x: float(x["price"]))
            price = float(best["price"])
            size_available = float(best["size"])
            shares_to_sell = round(min(existing["shares"], size_available), 1)
            if shares_to_sell <= 0:
                raise Exception("Taille d'ordre trop petite.")
            revenue = shares_to_sell * price
            pnl = revenue - (shares_to_sell * existing["avg_price"])
            balance = db.get_portfolio()["balance"] + revenue
            new_shares = existing["shares"] - shares_to_sell
            db.update_balance(balance)
            db.save_position(token_id, existing["market_id"], existing["question"], existing["outcome"], new_shares, existing["avg_price"], price)
            db.add_trade(existing["market_id"], existing["question"], token_id, "SELL", existing["outcome"], shares_to_sell, price, pnl)
            self.log(f"MANUAL SELL: {shares_to_sell} x '{existing['question'][:50]}' @ {price:.2f}$. PnL {pnl:+.2f}$", "INFO")
            return {"shares": shares_to_sell, "price": price, "revenue": revenue, "pnl": pnl}
