"""Stratégie 1 — Arbitrage (avec frais réels).

Extraite à l'identique de l'ancien `TradingBot.run_arbitrage_strategy`.
Couvre l'arbitrage neg-risk multi-marchés (YES et NO) et l'arbitrage binaire.
"""

import json

from backend import db
from backend.strategies.base import Strategy


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    async def run(self, ctx, markets, balance, portfolio_value):
        positions = db.get_positions()
        scanned_events = set()
        scanned_markets = set()

        for m in markets:
            market_id = m.get("id")
            if market_id in scanned_markets:
                continue
            question = m.get("question")
            neg_risk = m.get("negRisk", False)
            events_list = m.get("events", [])
            end_date = m.get("endDate") or m.get("endDateIso")

            # Skip markets closing too soon
            if ctx.risk.market_closing_too_soon(end_date):
                scanned_markets.add(market_id)
                continue

            # --- NEG RISK ARBITRAGE ---
            if neg_risk and events_list:
                event_id = events_list[0].get("id")
                if not event_id or event_id in scanned_events:
                    continue
                scanned_events.add(event_id)

                try:
                    event_data = await ctx.client.fetch_event(event_id)
                    event_markets = event_data.get("markets", [])
                    event_title = event_data.get("title", question)
                except Exception:
                    continue
                if len(event_markets) < 2:
                    continue

                event_market_ids = [em.get("id") for em in event_markets]
                if any(pos["market_id"] in event_market_ids for pos in positions):
                    for em in event_markets:
                        scanned_markets.add(em.get("id"))
                    continue

                any_closed = False
                for em in event_markets:
                    if em.get("closed", False) or not em.get("active", True):
                        any_closed = True
                        break
                    scanned_markets.add(em.get("id"))
                if any_closed:
                    continue

                # YES arbitrage
                clob_ids_list = []
                yes_tokens = []
                for em in event_markets:
                    c_ids = json.loads(em.get("clobTokenIds", "[]"))
                    clob_ids_list.append(c_ids)
                    yes_tokens.append(c_ids[0] if c_ids else None)
                if None in yes_tokens:
                    continue

                books = await ctx.client.fetch_books_concurrently(yes_tokens)
                yes_books = []
                for idx, book in enumerate(books):
                    if not book or not book.get("asks"):
                        break
                    yes_books.append((event_markets[idx], clob_ids_list[idx], book))
                if len(yes_books) != len(event_markets):
                    continue

                yes_asks = []
                yes_sizes = []
                for em, clob_ids, book in yes_books:
                    sorted_asks = sorted(book["asks"], key=lambda x: float(x["price"]))
                    yes_asks.append(float(sorted_asks[0]["price"]))
                    yes_sizes.append(float(sorted_asks[0]["size"]))

                price_sum = sum(yes_asks)
                total_fees = ctx.risk.total_arb_fees(yes_asks)
                net_cost = price_sum + total_fees

                # Profit only if net cost < 0.97 (3% min profit after fees)
                if 0.35 < net_cost < 0.97:
                    max_trade = ctx.risk.max_trade_usdc(balance, portfolio_value)
                    if max_trade < 5.0:
                        continue
                    shares_to_buy = min([max_trade / net_cost] + yes_sizes)
                    shares_to_buy = round(shares_to_buy, 1)
                    if shares_to_buy < 1.0:
                        continue

                    total_cost = shares_to_buy * net_cost
                    balance -= total_cost
                    db.update_balance(balance)

                    for idx, (em, clob_ids, book) in enumerate(yes_books):
                        em_q = em.get("question")
                        outcomes = json.loads(em.get("outcomes", "[]"))
                        em_end = em.get("endDate") or em.get("endDateIso")
                        ctx.accumulate_position(clob_ids[0], em.get("id"), em_q, outcomes[0], shares_to_buy, yes_asks[idx], em_end, payout_multiplier=1.0 / len(event_markets))
                        db.add_trade(em.get("id"), em_q, clob_ids[0], "BUY", outcomes[0], shares_to_buy, yes_asks[idx])

                    net_profit = shares_to_buy * (1.00 - net_cost)
                    ctx.log(f"ARB YES: '{event_title}' | Cost: {total_cost:.2f}$ (fees: {total_fees*shares_to_buy:.2f}$) | Net Profit: {net_profit:.2f}$", "SUCCESS")
                    break

                # NO arbitrage
                N = len(event_markets)
                no_tokens = []
                clob_ids_list2 = []
                for em in event_markets:
                    c_ids = json.loads(em.get("clobTokenIds", "[]"))
                    clob_ids_list2.append(c_ids)
                    no_tokens.append(c_ids[1] if len(c_ids) >= 2 else None)
                if None in no_tokens:
                    continue

                books_no = await ctx.client.fetch_books_concurrently(no_tokens)
                no_books = []
                for idx, book in enumerate(books_no):
                    if not book or not book.get("asks"):
                        break
                    no_books.append((event_markets[idx], clob_ids_list2[idx], book))
                if len(no_books) != N:
                    continue

                no_asks = []
                no_sizes = []
                for em, clob_ids, book in no_books:
                    sorted_asks = sorted(book["asks"], key=lambda x: float(x["price"]))
                    no_asks.append(float(sorted_asks[0]["price"]))
                    no_sizes.append(float(sorted_asks[0]["size"]))

                no_price_sum = sum(no_asks)
                no_fees = ctx.risk.total_arb_fees(no_asks)
                no_net_cost = no_price_sum + no_fees
                target_payout = N - 1.0

                if 0.35 < no_net_cost < (target_payout - 0.03):
                    max_trade = ctx.risk.max_trade_usdc(balance, portfolio_value)
                    if max_trade < 5.0:
                        continue
                    shares_to_buy = min([max_trade / no_net_cost] + no_sizes)
                    shares_to_buy = round(shares_to_buy, 1)
                    if shares_to_buy < 1.0:
                        continue
                    total_cost = shares_to_buy * no_net_cost
                    balance -= total_cost
                    db.update_balance(balance)
                    for idx, (em, clob_ids, book) in enumerate(no_books):
                        em_q = em.get("question")
                        outcomes = json.loads(em.get("outcomes", "[]"))
                        em_end = em.get("endDate") or em.get("endDateIso")
                        ctx.accumulate_position(clob_ids[1], em.get("id"), em_q, outcomes[1], shares_to_buy, no_asks[idx], em_end, payout_multiplier=(N - 1.0) / N)
                        db.add_trade(em.get("id"), em_q, clob_ids[1], "BUY", outcomes[1], shares_to_buy, no_asks[idx])
                    net_profit = shares_to_buy * (target_payout - no_net_cost)
                    ctx.log(f"ARB NO: '{event_title}' | Cost: {total_cost:.2f}$ (fees: {no_fees*shares_to_buy:.2f}$) | Net Profit: {net_profit:.2f}$", "SUCCESS")
                    break

            # --- BINARY ARBITRAGE ---
            elif not neg_risk:
                scanned_markets.add(market_id)
                if any(pos["market_id"] == market_id for pos in positions):
                    continue
                if ctx.risk.market_closing_too_soon(end_date):
                    continue
                try:
                    clob_token_ids = json.loads(m.get("clobTokenIds", "[]"))
                    outcomes = json.loads(m.get("outcomes", "[]"))
                except Exception:
                    continue
                if len(clob_token_ids) < 2:
                    continue

                books = await ctx.client.fetch_books_concurrently([clob_token_ids[0], clob_token_ids[1]])
                if not books[0] or not books[1]:
                    continue
                yes_asks_list = books[0].get("asks", [])
                no_asks_list = books[1].get("asks", [])
                if not yes_asks_list or not no_asks_list:
                    continue

                ask_yes = float(min(yes_asks_list, key=lambda x: float(x["price"]))["price"])
                ask_no = float(min(no_asks_list, key=lambda x: float(x["price"]))["price"])
                ask_yes_size = float(min(yes_asks_list, key=lambda x: float(x["price"]))["size"])
                ask_no_size = float(min(no_asks_list, key=lambda x: float(x["price"]))["size"])

                fees = ctx.risk.taker_fee(ask_yes) + ctx.risk.taker_fee(ask_no)
                net_cost = ask_yes + ask_no + fees

                if 0.40 < net_cost < 0.97:
                    max_trade = ctx.risk.max_trade_usdc(balance, portfolio_value)
                    if max_trade < 5.0:
                        continue
                    shares_to_buy = min(max_trade / net_cost, ask_yes_size, ask_no_size)
                    shares_to_buy = round(shares_to_buy, 1)
                    if shares_to_buy < 1.0:
                        continue
                    total_cost = shares_to_buy * net_cost
                    balance -= total_cost
                    db.update_balance(balance)
                    ctx.accumulate_position(clob_token_ids[0], market_id, question, outcomes[0], shares_to_buy, ask_yes, end_date, payout_multiplier=0.5)
                    ctx.accumulate_position(clob_token_ids[1], market_id, question, outcomes[1], shares_to_buy, ask_no, end_date, payout_multiplier=0.5)
                    db.add_trade(market_id, question, clob_token_ids[0], "BUY", outcomes[0], shares_to_buy, ask_yes)
                    db.add_trade(market_id, question, clob_token_ids[1], "BUY", outcomes[1], shares_to_buy, ask_no)
                    net_profit = shares_to_buy * (1.00 - net_cost)
                    ctx.log(f"ARB BIN: '{question}' | Y:{ask_yes:.3f}+N:{ask_no:.3f}+fees:{fees:.3f}={net_cost:.3f} | Profit: {net_profit:.2f}$", "SUCCESS")
                    break
