"""
ARCS-FX -- learning/trade_logger.py
SQLite trade DNA logging system.

WHY THIS MODULE IS THE FOUNDATION OF SELF-LEARNING:
Every trade the bot takes is a data point. Without structured logging,
the bot is blind to its own patterns -- it cannot know which setups work,
which regimes to avoid, or how its confidence scoring correlates with outcomes.

The "DNA" metaphor is intentional:
  Each trade record encodes the FULL CONTEXT that led to the trade --
  the regime, the signal type, the pattern, the session, the confidence
  score breakdown. When the Phase 5 learning module analyses performance,
  it can answer questions like:
    "What is my win rate on OB_RETEST in TRENDING regime during NY session?"
    "Does a confidence score above 85 predict a better R:R outcome?"
    "Which pairs are systematically underperforming?"

SCHEMA:
  trades table -- one row per trade, open + close in the same row.
    Open fields are written at entry. Close fields are written at exit.
    This means each row is initially incomplete (close_* fields NULL)
    and updated when the trade resolves.

  confidence_components table -- the 7-component breakdown for each trade.
    Stored separately to keep trades table clean and allow component-level
    performance analysis.

ALL WRITES ARE TRANSACTIONAL.
If a write fails mid-update, the database stays consistent.
SQLite's WAL journal mode is used for concurrent read safety during
future analytics queries (Phase 5).
"""

import os
import sys
import sqlite3
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_PATH

logger = logging.getLogger(__name__)

# Absolute path resolution regardless of working directory
_DB_ABS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    DB_PATH,
)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
# WHY we use TEXT for prices instead of REAL:
# Floating-point representation of forex prices (e.g. 1.08500) can lose
# precision in SQLite REAL columns. TEXT preserves the exact string.
# Analytics queries cast TEXT to REAL at query time.
# ---------------------------------------------------------------------------

_DDL_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    -- Identity
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT    NOT NULL UNIQUE,   -- ARCS_{symbol}_{ticket}
    mt5_ticket          INTEGER,
    symbol              TEXT    NOT NULL,
    direction           TEXT    NOT NULL,           -- BUY | SELL

    -- Entry (written at open)
    entry_price         TEXT,
    sl_price            TEXT,
    initial_sl_price    TEXT,
    tp_price            TEXT,
    lots                REAL,
    opened_at_utc       TEXT,

    -- Signal context (written at open)
    signal_type         TEXT,    -- OB_RETEST | SD_BOUNCE | FVG_FILL
    pattern             TEXT,    -- BULLISH_ENGULFING etc.
    r_ratio             REAL,
    confidence          REAL,
    regime              TEXT,    -- TRENDING | RANGING
    session             TEXT,    -- LONDON | NY | OVERLAP | ASIAN

    -- Full DNA fields (spec requirement: trade DNA tagging)
    spread_at_entry     REAL,    -- real-time spread in pips at entry
    day_of_week         INTEGER, -- 0=Monday .. 6=Sunday
    news_score_at_entry REAL,    -- news sentiment score (-1.0 to +1.0) at entry

    -- Risk context (written at open)
    risk_pct            REAL,
    risk_usd            REAL,
    win_rate_at_open    REAL,
    daily_loss_at_open  REAL,

    -- Exit (written at close -- initially NULL)
    close_price         TEXT,
    pnl_usd             REAL,
    won                 INTEGER,   -- 1 = win, 0 = loss
    close_reason        TEXT,      -- SL_HIT | TP_HIT | TRAIL_HIT | REGIME_CHANGE | MANUAL
    closed_at_utc       TEXT,
    duration_minutes    REAL,

    -- Trade management outcome
    breakeven_applied   INTEGER,   -- 1 | 0
    trailing_active     INTEGER,   -- 1 | 0
    max_adverse_exc_r   REAL,      -- max drawdown in R units (MAE)
    max_favourable_exc_r REAL,     -- max profit in R units (MFE)

    -- Metadata
    created_at          TEXT DEFAULT (datetime('now'))
);
"""

_DDL_CONFIDENCE_COMPONENTS = """
CREATE TABLE IF NOT EXISTS confidence_components (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT    NOT NULL REFERENCES trades(trade_id),
    component       TEXT    NOT NULL,   -- regime_clarity, price_action, etc.
    score           REAL    NOT NULL,
    max_score       REAL    NOT NULL,
    pct             REAL    NOT NULL    -- score/max_score * 100
);
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol       ON trades(symbol);",
    "CREATE INDEX IF NOT EXISTS idx_trades_regime       ON trades(regime);",
    "CREATE INDEX IF NOT EXISTS idx_trades_signal_type  ON trades(signal_type);",
    "CREATE INDEX IF NOT EXISTS idx_trades_session      ON trades(session);",
    "CREATE INDEX IF NOT EXISTS idx_trades_won          ON trades(won);",
    "CREATE INDEX IF NOT EXISTS idx_trades_opened_at    ON trades(opened_at_utc);",
    "CREATE INDEX IF NOT EXISTS idx_conf_trade_id       ON confidence_components(trade_id);",
]


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class TradeOpenRecord:
    """
    Everything known at the moment a trade is opened.
    Passed to TradeLogger.log_open().
    """
    trade_id:           str
    mt5_ticket:         int
    symbol:             str
    direction:          str
    entry_price:        float
    sl_price:           float
    initial_sl_price:   float
    tp_price:           float
    lots:               float
    opened_at_utc:      str
    signal_type:        str
    pattern:            str
    r_ratio:            float
    confidence:         float
    regime:             str
    session:            str
    spread_at_entry:    float = 0.0   # pips at entry (DNA spec requirement)
    day_of_week:        int   = 0     # 0=Mon..6=Sun (DNA spec requirement)
    news_score_at_entry: float = 0.0  # -1.0 to +1.0 (DNA spec requirement)
    risk_pct:           float = 0.0
    risk_usd:           float = 0.0
    win_rate_at_open:   float = 0.5
    daily_loss_at_open: float = 0.0
    confidence_components: Optional[list[dict]] = None   # [{name, score, max_score}]


