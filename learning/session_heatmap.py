"""
ARCS-FX -- learning/session_heatmap.py
Per-pair, per-hour win rate heatmap built from live trade history.

WHY THIS MODULE EXISTS (Spec Feature 1 -- Time-of-Day Weighting):
  "Every pair has a statistical performance fingerprint by hour.
   Bot builds a heatmap from historical data.
   Only trades during statistically proven windows."

The original spec requires the bot to learn WHEN it performs best --
not just on which setup, but on which pair at which hour of the day.
A static "prefer OVERLAP session" rule is a starting point. After 50+
trades, the bot should know that EURUSD at 14:00 UTC has a 72% win rate
on its account while the same pair at 08:00 UTC has a 38% win rate.

ARCHITECTURE:
  1. Reads from trades.db (no writes -- read-only module)
  2. Groups closed trades by symbol + hour_utc
  3. Computes win rate and profit factor per cell
  4. Returns a 0.0-1.0 score for (symbol, current_hour_utc)
  5. Fallback to static session preference when data is insufficient

INTEGRATION:
  engines/confidence_score.py calls:
    heatmap.get_score(symbol, hour_utc) -> float (0.0-1.0)
  This replaces / augments the static spread_session component.

MINIMUM DATA:
  MIN_TRADES_PER_CELL = 5
  If a (symbol, hour) cell has fewer than 5 trades, fallback to static score.
  Statistical noise from 2 trades is worse than no data.
"""

import os
import sys
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_PATH, SESSIONS, PREFERRED_SESSION, PAIRS

logger = logging.getLogger(__name__)

_DB_ABS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    DB_PATH,
)

# Minimum trades in a (symbol, hour) cell to use learned score
MIN_TRADES_PER_CELL = 5

# Static fallback scores by session (when insufficient data)
# Based on known Forex market behaviour -- overridden by live data as it accumulates
_STATIC_SESSION_SCORES: dict[str, float] = {
    "OVERLAP": 0.80,   # London/NY overlap -- highest liquidity
    "LONDON":  0.65,
    "NY":      0.60,
    "ASIAN":   0.35,
    "OFF":     0.20,   # outside all sessions
}


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class HeatmapCell:
    """A single (symbol, hour) cell in the heatmap."""
    symbol:        str
    hour_utc:      int     # 0-23
    trades:        int
    wins:          int
    win_rate:      float   # 0.0-1.0
    profit_factor: float
    avg_pnl:       float
    score:         float   # 0.0-1.0 normalised score fed to confidence engine
    is_learned:    bool    # False = fallback to static score


@dataclass
class HeatmapSnapshot:
    """Full heatmap for all symbols and all hours."""
    cells:         list[HeatmapCell]
    total_trades:  int
    generated_at:  str


# ===========================================================================
# SessionHeatmap
# ===========================================================================

