"""Registre Ville → coordonnées de la STATION DE RÉSOLUTION officielle.

Les marchés « Highest temperature in {Ville} » se résolvent sur une station
précise, nommée dans la description du marché (leçon : Londres = London City
Airport, PAS Heathrow ; Houston = Hobby, PAS Intercontinental). Les entrées
marquées « vérifié » proviennent des descriptions officielles relevées sur les
marchés actifs/récents. Le petit biais résiduel grille Open-Meteo ↔ capteur est
absorbé par la calibration.

Si un marché apparaît pour une ville absente d'ici, le bot logge un WARNING
« ville inconnue » : ajouter alors l'entrée avec la station lue dans la
description du marché.
"""

# nom (minuscules, tel qu'il apparaît dans le titre) -> (lat, lon)
CITIES = {
    # --- vérifié sur les descriptions officielles des marchés ---
    "atlanta": (33.640, -84.427),        # Hartsfield-Jackson Intl
    "austin": (30.183, -97.680),         # Austin-Bergstrom Intl
    "buenos aires": (-34.822, -58.536),  # Ministro Pistarini (Ezeiza)
    "chicago": (41.960, -87.930),        # O'Hare Intl
    "dallas": (32.847, -96.852),         # Dallas Love Field
    "denver": (39.702, -104.752),        # Buckley Space Force Base
    "houston": (29.646, -95.277),        # William P. Hobby
    "los angeles": (33.938, -118.389),   # LAX
    "mexico city": (19.436, -99.072),    # Benito Juárez Intl
    "miami": (25.793, -80.290),          # Miami Intl
    "panama city": (8.973, -79.556),     # Marcos A. Gelabert (Albrook)
    "san francisco": (37.620, -122.365), # SFO
    "sao paulo": (-23.435, -46.473),     # Guarulhos Intl
    "são paulo": (-23.435, -46.473),
    "seattle": (47.444, -122.314),       # Seattle-Tacoma Intl
    "toronto": (43.677, -79.631),        # Pearson Intl
    "london": (51.505, 0.055),           # London City Airport (EGLC)
    "hong kong": (22.302, 114.174),      # Hong Kong Observatory

    # --- vus sur des marchés, station non confirmée (approx. station principale) ---
    "shenzhen": (22.547, 114.085),
    "shanghai": (31.197, 121.336),       # Hongqiao
    "beijing": (40.080, 116.585),
    "tokyo": (35.690, 139.700),
    "singapore": (1.359, 103.989),       # Changi
    "dubai": (25.253, 55.364),           # DXB
    "paris": (48.724, 2.379),            # Orly
    "amsterdam": (52.318, 4.790),        # Schiphol
    "berlin": (52.366, 13.503),          # BER
    "madrid": (40.472, -3.561),          # Barajas
    "new york": (40.779, -73.969),       # Central Park
    "nyc": (40.779, -73.969),
    "philadelphia": (39.873, -75.241),
    "washington": (38.935, -77.447),
    "phoenix": (33.434, -112.012),
}


def resolve_city(title_or_city):
    """Renvoie (nom, (lat, lon)) si la ville est connue, sinon (None, None)."""
    s = (title_or_city or "").lower()
    # match le plus long d'abord (ex. 'mexico city' avant 'mexico')
    for name in sorted(CITIES, key=len, reverse=True):
        if name in s:
            return name, CITIES[name]
    return None, None
