"""Calibration du biais grille ↔ station officielle, par ville.

Constat (vu en prod) : la grille Open-Meteo (maille ~10-25 km) lit parfois
plus chaud/froid que LE capteur officiel qui résout le marché (tarmac
d'aéroport, observatoire...). Ce biais systématique décale toutes nos probas.

L'astuce : Polymarket a des CENTAINES de marchés température déjà RÉSOLUS.
La tranche gagnante d'un marché résolu révèle le max officiel du jour
(« 88-89°F » gagnant → max officiel ∈ [88, 90) → milieu 89.0). En la
comparant au max de la grille Open-Meteo pour ce même jour, on obtient un
échantillon de biais RÉEL par ville — sans attendre nos propres paris.

  biais(ville) = médiane( max_officiel(jour) − max_grille(jour) )

Appliqué ensuite aux membres d'ensemble (v + biais) avant le calcul des probas.
Persisté en base (table city_bias) → survit aux redéploiements via Supabase.
"""

import json
import statistics

from backend import config, db
from backend.cities import resolve_city
from backend.weather_model import parse_bucket


def winner_official_mid(label):
    """Tranche gagnante -> estimation du max officiel (centre de la tranche).
    Convention ARRONDI (majoritaire) : la source reporte des degrés entiers, donc
    '28°C' == max reporté 28 -> 28.0 ; '88-89°F' -> 88.5.
    (Avant : +0.5 systématique, qui gonflait artificiellement le biais « chaud ».)
    Tranches ouvertes (below/higher) : information non bornée -> None."""
    parsed = parse_bucket(label)
    if not parsed:
        return None
    kind, v1, v2, _unit = parsed
    if kind == "eq":
        return float(v1)
    if kind == "range":
        return (v1 + v2) / 2.0
    return None  # ge / le : non borné


async def _fetch_resolved_events(client, pages=4):
    """Events température résolus (les plus récents d'abord)."""
    out = []
    for offset in range(0, pages * 100, 100):
        page = await client.fetch_api_json(
            f"{client.gamma}/events?closed=true&limit=100&offset={offset}"
            f"&tag_id={client.TEMPERATURE_TAG_ID}&order=id&ascending=false"
        )
        if not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
    return out


async def harvest(feed, client, log):
    """Reconstruit les biais par ville depuis les marchés résolus et les stocke."""
    cfg = config
    try:
        events = await _fetch_resolved_events(client)
    except Exception as e:
        log(f"BIAS: récupération des marchés résolus impossible: {e}", "WARNING")
        return {}

    # 1. (ville, date locale, max officiel) depuis les tranches gagnantes
    samples = {}   # city -> list[(date, official_mid, unit)]
    for e in events:
        title = e.get("title") or ""
        if not title.lower().startswith("highest temperature"):
            continue
        city, coords = resolve_city(title)
        if not coords:
            continue
        end = (e.get("endDate") or "")[:10]   # date locale ≈ date d'endDate
        if not end:
            continue
        winner = None
        unit = "C"
        for m in e.get("markets") or []:
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
            except Exception:
                continue
            if prices and float(prices[0]) > 0.99:   # Yes a gagné
                winner = m.get("groupItemTitle") or m.get("question")
                unit = "F" if "°F" in (winner or "") else "C"
                break
        if not winner:
            continue
        mid = winner_official_mid(winner)
        if mid is None:
            continue
        samples.setdefault(city, []).append((end, mid, unit))

    # 2. comparer à l'historique de la grille, ville par ville
    biases = {}
    for city, rows in samples.items():
        _name, coords = resolve_city(city)
        if not coords:
            continue
        unit = rows[0][2]
        om_unit = "fahrenheit" if unit == "F" else "celsius"
        try:
            grid = await feed.grid_daily_max_history(coords[0], coords[1], om_unit)
        except Exception:
            continue
        diffs = []
        for (date, official_mid, _u) in rows:
            g = grid.get(date)
            if g is None:
                continue
            d = official_mid - g
            if abs(d) <= 8:               # écarte les aberrations (mauvais match de date)
                diffs.append(d)
        if len(diffs) >= cfg.BIAS_MIN_SAMPLES:
            med = statistics.median(diffs)
            med = max(-cfg.BIAS_CLAMP, min(cfg.BIAS_CLAMP, med))
            std = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
            biases[city] = (round(med, 2), round(std, 2), len(diffs))
            db.set_city_bias(city, med, std, len(diffs))

    if biases:
        top = sorted(biases.items(), key=lambda kv: -abs(kv[0] is not None and kv[1][0] or 0))[:6]
        msg = ", ".join(f"{c} {b[0]:+.1f}°(n={b[2]})" for c, b in top)
        log(f"BIAS: calibration mise à jour pour {len(biases)} villes — {msg}", "INFO")
    return biases
