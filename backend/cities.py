"""Registre Ville → coordonnées de la station de résolution.

Les marchés Polymarket « Highest temperature in {Ville} » se résolvent sur une
station officielle précise (aéroport / observatoire). On prévoit donc à ces
coordonnées. Les coords sont approximatives (station/aéroport principal) — le
petit biais résiduel station↔grille Open-Meteo est absorbé par la calibration.
"""

# nom (en minuscules, tel qu'il apparaît dans le titre) -> (lat, lon)
CITIES = {
    "hong kong": (22.302, 114.177),     # HK Observatory
    "shenzhen": (22.547, 114.085),
    "shanghai": (31.197, 121.336),      # Hongqiao
    "beijing": (40.080, 116.585),
    "tokyo": (35.690, 139.700),
    "singapore": (1.359, 103.989),      # Changi
    "dubai": (25.253, 55.364),          # DXB
    "london": (51.505, 0.055),          # London City Airport (EGLC) — station officielle du marché
    "paris": (48.724, 2.379),           # Orly
    "amsterdam": (52.318, 4.790),       # Schiphol
    "berlin": (52.366, 13.503),         # BER
    "madrid": (40.472, -3.561),         # Barajas
    "moscow": (55.756, 37.617),
    "new york": (40.779, -73.969),      # Central Park (NWS NYC)
    "nyc": (40.779, -73.969),
    "los angeles": (33.938, -118.389),  # LAX
    "chicago": (41.960, -87.930),       # ORD
    "houston": (29.990, -95.360),       # IAH
    "miami": (25.790, -80.290),         # MIA
    "seattle": (47.444, -122.314),      # SeaTac
    "toronto": (43.677, -79.631),       # Pearson
    "philadelphia": (39.873, -75.241),
    "washington": (38.935, -77.447),    # Dulles area
    "phoenix": (33.434, -112.012),
    "denver": (39.847, -104.656),
}


def resolve_city(title_or_city):
    """Renvoie (nom, (lat, lon)) si la ville est connue, sinon (None, None)."""
    s = (title_or_city or "").lower()
    # match le plus long d'abord (ex. 'new york' avant 'york')
    for name in sorted(CITIES, key=len, reverse=True):
        if name in s:
            return name, CITIES[name]
    return None, None
