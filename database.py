"""
Couche base de données SQLite — création des tables et opérations CRUD.
"""
import sqlite3
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import config

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Crée les tables si elles n'existent pas."""
    with db_cursor() as cur:
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address   TEXT    NOT NULL,
            token_symbol    TEXT    NOT NULL,
            token_name      TEXT    NOT NULL,
            chain           TEXT    NOT NULL,
            pair_address    TEXT,
            entry_price     REAL    NOT NULL,
            exit_price      REAL,
            position_size   REAL    NOT NULL,
            position_usd    REAL    NOT NULL,
            ai_score        INTEGER NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'open',
            open_at         TEXT    NOT NULL,
            close_at        TEXT,
            close_reason    TEXT,
            pnl_usd         REAL,
            pnl_pct         REAL,
            success         INTEGER,
            token_data      TEXT,
            notes           TEXT,
            tp1_hit         INTEGER DEFAULT 0,
            tp1_pnl_pct     REAL,
            tp1_at          TEXT
        );

        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address   TEXT    NOT NULL,
            token_symbol    TEXT    NOT NULL,
            chain           TEXT    NOT NULL,
            ai_score        INTEGER NOT NULL,
            price_usd       REAL    NOT NULL,
            signal_at       TEXT    NOT NULL,
            acted           INTEGER DEFAULT 0,
            reason          TEXT
        );

        CREATE TABLE IF NOT EXISTS model_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at          TEXT    NOT NULL,
            samples         INTEGER,
            accuracy        REAL,
            precision       REAL,
            status          TEXT,
            notes           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_token ON trades(token_address);
        CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(ai_score);
        """)

        # Migration : ajouter les colonnes TP1 si la table existait déjà sans elles
        cur.execute("PRAGMA table_info(trades)")
        existing_cols = {row["name"] for row in cur.fetchall()}
        if "tp1_hit" not in existing_cols:
            cur.execute("ALTER TABLE trades ADD COLUMN tp1_hit INTEGER DEFAULT 0")
            logger.info("Migration: colonne tp1_hit ajoutée")
        if "tp1_pnl_pct" not in existing_cols:
            cur.execute("ALTER TABLE trades ADD COLUMN tp1_pnl_pct REAL")
            logger.info("Migration: colonne tp1_pnl_pct ajoutée")
        if "tp1_at" not in existing_cols:
            cur.execute("ALTER TABLE trades ADD COLUMN tp1_at TEXT")
            logger.info("Migration: colonne tp1_at ajoutée")

    logger.info("Base de données initialisée: %s", config.DB_PATH)


# ── Trades ────────────────────────────────────────────────────────────────────

def open_trade(token: dict, entry_price: float, position_usd: float, ai_score: int) -> int:
    """Ouvre un trade et retourne son ID."""
    position_size = position_usd / entry_price if entry_price > 0 else 0
    import json
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO trades
            (token_address, token_symbol, token_name, chain, pair_address,
             entry_price, position_size, position_usd, ai_score, status, open_at, token_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """, (
            token["address"], token["symbol"], token["name"], token["chain"],
            token.get("pair_address", ""), entry_price, position_size, position_usd,
            ai_score, datetime.now(timezone.utc).isoformat(), json.dumps(token),
        ))
        return cur.lastrowid


def mark_tp1_hit(trade_id: int, pnl_pct: float):
    """
    Enregistre qu'un trade a touché TP1 (sortie partielle) sans le fermer.
    Le trade reste 'open' pour le reste de la position, mais on garde une trace
    du signal positif pour l'entraînement ML.
    """
    with db_cursor() as cur:
        cur.execute("""
            UPDATE trades SET
                tp1_hit = 1, tp1_pnl_pct = ?, tp1_at = ?
            WHERE id = ?
        """, (round(pnl_pct, 2), datetime.now(timezone.utc).isoformat(), trade_id))


def close_trade(trade_id: int, exit_price: float, reason: str) -> dict:
    """Ferme un trade et calcule le PnL."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        trade = dict(cur.fetchone())

        entry = trade["entry_price"]
        size = trade["position_size"]
        pnl_usd = (exit_price - entry) * size
        pnl_pct = ((exit_price - entry) / entry) * 100 if entry > 0 else 0
        success = 1 if pnl_usd > 0 else 0

        cur.execute("""
            UPDATE trades SET
                exit_price=?, close_at=?, close_reason=?,
                pnl_usd=?, pnl_pct=?, status='closed', success=?
            WHERE id=?
        """, (
            exit_price, datetime.now(timezone.utc).isoformat(),
            reason, round(pnl_usd, 4), round(pnl_pct, 2), success, trade_id,
        ))
        return {**trade, "exit_price": exit_price, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct}


def get_open_trades() -> list[dict]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY open_at")
        return [dict(r) for r in cur.fetchall()]


def get_closed_trades(limit: int = 100) -> list[dict]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM trades WHERE status = 'closed' ORDER BY close_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_all_closed_trades() -> list[dict]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM trades WHERE status = 'closed' ORDER BY close_at")
        return [dict(r) for r in cur.fetchall()]


def get_trade_by_address(address: str) -> list[dict]:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM trades WHERE token_address = ? AND status = 'open'", (address,))
        return [dict(r) for r in cur.fetchall()]


# ── Signals ───────────────────────────────────────────────────────────────────

def record_signal(token: dict, ai_score: int, acted: bool = False, reason: str = "") -> int:
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO signals (token_address, token_symbol, chain, ai_score, price_usd, signal_at, acted, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            token["address"], token["symbol"], token["chain"], ai_score,
            token.get("price_usd", 0), datetime.now(timezone.utc).isoformat(),
            1 if acted else 0, reason,
        ))
        return cur.lastrowid


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_portfolio_stats() -> dict:
    """Statistiques globales du portfolio."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl_usd) as total_pnl_usd,
                AVG(pnl_pct) as avg_pnl_pct,
                MAX(pnl_pct) as best_trade_pct,
                MIN(pnl_pct) as worst_trade_pct
            FROM trades WHERE status = 'closed'
        """)
        row = dict(cur.fetchone())

        cur.execute("SELECT COUNT(*) as open FROM trades WHERE status = 'open'")
        open_row = dict(cur.fetchone())

    total = row["total_trades"] or 0
    wins = row["wins"] or 0
    win_rate = (wins / total * 100) if total > 0 else 0

    return {
        **row,
        "open_positions": open_row["open"],
        "win_rate": round(win_rate, 1),
        "total_pnl_usd": round(row["total_pnl_usd"] or 0, 2),
        "avg_pnl_pct": round(row["avg_pnl_pct"] or 0, 2),
    }


# ── Model runs ────────────────────────────────────────────────────────────────

def record_model_run(samples: int, accuracy: float, precision: float, status: str, notes: str = ""):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO model_runs (run_at, samples, accuracy, precision, status, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), samples, accuracy, precision, status, notes))
