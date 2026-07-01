"""Arbitrage de panier NegRisk sur les events « Highest temperature » Polymarket.

PRINCIPE (edge structurel, zéro opinion)
----------------------------------------
Les tranches d'un event température sont mutuellement exclusives et exhaustives
(`enableNegRisk: true`) : une et UNE SEULE se résout à 1$. Donc si

    somme des meilleurs asks de TOUTES les tranches + frais  <  1$

acheter 1 part de CHAQUE tranche (« un set ») verrouille un profit garanti à la
résolution : le set paie 1$ quoi qu'il arrive au thermomètre. Aucune prévision,
aucune vitesse requise — ces fenêtres (retrait des makers pendant un mouvement)
durent des minutes, notre tick de 60 s suffit.

DIFFÉRENCES avec le moteur convergence :
  - le panier est GARDÉ JUSQU'À la résolution (c'est elle qui paie le 1$) ;
    les positions sont taguées « [ARB] » dans leur question pour que le moteur
    convergence ne les liquide pas le soir ;
  - le règlement passe par le tick principal (RESOLVE) : la patte gagnante paie
    1$, les autres 0 — le P&L net du panier est le profit verrouillé à l'achat.

GARDE-FOUS :
  - exhaustivité VÉRIFIÉE (un « or below », un « or higher », chaîne contiguë
    entre les deux) : une tranche manquante casse la garantie -> on passe ;
  - toutes les pattes doivent avoir un ask (sinon panier incomplet) ;
  - budget de carnets par tick + mémo de re-check par event (charge API bornée) ;
  - plafonds de capital par panier et global (le capital est immobilisé jusqu'au
    lendemain matin heure locale).

PAPER : l'exécution simultanée des pattes est simulée sans risque de patte ;
en réel, un ask peut disparaître entre deux pattes (à traiter avant tout argent
réel).
"""

import time

from backend import config, db
from backend.weather_model import parse_bucket
from backend.strategies.base import Strategy

ARB_TAG = "[ARB]"


def _best_ask(book):
    """(meilleur ask, taille) ou (None, None)."""
    if not book:
        return None, None
    rows = book.get("asks", [])
    if not rows:
        return None, None
    b = min(rows, key=lambda x: float(x["price"]))
    return float(b["price"]), float(b["size"])


def is_arb_position(pos):
    """True si la position appartient à un panier d'arbitrage (à ne pas gérer
    par les sorties du moteur convergence)."""
    return str(pos.get("question", "")).startswith(ARB_TAG)