@dataclass
class TradeCloseRecord:
    """Everything known when a trade is closed. Passed to TradeLogger.log_close()."""
    trade_id:            str
    close_price:         float
    pnl_usd:             float
    won:                 bool
    close_reason:        str
    closed_at_utc:       str
    breakeven_applied:   bool
    trailing_active:     bool
    max_adverse_exc_r:   float = 0.0
    max_favourable_exc_r: float = 0.0


# ===========================================================================
# TradeLogger
# ===========================================================================

class TradeLogger:
    """
    SQLite-backed trade DNA logger.

    USAGE:
        tl = TradeLogger()

        # At trade open:
        tl.log_open(TradeOpenRecord(...))

        # At trade close:
        tl.log_close(
            trade_id="ARCS_EURUSD_12345",
            close_price=1.09100,
            pnl_usd=45.0,
            won=True,
            close_reason="TRAIL_HIT",
            breakeven_applied=True,
            trailing_active=True,
        )

        # For analytics:
        stats = tl.get_performance_summary()
        by_signal = tl.get_performance_by_dimension("signal_type")
    """

    def __init__(self, db_path: str = _DB_ABS) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._db_path = db_path
        self._init_db()
        logger.info("[LOG] TradeLogger initialised. DB: %s", db_path)

    # -----------------------------------------------------------------------
    # Initialisation
    # -----------------------------------------------------------------------

    def _init_db(self) -> None:
        """
        Create tables and indexes if they don't exist.
        Also runs a migration to add DNA columns to existing databases
        that were created before the spread/day_of_week/news_score fields
        were added. SQLite does not support IF NOT EXISTS on ALTER TABLE,
        so we catch the OperationalError silently.
        """
        with self._connect() as conn:
            conn.execute(_DDL_TRADES)
            conn.execute(_DDL_CONFIDENCE_COMPONENTS)
            for idx_sql in _DDL_INDEXES:
                conn.execute(idx_sql)
            # Migration: add DNA columns to pre-existing databases
            for col_sql in (
                "ALTER TABLE trades ADD COLUMN spread_at_entry     REAL DEFAULT 0.0",
                "ALTER TABLE trades ADD COLUMN day_of_week         INTEGER DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN news_score_at_entry REAL DEFAULT 0.0",
            ):
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass   # column already exists -- safe to ignore
        logger.debug("[LOG] Database schema verified (including DNA migration).")

    def _connect(self) -> sqlite3.Connection:
        """
        Open a WAL-mode connection.

        WHY WAL (Write-Ahead Logging):
        WAL allows concurrent readers while a write is in progress.
        Phase 5 analytics scripts can query the live database while the
        bot is trading without blocking each other.
        """
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        return conn

    # -----------------------------------------------------------------------
    # Write API
    # -----------------------------------------------------------------------

    def log_open(self, record: "TradeOpenRecord") -> None:
        """
        Insert a new trade row (open fields only). Close fields are NULL.

        Called by OrderManager immediately after MT5 order confirmation.
        The close fields are populated later by log_close().

        WHY we log at open, not just at close:
        If the bot crashes between open and close, we still have a record
        of the open trade. The recovery path can detect orphaned trades
        (opened_at_utc set, closed_at_utc NULL) and handle them gracefully.
        """
        sql = """
            INSERT OR IGNORE INTO trades (
                trade_id, mt5_ticket, symbol, direction,
                entry_price, sl_price, initial_sl_price, tp_price, lots, opened_at_utc,
                signal_type, pattern, r_ratio, confidence, regime, session,
                spread_at_entry, day_of_week, news_score_at_entry,
                risk_pct, risk_usd, win_rate_at_open, daily_loss_at_open
            ) VALUES (
                :trade_id, :mt5_ticket, :symbol, :direction,
                :entry_price, :sl_price, :initial_sl_price, :tp_price, :lots, :opened_at_utc,
                :signal_type, :pattern, :r_ratio, :confidence, :regime, :session,
                :spread_at_entry, :day_of_week, :news_score_at_entry,
                :risk_pct, :risk_usd, :win_rate_at_open, :daily_loss_at_open
            )
        """

        params = {
            "trade_id":           record.trade_id,
            "mt5_ticket":         record.mt5_ticket,
            "symbol":             record.symbol,
            "direction":          record.direction,
            "entry_price":        str(record.entry_price),
            "sl_price":           str(record.sl_price),
            "initial_sl_price":   str(record.initial_sl_price),
            "tp_price":           str(record.tp_price),
            "lots":               record.lots,
            "opened_at_utc":      record.opened_at_utc,
            "signal_type":        record.signal_type,
            "pattern":            record.pattern,
            "r_ratio":            record.r_ratio,
            "confidence":         record.confidence,
            "regime":               record.regime,
            "session":              record.session,
            "spread_at_entry":      record.spread_at_entry,
            "day_of_week":          record.day_of_week,
            "news_score_at_entry":  record.news_score_at_entry,
            "risk_pct":             record.risk_pct,
            "risk_usd":             record.risk_usd,
            "win_rate_at_open":     record.win_rate_at_open,
            "daily_loss_at_open":   record.daily_loss_at_open,
        }

        try:
            with self._connect() as conn:
                conn.execute(sql, params)

                # Write confidence components if provided
                if record.confidence_components:
                    self._insert_components(conn, record.trade_id, record.confidence_components)

            logger.info(
                "[LOG] Trade open logged: %s %s %s | confidence=%.1f | regime=%s",
                record.trade_id, record.symbol, record.direction,
                record.confidence, record.regime,
            )

        except sqlite3.Error as exc:
            logger.error("[LOG] Failed to log trade open for %s: %s", record.trade_id, exc)

    def log_close(
        self,
        trade_id:            str,
        close_price:         float,
        pnl_usd:             float,
        won:                 bool,
        close_reason:        str,
        breakeven_applied:   bool = False,
        trailing_active:     bool = False,
        max_adverse_exc_r:   float = 0.0,
        max_favourable_exc_r: float = 0.0,
    ) -> None:
        """
        Update the trade row with close information.

        Also computes duration_minutes from the stored opened_at_utc.

        Called by OrderManager._on_trade_closed().
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # Compute duration
        duration_minutes = self._compute_duration(trade_id, now_utc)

        sql = """
            UPDATE trades SET
                close_price          = :close_price,
                pnl_usd              = :pnl_usd,
                won                  = :won,
                close_reason         = :close_reason,
                closed_at_utc        = :closed_at_utc,
                duration_minutes     = :duration_minutes,
                breakeven_applied    = :breakeven_applied,
                trailing_active      = :trailing_active,
                max_adverse_exc_r    = :max_adverse_exc_r,
                max_favourable_exc_r = :max_favourable_exc_r
            WHERE trade_id = :trade_id
        """

        params = {
            "trade_id":             trade_id,
            "close_price":          str(close_price),
            "pnl_usd":              pnl_usd,
            "won":                  1 if won else 0,
            "close_reason":         close_reason,
            "closed_at_utc":        now_utc,
            "duration_minutes":     duration_minutes,
            "breakeven_applied":    1 if breakeven_applied else 0,
            "trailing_active":      1 if trailing_active else 0,
            "max_adverse_exc_r":    max_adverse_exc_r,
            "max_favourable_exc_r": max_favourable_exc_r,
        }

        try:
            with self._connect() as conn:
                cursor = conn.execute(sql, params)
                if cursor.rowcount == 0:
                    logger.warning("[LOG] log_close: trade_id %s not found in DB.", trade_id)
                else:
                    logger.info(
                        "[LOG] Trade close logged: %s | won=%s | pnl=$%.2f | "
                        "reason=%s | duration=%.0fmin",
                        trade_id, won, pnl_usd, close_reason, duration_minutes or 0,
                    )

        except sqlite3.Error as exc:
            logger.error("[LOG] Failed to log trade close for %s: %s", trade_id, exc)

    def _insert_components(
        self,
        conn:       sqlite3.Connection,
        trade_id:   str,
        components: list[dict],
    ) -> None:
        """
        Insert confidence component breakdown rows.
        Each component: {"name": str, "score": float, "max_score": float}
        """
        sql = """
            INSERT INTO confidence_components (trade_id, component, score, max_score, pct)
            VALUES (:trade_id, :component, :score, :max_score, :pct)
        """
        rows = []
        for c in components:
            max_score = c.get("max_score", 1.0)
            pct = (c["score"] / max_score * 100.0) if max_score > 0 else 0.0
            rows.append({
                "trade_id":  trade_id,
                "component": c["name"],
                "score":     c["score"],
                "max_score": max_score,
                "pct":       pct,
            })
        conn.executemany(sql, rows)

    # -----------------------------------------------------------------------
    # Read / analytics API
    # -----------------------------------------------------------------------

    def get_performance_summary(self) -> dict:
        """
        Overall performance statistics across all closed trades.

        Returns a dict with: total, wins, losses, win_rate, total_pnl,
        avg_win, avg_loss, profit_factor, avg_duration_min, avg_r_ratio.

        WHY profit_factor:
        Profit factor (gross wins / gross losses) is the most reliable
        single metric for evaluating a strategy's edge. > 1.0 means profitable.
        A win rate of 40% with PF=1.5 is better than 60% win rate with PF=0.8.
        """
        sql = """
            SELECT
                COUNT(*)                                    AS total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)     AS wins,
                SUM(CASE WHEN won=0 THEN 1 ELSE 0 END)     AS losses,
                SUM(pnl_usd)                                AS total_pnl,
                AVG(CASE WHEN won=1 THEN pnl_usd END)       AS avg_win,
                AVG(CASE WHEN won=0 THEN pnl_usd END)       AS avg_loss,
                SUM(CASE WHEN won=1 THEN pnl_usd ELSE 0 END) AS gross_wins,
                SUM(CASE WHEN won=0 THEN ABS(pnl_usd) ELSE 0 END) AS gross_losses,
                AVG(duration_minutes)                       AS avg_duration_min,
                AVG(r_ratio)                                AS avg_r_ratio
            FROM trades
            WHERE closed_at_utc IS NOT NULL
        """
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()

        if row is None or row["total"] == 0:
            return {"total": 0, "message": "No closed trades yet."}

        gross_wins   = row["gross_wins"]   or 0.0
        gross_losses = row["gross_losses"] or 0.0
        pf           = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")
        win_rate     = (row["wins"] / row["total"] * 100.0) if row["total"] > 0 else 0.0

        return {
            "total":            row["total"],
            "wins":             row["wins"],
            "losses":           row["losses"],
            "win_rate_pct":     round(win_rate, 1),
            "total_pnl_usd":    round(row["total_pnl"] or 0.0, 2),
            "avg_win_usd":      round(row["avg_win"]  or 0.0, 2),
            "avg_loss_usd":     round(row["avg_loss"] or 0.0, 2),
            "profit_factor":    round(pf, 2),
            "avg_duration_min": round(row["avg_duration_min"] or 0.0, 1),
            "avg_r_ratio":      round(row["avg_r_ratio"] or 0.0, 2),
        }

    def get_performance_by_dimension(self, dimension: str) -> list[dict]:
        """
        Break performance down by a single dimension column.

        Valid dimensions: symbol, regime, signal_type, session, pattern,
                          direction, close_reason.

        WHY this generic interface:
        Phase 5 will call this for every dimension automatically.
        No code change needed to add a new analysis axis -- just pass
        the column name.
        """
        valid_dims = {
            "symbol", "regime", "signal_type", "session",
            "pattern", "direction", "close_reason", "day_of_week",
        }
        if dimension not in valid_dims:
            raise ValueError(f"Invalid dimension '{dimension}'. Valid: {valid_dims}")

        sql = f"""
            SELECT
                {dimension}                                     AS dim_value,
                COUNT(*)                                        AS total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)         AS wins,
                SUM(pnl_usd)                                    AS total_pnl,
                AVG(pnl_usd)                                    AS avg_pnl,
                SUM(CASE WHEN won=1 THEN pnl_usd ELSE 0 END)   AS gross_wins,
                SUM(CASE WHEN won=0 THEN ABS(pnl_usd) ELSE 0 END) AS gross_losses,
                AVG(confidence)                                 AS avg_confidence,
                AVG(r_ratio)                                    AS avg_r_ratio
            FROM trades
            WHERE closed_at_utc IS NOT NULL
            GROUP BY {dimension}
            ORDER BY total_pnl DESC
        """

        rows = []
        with self._connect() as conn:
            for row in conn.execute(sql).fetchall():
                gl = row["gross_losses"] or 0.0
                gw = row["gross_wins"]   or 0.0
                pf = (gw / gl) if gl > 0 else float("inf")
                wr = (row["wins"] / row["total"] * 100.0) if row["total"] > 0 else 0.0
                rows.append({
                    "dimension":       dimension,
                    "value":           row["dim_value"],
                    "total":           row["total"],
                    "wins":            row["wins"],
                    "win_rate_pct":    round(wr, 1),
                    "total_pnl_usd":   round(row["total_pnl"] or 0.0, 2),
                    "avg_pnl_usd":     round(row["avg_pnl"] or 0.0, 2),
                    "profit_factor":   round(pf, 2),
                    "avg_confidence":  round(row["avg_confidence"] or 0.0, 1),
                    "avg_r_ratio":     round(row["avg_r_ratio"] or 0.0, 2),
                })
        return rows

    def get_open_trades(self) -> list[dict]:
        """
        Return all trades that were logged as opened but not yet closed.
        Used by the recovery path on bot restart to detect orphaned trades.
        """
        sql = """
            SELECT trade_id, symbol, direction, entry_price, lots, opened_at_utc
            FROM trades
            WHERE closed_at_utc IS NULL
            ORDER BY opened_at_utc DESC
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def get_recent_trades(self, limit: int = 20) -> list[dict]:
        """Return the N most recent closed trades, newest first."""
        sql = """
            SELECT trade_id, symbol, direction, signal_type, pattern,
                   regime, session, confidence, lots, entry_price, close_price,
                   pnl_usd, won, close_reason, duration_minutes,
                   breakeven_applied, trailing_active, opened_at_utc, closed_at_utc
            FROM trades
            WHERE closed_at_utc IS NOT NULL
            ORDER BY closed_at_utc DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_confidence_correlation(self) -> list[dict]:
        """
        Returns average confidence score grouped by win/loss.

        WHY this matters:
        If avg confidence is 82 on wins and 71 on losses, the scoring system
        is genuinely predictive. If both are 76, the scoring is noise.
        Phase 5 uses this to calibrate the threshold.
        """
        sql = """
            SELECT
                CASE WHEN won=1 THEN 'WIN' ELSE 'LOSS' END AS outcome,
                COUNT(*)                                    AS count,
                AVG(confidence)                             AS avg_confidence,
                MIN(confidence)                             AS min_confidence,
                MAX(confidence)                             AS max_confidence
            FROM trades
            WHERE closed_at_utc IS NOT NULL
            GROUP BY won
        """
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _compute_duration(self, trade_id: str, closed_at_utc: str) -> Optional[float]:
        """Compute trade duration in minutes from stored open time."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT opened_at_utc FROM trades WHERE trade_id=?", (trade_id,)
                ).fetchone()

            if row is None or row["opened_at_utc"] is None:
                return None

            opened = datetime.fromisoformat(row["opened_at_utc"])
            closed = datetime.fromisoformat(closed_at_utc)

            # Ensure both are timezone-aware
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            if closed.tzinfo is None:
                closed = closed.replace(tzinfo=timezone.utc)

            return (closed - opened).total_seconds() / 60.0

        except Exception as exc:
            logger.warning("[LOG] Could not compute duration for %s: %s", trade_id, exc)
            return None


