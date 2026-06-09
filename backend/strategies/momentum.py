"""Stratégie 2 — Momentum (croisement d'EMA + filtre RSI).

Extraite à l'identique de l'ancien `TradingBot.run_momentum_strategy`.
"""

import json

from backend import config, db
from backend.indicators import ema, rsi
from backend.strategies.base import Strategy


class MomentumStrategy(Strategy):
    name = "momentum"

    async def run(self, ctx, markets, balance, portfolio_value):
        scanned_count = 0
        positions = db.get_positions()

        # First: check exit signals for existing momentum positions
        for pos in positions:
            token_id = pos["token_id"]
            history = ctx.price_histories.get(token_id, [])
            if len(history) < config.MIN_TICKS_FOR_TRADE:
                continue

            shares = pos["shares"]
            avg_price = pos["avg_price"]
            current_price = pos["current_price"]

            # Compute indicators
            ema_fast = ema(history, config.EMA_FAST_PERIOD)
            ema_slow = ema(history, config.EMA_SLOW_PERIOD)

            # Trailing stop: sell if price dropped 5% from peak
            peak = ctx.peak_prices.get(token_id, avg_price)
            trailing_stop_price = peak * 0.95

            trigger_sell = False
            reason = ""

            # Trailing Stop Loss
            if current_price <= trailing_stop_price and current_price < avg_price:
                trigger_sell = True
                reason = f"Trailing Stop (price {current_price:.3f} < peak {peak:.3f} * 0.95)"
            # Take Profit: +10%
            elif current_price >= avg_price * 1.10:
                trigger_sell = True
                reason = f"Take Profit (+10%: {current_price:.3f} >= {avg_price*1.10:.3f})"
            # Death Cross: fast EMA crosses below slow EMA
            elif ema_fast < ema_slow and current_price < avg_price:
                trigger_sell = True
                reason = f"Death Cross (EMA8:{ema_fast:.3f} < EMA21:{ema_slow:.3f})"
            # Market closing soon
            elif ctx.risk.market_closing_too_soon(pos.get("end_date")):
                trigger_sell = True
                reason = "Market closing < 48h"

            if trigger_sell:
                book = await ctx.client.fetch_book(token_id)
                if not book or not book.get("bids"):
                    continue
                sorted_bids = sorted(book["bids"], key=lambda x: float(x["price"]), reverse=True)
                bid_price = float(sorted_bids[0]["price"])
                bid_size = float(sorted_bids[0]["size"])
                shares_to_sell = min(shares, bid_size)
                shares_to_sell = round(shares_to_sell, 1)
                if shares_to_sell <= 0.1:
                    continue
                payout = shares_to_sell * bid_price
                pnl = payout - (shares_to_sell * avg_price)
                balance += payout
                db.update_balance(balance)
                remaining = shares - shares_to_sell
                db.save_position(token_id, pos["market_id"], pos["question"], pos["outcome"], remaining, avg_price, bid_price)
                db.add_trade(pos["market_id"], pos["question"], token_id, "SELL", pos["outcome"], shares_to_sell, bid_price, pnl)
                if token_id in ctx.peak_prices:
                    del ctx.peak_prices[token_id]
                ctx.log(f"MOM SELL: {reason} | '{pos['question'][:40]}' | PnL: {pnl:+.2f}$", "SUCCESS" if pnl >= 0 else "WARNING")

        # Then: scan for new entry signals
        for m in markets:
            if scanned_count >= ctx.max_markets_to_scan:
                break
            market_id = m.get("id")
            question = m.get("question")
            end_date = m.get("endDate") or m.get("endDateIso")

            if ctx.risk.market_closing_too_soon(end_date):
                continue

            try:
                clob_token_ids = json.loads(m.get("clobTokenIds", "[]"))
                outcomes = json.loads(m.get("outcomes", "[]"))
                outcome_prices = json.loads(m.get("outcomePrices", "[]"))
            except Exception:
                continue
            scanned_count += 1

            for i, token_id in enumerate(clob_token_ids):
                current_price = float(outcome_prices[i])

                # Update price history
                if token_id not in ctx.price_histories:
                    ctx.price_histories[token_id] = []
                ctx.price_histories[token_id].append(current_price)
                if len(ctx.price_histories[token_id]) > config.HISTORY_MAX_LEN:
                    ctx.price_histories[token_id].pop(0)

                history = ctx.price_histories[token_id]
                if len(history) < config.MIN_TICKS_FOR_TRADE:
                    continue

                # Already hold this token?
                if db.get_position(token_id):
                    continue

                # Compute indicators
                ema_fast = ema(history, config.EMA_FAST_PERIOD)
                ema_slow = ema(history, config.EMA_SLOW_PERIOD)
                rsi_val = rsi(history, config.RSI_PERIOD)

                # Entry signal: Golden Cross + RSI not overbought + price in range
                if ema_fast > ema_slow and 30 < rsi_val < 65 and 0.12 < current_price < 0.85:
                    book = await ctx.client.fetch_book(token_id)
                    if not book or not book.get("asks"):
                        continue
                    if ctx.risk.spread_too_wide(book):
                        continue

                    sorted_asks = sorted(book["asks"], key=lambda x: float(x["price"]))
                    ask_price = float(sorted_asks[0]["price"])
                    ask_size = float(sorted_asks[0]["size"])

                    buy_usdc = ctx.risk.max_trade_usdc(balance, portfolio_value) * 0.5
                    if buy_usdc < 5.0:
                        continue
                    shares_to_buy = min(buy_usdc / ask_price, ask_size)
                    shares_to_buy = round(shares_to_buy, 1)
                    if shares_to_buy < 3:
                        continue

                    cost = shares_to_buy * ask_price
                    balance -= cost
                    db.update_balance(balance)
                    ctx.accumulate_position(token_id, market_id, question, outcomes[i], shares_to_buy, ask_price, end_date)
                    db.add_trade(market_id, question, token_id, "BUY", outcomes[i], shares_to_buy, ask_price)
                    ctx.log(f"MOM BUY: '{question[:40]}' {outcomes[i]} | EMA8:{ema_fast:.3f}>EMA21:{ema_slow:.3f} RSI:{rsi_val:.0f} | {shares_to_buy} @ {ask_price:.2f}$", "SUCCESS")
