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
from backend.strategies import TradeContext, WeatherEdgeStrategy


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
        self.weather = WeatherEdgeStrategy()

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
        self.active_task = asyncio.create_task(self.loop())
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
                            db.add_trade(mid, question, pos["token_id"], "RESOLVE", pos["outcome"], pos["shares"], price, pnl)
                            db.settle_bet(pos["token_id"], 1 if price >= 0.5 else 0, pnl)
                            db.save_position(pos["token_id"], mid, question, pos["outcome"], 0.0, 0.0, 0.0)
                            self.log(f"RESOLVED: '{question}' -> {price:.2f}. PnL: {pnl:+.2f}$", "SUCCESS" if pnl >= 0 else "WARNING")
                        else:
                            db.update_position_price(pos["token_id"], price)
                except Exception as e:
                    self.log(f"Error checking market {mid}: {e}", "WARNING")

        # 2. Snapshot d'equity
        portfolio = db.get_portfolio()
        balance = portfolio["balance"]
        positions = db.get_positions()
        positions_value = sum(p["shares"] * p["current_price"] for p in positions)
        portfolio_value = balance + positions_value
        db.record_equity_snapshot(portfolio_value)

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