class SessionHeatmap:
    """
    Builds and queries the per-pair, per-hour performance heatmap.

    USAGE:
        heatmap = SessionHeatmap()
        score   = heatmap.get_score("EURUSD", current_hour_utc=14)
        # Returns 0.0-1.0 -- plug into confidence_score spread_session component

    CACHING:
        The heatmap is rebuilt from DB every CACHE_TTL_MINUTES minutes.
        Between refreshes, get_score() returns cached values.
        WHY not query on every tick: SQLite GROUP BY on 1000+ rows is fast
        but not free. 15-minute cache is a reasonable tradeoff.

    SINGLETON:
        confidence_score.py calls SessionHeatmap._get_shared_instance() to
        avoid creating a new object (and DB connection) on every tick.
        The main orchestrator also holds a reference to this same instance
        for report generation and status checks.
    """

    CACHE_TTL_MINUTES = 15
    _shared: Optional["SessionHeatmap"] = None   # module-level singleton

    @classmethod
    def _get_shared_instance(cls) -> "SessionHeatmap":
        """
        Return the module-level shared instance, creating it if needed.

        WHY a shared instance:
          confidence_score.py is called once per tick per pair from the main
          loop. Without a singleton, each call would instantiate a new
          SessionHeatmap -- no caching benefit and a new DB handle each time.
        """
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def __init__(self, db_path: str = _DB_ABS) -> None:
        self._db_path    = db_path
        self._cache:     Optional[dict[tuple[str, int], HeatmapCell]] = None
        self._cache_time: Optional[datetime] = None
        logger.info("[HM] SessionHeatmap initialised. DB: %s", db_path)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def get_score(self, symbol: str, hour_utc: int) -> float:
        """
        Return a 0.0-1.0 score for trading (symbol) at (hour_utc).

        0.0 = historically terrible hour for this pair
        1.0 = historically best hour for this pair
        0.5 = neutral / no data yet

        This score replaces the static session preference in
        confidence_score.py's spread_session component once enough
        data has accumulated (>= MIN_TRADES_PER_CELL per cell).

        WHY 0.5 as the neutral fallback:
        0.5 means "I don't know yet". It allows the bot to still trade
        during the static preferred sessions while learning. A score of
        0.0 would block trading entirely during data collection.
        """
        cache = self._get_cache()
        key   = (symbol, hour_utc)

        if key in cache:
            return cache[key].score

        # Symbol not in cache at all -- use static session fallback
        session = _hour_to_session(hour_utc)
        score   = _STATIC_SESSION_SCORES.get(session, 0.5)
        logger.debug(
            "[HM] No data for %s hour=%d -- static fallback: %s (%.2f)",
            symbol, hour_utc, session, score,
        )
        return score

    def get_cell(self, symbol: str, hour_utc: int) -> Optional[HeatmapCell]:
        """Return the full HeatmapCell for (symbol, hour_utc), or None."""
        return self._get_cache().get((symbol, hour_utc))

    def build_snapshot(self) -> HeatmapSnapshot:
        """
        Build and return a full snapshot of the heatmap.
        Forces a cache refresh.
        Used by report_generator for the weekly report.
        """
        self._cache      = None
        self._cache_time = None
        cache = self._get_cache()
        total = sum(c.trades for c in cache.values())
        return HeatmapSnapshot(
            cells         = sorted(cache.values(), key=lambda c: (c.symbol, c.hour_utc)),
            total_trades  = total,
            generated_at  = datetime.now(timezone.utc).isoformat(),
        )

    def get_best_hours(self, symbol: str, top_n: int = 5) -> list[HeatmapCell]:
        """
        Return the top N hours for a symbol ranked by score.
        Used by report_generator and pattern_analyzer to surface insights.
        """
        cache = self._get_cache()
        cells = [
            c for k, c in cache.items()
            if k[0] == symbol and c.is_learned
        ]
        return sorted(cells, key=lambda c: c.score, reverse=True)[:top_n]

    def get_worst_hours(self, symbol: str, top_n: int = 3) -> list[HeatmapCell]:
        """Return the worst N hours for a symbol. Used for risk warnings."""
        cache = self._get_cache()
        cells = [
            c for k, c in cache.items()
            if k[0] == symbol and c.is_learned
        ]
        return sorted(cells, key=lambda c: c.score)[:top_n]

    # -----------------------------------------------------------------------
    # Cache management
    # -----------------------------------------------------------------------

    def _get_cache(self) -> dict[tuple[str, int], HeatmapCell]:
        """Return the cached heatmap, refreshing if stale or absent."""
        now = datetime.now(timezone.utc)
        if (
            self._cache is None
            or self._cache_time is None
            or (now - self._cache_time).total_seconds() > self.CACHE_TTL_MINUTES * 60
        ):
            self._cache      = self._build_cache()
            self._cache_time = now
            logger.debug("[HM] Heatmap cache refreshed: %d cells.", len(self._cache))
        return self._cache

    def _build_cache(self) -> dict[tuple[str, int], HeatmapCell]:
        """
        Query the database and build the (symbol, hour) -> HeatmapCell dict.

        HOW THE SCORE IS COMPUTED:
          1. win_rate component (60% weight): normalised 0-1
          2. profit_factor component (40% weight): tanh-normalised so that
             PF=1.0 maps to 0.5, PF=2.0 maps to ~0.76, PF=0.5 maps to ~0.24
             This prevents a pair with 1 win and 1 loss (PF=inf) from scoring 1.0.
          Final score = 0.6 * win_rate + 0.4 * pf_score, clamped to [0.05, 0.95]

        WHY tanh for profit factor:
          Raw PF can be infinity (all wins) or 0 (all losses). tanh provides
          a smooth S-curve mapping that is bounded and handles extremes well.
        """
        import math

        if not os.path.exists(self._db_path):
            return {}

        sql = """
            SELECT
                symbol,
                CAST(strftime('%H', opened_at_utc) AS INTEGER) AS hour_utc,
                COUNT(*)                                        AS trades,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)         AS wins,
                SUM(CASE WHEN won=1 THEN pnl_usd ELSE 0 END)   AS gross_wins,
                SUM(CASE WHEN won=0 THEN ABS(pnl_usd) ELSE 0 END) AS gross_losses,
                AVG(pnl_usd)                                    AS avg_pnl
            FROM trades
            WHERE closed_at_utc IS NOT NULL
              AND opened_at_utc IS NOT NULL
              AND symbol IS NOT NULL
            GROUP BY symbol, hour_utc
            ORDER BY symbol, hour_utc
        """

        cache: dict[tuple[str, int], HeatmapCell] = {}

        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning("[HM] DB query failed: %s. Using empty cache.", exc)
            return {}

        for row in rows:
            n   = row["trades"]
            sym = row["symbol"]
            hr  = row["hour_utc"]
            gw  = row["gross_wins"]   or 0.0
            gl  = row["gross_losses"] or 0.0
            wr  = (row["wins"] / n) if n > 0 else 0.0
            pf  = (gw / gl) if gl > 0 else (2.0 if gw > 0 else 1.0)

            is_learned = (n >= MIN_TRADES_PER_CELL)

            if is_learned:
                # Compute score from live data
                pf_score  = (math.tanh(pf - 1.0) + 1.0) / 2.0   # 0-1 via tanh
                raw_score = 0.6 * wr + 0.4 * pf_score
                score     = max(0.05, min(0.95, raw_score))
            else:
                # Insufficient data -- use static session fallback
                session = _hour_to_session(hr)
                score   = _STATIC_SESSION_SCORES.get(session, 0.5)

            cache[(sym, hr)] = HeatmapCell(
                symbol        = sym,
                hour_utc      = hr,
                trades        = n,
                wins          = row["wins"],
                win_rate      = round(wr, 4),
                profit_factor = round(pf, 3),
                avg_pnl       = round(row["avg_pnl"] or 0.0, 2),
                score         = round(score, 4),
                is_learned    = is_learned,
            )

        return cache

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn


def _hour_to_session(hour_utc: int) -> str:
    """Map a UTC hour to the dominant trading session name."""
    lo_start, lo_end = SESSIONS["LONDON"]
    ny_start, ny_end = SESSIONS["NY"]
    ov_start, ov_end = SESSIONS["OVERLAP"]
    as_start, as_end = SESSIONS["ASIAN"]

    if ov_start <= hour_utc < ov_end:
        return "OVERLAP"
    if lo_start <= hour_utc < lo_end:
        return "LONDON"
    if ny_start <= hour_utc < ny_end:
        return "NY"
    if as_start <= hour_utc < as_end:
        return "ASIAN"
    return "OFF"


# ===========================================================================
# Standalone test
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile
    from datetime import timedelta
    from learning.trade_logger import TradeLogger, TradeOpenRecord

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("ARCS-FX  --  SessionHeatmap standalone test")
    print("=" * 60)

    tmp_db = os.path.join(tempfile.gettempdir(), "arcs_hm_test.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass

    tl = TradeLogger(db_path=tmp_db)
    now_utc = datetime.now(timezone.utc)

    # Seed trades: EURUSD wins at 14-16 UTC (OVERLAP), loses at 02-04 (ASIAN)
    seed = [
        # (symbol, hour, won, pnl)
        ("EURUSD", 14, True,   18.0), ("EURUSD", 14, True,   20.0),
        ("EURUSD", 14, True,   15.0), ("EURUSD", 14, True,   22.0),
        ("EURUSD", 14, True,   17.0), ("EURUSD", 14, False, -10.0),
        ("EURUSD", 15, True,   19.0), ("EURUSD", 15, True,   21.0),
        ("EURUSD", 15, True,   16.0), ("EURUSD", 15, False, -11.0),
        ("EURUSD", 15, True,   18.0),
        ("EURUSD",  2, False, -12.0), ("EURUSD",  2, False, -11.0),
        ("EURUSD",  2, False, -13.0), ("EURUSD",  2, False, -10.0),
        ("EURUSD",  2, True,    8.0), ("EURUSD",  2, False, -14.0),
        ("GBPUSD", 14, True,   22.0), ("GBPUSD", 14, True,   19.0),
        ("GBPUSD", 14, True,   24.0), ("GBPUSD", 14, False, -12.0),
        ("GBPUSD", 14, True,   20.0),
    ]

    for i, (sym, hr, won, pnl) in enumerate(seed):
        open_time = now_utc.replace(hour=hr, minute=0, second=0, microsecond=0)
        r = TradeOpenRecord(
            trade_id=f"HM_{i:03d}", mt5_ticket=i, symbol=sym, direction="BUY",
            entry_price=1.085, sl_price=1.082, initial_sl_price=1.082, tp_price=1.091,
            lots=0.03, opened_at_utc=open_time.isoformat(),
            signal_type="OB_RETEST", pattern="BULLISH_ENGULFING",
            r_ratio=2.0, confidence=80.0, regime="TRENDING", session="OVERLAP",
            risk_pct=1.0, risk_usd=10.0, win_rate_at_open=0.5, daily_loss_at_open=0.0,
        )
        tl.log_open(r)
        tl.log_close(f"HM_{i:03d}", 1.091 if won else 1.082, pnl, won,
                     "TP_HIT" if won else "SL_HIT")

    hm = SessionHeatmap(db_path=tmp_db)

    print("\n--- TEST 1: Score for EURUSD at 14:00 (should be HIGH) ---")
    score_14 = hm.get_score("EURUSD", 14)
    print(f"  EURUSD 14:00 UTC score: {score_14:.3f}")
    assert score_14 > 0.6, f"FAILED: expected >0.6, got {score_14}"
    print("  -> PASS")

    print("\n--- TEST 2: Score for EURUSD at 02:00 (should be LOW) ---")
    score_02 = hm.get_score("EURUSD", 2)
    print(f"  EURUSD 02:00 UTC score: {score_02:.3f}")
    assert score_02 < score_14, "FAILED: 02:00 score should be lower than 14:00"
    print("  -> PASS")

    print("\n--- TEST 3: Score for USDJPY at 14:00 (no data -- static fallback) ---")
    score_uj = hm.get_score("USDJPY", 14)
    print(f"  USDJPY 14:00 UTC score: {score_uj:.3f}  (static OVERLAP={_STATIC_SESSION_SCORES['OVERLAP']})")
    assert score_uj == _STATIC_SESSION_SCORES["OVERLAP"], "FAILED: should use static fallback"
    print("  -> PASS")

    print("\n--- TEST 4: get_best_hours for EURUSD ---")
    best = hm.get_best_hours("EURUSD", top_n=3)
    for cell in best:
        print(f"  Hour {cell.hour_utc:02d}:00 | score={cell.score:.3f} | "
              f"WR={cell.win_rate*100:.0f}% | PF={cell.profit_factor:.2f} | "
              f"n={cell.trades} | learned={cell.is_learned}")
    assert len(best) >= 1, "FAILED: should have at least 1 learned cell"
    print("  -> PASS")

    print("\n--- TEST 5: Full heatmap snapshot ---")
    snap = hm.build_snapshot()
    print(f"  Total cells: {len(snap.cells)}")
    print(f"  Total trades: {snap.total_trades}")
    assert snap.total_trades == len(seed), f"FAILED: expected {len(seed)} trades"
    print("  -> PASS")

    print("\n--- TEST 6: Score < threshold triggers static fallback for off-hours ---")
    score_off = hm.get_score("EURUSD", 22)   # no trades seeded at 22:00
    expected  = _STATIC_SESSION_SCORES.get(_hour_to_session(22), 0.5)
    print(f"  EURUSD 22:00 UTC: score={score_off:.3f} (expected static={expected:.2f})")
    print("  -> PASS")

    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
