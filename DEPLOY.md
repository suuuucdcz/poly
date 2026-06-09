# Héberger PolyQuant (paper trading)

Ce bot doit tourner **24/7** *et* **garder sa base** (`paper_trading.db` contient le
portefeuille **et** le journal d'apprentissage `bet_log`). Deux contraintes en découlent :

1. **Pas de mise en veille** : si l'hôte endort le service, le bot arrête de trader/apprendre.
2. **Disque persistant** : si le disque est effacé aux redéploiements, on **perd l'apprentissage**.
3. **Accès Binance** : `api.binance.com` est souvent **géo-bloqué depuis les IP cloud (US)**.
   Depuis ta machine ou une VM bien placée, pas de souci.

> ⚙️ La base est configurable via la variable d'env **`DB_PATH`** (sinon `backend/paper_trading.db`).

---

## Option A — Ta machine / un Raspberry Pi  (le plus simple, recommandé)

Gratuit, persistant, et Binance marche (IP résidentielle). Seul inconvénient : la machine doit rester allumée.

```bash
pip install -r requirements.txt
python run.py            # ouvre le dashboard sur http://127.0.0.1:8000
# ou sans navigateur :  uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Pour le laisser tourner en fond : un Raspberry Pi sous la main, ou `tmux` / un service systemd.

---

## Option B — VM cloud gratuite toujours allumée  (Oracle Cloud Free Tier)

La meilleure option *cloud gratuite* : VM **Always Free**, jamais en veille, disque persistant,
et tu choisis la **région** (en prends une où Binance n'est pas bloqué).

1. Crée un compte Oracle Cloud → une instance **Always Free** (Ampere ARM ou AMD micro).
2. Ouvre le port 8000 (security list + `iptables`/firewall de la VM).
3. Sur la VM :
   ```bash
   sudo apt update && sudo apt install -y python3-pip git
   git clone <ton-repo> polyquant && cd polyquant
   pip install -r requirements.txt
   # lancer en service systemd ou tmux :
   tmux new -s bot 'uvicorn backend.main:app --host 0.0.0.0 --port 8000'
   ```
4. Accède à `http://<IP-de-la-VM>:8000`.

---

## Option C — Render (gratuit) + Supabase Storage  ✅ persistance branchée

Le plus facile à brancher. Le plan gratuit s'endort et a un disque éphémère, mais on
règle les deux : **pinger externe** contre la veille, et **snapshots de la base vers
Supabase Storage** pour la persistance (déjà implémenté, cf. `backend/persistence.py`).
On évite le géo-blocage Binance en déployant en **région Frankfurt (EU)**.

1. **Supabase** : crée un projet (gratuit) → *Storage* → nouveau bucket **privé** `polyquant`.
   Dans *Settings → API*, note l'**URL du projet** et la clé **`service_role`**.
2. **GitHub** : pousse le projet (`git push`).
3. **Render** → *New* → *Blueprint* (lit `render.yaml`, région **frankfurt** déjà fixée).
   Dans *Environment*, renseigne les secrets :
   - `SUPABASE_URL` = `https://xxxx.supabase.co`
   - `SUPABASE_KEY` = (clé `service_role`)
   - `SUPABASE_BUCKET` = `polyquant`
4. **Anti-veille** : moniteur **UptimeRobot** (gratuit) en HTTP(s) sur
   `https://<ton-app>.onrender.com/api/bot/status`, toutes les 5 min.

Au démarrage l'app **restaure** la base depuis Supabase, puis **renvoie un snapshot
toutes les 2 min** (et à l'arrêt) → l'apprentissage survit aux redéploiements/réveils.

> ⚠️ Je n'ai pas pu brancher un vrai Supabase depuis l'environnement de dev : le code
> est écrit et la logique testée, mais **le premier run réel, c'est toi** qui le valides.
> Si Binance reste bloqué malgré Frankfurt, il reste à brancher un repli Coinbase/Kraken
> (endpoints déjà vérifiés joignables).

---

## Docker (Fly.io, Railway, n'importe quel hôte conteneur)

```bash
docker build -t polyquant .
docker run -p 8000:8000 -e DB_PATH=/data/paper_trading.db -v polyquant_data:/data polyquant
```
Le volume `polyquant_data` garde la base entre les redémarrages.

---

### Résumé honnête
- **Pour vraiment laisser apprendre le bot** : Option A (ta machine/Pi) ou B (Oracle free VM) — persistance + Binance OK.
- **Render gratuit** : pratique pour une démo, mais sans persistance ni Binance fiable, l'apprentissage repart de zéro.
- Restent à coder pour un cloud US robuste : **base externe (Postgres)** + **repli de prix (Coinbase/Kraken)**.
