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
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT token_id, market_id, question, outcome, shares, avg_price, current_price, end_date, payout_multiplier FROM positions")
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
    """Renseigne le résultat du dernier pari non réglé sur ce token."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM bet_log WHERE token_id = ? AND settled = 0 ORDER BY id DESC LIMIT 1",
            (token_id,),
        )
        row = cursor.fetchone()
        if not row:
            return
        cursor.execute(
            "UPDATE bet_log SET settled = 1, won = ?, pnl = ? WHERE id = ?",
            (1 if won else 0, pnl, row["id"]),
        )
        conn.commit()


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
