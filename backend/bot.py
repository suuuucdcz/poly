"""Orchestrateur du bot de paper trading.

`TradingBot` ne contient plus la logique métier des stratégies : il porte l'état
partagé, instancie les services (client API, feed de prix, gestion du risque),
fait tourner la boucle de tick (mise à jour des positions, règlement des marchés
résolus, snapshot d'equity) puis délègue à la stratégie active.
"""

import asyncio
import json
import traceback
from datetime import datetime

from backend import config, db
from backend.polymarket_client import PolymarketClient
from backend.price_feed import CryptoPriceFeed
from backend.risk import RiskManager
from backend.strategies import (
    ArbitrageStrategy,
    CryptoDirectionStrategy,
    MomentumStrategy,
    TradeContext,
    ValueStrategy,
)


class TradingBot:
    def __init__(self):
        self.is_running = False
        self.strategy = config.DEFAULT_STRATEGY
        self.tick_interval = config.DEFAULT_TICK_INTERVAL
        self.max_markets_to_scan = config.DEFAULT_MAX_MARKETS_TO_SCAN
        self.logs = []
        self.logs_max_size = 150
        self.price_histories = {}   # token_id -> liste de prix
        self.peak_prices = {}       # token_id -> plus haut depuis l'entrée (trailing stop)
        self.active_task = None
        self.lock = asyncio.Lock()

        # Services
        self.client = PolymarketClient()
        self.client.set_logger(self.log)
        self.risk = RiskManager()
        self.feed = CryptoPriceFeed()

        # Stratégies
        self.arbitrage = ArbitrageStrategy()
        self.momentum = MomentumStrategy()
        self.value = ValueStrategy()
        self.crypto = CryptoDirectionStrategy()

        # Snapshot des signaux crypto exposé au frontend
        self.crypto_state = {"signals": [], "updated_at": 0}

    # ============================================================
    # LOGS
    # ============================================================
    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = {"time": timestamp, "level": level, "message": message}
        self.logs.append(log_entry)
        if len(self.logs) > self.logs_max_size:
            self.logs.pop(0)
        print(f"[{timestamp}] [{level}] {message}")

    def get_logs(self):
        return self.logs

    def get_crypto_signals(self):
        return self.crypto_state

    # ============================================================
    # CONTRÔLE
    # ============================================================
    def start(self):
        if self.is_running:
            return False
        self.is_running = True
        self.log("Starting Polymarket Trading Bot (v2 — Hybrid Engine)...")
        self.active_task = asyncio.create_task(self.loop())
        return True

    def stop(self):
        if not self.is_running:
            return False
        self.is_running = False
        self.log("Stopping Polymarket Trading Bot...")
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
                self.log(f"Error in bot tick loop: {e}", "ERROR")
                self.log(traceback.format_exc(), "DEBUG")
            await asyncio.sleep(self.tick_interval)

    # ============================================================
    # PASSERELLES API (utilisées par main.py)
    # ============================================================
    async def fetch_markets(self):
        return await self.client.fetch_markets()

    async def fetch_api_json(self, url):
        return await self.client.fetch_api_json(url)

    async def fetch_book(self, token_id):
        return await self.client.fetch_book(token_id)

    def _make_ctx(self, portfolio_value):
        return TradeContext(
            client=self.client,
            risk=self.risk,
            feed=self.feed,
            log=self.log,
            price_histories=self.price_histories,
            peak_prices=self.peak_prices,
            max_markets_to_scan=self.max_markets_to_scan,
            portfolio_value=portfolio_value,
            crypto_state=self.crypto_state,
        )

    # ============================================================
    # TICK PRINCIPAL
    # ============================================================
    async def tick(self):
        # 1. Mise à jour des positions et règlement des marchés résolus
        portfolio = db.get_portfolio()
        balance = portfolio["balance"]
        positions = db.get_positions()

        market_ids = list(set(pos["market_id"] for pos in positions))
        market_details = {}
        if market_ids:
            tasks = [self.client.fetch_market(mid) for mid in market_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, mid in enumerate(market_ids):
                res = results[idx]
                if res and not isinstance(res, Exception):
                    market_details[mid] = res

        active_positions_to_price = []

        for mid in market_ids:
            m_details = market_details.get(mid)
            if not m_details:
                continue
            try:
                is_closed = m_details.get("closed", False) or not m_details.get("active", True)
                question = m_details.get("question", "Unknown Market")
                m_positions = [p for p in positions if p["market_id"] == mid]

                if is_closed:
                    self.log(f"Market resolved: '{question}'! Settle processing...")
                    try:
                        outcomes = json.loads(m_details.get("outcomes", "[]"))
                        outcome_prices = json.loads(m_details.get("outcomePrices", "[]"))
                    except Exception:
                        continue
                    for pos in m_positions:
                        outcome_name = pos["outcome"]
                        try:
                            idx = outcomes.index(outcome_name)
                            final_price = float(outcome_prices[idx])
                        except (ValueError, IndexError):
                            final_price = 0.0
                        settle_value = pos["shares"] * final_price
                        pnl = settle_value - (pos["shares"] * pos["avg_price"])
                        balance += settle_value
                        db.update_balance(balance)
                        db.add_trade(mid, question, pos["token_id"], "RESOLVE", outcome_name, pos["shares"], final_price, pnl)
                        db.save_position(pos["token_id"], mid, question, outcome_name, 0.0, 0.0, 0.0)
                        self.log(f"RESOLVED: '{question}' -> '{outcome_name}' at {final_price:.2f}. Payout: {settle_value:.2f}. PnL: {pnl:+.2f}", "SUCCESS")
                        # Nettoyage du pic de prix associé
                        if pos["token_id"] in self.peak_prices:
                            del self.peak_prices[pos["token_id"]]
                else:
                    active_positions_to_price.extend(m_positions)
            except Exception as e:
                self.log(f"Error checking market {mid}: {e}", "WARNING")

        # Mise à jour des prix des positions actives
        if active_positions_to_price:
            price_tasks = [self.client.fetch_token_price(pos["token_id"]) for pos in active_positions_to_price]
            clob_prices = await asyncio.gather(*price_tasks, return_exceptions=True)
            for idx, pos in enumerate(active_positions_to_price):
                clob_price = clob_prices[idx]
                if clob_price is not None and not isinstance(clob_price, Exception):
                    db.update_position_price(pos["token_id"], clob_price)
                    tid = pos["token_id"]
                    if tid in self.peak_prices:
                        self.peak_prices[tid] = max(self.peak_prices[tid], clob_price)
                    else:
                        self.peak_prices[tid] = clob_price
                else:
                    mid = pos["market_id"]
                    m_details = market_details.get(mid)
                    if m_details:
                        try:
                            outcomes = json.loads(m_details.get("outcomes", "[]"))
                            outcome_prices = json.loads(m_details.get("outcomePrices", "[]"))
                            o_idx = outcomes.index(pos["outcome"])
                            db.update_position_price(pos["token_id"], float(outcome_prices[o_idx]))
                        except Exception:
                            pass

        # Snapshot
        portfolio = db.get_portfolio()
        balance = portfolio["balance"]
        positions = db.get_positions()
        positions_value = sum(pos["shares"] * pos["current_price"] for pos in positions)
        portfolio_value = balance + positions_value
        db.record_equity_snapshot(portfolio_value)

        self.log(f"Tick (Val: {portfolio_value:.2f}$ | Cash: {balance:.2f}$ | Pos: {len(positions)} | Mode: {self.strategy})")

        # 2. Stratégie active
        ctx = self._make_ctx(portfolio_value)

        # La stratégie crypto fonctionne sur ses propres marchés (slugs déterministes),
        # indépendamment de la liste des marchés liquides génériques.
        if self.strategy == "crypto_direction":
            await self.crypto.run(ctx, [], balance, portfolio_value)
            return

        markets = await self.client.fetch_markets()
        if not markets:
            return

        if self.strategy == "arbitrage":
            await self.arbitrage.run(ctx, markets, balance, portfolio_value)
        elif self.strategy == "momentum":
            await self.momentum.run(ctx, markets, balance, portfolio_value)
        elif self.strategy == "value":
            await self.value.run(ctx, markets, balance, portfolio_value)
        elif self.strategy == "hybrid":
            # Toutes les stratégies en séquence à chaque tick
            await self.arbitrage.run(ctx, markets, balance, portfolio_value)
            balance = db.get_portfolio()["balance"]
            await self.momentum.run(ctx, markets, balance, portfolio_value)
            balance = db.get_portfolio()["balance"]
            await self.value.run(ctx, markets, balance, portfolio_value)

    # ============================================================
    # TRADES MANUELS
    # ============================================================
    async def place_manual_trade(self, market_id, token_id, action, outcome, amount_usdc):
        async with self.lock:
            portfolio = db.get_portfolio()
            balance = portfolio["balance"]

            if action == "SELL":
                existing = db.get_position(token_id)
                if not existing or existing["shares"] <= 0:
                    raise Exception("You do not hold a position in this token.")
                market_id = existing["market_id"]
                question = existing["question"]
            else:
                m_details = await self.client.fetch_market(market_id)
                question = m_details.get("question", "Unknown Market")

            book = await self.client.fetch_book(token_id)
            if not book:
                raise Exception("Could not retrieve order book for this token.")

            if action == "BUY":
                if amount_usdc > balance:
                    raise Exception(f"Insufficient funds. Required {amount_usdc:.2f} USDC, available {balance:.2f} USDC.")
                asks = book.get("asks", [])
                if not asks:
                    raise Exception("No sell orders available.")
                sorted_asks = sorted(asks, key=lambda x: float(x["price"]))
                price = float(sorted_asks[0]["price"])
                size_available = float(sorted_asks[0]["size"])
                shares = min(amount_usdc / price, size_available)
                shares = round(shares, 1)
                if shares <= 0:
                    raise Exception("Order size too small.")
                cost = shares * price
                balance -= cost
                end_date = None
                try:
                    md = await self.client.fetch_market(market_id)
                    end_date = md.get("endDate") or md.get("endDateIso")
                except Exception:
                    pass
                db.update_balance(balance)
                self._accumulate_position(token_id, market_id, question, outcome, shares, price, end_date)
                db.add_trade(market_id, question, token_id, "BUY", outcome, shares, price)
                self.log(f"MANUAL BUY: {shares} x {outcome} in '{question}' @ {price:.2f}$. Cost: {cost:.2f}$", "INFO")
                return {"shares": shares, "price": price, "cost": cost}

            elif action == "SELL":
                existing = db.get_position(token_id)
                if not existing or existing["shares"] <= 0:
                    raise Exception("You do not hold a position in this token.")
                bids = book.get("bids", [])
                if not bids:
                    raise Exception("No buy orders available.")
                sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
                price = float(sorted_bids[0]["price"])
                size_available = float(sorted_bids[0]["size"])
                shares_to_sell = min(existing["shares"], size_available)
                shares_to_sell = round(shares_to_sell, 1)
                if shares_to_sell <= 0:
                    raise Exception("Order size too small.")
                revenue = shares_to_sell * price
                pnl = revenue - (shares_to_sell * existing["avg_price"])
                balance += revenue
                new_shares = existing["shares"] - shares_to_sell
                db.update_balance(balance)
                db.save_position(token_id, market_id, question, outcome, new_shares, existing["avg_price"], price)
                db.add_trade(market_id, question, token_id, "SELL", outcome, shares_to_sell, price, pnl)
                self.log(f"MANUAL SELL: {shares_to_sell} x {outcome} in '{question}' @ {price:.2f}$. PnL: {pnl:+.2f}$", "INFO")
                return {"shares": shares_to_sell, "price": price, "revenue": revenue, "pnl": pnl}
            else:
                raise Exception(f"Invalid action: {action}")

    def _accumulate_position(self, token_id, market_id, question, outcome, new_shares, new_price, end_date=None, payout_multiplier=None):
        """Accumulation pondérée pour les trades manuels (réutilise la logique du contexte)."""
        existing = db.get_position(token_id)
        if existing and existing["shares"] > 0.0001:
            old_shares = existing["shares"]
            old_avg = existing["avg_price"]
            total_shares = old_shares + new_shares
            weighted_avg = ((old_shares * old_avg) + (new_shares * new_price)) / total_shares
            if end_date is None:
                end_date = existing.get("end_date")
            db.save_position(token_id, market_id, question, outcome, total_shares, weighted_avg, new_price, end_date, payout_multiplier)
            return total_shares, weighted_avg
        else:
            db.save_position(token_id, market_id, question, outcome, new_shares, new_price, new_price, end_date, payout_multiplier)
            self.peak_prices[token_id] = new_price
            return new_shares, new_price
