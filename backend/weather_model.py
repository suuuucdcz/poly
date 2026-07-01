"""Transforme une distribution d'ensemble (liste de maxima simulés) en
probabilités par bucket de marché Polymarket, et parse les métadonnées
(tranches, date cible) depuis les libellés du marché.

Buckets supportés :
  '24°C'             -> max arrondi == 24
  '33°C or higher'   -> max arrondi >= 33
  '23°C or below'    -> max arrondi <= 23
  '24-25°C'          -> 24 <= max arrondi <= 25   (certains marchés)

Règle officielle (RELUE dans les descriptions de marché) : la grande majorité des
marchés se résolvent sur une source qui reporte le max « to whole degrees Celsius
(eg, 9°C) » (METAR / Wunderground / NOAA) — c.-à-d. **arrondi au degré entier le
plus proche**. La tranche « 28°C » couvre donc [27.5, 28.5) → on **arrondit** chaque
scénario au demi près (28.6 → 29, 28.4 → 28), on ne tronque PAS.
(Cas particulier : quelques marchés « to one decimal place » comme Hong Kong sont
en réalité au plancher [28.0, 28.9] ; on privilégie ici la convention majoritaire.)

Conditionnement intraday : si un max `realized` est déjà observé AUJOURD'HUI à
la station (et que le marché porte sur aujourd'hui), le max final sera ≥
realized → on plafonne chaque scénario par le bas (`max(scénario, realized)`),
ce qui élimine les tranches devenues impossibles.
"""

