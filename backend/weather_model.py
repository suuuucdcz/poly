"""Transforme une distribution d'ensemble (liste de maxima simulés) en
probabilités par bucket de marché Polymarket, et parse les métadonnées
(tranches, date cible) depuis les libellés du marché.

Buckets supportés :
  '24°C'             -> max arrondi == 24
  '33°C or higher'   -> max arrondi >= 33
  '23°C or below'    -> max arrondi <= 23
  '24-25°C'          -> 24 <= max arrondi <= 25   (certains marchés)

Règle officielle (lue dans les descriptions de marché) : la résolution est « la
tranche qui CONTIENT le max », mesuré **à une décimale** (ex. 28.6°C). La tranche
« 28°C » couvre donc [28.0, 28.9] → on **tronque** chaque scénario (floor), on
n'arrondit PAS (28.6 → 28, pas 29).

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


def bucket_probabilities(members, buckets, realized=None):
    """members: liste de float (max simulés). buckets: liste de (label, parsed).
    Retourne {label: proba} ; somme ≈ 1 sur des buckets exhaustifs."""
    if not members:
        return {}
    eff = [max(m, realized) for m in members] if realized is not None else list(members)
    # Troncature (floor), conformément à la règle « tranche qui contient le max »
    ints = [math.floor(x) for x in eff]
    n = len(ints)
    out = {}
    for label, parsed in buckets:
        if not parsed:
            out[label] = None
            continue
        kind, v1, v2, _unit = parsed
        if kind == "eq":
            c = sum(1 for i in ints if i == v1)
        elif kind == "ge":
            c = sum(1 for i in ints if i >= v1)
        elif kind == "le":
            c = sum(1 for i in ints if i <= v1)
        else:  # range
            c = sum(1 for i in ints if v1 <= i <= v2)
        out[label] = c / n
    return out


def ensemble_summary(members):
    """(min, médiane, max, écart-type) pour l'affichage."""
    if not members:
        return None
    s = sorted(members)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n
    return {"min": s[0], "median": med, "max": s[-1], "std": var ** 0.5}
