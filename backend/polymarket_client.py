"""Client de l'API Polymarket (Gamma + CLOB).

Regroupe tous les accès réseau côté Polymarket, extraits de l'ancien `bot.py`,
plus la découverte des marchés crypto "Up/Down" courts par slug déterministe.
"""

import asyncio
import json
import time
import urllib.request

from backend import config


def parse_updown_slug(slug):
    """`btc-updown-5m-1780950300` -> ('BTC', '5m', 1780950300) ; sinon None."""
    parts = slug.split("-")
    if len(parts) != 4 or parts[1] != "updown":
        return None
    asset = parts[0].upper()
    window = parts[2]
    try:
        open_ts = int(parts[3])
    except ValueError:
        return None
    return asset, window, open_ts


class PolymarketClient:
    def __init__(self, cfg=config):
        self.headers = cfg.HTTP_HEADERS
        self.timeout = cfg.HTTP_TIMEOUT
        self.gamma = cfg.GAMMA_API
        self.clob = cfg.CLOB_API
        self.markets_url = cfg.MARKETS_FETCH_URL

        # Cache des marchés génériques
        self.cached_markets = []
        self.last_markets_fetch_time = 0.0
        self.cache_ttl = cfg.MARKETS_CACHE_TTL

        # Logger optionnel (injecté par le bot)
        self._log = lambda *a, **k: None

    def set_logger(self, log):
        self._log = log

    # ============================================================
    # ACCÈS HTTP BAS NIVEAU
    # ============================================================
    async def fetch_api_json(self, url):
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode())

        return await loop.run_in_executor(None, _fetch)

    # ============================================================
    # MARCHÉS GÉNÉRIQUES (avec cache mémoire court)
    # ============================================================
    async def fetch_markets(self):
        current_time = time.time()
        if self.cached_markets and (current_time - self.last_markets_fetch_time < self.cache_ttl):
            return self.cached_markets
        try:
            markets = await self.fetch_api_json(self.markets_url)
            valid_markets = []
            for m in markets:
                try:
                    outcomes = json.loads(m.get("outcomes", "[]"))
                    token_ids = json.loads(m.get("clobTokenIds", "[]"))
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    if (
                        len(outcomes) >= 2
                        and len(token_ids) == len(outcomes)
                        and len(prices) == len(outcomes)
                    ):
                        valid_markets.append(m)
                except Exception:
                    continue
            self.cached_markets = valid_markets
            self.last_markets_fetch_time = current_time
            return valid_markets
        except Exception as e:
            self._log(f"Error fetching markets: {e}", "WARNING")
            return self.cached_markets if self.cached_markets else []

    async def fetch_market(self, market_id):
        return await self.fetch_api_json(f"{self.gamma}/markets/{market_id}")

    async def fetch_event(self, event_id):
        return await self.fetch_api_json(f"{self.gamma}/events/{event_id}")

    # ============================================================
    # CARNETS / PRIX CLOB
    # ============================================================
    async def fetch_book(self, token_id):
        url = f"{self.clob}/book?token_id={token_id}"
        try:
            return await self.fetch_api_json(url)
        except Exception:
            return None

    async def fetch_books_concurrently(self, token_ids):
        tasks = [self.fetch_book(tid) for tid in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [res if not isinstance(res, Exception) else None for res in results]

    async def fetch_token_price(self, token_id):
        url = f"{self.clob}/price?token_id={token_id}"
        try:
            res = await self.fetch_api_json(url)
            return float(res.get("price", 0.0))
        except Exception:
            return None

    # ============================================================
    # MARCHÉS CRYPTO "UP / DOWN" (slug déterministe)
    # ============================================================
    async def find_updown_events(self, assets, windows, window_seconds):
        """Récupère, pour la fenêtre *en cours* de chaque actif × durée, le marché
        Up/Down correspondant.

        Le slug est déterministe et aligné sur les frontières de durée :
        `open_ts = (now // dur) * dur` → `{asset}-updown-{window}-{open_ts}`.
        On récupère donc directement la fenêtre qui se clôture au prochain palier,
        sans dépendre d'un tri d'API peu fiable.
        """
        now = int(time.time())
        specs = []
        for asset in assets:
            for window in windows:
                dur = window_seconds[window]
                open_ts = (now // dur) * dur
                slug = f"{asset.lower()}-updown-{window}-{open_ts}"
                specs.append((asset, window, dur, open_ts, slug))

        async def fetch_one(spec):
            asset, window, dur, open_ts, slug = spec
            try:
                data = await self.fetch_api_json(f"{self.gamma}/events?slug={slug}")
            except Exception:
                return None
            if not data:
                return None
            ev = data[0]
            markets = ev.get("markets") or []
            if not markets:
                return None
            m = markets[0]
            try:
                tokens = json.loads(m.get("clobTokenIds", "[]"))
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = json.loads(m.get("outcomePrices", "[]"))
            except Exception:
                return None
            if len(tokens) < 2 or len(prices) < 2:
                return None
            return {
                "asset": asset,
                "window": window,
                "open_ts": open_ts,
                "end_ts": open_ts + dur,
                "slug": slug,
                "market_id": m.get("id"),
                "question": m.get("question", "Unknown Market"),
                "closed": bool(m.get("closed", False)) or not m.get("active", True),
                "end_date": m.get("endDate") or m.get("endDateIso"),
                "up_token": tokens[0],
                "down_token": tokens[1],
                "up_outcome": outcomes[0] if outcomes else "Up",
                "down_outcome": outcomes[1] if len(outcomes) > 1 else "Down",
                "up_price": float(prices[0]),
                "down_price": float(prices[1]),
            }

        results = await asyncio.gather(*[fetch_one(s) for s in specs])
        return [r for r in results if r]
