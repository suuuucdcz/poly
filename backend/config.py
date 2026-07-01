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
# Déploiement du modèle corrigé (biais+noyau+filtres+NWS) — sert de frontière
# « ancien / nouveau » pour les badges et la purge.
MODEL_V2_CUTOFF = "2026-06-10T16:00:00"
DEFAULT_TICK_INTERVAL = 60
MIN_TICK_INTERVAL = 10

# ============================================================
# GESTION DU RISQUE / FRAIS
# ============================================================
MAX_EXPOSURE_PCT = 0.60
MAX_POSITION_PCT = 0.15
MAX_TRADE_USDC_CAP = 200.0
# Frais taker météo = 5 % (catégorie « weather », doc officielle Polymarket) :
# frais_par_part = FEE_RATE · p · (1−p). Réellement prélevé (feesEnabled=true).
FEE_RATE = 0.05
FEE_REBATE = 0.0    # (obsolète) le rebate de 25 % était fictif -> mis à 0, plus utilisé

# ============================================================
# HISTORIQUE D'EQUITY / PERSISTANCE
# ============================================================
EQUITY_HISTORY_MAX_ROWS = 5000
SNAPSHOT_INTERVAL_SEC = 120   # fréquence d'envoi du snapshot vers Supabase Storage

# ============================================================
# CALIBRATION (apprentissage des probabilités)
# ============================================================
CALIBRATION_MIN_SAMPLES = 50   # 58 résolutions réelles en stock ; V4 ne résout plus -> activer sur l'acquis
CALIBRATION_MIN_LOSSES = 10
CALIBRATION_BINS = 10
# Prior abaissé (25 -> 12) : avec 504 résolutions / 0 gagnée, l'évidence empirique
# DOIT tirer les probas calibrées vers le bas. Un prior trop fort maintenait des
# probas gonflées (ex. 0.40 -> 0.20) qui créaient de faux edges face à des prix
# quasi nuls -> le bot achetait des tranches déjà mortes. Voir aussi WEATHER_MIN_BUY_PRICE.
CALIBRATION_PRIOR = 12.0
CALIBRATION_REFIT_SEC = 90

# ============================================================
# STRATÉGIE MÉTÉO (marchés « Highest temperature »)
# ============================================================
# Ensemble multi-modèles Open-Meteo (≈ 120 scénarios cumulés)
WEATHER_MODELS = "gfs025,icon_seamless,ecmwf_ifs025"
WEATHER_TICK_INTERVAL = 60          # cadence lente (marchés journaliers)
WEATHER_ENS_CACHE_TTL = 10800       # cache ensemble 3 h (runs 6-horaires ; quota gratuit serré)
WEATHER_ENS_BUDGET_PER_TICK = 6     # nouveaux fetchs d'ensemble max par tick (anti-429)
WEATHER_429_COOLDOWN = 300          # pause des appels Open-Meteo après un 429 (s)

# Source de SECOURS quand Open-Meteo est indisponible (quota IP grillé...) :
# met.no (institut météo norvégien, gratuit, sans clé). Prévision déterministe
# -> on synthétise une distribution autour d'elle (sigma climatologique).
WEATHER_METNO_TTL = 1800
WEATHER_SYNTH_SIGMA_C = 1.6
WEATHER_SYNTH_SIGMA_F = 2.9
WEATHER_REALIZED_CACHE_TTL = 300    # cache du max réalisé (s)

# INTERRUPTEUR D'ENTRÉES — coupé le 12/06 après verdict statistique : 58
# résolutions, 0 gagnée (proba de malchance ~0,2 %). Les SORTIES continuent de
# gérer le livre existant. Ne réactiver qu'avec une stratégie d'entrée revue.
WEATHER_ENTRIES_ENABLED = True   # réactivé en mode V4 « cycle » (jamais de résolution)

