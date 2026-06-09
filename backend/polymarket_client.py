"""Client de l'API Polymarket (Gamma + CLOB) — version météo.

Découverte des marchés « Highest temperature in… » + accès marché/carnet
nécessaires au règlement et à la clôture manuelle des positions.
"""

import asyncio
import json
import urllib.request

from backend import config


class PolymarketClient:
    def __init__(self, cfg=config):
        self.headers = cfg.HTTP_HEADERS
        self.timeout = cfg.HTTP_TIMEOUT
        self.gamma = cfg.GAMMA_API
        self.clob = cfg.CLOB_API
        self._log = lambda *a, **k: None

    def set_logger(self, log):
        self._log = log

    # ------------------------------------------------------------
    # ACCÈS HTTP
    # ------------------------------------------------------------
    async def fetch_api_json(self, url):
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode())

        return await loop.run_in_executor(None, _fetch)

    async def fetch_market(self, market_id):
        return await self.fetch_api_json(f"{self.gamma}/markets/{market_id}")

    async def fetch_event(self, event_id):
        return await self.fetch_api_json(f"{self.gamma}/events/{event_id}")

    async def fetch_book(self, token_id):
        try:
            return await self.fetch_api_json(f"{self.clob}/book?token_id={token_id}")
        except Exception:
            return None

    # ------------------------------------------------------------
    # MARCHÉS « HIGHEST TEMPERATURE »
    # ------------------------------------------------------------
    async def find_temperature_events(self):
        """Découvre les events « Highest temperature in {Ville} on {Date} » et
        parse leurs buckets (tranches de degré + prix Yes)."""
        url = (
            f"{self.gamma}/events?active=true&closed=false&limit=300"
            f"&order=volume24hr&ascending=false"
        )
        try:
            events = await self.fetch_api_json(url)
        except Exception as e:
            self._log(f"Error fetching temperature events: {e}", "WARNING")
            return []
        out = []
        for e in events:
            title = e.get("title") or ""
            if not title.lower().startswith("highest temperature"):
                continue
            markets = e.get("markets") or []
            if len(markets) < 3:
                continue
            buckets = []
            for m in markets:
                label = m.get("groupItemTitle") or m.get("question")
                try:
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    outcomes = json.loads(m.get("outcomes", "[]"))
                except Exception:
                    continue
                if len(tokens) < 2 or len(prices) < 2:
                    continue
                buckets.append({
                    "label": label,
                    "market_id": m.get("id"),
                    "yes_token": tokens[0],
                    "yes_outcome": outcomes[0] if outcomes else "Yes",
                    "yes_price": float(prices[0]),
                    "end_date": m.get("endDate") or m.get("endDateIso"),
                    "closed": bool(m.get("closed", False)) or not m.get("active", True),
                })
            if len(buckets) >= 3:
                out.append({
                    "title": title,
                    "slug": e.get("slug"),
                    "end_date": e.get("endDate"),
                    "buckets": buckets,
                })
        return out
