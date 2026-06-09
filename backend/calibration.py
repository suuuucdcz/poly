"""Calibration des probabilités — apprentissage léger et robuste petit-échantillon.

Le modèle `P(up) = Φ(…)` produit une proba *théorique*. La calibration apprend,
à partir des résultats réels passés, à corriger cette proba pour qu'elle colle à
la fréquence de gain observée (ex. si les paris annoncés « 90 % » ne gagnent qu'à
85 %, on apprend à annoncer 85 %).

Méthode : **bins de fiabilité avec lissage bayésien (Beta)**. Chaque bin de proba
est rapproché (shrink) de la proba modèle tant qu'il y a peu de données, et se
laisse déplacer par l'évidence quand les données s'accumulent. Conséquences :
  - sans données -> identité (comportement = modèle brut) ;
  - peu de données -> correction prudente ;
  - beaucoup de données -> la fréquence empirique domine.

Aucune dépendance externe (pas de numpy/scikit-learn) : robuste et interprétable.
"""


class Calibrator:
    def __init__(self, n_bins=10, prior_strength=25.0, min_samples=60, min_losses=10):
        self.n_bins = max(2, int(n_bins))
        self.prior_strength = float(prior_strength)
        self.min_samples = int(min_samples)
        self.min_losses = int(min_losses)
        self.active = False
        self.n_samples = 0
        self.bins = []  # par bin: {"n", "wins", "mean_p"}

    def fit(self, samples):
        """samples : liste de (p_raw, y) avec p_raw ∈ [0,1] et y ∈ {0,1}."""
        clean = [(p, 1 if y else 0) for (p, y) in samples
                 if p is not None and 0.0 <= p <= 1.0 and y is not None]
        self.n_samples = len(clean)
        wins = sum(y for _, y in clean)
        losses = self.n_samples - wins

        # Pas assez de données (ou pas assez de défaites) -> on reste en identité.
        if self.n_samples < self.min_samples or losses < self.min_losses:
            self.active = False
            return False

        bins = [{"n": 0, "wins": 0, "sum_p": 0.0} for _ in range(self.n_bins)]
        for p, y in clean:
            idx = min(self.n_bins - 1, max(0, int(p * self.n_bins)))
            b = bins[idx]
            b["n"] += 1
            b["wins"] += y
            b["sum_p"] += p
        for b in bins:
            b["mean_p"] = (b["sum_p"] / b["n"]) if b["n"] else None
        self.bins = bins
        self.active = True
        return True

    def predict(self, p):
        """Renvoie la proba calibrée (ou p inchangé si inactif / bin vide)."""
        if not self.active or p is None:
            return p
        idx = min(self.n_bins - 1, max(0, int(p * self.n_bins)))
        b = self.bins[idx]
        if not b["n"]:
            return p  # aucun historique dans ce bin -> identité locale
        center = b["mean_p"] if b["mean_p"] is not None else p
        # Moyenne lissée : (victoires + prior·centre) / (n + prior)
        rate = (b["wins"] + self.prior_strength * center) / (b["n"] + self.prior_strength)
        return min(0.99, max(0.01, rate))

    def status(self):
        return {"active": self.active, "samples": self.n_samples}