class NegRiskArbStrategy(Strategy):
    name = "negrisk_arb"

    def __init__(self, cfg=config):
        self.cfg = cfg
        self._next_check = {}    # titre event -> ts avant lequel on ne relit pas ses carnets
        self.stats = {"windows": 0, "baskets": 0}

    # ------------------------------------------------------------
    # Exhaustivité : les tranches couvrent-elles TOUT l'axe des températures ?
    # ------------------------------------------------------------
    @staticmethod
    def exhaustive(parsed):
        """True si les tranches forment une partition complète : exactement un
        « or below », un « or higher », et une chaîne CONTIGUË d'entiers entre
        les deux ('30 or below', '31', '32'... ou '87 or below', '88-89'...).
        Un trou (tranche filtrée/illisible) casserait la garantie du panier."""
        if any(p is None for p in parsed):
            return False
        les = [p for p in parsed if p[0] == "le"]
        ges = [p for p in parsed if p[0] == "ge"]
        mids = [p for p in parsed if p[0] in ("eq", "range")]
        if len(les) != 1 or len(ges) != 1 or len(mids) != len(parsed) - 2:
            return False
        cur = les[0][1]                       # « X or below » couvre jusqu'à X
        for p in sorted(mids, key=lambda q: q[1]):
            lo = p[1]
            hi = p[1] if p[0] == "eq" else p[2]
            if lo != cur + 1:
                return False                  # trou ou doublon dans la chaîne
            cur = hi
        return ges[0][1] == cur + 1           # « Y or higher » reprend juste après

    # ------------------------------------------------------------
    # Boucle : scanner -> vérifier au carnet -> acheter le panier
    # ------------------------------------------------------------
    async def run(self, ctx, markets, balance, portfolio_value):
        cfg = self.cfg
        if not getattr(cfg, "ARB_ENABLED", True):
            return
        now = time.time()
        try:
            events = await ctx.client.find_temperature_events()   # cache 240 s partagé
        except Exception:
            return

        balance = db.get_portfolio()["balance"]
        positions = db.get_positions()
        arb_open_cost = sum(p["shares"] * p["avg_price"] for p in positions
                            if is_arb_position(p) and p["shares"] > 0)
        # TOUTES les positions ouvertes (arb OU convergence) bloquent l'event :
        # les positions sont indexées par token_id -> acheter une patte sur un
        # token déjà tenu par la convergence FUSIONNERAIT les deux positions
        # (et le tag [ARB] écraserait la question), mélangeant deux logiques de
        # sortie incompatibles. Couvre aussi le one-shot par panier.
        held_tokens = {p["token_id"] for p in positions if p["shares"] > 0}

        budget = cfg.ARB_BOOKS_BUDGET
        checked, best_cost = 0, None
        if len(self._next_check) > 500:      # hygiène : events des jours passés
            self._next_check = {t: ts for t, ts in self._next_check.items() if ts > now}

        # Les plus prometteurs d'abord : somme des prix gamma la plus basse.
        # (Prix gamma = indication ; la décision se prend sur les VRAIS carnets.)
        for ev in sorted(events, key=lambda e: sum(b["yes_price"] for b in e["buckets"])):
            if budget <= 0:
                break
            buckets = ev["buckets"]
            if len(buckets) < 3:
                continue
            # Panier complet exigé : toutes les pattes ouvertes et achetables
            if any(b["closed"] or not b.get("accepting", True) for b in buckets):
                continue
            if any(b["yes_token"] in held_tokens for b in buckets):
                continue                       # déjà un panier sur cet event (one-shot)
            parsed = [parse_bucket(b["label"]) for b in buckets]
            if not self.exhaustive(parsed):
                continue
            nc = self._next_check.get(ev["title"])
            if nc and now < nc:
                continue
            # Pré-filtre gamma : si la somme des prix est déjà loin au-dessus de 1,
            # inutile de brûler le budget de carnets sur cet event ce tick.
            if sum(b["yes_price"] for b in buckets) > 1.05:
                self._next_check[ev["title"]] = now + cfg.ARB_RECHECK_FAR_SEC
                continue

            # --- Vérité du carnet : un ask exécutable sur CHAQUE patte ---
            legs, complete = [], True
            for b in buckets:
                book = await ctx.client.fetch_book(b["yes_token"])
                ask, size = _best_ask(book)
                if ask is None or size < 1.0:
                    complete = False
                    break
                legs.append((b, ask, size))
            budget -= len(buckets)
            checked += 1
            if not complete:
                self._next_check[ev["title"]] = now + cfg.ARB_RECHECK_NEAR_SEC
                continue

            cost = sum(a + ctx.risk.taker_fee(a) for _, a, _ in legs)
            if best_cost is None or cost < best_cost:
                best_cost = cost
            edge = 1.0 - cost
            if edge < cfg.ARB_MIN_EDGE:
                self._next_check[ev["title"]] = now + (
                    cfg.ARB_RECHECK_NEAR_SEC if cost < 1.05 else cfg.ARB_RECHECK_FAR_SEC
                )
                continue

            # --- FENÊTRE TROUVÉE : taille = patte la plus fine + plafonds ---
            self.stats["windows"] += 1
            min_sets = max(float(b.get("min_size") or 5) for b, _, _ in legs)
            sets = min(s for _, _, s in legs)                    # profondeur dispo
            sets = min(sets,
                       cfg.ARB_STAKE_MAX_USDC / cost,
                       (cfg.ARB_MAX_TOTAL_USDC - arb_open_cost) / cost,
                       (balance * 0.9) / cost)
            sets = float(int(sets))
            if sets < min_sets:
                self._next_check[ev["title"]] = now + cfg.ARB_RECHECK_NEAR_SEC
                continue

            # --- Exécution paper : toutes les pattes, coût effectif frais inclus ---
            for b, ask, _ in legs:
                q = f"{ARB_TAG} {ev['title']} {b['label']}"
                eff = ask + ctx.risk.taker_fee(ask)
                balance -= sets * eff
                db.update_balance(balance)
                ctx.accumulate_position(b["yes_token"], b["market_id"], q,
                                        b["yes_outcome"], sets, eff, b["end_date"])
                db.add_trade(b["market_id"], q, b["yes_token"], "BUY",
                             b["yes_outcome"], sets, ask)
                held_tokens.add(b["yes_token"])
            arb_open_cost += sets * cost
            self.stats["baskets"] += 1
            ctx.log(
                f"ARB PANIER {ev['title']} | somme asks+frais {cost:.3f} | "
                f"profit verrouillé {edge * 100:.1f}% × {sets:.0f} sets = "
                f"{edge * sets:+.2f}$ à la résolution",
                "SUCCESS",
            )

        if ctx.ui_state is not None:
            ctx.ui_state["arb"] = {
                "checked": checked,
                "best_cost": round(best_cost, 4) if best_cost is not None else None,
                "windows": self.stats["windows"],
                "baskets": self.stats["baskets"],
                "open_cost": round(arb_open_cost, 2),
                "ts": now,
            }
