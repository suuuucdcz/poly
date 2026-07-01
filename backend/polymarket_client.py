"""Client de l'API Polymarket (Gamma + CLOB) — version météo.

Découverte des marchés « Highest temperature in… » + accès marché/carnet
nécessaires au règlement et à la clôture manuelle des positions.
"""

import asyncio
import json
import time
import urllib.request

from backend import config


class PolymarketClient:
    def __init__(self, cfg=config):
        self.headers = cfg.HTTP_HEADERS
        self.timeout = cfg.HTTP_TIMEOUT
        self.gamma = cfg.GAMMA_API
        self.clob = cfg.CLOB_API
        self._log = lambda *a, **k: None
        self._temp_cache = []
        self._temp_cache_ts = 0.0

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
    # Polymarket taggue ces marchés : 104596 = highest-temperature (84 = weather).
    # La découverte par tag est insensible aux flots d'events (sport...) qui
    # éjectaient la météo des fenêtres triées (l'API plafonne limit à 100).
    TEMPERATURE_TAG_ID = 104596
    _DISCOVERY_TTL = 240

    async def find_temperature_events(self):
        """Découvre les events « Highest temperature in {Ville} on {Date} » et
        parse leurs buckets (tranches de degré + prix Yes)."""
        now = time.time()
        if self._temp_cache and now - self._temp_cache_ts < self._DISCOVERY_TTL:
            return self._temp_cache

        events = []
        try:
            for offset in (0, 100, 200):   # l'API plafonne à 100 par page
                page = await self.fetch_api_json(
                    f"{self.gamma}/events?active=true&closed=false&limit=100"
                    f"&offset={offset}&tag_id={self.TEMPERATURE_TAG_ID}"
                )
                if not page:
                    break
                events.extend(page)
                if len(page) < 100:
                    break
        except Exception as e:
            self._log(f"DISCOVERY: requête tag échouée ({e}) — repli sur le scan paginé", "WARNING")
            events = []

        # Repli : si le tag ne renvoie rien (CDN capricieux...), scan paginé
        # des events triés par fin la plus proche (les journaliers y sont).
        if not events:
            try:
                for offset in range(0, 800, 100):
                    page = await self.fetch_api_json(
                        f"{self.gamma}/events?active=true&closed=false&limit=100"
                        f"&offset={offset}&order=endDate&ascending=true"
                    )
                    if not page:
                        break
                    events.extend(page)
                    if len(page) < 100:
                        break
                self._log(f"DISCOVERY: repli paginé -> {len(events)} events bruts", "INFO")
            except Exception as e:
                self._log(f"DISCOVERY: repli échoué aussi: {e}", "WARNING")
                return self._temp_cache or []
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
                    # gameStartTime = minuit LOCAL du jour de résolution (en UTC) ->
                    # donne le fuseau exact ; acceptingOrders = marché réellement ouvert ;
                    # orderMinSize = taille d'ordre minimale imposée par le CLOB.
                    "game_start": m.get("gameStartTime"),
                    "accepting": bool(m.get("acceptingOrders", True)),
                    "min_size": float(m.get("orderMinSize") or 5),
                    "closed": bool(m.get("closed", False)) or not m.get("active", True),
                })
            if len(buckets) >= 3 and not any(o["title"] == title for o in out):
                gs = next((b["game_start"] for b in buckets if b.get("game_start")), None)
                out.append({
                    "title": title,
                    "slug": e.get("slug"),
                    "end_date": e.get("endDate"),
                    # date LOCALE de résolution (robuste, vs parsing du titre) + fuseau
                    "event_date": e.get("eventDate") or (m.get("endDateIso") if buckets else None),
                    "game_start": gs,
                    "buckets": buckets,
                })
        self._temp_cache = out
        self._temp_cache_ts = now
        return out
