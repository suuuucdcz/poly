import sqlite3
import os
import json
from datetime import datetime

from backend import config

# DB_PATH (variable d'env) permet de pointer vers un volume persistant en hébergement.
DB_FILE = os.environ.get("DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "paper_trading.db"
)

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(default_budget=1000.0):
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Portfolio table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY DEFAULT 1,
            balance REAL NOT NULL,
            initial_budget REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        
        # Positions table (with end_date for market closing info)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            token_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            outcome TEXT NOT NULL,
            shares REAL NOT NULL,
            avg_price REAL NOT NULL,
            current_price REAL NOT NULL,
            end_date TEXT,
            payout_multiplier REAL DEFAULT 0.0
        )
        """)
        
        # Auto-migration: add end_date column if missing (for existing databases)
        try:
            cursor.execute("ALTER TABLE positions ADD COLUMN end_date TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
            
        # Auto-migration: add payout_multiplier column if missing
        try:
            cursor.execute("ALTER TABLE positions ADD COLUMN payout_multiplier REAL DEFAULT 0.0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Trades table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            token_id TEXT NOT NULL,
            action TEXT NOT NULL,
            outcome TEXT NOT NULL,
            shares REAL NOT NULL,
            price REAL NOT NULL,
            pnl REAL
        )
        """)
        
        # Equity history table for charting
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            balance REAL NOT NULL,
            portfolio_value REAL NOT NULL
        )
        """)

        # Bet log — features at entry + outcome, used by the learning/calibration layer
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS bet_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            kind TEXT,
            token_id TEXT NOT NULL,
            asset TEXT,
            window TEXT,
            side TEXT,
            is_up INTEGER,
            entry_price REAL,
            model_p REAL,
            edge REAL,
            delta_pct REAL,
            t_left INTEGER,
            sigma REAL,
            flow REAL,
            shares REAL,
            cost REAL,
            settled INTEGER DEFAULT 0,
            won INTEGER,
            pnl REAL
        )
        """)
        # Auto-migration : colonne 'kind' (crypto / weather) pour ne pas mélanger
        # les calibrations entre stratégies.
        try:
            cursor.execute("ALTER TABLE bet_log ADD COLUMN kind TEXT")
        except sqlite3.OperationalError:
            pass

        # Biais grille<->station officielle appris des marchés résolus (par ville)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS city_bias (
            city TEXT PRIMARY KEY,
            bias REAL NOT NULL,
            std REAL,
            n INTEGER,
            updated TEXT
        )
        """)
        
        # Check if portfolio already exists
        cursor.execute("SELECT COUNT(*) FROM portfolio")
        if cursor.fetchone()[0] == 0:
            now = datetime.utcnow().isoformat()
            cursor.execute(
                "INSERT INTO portfolio (id, balance, initial_budget, created_at) VALUES (1, ?, ?, ?)",
                (default_budget, default_budget, now)
            )
            # Add initial equity history point
            cursor.execute(
                "INSERT INTO equity_history (timestamp, balance, portfolio_value) VALUES (?, ?, ?)",
                (now, default_budget, default_budget)
            )
        conn.commit()

def reset_portfolio(budget):
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        
        # Clear positions and trades
        cursor.execute("DELETE FROM positions")
        cursor.execute("DELETE FROM trades")
        cursor.execute("DELETE FROM equity_history")
        # Efface aussi l'historique d'apprentissage (les 504 paris 0-gagnés de
        # l'ancien modèle) pour un VRAI départ propre du moteur convergence.
        try:
            cursor.execute("DELETE FROM bet_log")
        except sqlite3.OperationalError:
            pass

        # Reset portfolio settings
        cursor.execute(
            "INSERT OR REPLACE INTO portfolio (id, balance, initial_budget, created_at) VALUES (1, ?, ?, ?)",
            (budget, budget, now)
        )
        # Seed initial history point
        cursor.execute(
            "INSERT INTO equity_history (timestamp, balance, portfolio_value) VALUES (?, ?, ?)",
            (now, budget, budget)
        )
        conn.commit()

def get_portfolio():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance, initial_budget, created_at FROM portfolio WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            # Fallback if somehow missing
            return {"balance": 1000.0, "initial_budget": 1000.0, "created_at": datetime.utcnow().isoformat()}
        return {
            "balance": row["balance"],
            "initial_budget": row["initial_budget"],
            "created_at": row["created_at"]
        }

def update_balance(new_balance):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE portfolio SET balance = ? WHERE id = 1", (new_balance,))
        conn.commit()

def get_positions():
    """Positions ouvertes + date du PREMIER achat (permet de distinguer les paris
    de l'ancien modèle de ceux du modèle corrigé dans le dashboard)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.token_id, p.market_id, p.question, p.outcome, p.shares,
                   p.avg_price, p.current_price, p.end_date, p.payout_multiplier,
                   b.opened_at
            FROM positions p
            LEFT JOIN (
                SELECT token_id, MIN(timestamp) AS opened_at
                FROM trades WHERE action = 'BUY' GROUP BY token_id
            ) b ON b.token_id = p.token_id
        """)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_position(token_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT token_id, market_id, question, outcome, shares, avg_price, current_price, end_date, payout_multiplier FROM positions WHERE token_id = ?", (token_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def save_position(token_id, market_id, question, outcome, shares, avg_price, current_price, end_date=None, payout_multiplier=None):
    """Save or update a position. If end_date/payout_multiplier is None and position already exists, preserve old values."""
    with get_db() as conn:
        cursor = conn.cursor()
        if shares <= 0.0001:
            cursor.execute("DELETE FROM positions WHERE token_id = ?", (token_id,))
        else:
            # If end_date or payout_multiplier is not provided, try to preserve the existing ones
            if end_date is None or payout_multiplier is None:
                cursor.execute("SELECT end_date, payout_multiplier FROM positions WHERE token_id = ?", (token_id,))
                existing = cursor.fetchone()
                if existing:
                    if end_date is None:
                        end_date = existing["end_date"]
                    if payout_multiplier is None:
                        payout_multiplier = existing["payout_multiplier"]
            
            if payout_multiplier is None:
                payout_multiplier = 0.0
            
            cursor.execute("""
            INSERT OR REPLACE INTO positions (token_id, market_id, question, outcome, shares, avg_price, current_price, end_date, payout_multiplier)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (token_id, market_id, question, outcome, shares, avg_price, current_price, end_date, payout_multiplier))
        conn.commit()