WEATHER_EDGE_THRESHOLD = 0.05       # edge mini (P calibrée − prix − frais)
# Plancher de prix RELEVÉ 0.02 -> 0.10 : sur ce marché très « sharp », une tranche
# cotée < 0.10 est une tranche que le marché a (correctement) quasi exclue. Nos 504
# résolutions à 0 gagnée sont massivement des achats de tranches à 0.001–0.06 :
# le modèle prenait le prix minuscule pour une « sous-évaluation » alors que c'était
# de l'information juste. On exige désormais que le marché corrobore (prix >= 0.10).
WEATHER_MIN_BUY_PRICE = 0.10        # garde-fous de prix
WEATHER_MAX_BUY_PRICE = 0.60        # NE PAS acheter de favori cher (leçon crypto)
# Si une tranche est déjà quasi certaine (favori du marché au-dessus de ce seuil),
# la journée est jouée : on n'entre PAS sur les tranches voisines (longshots perdants).
WEATHER_SKIP_IF_FAVORITE_ABOVE = 0.90

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
WEATHER_EXIT_EDGE = 0.07            # V4 : prise de profit plus rapide
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
BIAS_MIN_SAMPLES = 6          # n mini de jours résolus (3 = bruit) pour appliquer un biais
# Borne ABAISSÉE 4.0 -> 1.0 : la distribution est « sharp » (σ ≈ 1–1.5°). Un biais
# appris de +2.4° (vu en prod sur Guangzhou) déplaçait tout le paquet de probas de
# 2 tranches vers le CHAUD et faisait parier sur des tranches trop hautes qui ne
# sortaient jamais. Preuve : ensemble brut Guangzhou ≈ 33.6°, marché + réalisé ≈ 33° ;
# le biais +2.4 poussait la médiane affichée à 36° -> achats de 35/36° perdus.
# On ne s'autorise plus qu'un recentrage fin. (À envisager : le désactiver.)
BIAS_CLAMP = 1.0             # borne de sécurité (degrés, unité du marché)
BIAS_REFRESH_HOURS = 12       # fréquence de re-harvest

# Plafond d'exposition par RÉGION météo (les villes d'une région partagent le
# même régime : 10 paris Texas = 1 seul pari canicule).
WEATHER_MAX_REGION_USDC = 90.0

# Incertitude par ville (std du biais appris) : élargit la distribution et
# réduit Kelly pour les villes imprévisibles (Dallas std 1.7) vs fiables (Madrid 0.3).
WEATHER_STD_INFLATE_K = 0.10        # inflation += K * std
WEATHER_STD_KELLY_DAMP = 0.5        # kelly /= (1 + DAMP * std)

# Blend avec la prévision NWS officielle (villes US) : moyenne entre la médiane
# d'ensemble et la prévision du forecaster (déjà corrigée station par des humains).
WEATHER_NWS_BLEND = 0.5             # poids du recentrage vers la prévision NWS
WEATHER_NWS_FORECAST_TTL = 10800

WEATHER_KELLY_FRACTION = 0.20       # sizing par edge (prudent)
WEATHER_STAKE_MIN_USDC = 2.0
WEATHER_STAKE_MAX_USDC = 10.0       # V4 : pilote à petites mises
WEATHER_MAX_BUCKETS_PER_MARKET = 3

# ===== MODE V4 « CYCLE » (trader le chemin, jamais la destination) =====
# Verdict 0/58 aux résolutions -> on ne détient PLUS JAMAIS jusqu'à l'arrivée :
#  - liquidation forcée le soir même (heure locale station) : un gagnant se vend
#    ~0.90-0.97 sans risque binaire, un perdant se sauve à quelques centimes ;
#  - entrées limitées à aujourd'hui/demain (la dérive J+2 a coûté cher).
# Liquidation calée sur la clôture RÉELLE de chaque marché (son endDate) :
# vendre dans les X dernières heures avant que CE marché se résolve. S'adapte
# tout seul au fuseau de chaque ville, sans table d'heures locales.
WEATHER_LIQUIDATE_BEFORE_CLOSE_H = 2.0
WEATHER_MAX_TARGET_DAYS = 1         # entrées: J (0) et J+1 (1) uniquement
WEATHER_SIGNALS_MAX = 60            # snapshots exposés au frontend

# ============================================================
# MOTEUR « CONVERGENCE INTRADAY » (le vrai edge : lire le thermomètre)
# ============================================================
# Principe de trader : le max du jour ne se DEVINE pas, il se LIT au capteur,
# heure par heure. Le max est monotone croissant ; passé le pic (~15h locale), il
# est quasi figé. On achète la tranche qui CONTIENT le max réel quand le marché
# tarde à s'ajuster (quasi-arbitrage), on encaisse la convergence par paliers, et
# on COUPE tout de suite ce qui diverge. Jamais de tenue jusqu'à la résolution.
STRATEGY_ENGINE = "convergence"     # "convergence" (nouveau) ou "edge" (ancien modèle prévision)

