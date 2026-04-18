import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH


DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL UNIQUE,
    ticket INTEGER,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    session TEXT NOT NULL,
    entry_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    lots REAL NOT NULL,
    opened_at_utc TEXT NOT NULL,
    closed_at_utc TEXT,
    close_price REAL,
    pnl_usd REAL,
    result TEXT,
    close_reason TEXT,
    mae_r REAL DEFAULT 0,
    mfe_r REAL DEFAULT 0
);
"""


class TradeStore:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(DDL)

    def log_open(self, record: dict):
        columns = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO trades ({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )

    def log_close(self, trade_id: str, close_price: float, pnl_usd: float, close_reason: str, mae_r: float, mfe_r: float):
        result = "WIN" if pnl_usd > 0 else "LOSS" if pnl_usd < 0 else "FLAT"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trades
                SET closed_at_utc = ?, close_price = ?, pnl_usd = ?, result = ?,
                    close_reason = ?, mae_r = ?, mfe_r = ?
                WHERE trade_id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    close_price,
                    pnl_usd,
                    result,
                    close_reason,
                    mae_r,
                    mfe_r,
                    trade_id,
                ),
            )

    def open_trade_rows(self):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM trades WHERE closed_at_utc IS NULL ORDER BY opened_at_utc").fetchall()]

    def recent_trades(self, limit: int = 30):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY opened_at_utc DESC LIMIT ?", (limit,)).fetchall()]

    def summary(self):
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) AS wins,
                    SUM(COALESCE(pnl_usd, 0)) AS total_pnl
                FROM trades
                WHERE closed_at_utc IS NOT NULL
                """
            ).fetchone()
        total = int(row["total"] or 0)
        wins = int(row["wins"] or 0)
        pnl = float(row["total_pnl"] or 0.0)
        return {
            "total": total,
            "wins": wins,
            "losses": max(total - wins, 0),
            "win_rate": (wins / total) if total else 0.0,
            "total_pnl": pnl,
        }

