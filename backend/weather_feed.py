"""Feed météo via Open-Meteo (gratuit, sans clé).

Deux flux :
  - `ensemble_max(lat, lon)` : la liste de TOUS les membres d'ensemble
    (GFS + ICON + ECMWF, ≈120 scénarios) du `temperature_2m_max` du jour →
    une vraie distribution de probabilité du maximum.
  - `realized_max_today(lat, lon)` : le maximum DÉJÀ atteint aujourd'hui
    (via les heures passées + l'observation courante) → sert à conditionner
    la distribution en cours de journée (gros edge l'après-midi).
"""

import asyncio
import json
import time
import urllib.request

from backend import config

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherFeed:
    def __init__(self, cfg=config):
        self.models = cfg.WEATHER_MODELS
        self.headers = cfg.HTTP_HEADERS
        self.timeout = cfg.HTTP_TIMEOUT + 12   # Open-Meteo peut être un peu lent
        self.ens_ttl = cfg.WEATHER_ENS_CACHE_TTL
        self.real_ttl = cfg.WEATHER_REALIZED_CACHE_TTL
        self._ens_cache = {}    # (lat,lon,unit) -> (members, ts)
        self._real_cache = {}   # (lat,lon,unit) -> (realized, ts)

    async def _get(self, url):
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())

        return await loop.run_in_executor(None, _fetch)

    # ------------------------------------------------------------
    # DISTRIBUTION DU MAX (ensemble multi-modèles)
    # ------------------------------------------------------------
    async def ensemble_max(self, lat, lon, unit="celsius"):
        """Liste des maxima simulés (tous membres, tous modèles) pour aujourd'hui."""
        key = (round(lat, 3), round(lon, 3), unit)
        now = time.time()
        cached = self._ens_cache.get(key)
        if cached and now - cached[1] < self.ens_ttl:
            return cached[0]
        url = (
            f"{ENSEMBLE_URL}?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit={unit}"
            f"&forecast_days=1&models={self.models}&timezone=auto"
        )
        try:
            d = await self._get(url)
            daily = d.get("daily", {})
            members = []
            for k, arr in daily.items():
                if k.startswith("temperature_2m_max") and isinstance(arr, list) and arr:
                    v = arr[0]
                    if v is not None:
                        members.append(float(v))
            if members:
                self._ens_cache[key] = (members, now)
                return members
        except Exception:
            pass
        return cached[0] if cached else []

    # ------------------------------------------------------------
    # MAX RÉALISÉ AUJOURD'HUI (jusqu'à maintenant)
    # ------------------------------------------------------------
    async def realized_max_today(self, lat, lon, unit="celsius"):
        """Max observé aujourd'hui jusqu'à l'heure courante (ou None si indispo)."""
        key = (round(lat, 3), round(lon, 3), unit)
        now = time.time()
        cached = self._real_cache.get(key)
        if cached and now - cached[1] < self.real_ttl:
            return cached[0]
        url = (
            f"{FORECAST_URL}?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m&current=temperature_2m&temperature_unit={unit}"
            f"&forecast_days=1&timezone=auto"
        )
        try:
            d = await self._get(url)
            cur = d.get("current", {})
            now_t = cur.get("time")            # heure locale courante 'YYYY-MM-DDTHH:MM'
            h = d.get("hourly", {})
            times = h.get("time", [])
            temps = h.get("temperature_2m", [])
            realized = None
            for t, v in zip(times, temps):
                if v is None:
                    continue
                if now_t and t <= now_t:        # seulement les heures déjà écoulées
                    realized = v if realized is None else max(realized, v)
            cv = cur.get("temperature_2m")
            if cv is not None:
                realized = cv if realized is None else max(realized, cv)
            self._real_cache[key] = (realized, now)
            return realized
        except Exception:
            return cached[0] if cached else None