import math
import re

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def parse_target_date(title):
    """'Highest temperature in NYC on June 9?' -> (6, 9) ou None."""
    m = re.search(r"\bon\s+([A-Za-z]+)\.?\s+(\d{1,2})", title or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return None
    return (month, int(m.group(2)))


def match_date(dates, target):
    """Trouve dans `dates` (['YYYY-MM-DD', ...], locales station) celle qui
    correspond à (mois, jour). Gère le changement d'année implicitement
    puisque seules ~3 dates consécutives sont proposées."""
    if not target:
        return None
    month, day = target
    for dt in dates:
        try:
            _y, m, d = dt.split("-")
            if int(m) == month and int(d) == day:
                return dt
        except ValueError:
            continue
    return None


def parse_bucket(label):
    """Label -> (kind, v1, v2, unit) ; kind ∈ {'eq','ge','le','range'} ;
    unit ∈ {'C','F'} ; None si non parsable."""
    if not label:
        return None
    s = str(label).strip()
    unit = "F" if "°F" in s or s.upper().rstrip("?").endswith("F") else "C"
    low = s.lower()
    # Tranche « 24-25°C » / « 24 to 25°C » (séparateur explicite, signes gérés)
    m = re.search(r"(-?\d+)\s*(?:-|–|\bto\b)\s*(-?\d+)", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return ("range", min(a, b), max(a, b), unit)
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    v = int(m.group(0))
    if "higher" in low or "above" in low or "or more" in low or "+" in low:
        return ("ge", v, None, unit)
    if "below" in low or "lower" in low or "or less" in low or "under" in low:
        return ("le", v, None, unit)
    return ("eq", v, None, unit)


def _normalize(members):
    """Accepte [float, ...] ou [(valeur, poids), ...] -> [(valeur, poids), ...]."""
    out = []
    for m in members:
        if isinstance(m, (tuple, list)):
            out.append((float(m[0]), float(m[1])))
        else:
            out.append((float(m), 1.0))
    return out


def inflate_members(members, factor):
    """Élargit la dispersion autour de la médiane (les ensembles sont souvent
    trop confiants). factor=1.0 -> inchangé."""
    pairs = _normalize(members)
    if not pairs or factor == 1.0:
        return pairs
    med = weighted_median(pairs)
    return [(med + (v - med) * factor, w) for v, w in pairs]


def weighted_median(pairs):
    s = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in s)
    acc = 0.0
    for v, w in s:
        acc += w
        if acc >= total / 2:
            return v
    return s[-1][0] if s else 0.0


def _phi(x):
    """CDF de la normale centrée réduite."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bucket_bounds(parsed):
    """Bornes CONTINUES [a, b) du max officiel pour une tranche (règle ARRONDI) :
    'eq 28' -> [27.5,28.5) ; 'range 88-89' -> [87.5,89.5) ; 'ge 33' -> [32.5,+inf) ;
    'le 23' -> (-inf,23.5)."""
    kind, v1, v2, _u = parsed
    if kind == "eq":
        return v1 - 0.5, v1 + 0.5
    if kind == "range":
        return v1 - 0.5, v2 + 0.5
    if kind == "ge":
        return v1 - 0.5, None
    return None, v1 + 0.5   # le


def bucket_probabilities(members, buckets, realized=None, bandwidth=0.0):
    """members: [float] ou [(valeur, poids)]. buckets: liste de (label, parsed).
    Retourne {label: proba pondérée} ; somme ≈ 1 sur des buckets exhaustifs.

    bandwidth > 0 : LISSAGE PAR NOYAU — chaque scénario devient une petite
    gaussienne N(v, bandwidth). Corrige la granularité (1/n) et les queues
    bruitées du comptage brut. Le conditionnement au max déjà réalisé est
    appliqué par troncature propre de chaque noyau (pas de fuite de masse
    sous le réalisé). bandwidth = 0 : comptage brut historique.
    """
    pairs = _normalize(members)
    if not pairs:
        return {}

    if bandwidth and bandwidth > 0:
        total_w = sum(w for _, w in pairs)
        bounds = [(lbl, _bucket_bounds(p) if p else None) for lbl, p in buckets]
        out = {lbl: (0.0 if bb is not None else None) for lbl, bb in bounds}
        r = realized
        for v, w in pairs:
            Fr = _phi((r - v) / bandwidth) if r is not None else 0.0
            denom = 1.0 - Fr
            if denom < 1e-9:
                # scénario entièrement sous le réalisé -> masse au point r
                for lbl, bb in bounds:
                    if bb is None:
                        continue
                    a, b = bb
                    lo = a if a is not None else float("-inf")
                    hi = b if b is not None else float("inf")
                    if lo <= r < hi:
                        out[lbl] += w
                        break
                continue
            for lbl, bb in bounds:
                if bb is None:
                    continue
                a, b = bb
                Fb = 1.0 if b is None else _phi((b - v) / bandwidth)
                Fa = 0.0 if a is None else _phi((a - v) / bandwidth)
                if r is not None:
                    Fb = max(Fb, Fr)
                    Fa = max(Fa, Fr)
                if Fb > Fa:
                    out[lbl] += w * (Fb - Fa) / denom
        for lbl in out:
            if out[lbl] is not None:
                out[lbl] /= total_w
        return out

    # --- comptage brut (arrondi au degré entier le plus proche) ---
    if realized is not None:
        pairs = [(max(v, realized), w) for v, w in pairs]
    ints = [(math.floor(v + 0.5), w) for v, w in pairs]   # round-half-up
    total = sum(w for _, w in ints)
    out = {}
    for label, parsed in buckets:
        if not parsed:
            out[label] = None
            continue
        kind, v1, v2, _unit = parsed
        if kind == "eq":
            c = sum(w for i, w in ints if i == v1)
        elif kind == "ge":
            c = sum(w for i, w in ints if i >= v1)
        elif kind == "le":
            c = sum(w for i, w in ints if i <= v1)
        else:  # range
            c = sum(w for i, w in ints if v1 <= i <= v2)
        out[label] = c / total
    return out


def ensemble_summary(members):
    """(min, médiane, max, écart-type) pour l'affichage (pondéré)."""
    pairs = _normalize(members)
    if not pairs:
        return None
    vals = [v for v, _ in pairs]
    med = weighted_median(pairs)
    total = sum(w for _, w in pairs)
    mean = sum(v * w for v, w in pairs) / total
    var = sum(w * (v - mean) ** 2 for v, w in pairs) / total
    return {"min": min(vals), "median": med, "max": max(vals), "std": var ** 0.5}
