"""Configuration centralisée du bot de paper trading (météo)."""

# ============================================================
# ENDPOINTS API
# ============================================================
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    )
}
HTTP_TIMEOUT = 8

# ============================================================
# DÉFAUTS DU BOT
# ============================================================
DEFAULT_STRATEGY = "weather"
DEFAULT_TICK_INTERVAL = 60
MIN_TICK_INTERVAL = 10

# ============================================================
# GESTION DU RISQUE / FRAIS
# ============================================================
MAX_EXPOSURE_PCT = 0.60
MAX_POSITION_PCT = 0.15
MAX_TRADE_USDC_CAP = 200.0
FEE_RATE = 0.05
FEE_REBATE = 0.25

# ============================================================
# HISTORIQUE D'EQUITY / PERSISTANCE
# ============================================================
EQUITY_HISTORY_MAX_ROWS = 5000
SNAPSHOT_INTERVAL_SEC = 120   # fréquence d'envoi du snapshot vers Supabase Storage

# ============================================================
# CALIBRATION (apprentissage des probabilités)
# ============================================================
CALIBRATION_MIN_SAMPLES = 60   # avant ça : identité (pas assez de données)
CALIBRATION_MIN_LOSSES = 10
CALIBRATION_BINS = 10
CALIBRATION_PRIOR = 25.0
CALIBRATION_REFIT_SEC = 90

# ============================================================
# STRATÉGIE MÉTÉO (marchés « Highest temperature »)
# ============================================================
# Ensemble multi-modèles Open-Meteo (≈ 120 scénarios cumulés)
WEATHER_MODELS = "gfs025,icon_seamless,ecmwf_ifs025"
WEATHER_TICK_INTERVAL = 60          # cadence lente (marchés journaliers)
WEATHER_ENS_CACHE_TTL = 1800        # cache ensemble (s) — les runs sortent toutes les 6h
WEATHER_ENS_BUDGET_PER_TICK = 8     # nouveaux fetchs d'ensemble max par tick (anti-429)
WEATHER_429_COOLDOWN = 120          # pause des appels Open-Meteo après un 429 (s)
WEATHER_REALIZED_CACHE_TTL = 300    # cache du max réalisé (s)

WEATHER_EDGE_THRESHOLD = 0.05       # edge mini (P calibrée − prix − frais)
WEATHER_MIN_BUY_PRICE = 0.02        # garde-fous de prix
WEATHER_MAX_BUY_PRICE = 0.60        # NE PAS acheter de favori cher (leçon crypto)

# Pondération des familles de modèles dans l'ensemble (ECMWF = le plus précis)
WEATHER_MODEL_WEIGHTS = {"ecmwf": 1.4, "gfs": 1.0, "icon": 1.0}
# Les ensembles sont souvent trop confiants -> on élargit la dispersion autour
# de la médiane (1.0 = brut)
WEATHER_SPREAD_INFLATE = 1.12

# Max réalisé : la grille Open-Meteo peut surestimer le capteur officiel ->
# marge de sécurité soustraite avant conditionnement (0 pour les stations NWS,
# qui SONT le capteur officiel).
WEATHER_REALIZED_MARGIN_C = 0.4     # en °C (convertie en °F au besoin)
WEATHER_NWS_CACHE_TTL = 300

# Sorties : surpayé (le marché paie plus que la valeur modèle), sauvetage
# (notre proba s'est effondrée mais un bid existe), verrouillage (issue quasi
# certaine, on encaisse tôt et on recycle le capital).
WEATHER_EXIT_EDGE = 0.12
WEATHER_SALVAGE_P = 0.05
WEATHER_SALVAGE_MIN_BID = 0.02
WEATHER_LOCK_P = 0.80
WEATHER_LOCK_BID = 0.90

# Qualité de carnet exigée à l'achat (leçon anti-sélection)
WEATHER_MAX_SPREAD = 0.10           # ask - bid maximum
WEATHER_MIN_BOOK_USDC = 4.0         # profondeur mini au meilleur ask

# Lissage par noyau des probabilités de tranches (122 scénarios = granularité
# 0,8 % et queues bruitées -> chaque scénario devient une petite gaussienne)
WEATHER_KERNEL_BW_C = 0.8           # bande passante (°C)
WEATHER_KERNEL_BW_F = 1.4           # bande passante (°F)

# Calibration du biais grille↔station par ville, apprise sur les marchés
# température DÉJÀ RÉSOLUS de Polymarket (la tranche gagnante révèle le max
# officiel) comparés à l'historique de la grille Open-Meteo.
BIAS_HISTORY_DAYS = 60        # profondeur d'historique grille
BIAS_MIN_SAMPLES = 3          # n mini de jours résolus pour appliquer un biais
BIAS_CLAMP = 4.0              # borne de sécurité (degrés, unité du marché)
BIAS_REFRESH_HOURS = 12       # fréquence de re-harvest

WEATHER_KELLY_FRACTION = 0.30       # sizing par edge (prudent)
WEATHER_STAKE_MIN_USDC = 2.0
WEATHER_STAKE_MAX_USDC = 25.0
WEATHER_MAX_BUCKETS_PER_MARKET = 3  # diversifier, pas tout sur un seul marché
WEATHER_SIGNALS_MAX = 60            # snapshots exposés au frontend
