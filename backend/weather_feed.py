"""Feed météo : Open-Meteo (prévisions d'ensemble) + NWS (capteur officiel US).

Trois flux :
  - `ensemble_by_date(lat, lon, unit)` : pour chaque jour J0..J+2 (dates LOCALES
    de la station), la liste des membres d'ensemble PONDÉRÉS [(valeur, poids)]
    de `temperature_2m_max` (GFS + ICON + ECMWF ; ECMWF pèse plus, il est plus
    précis). Un marché « on June 10 » utilise la distribution du 10 juin local.
  - `realized_today(lat, lon, unit)` : (max grille observé aujourd'hui, date
    locale station, décalage UTC en s). Sert de repli hors stations NWS.
  - `nws_max_today(station_id, local_date, utc_offset, unit)` : le max du jour
    lu sur LE capteur officiel du marché (api.weather.gov) — la donnée exacte
    sur laquelle le marché se résout (villes US).
"""

import asyncio
import json
import time
import urllib.request

from backend import config

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
NWS_URL = "https://api.weather.gov/stations/{sid}/observations?limit=160"
METNO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"

# api.weather.gov EXIGE un User-Agent identifiant (contact) — un UA « navigateur »
# depuis une IP datacenter (Render) se fait rejeter par leur CDN.
NWS_HEADERS = {
    "User-Agent": "polyquant-paper/1.0 (github.com/suuuucdcz/poly; catesson.maxence19@gmail.com)",
    "Accept": "application/geo+json",
}


def station_local_date(lon):
    """Date locale approximative de la station via le fuseau géométrique
    (lon/15h). Sert de repli quand Open-Meteo (qui donne le vrai fuseau)
    est indisponible ; l'erreur DST de ±1 h ne change la date que près de
    minuit, sans impact pratique sur le max journalier."""
    import datetime as _dt
    t = _dt.datetime.utcnow() + _dt.timedelta(hours=round(lon / 15.0))
    return t.strftime("%Y-%m-%d"), int(round(lon / 15.0) * 3600)


def _model_weight(member_key, weights):
    k = member_key.lower()
    if "ecmwf" in k:
        return weights.get("ecmwf", 1.0)
    if "gfs" in k or "gefs" in k:
        return weights.get("gfs", 1.0)
    if "icon" in k:
        return weights.get("icon", 1.0)
    return 1.0


