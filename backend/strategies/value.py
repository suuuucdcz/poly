"""Stratégie 3 — Value / Mean Reversion (surventes).

Extraite à l'identique de l'ancien `TradingBot.run_value_strategy`.
"""

import json

from backend import config, db
from backend.indicators import rsi
from backend.strategies.base import Strategy


class ValueStrategy(Strategy):
    name = "value"

    async def run(self, ctx, markets, balance, portfolio_value):
        positions = db.get_positions()

        # Exit: check if mean-reversion positions have recovered
        for pos in positions:
            token_id = pos["token_id"]
            history = ctx.price_histories.get(token_id, [])
            if len(history) < config.MIN_TICKS_FOR_TRADE:
                continue
            current_price = pos["current_price"]
            avg_price = pos["avg_price"]
            sma20 = sum(history[-20:]) / min(len(history), 20)

            trigger_sell = False
            reason = ""

            # Mean reversion target: price returned to average
            if current_price >= sma20 and current_price > avg_price * 1.02:
                trigger_sell = True
                reason = f"Mean Reversion Target (price {current_price:.3f} >= SMA20 {sma20:.3f})"
            # Hard stop loss at -12%
            elif current_price <= avg_price * 0.88:
                trigger_sell = True
                reason = f"Value Stop Loss (-12%: {current_price:.3f} <= {avg_price*0.88:.3f})"
            # Market closing soon
            elif ctx.risk.market_closing_too_soon(pos.get("end_date")):
                trigger_sell = True
                reason = "Market closing < 48h"

            if trigger_sell:
                # Only sell if we don't also have a momentum or arb position
                # (check if momentum strategy already manages this position)
                book = await ctx.client.fetch_book(token_id)
                if not book or not book.get("bids"):
                    continue
                sorted_bids = sorted(book["bids"], key=lambda x: float(x["price"]), reverse=True)
                bid_price = float(sorted_bids[0]["price"])
                bid_size = float(sorted_bids[0]["size"])
                shares_to_sell = min(pos["shares"], bid_size)
                shares_to_sell = round(shares_to_sell, 1)
                if shares_to_sell <= 0.1:
                    continue
                payout = shares_to_sell * bid_price
                pnl = payout - (shares_to_sell * avg_price)
                balance += payout
                db.update_balance(balance)
                remaining = pos["shares"] - shares_to_sell
                db.save_position(token_id, pos["market_id"], pos["question"], pos["outcome"], remaining, avg_price, bid_price)
                db.add_trade(pos["market_id"], pos["question"], token_id, "SELL", pos["outcome"], shares_to_sell, bid_price, pnl)
                ctx.log(f"VAL SELL: {reason} | '{pos['question'][:40]}' | PnL: {pnl:+.2f}$", "SUCCESS" if pnl >= 0 else "WARNING")

        # Entry: look for oversold tokens (mean reversion buy)
        scanned = 0
        for m in markets:
            if scanned >= ctx.max_markets_to_scan:
                break
            market_id = m.get("id")
            end_date = m.get("endDate") or m.get("endDateIso")
            question = m.get("question")

            if ctx.risk.market_closing_too_soon(end_date):
                continue

            try:
                clob_token_ids = json.loads(m.get("clobTokenIds", "[]"))
                outcomes = json.loads(m.get("outcomes", "[]"))
                outcome_prices = json.loads(m.get("outcomePrices", "[]"))
            except Exception:
                continue
            scanned += 1

            for i, token_id in enumerate(clob_token_ids):
                current_price = float(outcome_prices[i])

                # Update history (if not already done by momentum)
                if token_id not in ctx.price_histories:
                    ctx.price_histories[token_id] = []
                if not ctx.price_histories[token_id] or ctx.price_histories[token_id][-1] != current_price:
                    ctx.price_histories[token_id].append(current_price)
                    if len(ctx.price_histories[token_id]) > config.HISTORY_MAX_LEN:
                        ctx.price_histories[token_id].pop(0)

                history = ctx.price_histories[token_id]
                if len(history) < config.MIN_TICKS_FOR_TRADE:
                    continue

                # Already hold?
                if db.get_position(token_id):
                    continue

                sma20 = sum(history[-20:]) / min(len(history), 20)
                rsi_val = rsi(history, config.RSI_PERIOD)

                # Value signal: price dropped > 8% below SMA20 and RSI < 30 (oversold)
                if current_price < sma20 * 0.92 and rsi_val < 30 and 0.08 < current_price < 0.80:
                    book = await ctx.client.fetch_book(token_id)
                    if not book or not book.get("asks"):
                        continue
                    if ctx.risk.spread_too_wide(book):
                        continue

                    sorted_asks = sorted(book["asks"], key=lambda x: float(x["price"]))
                    ask_price = float(sorted_asks[0]["price"])
                    ask_size = float(sorted_asks[0]["size"])

                    buy_usdc = ctx.risk.max_trade_usdc(balance, portfolio_value) * 0.4
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
                    ctx.log(f"VAL BUY: '{question[:40]}' {outcomes[i]} | RSI:{rsi_val:.0f} Price:{current_price:.3f} < SMA20:{sma20:.3f}*0.92 | {shares_to_buy} @ {ask_price:.2f}$", "SUCCESS")