# ===========================================================================
# Standalone test harness
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("ARCS-FX  --  TradeLogger standalone test")
    print("=" * 60)

    # Use a temporary database so tests don't pollute the real one
    tmp_db = os.path.join(tempfile.gettempdir(), "arcs_test_trades.db")
    if os.path.exists(tmp_db):
        os.remove(tmp_db)

    tl = TradeLogger(db_path=tmp_db)

    now_utc = datetime.now(timezone.utc)

    # --- Test 1: log_open --------------------------------------------------
    print("\n--- TEST 1: log_open ---")

    sample_trades = [
        # (trade_id, symbol, direction, signal_type, pattern, regime, session,
        #  confidence, entry, sl, tp, r_ratio, risk_pct, risk_usd)
        ("T001", "EURUSD", "BUY",  "OB_RETEST",  "BULLISH_ENGULFING", "TRENDING", "OVERLAP", 82.0, 1.0850, 1.0820, 1.0910, 2.0, 1.0, 10.0),
        ("T002", "GBPUSD", "SELL", "SD_BOUNCE",  "BEARISH_PIN_BAR",   "RANGING",  "LONDON",  75.0, 1.2700, 1.2730, 1.2640, 2.0, 1.0, 10.0),
        ("T003", "USDJPY", "BUY",  "FVG_FILL",   "BULLISH_ENGULFING", "TRENDING", "NY",      79.0, 149.50, 149.20, 150.10, 2.0, 1.0, 14.95),
        ("T004", "EURUSD", "SELL", "OB_RETEST",  "BEARISH_ENGULFING", "TRENDING", "OVERLAP", 88.0, 1.0920, 1.0950, 1.0860, 2.0, 1.0, 10.0),
        ("T005", "AUDUSD", "BUY",  "SD_BOUNCE",  "BULLISH_PIN_BAR",   "RANGING",  "ASIAN",   71.0, 0.6400, 0.6370, 0.6460, 2.0, 0.5,  5.0),
    ]

    for t in sample_trades:
        open_rec = TradeOpenRecord(
            trade_id           = t[0],
            mt5_ticket         = int(t[0][1:]) * 1000,
            symbol             = t[1],
            direction          = t[2],
            entry_price        = t[8],
            sl_price           = t[9],
            initial_sl_price   = t[9],
            tp_price           = t[10],
            lots               = 0.03,
            opened_at_utc      = now_utc.isoformat(),
            signal_type        = t[3],
            pattern            = t[4],
            r_ratio            = t[11],
            confidence         = t[7],
            regime             = t[5],
            session            = t[6],
            risk_pct           = t[12],
            risk_usd           = t[13],
            win_rate_at_open   = 0.5,
            daily_loss_at_open = 0.0,
            confidence_components = [
                {"name": "regime_clarity",  "score": t[7] * 0.25, "max_score": 25},
                {"name": "price_action",    "score": t[7] * 0.20, "max_score": 20},
            ],
        )
        tl.log_open(open_rec)

    open_trades = tl.get_open_trades()
    print(f"  Open (unclosed) trades: {len(open_trades)}")
    assert len(open_trades) == 5, f"TEST 1 FAILED: expected 5, got {len(open_trades)}"
    print("  -> PASS")

    # --- Test 2: log_close -------------------------------------------------
    print("\n--- TEST 2: log_close ---")
    from datetime import timedelta

    close_data = [
        ("T001", 1.0910, 18.0,  True,  "TP_HIT",      True,  True),
        ("T002", 1.2730, -15.0, False, "SL_HIT",      False, False),
        ("T003", 150.10, 22.35, True,  "TRAIL_HIT",   True,  True),
        ("T004", 1.0870, 15.0,  True,  "TP_HIT",      True,  False),
        ("T005", 0.6370, -12.5, False, "SL_HIT",      False, False),
    ]

    for cd in close_data:
        tl.log_close(
            trade_id            = cd[0],
            close_price         = cd[1],
            pnl_usd             = cd[2],
            won                 = cd[3],
            close_reason        = cd[4],
            breakeven_applied   = cd[5],
            trailing_active     = cd[6],
            max_adverse_exc_r   = 0.3,
            max_favourable_exc_r= 2.1,
        )

    open_trades_after = tl.get_open_trades()
    print(f"  Open trades after close: {len(open_trades_after)}")
    assert len(open_trades_after) == 0, "TEST 2 FAILED: all should be closed"
    print("  -> PASS")

    # --- Test 3: performance summary ---------------------------------------
    print("\n--- TEST 3: Performance summary ---")
    summary = tl.get_performance_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")
    assert summary["total"] == 5, "TEST 3 FAILED: total should be 5"
    assert summary["wins"]  == 3, "TEST 3 FAILED: wins should be 3"
    assert summary["win_rate_pct"] == 60.0, "TEST 3 FAILED: win_rate should be 60%"
    assert summary["profit_factor"] > 1.0, "TEST 3 FAILED: PF should be > 1"
    print("  -> PASS")

    # --- Test 4: performance by dimension ----------------------------------
    print("\n--- TEST 4: Performance by signal_type ---")
    by_signal = tl.get_performance_by_dimension("signal_type")
    for row in by_signal:
        print(f"  {row['value']:20s} | trades={row['total']} | "
              f"win_rate={row['win_rate_pct']}% | pnl=${row['total_pnl_usd']} | "
              f"PF={row['profit_factor']}")
    assert len(by_signal) == 3, "TEST 4 FAILED: 3 distinct signal types"
    print("  -> PASS")

    # --- Test 5: performance by symbol ------------------------------------
    print("\n--- TEST 5: Performance by symbol ---")
    by_symbol = tl.get_performance_by_dimension("symbol")
    for row in by_symbol:
        print(f"  {row['value']:10s} | trades={row['total']} | "
              f"win_rate={row['win_rate_pct']}% | pnl=${row['total_pnl_usd']}")
    print("  -> PASS")

    # --- Test 6: confidence correlation ------------------------------------
    print("\n--- TEST 6: Confidence score correlation with outcome ---")
    corr = tl.get_confidence_correlation()
    for row in corr:
        print(f"  {row['outcome']:5s}: count={row['count']} | "
              f"avg_conf={row['avg_confidence']:.1f} | "
              f"range=[{row['min_confidence']:.0f}-{row['max_confidence']:.0f}]")
    assert len(corr) == 2, "TEST 6 FAILED: should have WIN and LOSS rows"
    print("  -> PASS")

    # --- Test 7: recent trades --------------------------------------------
    print("\n--- TEST 7: get_recent_trades() ---")
    recent = tl.get_recent_trades(limit=3)
    print(f"  Fetched {len(recent)} recent trades")
    for r in recent:
        print(f"  {r['trade_id']} | {r['symbol']} {r['direction']} | "
              f"pnl=${r['pnl_usd']} | won={r['won']}")
    assert len(recent) == 3, "TEST 7 FAILED: should return 3"
    print("  -> PASS")

    # Cleanup -- WAL mode creates auxiliary files on Windows; remove all
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass
    print(f"\n  Test DB removed: {tmp_db}")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
