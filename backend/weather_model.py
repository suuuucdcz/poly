"""Transforme une distribution d'ensemble (liste de maxima simulés) en
probabilités par bucket de marché Polymarket.

Les buckets sont des tranches d'1 degré entier :
  '24°C'            -> le max arrondi == 24
  '33°C or higher'  -> le max arrondi >= 33
  '23°C or below'   -> le max arrondi <= 23

La station de résolution rapporte un degré ENTIER → on arrondit chaque scénario
à l'entier le plus proche, puis on compte la fraction de scénarios par bucket.

Conditionnement intraday : si un max `realized` est déjà observé, le max FINAL
sera ≥ realized → on plafonne chaque scénario par le bas à `realized`
(`effectif = max(scénario, realized)`), ce qui tue les buckets trop bas.
"""

import re


def parse_bucket(label):
    """'24°C'/'33°C or higher'/'23°C or below' -> (kind, value, unit) ou None.
    kind ∈ {'eq','ge','le'} ; unit ∈ {'C','F'}."""
    if not label:
        return None
    s = str(label).strip()
    unit = "F" if "°F" in s or s.upper().endswith("F") else "C"
    m = re.search(r"(-?\d+)", s)
    if not m:
        return None
    v = int(m.group(1))
    low = s.lower()
    if "higher" in low or "above" in low or "or more" in low or "+" in low:
        kind = "ge"
    elif "below" in low or "lower" in low or "or less" in low or "under" in low:
        kind = "le"
    else:
        kind = "eq"
    return (kind, v, unit)


def bucket_probabilities(members, buckets, realized=None):
    """members: liste de float (max simulés). buckets: liste de (label, parsed).
    Retourne {label: proba} ; somme ≈ 1 sur des buckets exhaustifs."""
    if not members:
        return {}
    if realized is not None:
        eff = [max(m, realized) for m in members]
    else:
        eff = list(members)
    ints = [int(round(x)) for x in eff]
    n = len(ints)
    out = {}
    for label, parsed in buckets:
        if not parsed:
            out[label] = None
            continue
        kind, v, _unit = parsed
        if kind == "eq":
            c = sum(1 for i in ints if i == v)
        elif kind == "ge":
            c = sum(1 for i in ints if i >= v)
        else:  # le
            c = sum(1 for i in ints if i <= v)
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