def accumulate_position(token_id, market_id, question, outcome, new_shares, new_price, end_date=None):
    """Ajoute des parts à une position (prix moyen pondéré) ou la crée."""
    existing = get_position(token_id)
    if existing and existing["shares"] > 0.0001:
        old_shares = existing["shares"]
        old_avg = existing["avg_price"]
        total_shares = old_shares + new_shares
        weighted_avg = ((old_shares * old_avg) + (new_shares * new_price)) / total_shares
        if end_date is None:
            end_date = existing.get("end_date")
        save_position(token_id, market_id, question, outcome, total_shares, weighted_avg, new_price, end_date)
        return total_shares, weighted_avg
    save_position(token_id, market_id, question, outcome, new_shares, new_price, new_price, end_date)
    return new_shares, new_price


def update_position_price(token_id, new_price):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE positions SET current_price = ? WHERE token_id = ?", (new_price, token_id))
        conn.commit()

def add_trade(market_id, question, token_id, action, outcome, shares, price, pnl=None):
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
        INSERT INTO trades (timestamp, market_id, question, token_id, action, outcome, shares, price, pnl)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, market_id, question, token_id, action, outcome, shares, price, pnl))
        conn.commit()

def get_trades(limit=100):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, timestamp, market_id, question, token_id, action, outcome, shares, price, pnl FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_trade_totals():
    """Agrégats VIE ENTIÈRE sur la table trades (pas la fenêtre des 100 derniers) :
    la vérité comptable du P&L réalisé."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT
              COUNT(*)                                                        AS total_trades,
              SUM(CASE WHEN action IN ('SELL','RESOLVE') AND pnl IS NOT NULL THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN action IN ('SELL','RESOLVE') AND pnl > 0 THEN 1 ELSE 0 END)         AS wins,
              SUM(CASE WHEN action='SELL'    AND pnl IS NOT NULL THEN 1 ELSE 0 END)             AS sells,
              SUM(CASE WHEN action='RESOLVE' AND pnl IS NOT NULL THEN 1 ELSE 0 END)             AS resolves,
              SUM(CASE WHEN action='SELL'    THEN COALESCE(pnl,0) ELSE 0 END)                   AS pnl_sells,
              SUM(CASE WHEN action='RESOLVE' THEN COALESCE(pnl,0) ELSE 0 END)                   AS pnl_resolves
            FROM trades
        """).fetchone()
        return {
            "total_trades": row["total_trades"] or 0,
            "closed": row["closed"] or 0,
            "wins": row["wins"] or 0,
            "sells": row["sells"] or 0,
            "resolves": row["resolves"] or 0,
            "pnl_sells": round(row["pnl_sells"] or 0.0, 2),
            "pnl_resolves": round(row["pnl_resolves"] or 0.0, 2),
            "pnl_total": round((row["pnl_sells"] or 0.0) + (row["pnl_resolves"] or 0.0), 2),
        }


