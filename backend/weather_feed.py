"""Feed météo via Open-Meteo (gratuit, sans clé).

Deux flux :
  - `ensemble_by_date(lat, lon, unit)` : pour CHAQUE jour de prévision (J0..J+2,
    dates LOCALES de la station), la liste de tous les membres d'ensemble
    (GFS + ICON + ECMWF, ≈120 scénarios) du `temperature_2m_max` → une vraie
    distribution de probabilité du maximum, **par date cible**.
    Indispensable : un marché « on June 10 » doit utiliser la distribution du
    10 juin à la station, pas celle d'aujourd'hui.
  - `realized_today(lat, lon, unit)` : le maximum DÉJÀ observé aujourd'hui
    (heure locale de la station) + la date locale courante de la station →
    permet de conditionner la distribution uniquement si la date cible est
    bien « aujourd'hui » là-bas.
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
        self.timeout = cfg.HTTP_TIMEOUT + 17   # l'API ensemble peut être lente
        self.ens_ttl = cfg.WEATHER_ENS_CACHE_TTL
        self.real_ttl = cfg.WEATHER_REALIZED_CACHE_TTL
        self._ens_cache = {}    # (lat,lon,unit) -> ({date: [members]}, ts)
        self._real_cache = {}   # (lat,lon,unit) -> ((max, date_locale), ts)

    async def _get(self, url):
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())

        return await loop.run_in_executor(None, _fetch)

    # ------------------------------------------------------------
    # DISTRIBUTIONS DU MAX, PAR DATE LOCALE DE LA STATION
    # ------------------------------------------------------------
    async def ensemble_by_date(self, lat, lon, unit="celsius"):
        """{ 'YYYY-MM-DD': [max simulés...] } pour J0..J+2 (dates station)."""
        key = (round(lat, 3), round(lon, 3), unit)
        now = time.time()
        cached = self._ens_cache.get(key)
        if cached and now - cached[1] < self.ens_ttl:
            return cached[0]
        url = (
            f"{ENSEMBLE_URL}?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit={unit}"
            f"&forecast_days=3&models={self.models}&timezone=auto"
        )
        try:
            d = await self._get(url)
            daily = d.get("daily", {})
            dates = daily.get("time", []) or []
            by_date = {dt: [] for dt in dates}
            for k, arr in daily.items():
                if not k.startswith("temperature_2m_max") or not isinstance(arr, list):
                    continue
                for i, dt in enumerate(dates):
                    if i < len(arr) and arr[i] is not None:
                        by_date[dt].append(float(arr[i]))
            # ne garder que les dates avec assez de membres pour une vraie distribution
            by_date = {dt: v for dt, v in by_date.items() if len(v) >= 20}
            if by_date:
                self._ens_cache[key] = (by_date, now)
                return by_date
        except Exception:
            pass
        return cached[0] if cached else {}

    # ------------------------------------------------------------
    # MAX RÉALISÉ AUJOURD'HUI (heure + date locales de la station)
    # ------------------------------------------------------------
    async def realized_today(self, lat, lon, unit="celsius"):
        """(max observé aujourd'hui, 'YYYY-MM-DD' locale station) ou (None, None)."""
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
            now_t = cur.get("time")                  # 'YYYY-MM-DDTHH:MM' locale station
            local_date = now_t.split("T")[0] if now_t else None
            h = d.get("hourly", {})
            times = h.get("time", [])
            temps = h.get("temperature_2m", [])
            realized = None
            for t, v in zip(times, temps):
                if v is None:
                    continue
                if now_t and t <= now_t:             # seulement les heures déjà écoulées
                    realized = v if realized is None else max(realized, v)
            cv = cur.get("temperature_2m")
            if cv is not None:
                realized = cv if realized is None else max(realized, cv)
            result = (realized, local_date)
            self._real_cache[key] = (result, now)
            return result
        except Exception:
            return cached[0] if cached else (None, None)
