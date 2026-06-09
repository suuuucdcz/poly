"""Feed de prix crypto temps réel via l'API publique Binance.

Fournit trois choses à la stratégie crypto "Up/Down" :
  - le **spot** courant (mis en cache ~1s) ;
  - le **prix de référence d'ouverture** d'une fenêtre (enregistré en direct si on
    voit la fenêtre s'ouvrir, sinon reconstruit via la bougie 1m Binance) ;
  - une **estimation de volatilité** (écart-type des rendements 1m récents),
    exprimée "par racine de seconde" pour le modèle brownien.

Note d'honnêteté : la résolution Polymarket utilise l'oracle Chainlink BTC/USD,
pas Binance. Binance est un excellent proxy (Chainlink agrège les CEX), mais une
petite erreur de suivi existe. Acceptable en paper trading / recherche.
"""

import asyncio
import json
import math
import statistics
import time
import urllib.request

from backend import config


class CryptoPriceFeed:
    def __init__(self, cfg=config):
        self.binance = cfg.BINANCE_API
        self.headers = cfg.HTTP_HEADERS
        self.timeout = cfg.HTTP_TIMEOUT
        self.assets = cfg.CRYPTO_ASSETS
        self.spot_cache_ttl = cfg.CRYPTO_SPOT_CACHE_TTL
        self.vol_lookback = cfg.CRYPTO_VOL_LOOKBACK_MIN
        self.vol_refresh_sec = cfg.CRYPTO_VOL_REFRESH_SEC
        self.default_sigma = cfg.CRYPTO_DEFAULT_SIGMA

        self.flow_lookback = cfg.CRYPTO_FLOW_LOOKBACK_SEC
        self.flow_cache_ttl = cfg.CRYPTO_FLOW_CACHE_TTL

        self._spot_cache = {}   # symbole -> (prix, ts)
        self._sigma_cache = {}  # symbole -> (sigma_sqrt_sec, ts)
        self._ref_cache = {}    # (symbole, open_ts) -> prix de référence
        self._flow_cache = {}   # symbole -> (flux, ts)

    async def _get_json(self, url):
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode())

        return await loop.run_in_executor(None, _fetch)

    # ------------------------------------------------------------
    # SPOT
    # ------------------------------------------------------------
    async def spot(self, asset):
        symbol = self.assets.get(asset)
        if not symbol:
            return None
        now = time.time()
        cached = self._spot_cache.get(symbol)
        if cached and now - cached[1] < self.spot_cache_ttl:
            return cached[0]
        try:
            data = await self._get_json(
                f"{self.binance}/api/v3/ticker/price?symbol={symbol}"
            )
            price = float(data["price"])
            self._spot_cache[symbol] = (price, now)
            return price
        except Exception:
            return cached[0] if cached else None

    # ------------------------------------------------------------
    # PRIX DE RÉFÉRENCE D'OUVERTURE DE LA FENÊTRE
    # ------------------------------------------------------------
    async def reference_price(self, asset, open_ts, live_spot=None):
        symbol = self.assets.get(asset)
        if not symbol:
            return None
        key = (symbol, open_ts)
        if key in self._ref_cache:
            return self._ref_cache[key]

        now = time.time()
        # Si la fenêtre vient tout juste de s'ouvrir et qu'on a un spot live,
        # on l'enregistre comme référence (au plus proche de l'instant d'ouverture).
        if live_spot is not None and abs(now - open_ts) <= 3:
            self._ref_cache[key] = live_spot
            return live_spot

        # Sinon, on reconstruit via la bougie 1m Binance à l'instant d'ouverture.
        try:
            start_ms = int(open_ts * 1000)
            data = await self._get_json(
                f"{self.binance}/api/v3/klines?symbol={symbol}"
                f"&interval=1m&startTime={start_ms}&limit=1"
            )
            if data:
                ref = float(data[0][1])  # open de la bougie 1m
                self._ref_cache[key] = ref
                return ref
        except Exception:
            pass
        return None

    # ------------------------------------------------------------
    # VOLATILITÉ (par racine de seconde)
    # ------------------------------------------------------------
    async def volatility(self, asset):
        symbol = self.assets.get(asset)
        if not symbol:
            return self.default_sigma
        now = time.time()
        cached = self._sigma_cache.get(symbol)
        if cached and now - cached[1] < self.vol_refresh_sec:
            return cached[0]
        try:
            data = await self._get_json(
                f"{self.binance}/api/v3/klines?symbol={symbol}"
                f"&interval=1m&limit={self.vol_lookback}"
            )
            closes = [float(k[4]) for k in data]
            rets = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            if len(rets) >= 5:
                stdev_1m = statistics.pstdev(rets)        # vol sur 1 minute
                sigma_sqrt_sec = stdev_1m / math.sqrt(60.0)  # -> par racine de seconde
                sigma_sqrt_sec = max(sigma_sqrt_sec, 1e-6)
                self._sigma_cache[symbol] = (sigma_sqrt_sec, now)
                return sigma_sqrt_sec
        except Exception:
            pass
        return self.default_sigma

    # ------------------------------------------------------------
    # FLUX D'ORDRES AGRESSEUR (Binance)
    # ------------------------------------------------------------
    async def trade_flow(self, asset):
        """Déséquilibre acheteur/vendeur AGRESSEUR sur les dernières secondes.

        Renvoie un flux ∈ [-1, 1] : positif = pression acheteuse au marché
        (les agresseurs lèvent l'offre), négatif = pression vendeuse.
        Utilise aggTrades : le flag `m`=True signifie que l'acheteur est maker,
        donc que l'AGRESSEUR est le vendeur.
        """
        symbol = self.assets.get(asset)
        if not symbol:
            return 0.0
        now = time.time()
        cached = self._flow_cache.get(symbol)
        if cached and now - cached[1] < self.flow_cache_ttl:
            return cached[0]
        try:
            data = await self._get_json(
                f"{self.binance}/api/v3/aggTrades?symbol={symbol}&limit=1000"
            )
            cutoff_ms = (now - self.flow_lookback) * 1000.0
            buy = sell = 0.0
            for t in data:
                if t.get("T", 0) < cutoff_ms:
                    continue
                q = float(t["q"])
                if t.get("m"):      # acheteur maker -> agresseur = VENDEUR
                    sell += q
                else:               # agresseur = ACHETEUR
                    buy += q
            total = buy + sell
            flow = (buy - sell) / total if total > 0 else 0.0
            self._flow_cache[symbol] = (flow, now)
            return flow
        except Exception:
            return cached[0] if cached else 0.0