class WeatherFeed:
    def __init__(self, cfg=config):
        self.models = cfg.WEATHER_MODELS
        self.weights = cfg.WEATHER_MODEL_WEIGHTS
        self.headers = cfg.HTTP_HEADERS
        self.timeout = cfg.HTTP_TIMEOUT + 17   # l'API ensemble peut être lente
        self.ens_ttl = cfg.WEATHER_ENS_CACHE_TTL
        self.real_ttl = cfg.WEATHER_REALIZED_CACHE_TTL
        self.nws_ttl = cfg.WEATHER_NWS_CACHE_TTL
        self._ens_cache = {}    # (lat,lon,unit) -> ({date: [(val,poids)]}, ts)
        self._real_cache = {}   # (lat,lon,unit) -> ((max, date, offset), ts)
        self._nws_cache = {}    # station -> (json, ts)
        self.last_error = None       # dernière erreur réseau (diagnostic)
        self._cooldown_until = 0.0   # pause globale après un 429 Open-Meteo
        self.ens_budget = 999        # fetchs d'ensemble restants ce tick (anti-burst)

    async def _get(self, url, headers=None):
        loop = asyncio.get_running_loop()

        def _fetch():
            req = urllib.request.Request(url, headers=headers or self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())

        # Pacing doux + disjoncteur SCOPÉ À OPEN-METEO : leurs requêtes d'ensemble
        # comptent lourd dans le quota ; après un 429 on coupe ce fournisseur un
        # moment. Les autres (NWS...) ne doivent PAS être punis avec lui.
        is_open_meteo = "open-meteo" in url
        if is_open_meteo and time.time() < self._cooldown_until:
            raise RuntimeError("open-meteo en cooldown apres 429")
        await asyncio.sleep(0.25 if is_open_meteo else 0.1)
        try:
            return await loop.run_in_executor(None, _fetch)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {str(e)[:90]}"
            if is_open_meteo and getattr(e, "code", None) == 429:
                self._cooldown_until = time.time() + config.WEATHER_429_COOLDOWN
            raise

    # ------------------------------------------------------------
    # DISTRIBUTIONS PONDÉRÉES DU MAX, PAR DATE LOCALE
    # ------------------------------------------------------------
    async def ensemble_by_date(self, lat, lon, unit="celsius"):
        """{ 'YYYY-MM-DD': [(max simulé, poids modèle), ...] } pour J0..J+2."""
        key = (round(lat, 3), round(lon, 3), unit)
        now = time.time()
        cached = self._ens_cache.get(key)
        if cached and now - cached[1] < self.ens_ttl:
            return cached[0]
        # Budget par tick : on étale le préchauffage des ~58 villes sur
        # plusieurs ticks au lieu d'un burst qui déclenche le 429.
        if self.ens_budget <= 0:
            return cached[0] if cached else {}
        self.ens_budget -= 1
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
                w = _model_weight(k, self.weights)
                for i, dt in enumerate(dates):
                    if i < len(arr) and arr[i] is not None:
                        by_date[dt].append((float(arr[i]), w))
            by_date = {dt: v for dt, v in by_date.items() if len(v) >= 20}
            if by_date:
                self._ens_cache[key] = (by_date, now)
                return by_date
        except Exception:
            pass
        return cached[0] if cached else {}

    # ------------------------------------------------------------
    # MAX RÉALISÉ (grille Open-Meteo, repli hors stations NWS)
    # ------------------------------------------------------------
    async def realized_today(self, lat, lon, unit="celsius"):
        """(max observé aujourd'hui, 'YYYY-MM-DD' locale, décalage UTC en s)."""
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
            now_t = cur.get("time")
            local_date = now_t.split("T")[0] if now_t else None
            offset = int(d.get("utc_offset_seconds") or 0)
            h = d.get("hourly", {})
            realized = None
            for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
                if v is None:
                    continue
                if now_t and t <= now_t:
                    realized = v if realized is None else max(realized, v)
            cv = cur.get("temperature_2m")
            if cv is not None:
                realized = cv if realized is None else max(realized, cv)
            result = (realized, local_date, offset)
            self._real_cache[key] = (result, now)
            return result
        except Exception:
            return cached[0] if cached else (None, None, 0)

    # ------------------------------------------------------------
    # PRÉVISION NWS OFFICIELLE (villes US) — le forecaster humain,
    # déjà corrigé du biais de station. Sert de 2e avis (blend).
    # ------------------------------------------------------------
    async def nws_forecast_max(self, lat, lon):
        """{ 'YYYY-MM-DD' (locale, offsets NWS): max prévu °F } ou {}."""
        key = ("nwsf", round(lat, 3), round(lon, 3))
        now = time.time()
        cached = self._real_cache.get(key)
        if cached and now - cached[1] < config.WEATHER_NWS_FORECAST_TTL:
            return cached[0]
        try:
            pts = await self._get(f"https://api.weather.gov/points/{lat},{lon}", headers=NWS_HEADERS)
            url = pts.get("properties", {}).get("forecast")
            if not url:
                return {}
            fc = await self._get(url, headers=NWS_HEADERS)
            out = {}
            for p in fc.get("properties", {}).get("periods", []):
                if p.get("isDaytime") and p.get("temperatureUnit") == "F":
                    d = (p.get("startTime") or "")[:10]   # heure LOCALE fournie par la NWS
                    t = p.get("temperature")
                    if d and t is not None:
                        out[d] = float(t)
            self._real_cache[key] = (out, now)
            return out
        except Exception as e:
            self.last_error = f"nwsf {type(e).__name__}: {str(e)[:70]}"
            return cached[0] if cached else {}

    # ------------------------------------------------------------
    # SECOURS met.no : max journalier prévu, par date locale
    # ------------------------------------------------------------
    async def metno_daily_max_by_date(self, lat, lon, unit="celsius"):
        """{ 'YYYY-MM-DD' (locale géométrique): max prévu } via met.no.
        Indépendant d'Open-Meteo (autre fournisseur, autre quota)."""
        key = ("metno", round(lat, 3), round(lon, 3))
        now = time.time()
        cached = self._real_cache.get(key)
        if cached and now - cached[1] < config.WEATHER_METNO_TTL:
            data = cached[0]
        else:
            loop = asyncio.get_running_loop()

            def _fetch():
                req = urllib.request.Request(
                    METNO_URL.format(lat=lat, lon=lon),
                    # met.no exige un User-Agent identifiant (sinon 403)
                    headers={"User-Agent": "polyquant-paper/1.0 github.com/suuuucdcz/poly"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode())

            try:
                await asyncio.sleep(0.2)
                data = await loop.run_in_executor(None, _fetch)
                self._real_cache[key] = (data, now)
            except Exception as e:
                self.last_error = f"metno {type(e).__name__}: {str(e)[:70]}"
                return {}
        try:
            import datetime as _dt
            off_h = round(lon / 15.0)
            out = {}
            for ts in data.get("properties", {}).get("timeseries", []):
                t = ts.get("time")
                v = ts.get("data", {}).get("instant", {}).get("details", {}).get("air_temperature")
                if t is None or v is None:
                    continue
                local = _dt.datetime.fromisoformat(t.replace("Z", "+00:00")) + _dt.timedelta(hours=off_h)
                d = local.strftime("%Y-%m-%d")
                v = float(v)
                if unit == "fahrenheit":
                    v = v * 9.0 / 5.0 + 32.0
                out[d] = v if d not in out else max(out[d], v)
            return out
        except Exception:
            return {}

    # ------------------------------------------------------------
    # HISTORIQUE GRILLE (pour la calibration de biais)
    # ------------------------------------------------------------
    async def grid_daily_max_history(self, lat, lon, unit="celsius"):
        """{ 'YYYY-MM-DD' (locale station): max grille } sur ~60 jours passés."""
        key = ("hist", round(lat, 3), round(lon, 3), unit)
        now = time.time()
        cached = self._real_cache.get(key)
        if cached and now - cached[1] < 12 * 3600:
            return cached[0]
        url = (
            f"{FORECAST_URL}?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&temperature_unit={unit}"
            f"&past_days={config.BIAS_HISTORY_DAYS}&forecast_days=1&timezone=auto"
        )
        try:
            d = await self._get(url)
            daily = d.get("daily", {})
            out = {}
            for dt, v in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
                if v is not None:
                    out[dt] = float(v)
            self._real_cache[key] = (out, now)
            return out
        except Exception:
            return cached[0] if cached else {}

    # ------------------------------------------------------------
    # MAX RÉALISÉ — CAPTEUR OFFICIEL NWS (villes US)
    # ------------------------------------------------------------
    async def nws_max_today(self, station_id, local_date, utc_offset, unit="celsius"):
        """Max du jour (date locale station) lu sur la station NWS officielle,
        converti dans l'unité du marché. None si indisponible."""
        if not station_id or not local_date:
            return None
        now = time.time()
        cached = self._nws_cache.get(station_id)
        if cached and now - cached[1] < self.nws_ttl:
            data = cached[0]
        else:
            try:
                data = await self._get(NWS_URL.format(sid=station_id), headers=NWS_HEADERS)
                self._nws_cache[station_id] = (data, now)
            except Exception:
                return None
        best = None
        for f in data.get("features", []):
            p = f.get("properties", {})
            temp = p.get("temperature") or {}
            v = temp.get("value")
            ts = p.get("timestamp")
            if v is None or not ts:
                continue
            # Contrôle qualité NWS : X = rejeté, Q = douteux. Une seule lecture
            # aberrante (capteur HS) fausserait R -> mauvaise tranche achetée ou
            # bonne position coupée à tort.
            qc = str(temp.get("qualityControl") or "").split(":")[-1].upper()
            if qc in ("X", "Q"):
                continue
            try:
                # timestamp UTC -> date locale station via le décalage Open-Meteo
                import datetime as _dt
                t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local = t + _dt.timedelta(seconds=utc_offset)
                if local.strftime("%Y-%m-%d") != local_date:
                    continue
            except Exception:
                continue
            best = v if best is None else max(best, v)
        if best is None:
            return None
        if unit == "fahrenheit":
            return best * 9.0 / 5.0 + 32.0
        return best
