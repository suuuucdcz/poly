"""Configuration centralisée du bot de paper trading.

Toutes les constantes ajustables (frais, risque, indicateurs, paramètres de
stratégie, endpoints d'API) vivent ici. Les valeurs modifiables à chaud par
l'utilisateur (stratégie active, intervalle de tick, volume de scan) restent
portées par l'instance du bot et sont seulement *initialisées* depuis ce module.
"""

# ============================================================
# ENDPOINTS API
# ============================================================
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_API = "https://api.binance.com"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    )
}
HTTP_TIMEOUT = 8

# ============================================================
# DÉFAUTS DU BOT (modifiables à chaud sur l'instance)
# ============================================================
DEFAULT_STRATEGY = "crypto_direction"
DEFAULT_TICK_INTERVAL = 2
DEFAULT_MAX_MARKETS_TO_SCAN = 15
VALID_STRATEGIES = ["arbitrage", "momentum", "value", "hybrid", "crypto_direction"]

# ============================================================
# RÉCUPÉRATION DES MARCHÉS GÉNÉRIQUES
# ============================================================
MARKETS_CACHE_TTL = 5.0
MARKETS_FETCH_URL = (
    GAMMA_API + "/markets?active=true&closed=false&limit=100&minimum_liquidity=2000"
)

# ============================================================
# GESTION DU RISQUE
# ============================================================
MAX_EXPOSURE_PCT = 0.60   # Jamais plus de 60% du capital total investi
MAX_POSITION_PCT = 0.15   # Max 15% du capital par marché
MIN_MARKET_HOURS = 48     # Ignore les marchés clôturant dans < 48h (stratégies classiques)
MAX_SPREAD_PCT = 0.08     # Ignore les marchés au spread > 8%
MAX_TRADE_USDC_CAP = 200.0

# ============================================================
# FRAIS POLYMARKET (taker 5%, rebate 25%)
# ============================================================
FEE_RATE = 0.05
FEE_REBATE = 0.25

# ============================================================
# INDICATEURS TECHNIQUES
# ============================================================
EMA_FAST_PERIOD = 8
EMA_SLOW_PERIOD = 21
RSI_PERIOD = 14
HISTORY_MAX_LEN = 40       # Garde jusqu'à 40 ticks d'historique de prix
MIN_TICKS_FOR_TRADE = 10   # Au moins 10 ticks avant tout signal

# ============================================================
# HISTORIQUE D'EQUITY (borne la table SQLite)
# ============================================================
EQUITY_HISTORY_MAX_ROWS = 5000

# ============================================================
# PERSISTANCE (snapshots vers Supabase Storage, cf. persistence.py)
# ============================================================
SNAPSHOT_INTERVAL_SEC = 120   # fréquence d'envoi du snapshot de la base

# ============================================================
# STRATÉGIE CRYPTO "UP / DOWN" (court terme)
# ============================================================
# Actif -> symbole Binance. Les actifs sans symbole (ex: HYPE) sont ignorés.
CRYPTO_ASSETS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "DOGE": "DOGEUSDT",
    "XRP": "XRPUSDT",
}
CRYPTO_WINDOWS = ["5m", "15m"]
CRYPTO_WINDOW_SECONDS = {"5m": 300, "15m": 900}

# Seuil d'edge (espérance positive après frais) requis pour entrer.
# Le gating de confiance (CRYPTO_MIN_CONFIDENCE) étant le vrai filtre de qualité
# directionnelle, ce seuil d'edge peut rester modéré.
CRYPTO_EDGE_THRESHOLD = 0.04

# On n'entre que dans la bande de temps finale de la fenêtre :
#  - pas plus tôt que MAX secondes avant la clôture (signal trop bruité)
#  - pas plus tard que MIN secondes (risque de non-exécution / clôture)
CRYPTO_ENTRY_MAX_SECONDS_LEFT = 90
CRYPTO_ENTRY_MIN_SECONDS_LEFT = 5

# Garde-fous de prix du token (évite les extrêmes 0/1 sans valeur)
CRYPTO_MIN_PRICE = 0.02
CRYPTO_MAX_PRICE = 0.98

# Feed de prix
CRYPTO_SPOT_CACHE_TTL = 1.0      # cache du spot Binance (secondes)
CRYPTO_VOL_LOOKBACK_MIN = 30     # minutes de klines 1m pour estimer la volatilité
CRYPTO_VOL_REFRESH_SEC = 60      # recalcul de sigma au plus une fois par minute
# Volatilité par défaut, exprimée "par racine de seconde" (sigma·√t = stdev du
# rendement sur t secondes). ~ stdev 1m de BTC (0.06%) / √60.
CRYPTO_DEFAULT_SIGMA = 0.00008

# Taille max d'une position sur une fenêtre crypto (en plus des limites de risque)
CRYPTO_MAX_POSITION_USDC = 50.0

# Nombre de snapshots de signaux conservés/exposés au frontend
CRYPTO_SIGNALS_MAX = 40

# ---- v2 : gating de confiance + order flow + règlement proactif ----
# Ne parie un côté que si le modèle lui accorde AU MOINS cette probabilité.
# (Empêche de parier l'outsider « pas cher » CONTRE le mouvement réel — la faille de la v1.)
CRYPTO_MIN_CONFIDENCE = 0.62

# Confirmation par flux d'ordres agresseur (Binance)
CRYPTO_USE_ORDERFLOW = True
CRYPTO_FLOW_LOOKBACK_SEC = 45     # fenêtre du flux agresseur
CRYPTO_FLOW_CACHE_TTL = 2.5       # cache du flux (s)
CRYPTO_FLOW_VETO = 0.55          # bloque le trade si le flux contredit fortement le côté visé

# Règlement proactif : encaisse dès la fin de fenêtre via le prix de clôture Binance,
# au lieu d'attendre que Polymarket bascule closed=True (~3-4 min).
CRYPTO_PROACTIVE_SETTLE = True
CRYPTO_SETTLE_GRACE_SEC = 3       # délai après la clôture avant de lire le prix final

# ---- v3 : sizing adaptatif (Kelly) + plafond de corrélation + calibration ----
# Mise proportionnelle à l'edge (Kelly fractionnaire) au lieu d'un montant fixe.
CRYPTO_KELLY_FRACTION = 0.5       # fraction de Kelly (prudence)
CRYPTO_STAKE_MIN_USDC = 4.0       # en-dessous, on ne mise pas (trop petit)
CRYPTO_STAKE_MAX_USDC = 60.0      # plafond par pari

# Plafond d'exposition simultanée sur le PANIER crypto (BTC/ETH/SOL… corrélés)
# -> corrige le défaut « 8 paris corrélés = un seul gros pari ».
CRYPTO_MAX_BUCKET_USDC = 120.0

# Calibration : la stratégie apprend de ses résultats passés à corriger ses probas.
CRYPTO_CALIBRATION_ENABLED = True
CRYPTO_CALIBRATION_MIN_SAMPLES = 60   # avant ça : identité (pas assez de données)
CRYPTO_CALIBRATION_MIN_LOSSES = 10    # besoin de défaites pour calibrer (sinon biais)
CRYPTO_CALIBRATION_BINS = 10
CRYPTO_CALIBRATION_PRIOR = 25.0       # force du prior (shrink vers la proba modèle)
CRYPTO_CALIBRATION_REFIT_SEC = 90     # refit périodique