def record_equity_snapshot(portfolio_value):
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        portfolio = get_portfolio()
        cursor.execute("""
        INSERT INTO equity_history (timestamp, balance, portfolio_value)
        VALUES (?, ?, ?)
        """, (now, portfolio["balance"], portfolio_value))
        # Borne la table : ne conserve que les EQUITY_HISTORY_MAX_ROWS derniers points
        cursor.execute(
            """
            DELETE FROM equity_history
            WHERE id NOT IN (
                SELECT id FROM equity_history ORDER BY id DESC LIMIT ?
            )
            """,
            (config.EQUITY_HISTORY_MAX_ROWS,),
        )
        conn.commit()

def get_equity_history(limit=200):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, balance, portfolio_value FROM equity_history ORDER BY id ASC")
        rows = cursor.fetchall()
        # Sub-sample if we have too many history items to render cleanly
        data = [dict(row) for row in rows]
        if len(data) > limit:
            step = len(data) // limit
            data = data[::step]
        return data


# ============================================================
# BET LOG (apprentissage / calibration)
# ============================================================
def log_bet(token_id, asset, window, side, is_up, entry_price, model_p, edge,
            delta_pct, t_left, sigma, flow, shares, cost, kind=None):
    """Enregistre un pari à l'entrée (résultat rempli plus tard par settle_bet)."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()
        cursor.execute("""
        INSERT INTO bet_log (timestamp, kind, token_id, asset, window, side, is_up,
            entry_price, model_p, edge, delta_pct, t_left, sigma, flow, shares, cost,
            settled, won, pnl)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
        """, (now, kind, token_id, asset, window, side, 1 if is_up else 0, entry_price,
              model_p, edge, delta_pct, t_left, sigma, flow, shares, cost))
        conn.commit()


def settle_bet(token_id, won, pnl):
    """Renseigne le résultat du dernier pari non réglé sur ce token.

    `won=None` = sortie anticipée (résultat final inconnu) : le PnL est
    enregistré mais le pari est EXCLU de la calibration (won reste NULL).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM bet_log WHERE token_id = ? AND settled = 0 ORDER BY id DESC LIMIT 1",
            (token_id,),
        )
        row = cursor.fetchone()
        if not row:
            return
        won_val = None if won is None else (1 if won else 0)
        cursor.execute(
            "UPDATE bet_log SET settled = 1, won = ?, pnl = ? WHERE id = ?",
            (won_val, pnl, row["id"]),
        )
        conn.commit()


def set_city_bias(city, bias, std, n):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO city_bias (city, bias, std, n, updated) VALUES (?, ?, ?, ?, ?)",
            (city, bias, std, n, datetime.utcnow().isoformat()),
        )
        conn.commit()


def get_city_biases():
    """{ville: (bias, std, n)} — biais appris des marchés résolus."""
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT city, bias, std, n FROM city_bias").fetchall()
            return {r["city"]: (r["bias"], r["std"] or 0.0, r["n"] or 0) for r in rows}
    except sqlite3.OperationalError:
        return {}


def get_bet_samples(limit=3000, kind=None):
    """Échantillons (proba modèle brute, gagné) pour la calibration, filtrés par
    `kind` (crypto/weather) pour ne pas mélanger les stratégies."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            q = "SELECT model_p, won FROM bet_log WHERE settled = 1 AND model_p IS NOT NULL"
            params = []
            if kind:
                q += " AND kind = ?"
                params.append(kind)
            q += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            cursor.execute(q, params)
            return [(row["model_p"], row["won"]) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        return []


def get_bet_stats(kind=None):
    """Résumé du journal des paris (optionnellement filtré par `kind`)."""
    default = {"total": 0, "settled": 0, "wins": 0, "pnl": 0.0}
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            where = "WHERE kind = ?" if kind else ""
            params = (kind,) if kind else ()
            cursor.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN settled = 1 THEN 1 ELSE 0 END) AS settled,
                       SUM(CASE WHEN settled = 1 AND won = 1 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN settled = 1 THEN COALESCE(pnl, 0) ELSE 0 END) AS pnl
                FROM bet_log {where}
            """, params)
            row = cursor.fetchone()
            return {
                "total": row["total"] or 0,
                "settled": row["settled"] or 0,
                "wins": row["wins"] or 0,
                "pnl": row["pnl"] or 0.0,
            }
    except sqlite3.OperationalError:
        return default
