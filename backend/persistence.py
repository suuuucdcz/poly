"""Persistance optionnelle de la base via **Supabase Storage** (snapshots SQLite).

Sur un hôte à disque éphémère (Render free), `paper_trading.db` — qui contient le
portefeuille ET le journal d'apprentissage `bet_log` — est perdu aux redéploiements
et réveils. Ce module, si les variables d'env Supabase sont fournies :

  - **au démarrage** : télécharge le dernier snapshot et restaure la base ;
  - **périodiquement + à l'arrêt** : envoie un snapshot *cohérent* (via l'API
    `backup` de sqlite3, pas une copie brute) vers Supabase Storage.

Stdlib uniquement (urllib). Sans variables d'env → **no-op** : le dev local est
inchangé. On garde ainsi le moteur SQLite éprouvé tout en gagnant la persistance.

Variables d'env attendues :
    SUPABASE_URL        ex: https://xxxx.supabase.co
    SUPABASE_KEY        clé service_role (Settings → API)
    SUPABASE_BUCKET     défaut: polyquant   (bucket Storage à créer, privé)
    SUPABASE_DB_OBJECT  défaut: paper_trading.db
"""

import os
import sqlite3
import tempfile
import urllib.error
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "polyquant")
SUPABASE_OBJECT = os.environ.get("SUPABASE_DB_OBJECT", "paper_trading.db")


def enabled():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _object_url():
    return f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{SUPABASE_OBJECT}"


def _headers(extra=None):
    h = {"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY}
    if extra:
        h.update(extra)
    return h


def restore_db(db_path):
    """Télécharge le dernier snapshot vers `db_path`. Retourne True si restauré."""
    if not enabled():
        return False
    try:
        req = urllib.request.Request(_object_url(), headers=_headers())
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        if data:
            with open(db_path, "wb") as f:
                f.write(data)
            print(f"[persistence] base restaurée depuis Supabase ({len(data)} octets)")
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[persistence] aucun snapshot distant (premier démarrage)")
        else:
            print(f"[persistence] restore HTTPError {e.code}")
    except Exception as e:
        print(f"[persistence] restore error: {e}")
    return False


def snapshot_and_upload(db_path):
    """Snapshot cohérent (API backup) puis upload (upsert). Retourne True si OK."""
    if not enabled() or not os.path.exists(db_path):
        return False
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)   # snapshot cohérent même pendant les écritures
        src.close()
        dst.close()

        with open(tmp, "rb") as f:
            body = f.read()
        req = urllib.request.Request(
            _object_url(),
            data=body,
            method="POST",
            headers=_headers({
                "Content-Type": "application/octet-stream",
                "x-upsert": "true",
            }),
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except Exception as e:
        print(f"[persistence] snapshot error: {e}")
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