CONV_PEAK_HOUR = 15.0               # heure locale ≈ pic de température (max ≈ figé après)
CONV_ENTRIES_NWS_ONLY = True        # PRUDENT : n'entrer que là où on lit le capteur EXACT
                                    #   (stations NWS US = la source de résolution). Ailleurs
                                    #   la grille ≠ le capteur qui résout -> on gère seulement.
CONV_MIN_FAIR = 0.60               # proba conditionnée mini pour juger le gagnant « connu »
                                   #   (0.60 : on évite les cas ambigus au bord du .5 d'arrondi)
CONV_EDGE = 0.06                   # (juste − ask) mini pour entrer (le marché sous-évalue)
CONV_MIN_ENTRY = 0.12              # ne pas acheter la poussière
CONV_MAX_ENTRY = 0.90              # ne pas surpayer une convergence déjà faite
CONV_SIGMA_PASTPEAK = 0.20         # σ après le pic, en DEGRÉS-MARCHÉ (arrondi/lecture) — petit = confiant
CONV_SIGMA_PRE_C = 1.4             # σ avant le pic (°C, physique) — on s'appuie encore sur la prévision
CONV_SIGMA_EVENING_DECAY = 0.02    # σ diminue de ce montant par heure après le pic (plus sûr le soir)
CONV_FAIR_CUT = 0.25               # sortie « molle » : si notre tranche passe sous ce seuil, on coupe
CONV_TP1 = 0.80                    # scale-out : au-dessus de ce bid, on allège
CONV_TP1_FRAC = 0.5                # fraction vendue au palier TP1
CONV_TP_MIN_PROFIT = 0.04          # TP seulement si bid > coût + marge (sinon on churnerait :
                                   #   acheté 0.87, TP à bid 0.84 = vente À PERTE + frais x2)
CONV_TP2 = 0.93                    # lock : au-dessus de ce bid, on vend tout (risque binaire nul)
# Confirmation du PIC : le pic thermique varie selon la ville (LA/Phoenix ~16-17h).
# 15h fixe ne suffit pas -> on exige aussi que R n'ait plus monté depuis X secondes
# avant d'autoriser une entrée (sinon on achèterait round(R) pendant que ça chauffe
# encore, pour se faire couper 1h après).
CONV_STABLE_SEC = 2700             # R inchangé depuis 45 min = pic confirmé
CONV_FLATTEN_LOCAL_HOUR = 20.0     # liquidation TOTALE passé cette heure locale (jamais de résolution)
CONV_KELLY = 0.25                  # Kelly fractionnaire sur l'edge de convergence
CONV_STAKE_MIN_USDC = 2.0
CONV_STAKE_MAX_USDC = 8.0          # PRUDENT : petites mises tant que l'edge n'est pas prouvé
CONV_MAX_BUCKETS_PER_MARKET = 2

# ============================================================
# ARBITRAGE DE PANIER (NegRisk) — edge STRUCTUREL, sans opinion
# ============================================================
# Une et UNE SEULE tranche d'un event température se résout à 1$ (negRisk).
# Si la somme des meilleurs asks de TOUTES les tranches + frais < 1$, acheter le
# panier complet verrouille un profit garanti à la résolution, quoi qu'il arrive
# au thermomètre. Aucune prévision, aucune vitesse (les fenêtres durent des
# minutes -> tick 60 s suffisant). Positions taguées [ARB] et tenues jusqu'à la
# résolution (le seul cas où la résolution binaire est notre alliée).
ARB_ENABLED = True
ARB_MIN_EDGE = 0.015          # profit net mini par set : 1 − (somme asks + frais)
ARB_STAKE_MAX_USDC = 40.0     # capital max immobilisé par panier
ARB_MAX_TOTAL_USDC = 200.0    # capital max immobilisé en paniers, toutes villes
ARB_BOOKS_BUDGET = 36         # carnets lus max par tick (maîtrise la charge API)
ARB_RECHECK_NEAR_SEC = 120    # somme proche de 1 -> re-vérifier dans 2 min
ARB_RECHECK_FAR_SEC = 600     # somme loin de 1 -> re-vérifier dans 10 min
