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

    # --- stations lues dans les descriptions officielles (2e vague) ---
    "ankara": (40.128, 32.995),          # Esenboğa Intl
    "busan": (35.179, 128.938),          # Gimhae Intl
    "cape town": (-33.971, 18.602),      # Cape Town Intl
    "chengdu": (30.578, 103.947),        # Shuangliu Intl
    "chongqing": (29.719, 106.642),      # Jiangbei Intl
    "guangzhou": (23.392, 113.299),      # Baiyun Intl
    "helsinki": (60.317, 24.963),        # Vantaa
    "istanbul": (41.262, 28.742),        # Istanbul Airport (IST)
    "jeddah": (21.680, 39.157),          # King Abdulaziz Intl
    "jinan": (36.857, 117.216),          # Yaoqiang Intl
    "karachi": (24.894, 66.939),         # Masroor Airbase
    "kuala lumpur": (2.744, 101.710),    # KLIA
    "lucknow": (26.761, 80.889),         # Chaudhary Charan Singh Intl
    "manila": (14.509, 121.020),         # Ninoy Aquino Intl
    "milan": (45.630, 8.728),            # Malpensa
    "moscow": (55.596, 37.267),          # Vnukovo Intl
    "munich": (48.354, 11.786),          # Munich Airport
    "qingdao": (36.362, 120.088),        # Jiaodong Intl
    "seoul": (37.469, 126.451),          # Incheon Intl
    "taipei": (25.069, 121.552),         # Songshan
    "tel aviv": (32.000, 34.871),        # Ben Gurion Intl
    "warsaw": (52.166, 20.967),          # Chopin
    "wellington": (-41.327, 174.805),    # Wellington Intl
    "wuhan": (30.784, 114.208),          # Tianhe Intl
    "zhengzhou": (34.520, 113.841),      # Xinzheng Intl

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


# Ville -> région météo (régimes synoptiques partagés : une canicule au Texas
# touche Austin+Dallas+Houston ensemble -> l'exposition se plafonne PAR RÉGION).
REGIONS = {
    # Amérique du Nord
    **{c: "noram" for c in ["atlanta", "austin", "chicago", "dallas", "denver",
                             "houston", "los angeles", "miami", "san francisco",
                             "seattle", "new york", "nyc", "philadelphia",
                             "washington", "phoenix", "toronto"]},
    # Amérique latine
    **{c: "latam" for c in ["mexico city", "panama city", "buenos aires",
                             "sao paulo", "são paulo"]},
    # Europe
    **{c: "europe" for c in ["london", "paris", "amsterdam", "berlin", "madrid",
                              "moscow", "munich", "warsaw", "helsinki", "milan"]},
    # Asie
    **{c: "asia" for c in ["hong kong", "shenzhen", "shanghai", "beijing",
                            "tokyo", "singapore", "seoul", "busan", "taipei",
                            "chengdu", "chongqing", "guangzhou", "qingdao",
                            "jinan", "wuhan", "zhengzhou", "kuala lumpur",
                            "manila", "karachi", "lucknow"]},
    # Moyen-Orient / Turquie
    **{c: "mea" for c in ["dubai", "jeddah", "tel aviv", "istanbul", "ankara"]},
    "wellington": "oceania",
    "cape town": "africa",
}


def region_of(city):
    return REGIONS.get(city, "other")


# Ville -> identifiant de la station NWS (api.weather.gov) = LE capteur officiel
# des marchés US. Permet de lire le max réalisé EXACT (et non la grille météo).
NWS_STATIONS = {
    "atlanta": "KATL",
    "austin": "KAUS",
    "chicago": "KORD",
    "dallas": "KDAL",        # Love Field
    "denver": "KBKF",        # Buckley Space Force Base
    "houston": "KHOU",       # Hobby
    "los angeles": "KLAX",
    "miami": "KMIA",
    "san francisco": "KSFO",
    "seattle": "KSEA",
    "new york": "KNYC",      # Central Park
    "nyc": "KNYC",
    "philadelphia": "KPHL",
    "phoenix": "KPHX",
}


def resolve_city(title_or_city):
    """Renvoie (nom, (lat, lon)) si la ville est connue, sinon (None, None)."""
    s = (title_or_city or "").lower()
    # match le plus long d'abord (ex. 'mexico city' avant 'mexico')
    for name in sorted(CITIES, key=len, reverse=True):
        if name in s:
            return name, CITIES[name]
    return None, None
