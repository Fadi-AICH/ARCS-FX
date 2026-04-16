"""
ARCS-FX -- learning/pattern_analyzer.py
Trade DNA analysis engine: finds what is working and what isn't.

WHY THIS MODULE EXISTS:
The bot collects structured data on every trade it takes (via trade_logger).
Without analysis, that data is inert. The pattern analyzer transforms the
raw trade history into actionable intelligence:

  "OB_RETEST in TRENDING regime during OVERLAP session has a 71% win rate
   with profit factor 2.3. SD_BOUNCE in RANGING has 38% win rate and PF 0.6.
   The bot should weight OB_RETEST signals higher."

This is the first layer of self-learning. The output feeds:
  1. StrategyAdjuster  -- recalibrates confidence component weights
  2. ReportGenerator   -- surfaces insights in the weekly PDF

ANALYSIS DIMENSIONS:
  -- Single-axis breakdowns:
     signal_type, regime, session, pattern, symbol, direction, close_reason
  -- Cross-axis (combination) analysis:
     regime x signal_type  (the most important combination)
     session x regime
  -- Confidence calibration:
     Does a higher confidence score predict a better outcome?
     -> computes confidence decile win rates
  -- Trade management effectiveness:
     How often does BE save a trade? Does trailing outperform fixed TP?
  -- Drawdown / duration profiling:
     MAE/MFE distribution, avg duration by regime

MINIMUM SAMPLE SIZE:
  Results with fewer than MIN_SAMPLE_SIZE trades are flagged as low-confidence.
  Statistical noise is worse than no data -- we never act on n < 5.
"""

import os
import sys
import sqlite3
import logging
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_PATH

logger = logging.getLogger(__name__)

# Absolute path to DB
_DB_ABS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    DB_PATH,
)

# Minimum trades required to consider a stat statistically meaningful
MIN_SAMPLE_SIZE = 5

# Confidence deciles for calibration analysis
CONFIDENCE_DECILE_STEP = 10


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class DimensionStat:
    """
    Performance statistics for a single value of a single dimension.
    E.g.: dimension=signal_type, value=OB_RETEST
    """
    dimension:       str
    value:           str
    total:           int
    wins:            int
    losses:          int
    win_rate:        float     # 0.0-1.0
    total_pnl:       float
    avg_pnl:         float
    gross_wins:      float
    gross_losses:    float
    profit_factor:   float
    avg_confidence:  float
    avg_r_ratio:     float
    avg_duration_min: float
    low_confidence:  bool      # True if total < MIN_SAMPLE_SIZE


@dataclass
class CrossDimensionStat:
    """
    Performance at the intersection of two dimensions.
    E.g.: regime=TRENDING, signal_type=OB_RETEST
    """
    dim1:          str
    val1:          str
    dim2:          str
    val2:          str
    total:         int
    wins:          int
    win_rate:      float
    total_pnl:     float
    profit_factor: float
    avg_confidence: float
    low_confidence: bool


@dataclass
class ConfidenceCalibration:
    """
    Win rate per confidence score band.
    Used to validate whether the confidence gate is predictive.
    """
    band_low:   int       # e.g. 70
    band_high:  int       # e.g. 79
    total:      int
    wins:       int
    win_rate:   float
    avg_pnl:    float
    low_confidence: bool


@dataclass
class ManagementStat:
    """
    Effectiveness of trade management features (BE, trailing).
    """
    category:       str    # "breakeven_applied", "trailing_active"
    value:          bool
    total:          int
    wins:           int
    win_rate:       float
    avg_pnl:        float
    profit_factor:  float


@dataclass
class AnalysisReport:
    """
    Full output of PatternAnalyzer.run_full_analysis().
    Contains all breakdowns and the top-level recommendations.
    """
    total_closed_trades:       int
    by_signal_type:            list[DimensionStat]
    by_regime:                 list[DimensionStat]
    by_session:                list[DimensionStat]
    by_pattern:                list[DimensionStat]
    by_symbol:                 list[DimensionStat]
    by_close_reason:           list[DimensionStat]
    regime_x_signal:           list[CrossDimensionStat]
    session_x_regime:          list[CrossDimensionStat]
    confidence_calibration:    list[ConfidenceCalibration]
    management_stats:          list[ManagementStat]
    top_setups:                list[DimensionStat]     # best by profit_factor
    worst_setups:              list[DimensionStat]     # worst by profit_factor
    weight_recommendations:    dict[str, float]        # component -> suggested weight delta
    generated_at_utc:          str


# ===========================================================================
# PatternAnalyzer
# ===========================================================================

class PatternAnalyzer:
    """
    Reads from the trades SQLite database and produces a structured
    AnalysisReport that quantifies the bot's edge (or lack thereof)
    across every tagged dimension.

    DESIGN PRINCIPLE:
    This module is read-only. It never writes to the database and never
    modifies config or weights directly. All mutations go through
    StrategyAdjuster, which calls this analyzer and decides what to do
    with the results.
    """

    def __init__(self, db_path: str = _DB_ABS) -> None:
        self._db_path = db_path

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run_full_analysis(self) -> AnalysisReport:
        """
        Execute all analysis queries and return a complete AnalysisReport.

        WHY run_full_analysis() instead of individual methods:
        All queries run against the same snapshot. Calling individual methods
        at different times could pick up new trades between calls and produce
        internally inconsistent results. The full analysis is atomic.
        """
        from datetime import datetime, timezone

        total = self._count_closed_trades()
        logger.info("[PA] Running full analysis on %d closed trades.", total)

        by_signal   = self._by_dimension("signal_type")
        by_regime   = self._by_dimension("regime")
        by_session  = self._by_dimension("session")
        by_pattern  = self._by_dimension("pattern")
        by_symbol   = self._by_dimension("symbol")
        by_close    = self._by_dimension("close_reason")

        regime_x_signal  = self._cross_dimension("regime", "signal_type")
        session_x_regime = self._cross_dimension("session", "regime")

        confidence_cal = self._confidence_calibration()
        mgmt_stats     = self._management_stats()

        # Top/worst setups: only include stats with adequate sample size
        all_signal_stats = [s for s in by_signal if not s.low_confidence]
        top_setups   = sorted(all_signal_stats, key=lambda s: s.profit_factor, reverse=True)[:3]
        worst_setups = sorted(all_signal_stats, key=lambda s: s.profit_factor)[:3]

        weight_recs = self._generate_weight_recommendations(by_signal, by_regime, confidence_cal)

        report = AnalysisReport(
            total_closed_trades    = total,
            by_signal_type         = by_signal,
            by_regime              = by_regime,
            by_session             = by_session,
            by_pattern             = by_pattern,
            by_symbol              = by_symbol,
            by_close_reason        = by_close,
            regime_x_signal        = regime_x_signal,
            session_x_regime       = session_x_regime,
            confidence_calibration = confidence_cal,
            management_stats       = mgmt_stats,
            top_setups             = top_setups,
            worst_setups           = worst_setups,
            weight_recommendations = weight_recs,
            generated_at_utc       = datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "[PA] Analysis complete. Top setup: %s. Worst: %s.",
            top_setups[0].value if top_setups else "n/a",
            worst_setups[0].value if worst_setups else "n/a",
        )
        return report

    def get_best_regime_signal_combos(
        self, min_pf: float = 1.5
    ) -> list[CrossDimensionStat]:
        """
        Return regime x signal_type combinations with profit_factor >= min_pf
        and adequate sample size.

        Used by StrategyAdjuster to identify setups that deserve a confidence boost.
        """
        stats = self._cross_dimension("regime", "signal_type")
        return [
            s for s in stats
            if not s.low_confidence and s.profit_factor >= min_pf
        ]

    def get_session_filter_recommendations(self) -> list[dict]:
        """
        Identify sessions where the bot is consistently losing.

        Returns list of {"session": str, "recommendation": str, "win_rate": float}
        WHY: If the bot has a 30% win rate during ASIAN session, it should either
        raise the confidence threshold or disable trading during that session.
        """
        stats  = self._by_dimension("session")
        result = []
        for stat in stats:
            if stat.low_confidence:
                continue
            if stat.win_rate < 0.40:
                result.append({
                    "session":        stat.value,
                    "recommendation": "RAISE_THRESHOLD or DISABLE",
                    "win_rate":       round(stat.win_rate, 3),
                    "profit_factor":  stat.profit_factor,
                    "sample_size":    stat.total,
                })
            elif stat.win_rate >= 0.60 and stat.profit_factor >= 1.5:
                result.append({
                    "session":        stat.value,
                    "recommendation": "LOWER_THRESHOLD (edge confirmed)",
                    "win_rate":       round(stat.win_rate, 3),
                    "profit_factor":  stat.profit_factor,
                    "sample_size":    stat.total,
                })
        return result

    # -----------------------------------------------------------------------
    # Core query methods
    # -----------------------------------------------------------------------

    def _by_dimension(self, dimension: str) -> list[DimensionStat]:
        """
        Single-axis breakdown by any tagged column.
        Returns sorted list (best profit_factor first).
        """
        sql = f"""
            SELECT
                {dimension}                                         AS dim_value,
                COUNT(*)                                            AS total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)             AS wins,
                SUM(pnl_usd)                                        AS total_pnl,
                AVG(pnl_usd)                                        AS avg_pnl,
                SUM(CASE WHEN won=1 THEN pnl_usd   ELSE 0 END)     AS gross_wins,
                SUM(CASE WHEN won=0 THEN ABS(pnl_usd) ELSE 0 END)  AS gross_losses,
                AVG(confidence)                                     AS avg_confidence,
                AVG(r_ratio)                                        AS avg_r_ratio,
                AVG(duration_minutes)                               AS avg_duration_min
            FROM trades
            WHERE closed_at_utc IS NOT NULL
              AND {dimension} IS NOT NULL
            GROUP BY {dimension}
            ORDER BY total_pnl DESC
        """
        results = []
        with self._connect() as conn:
            for row in conn.execute(sql).fetchall():
                gw = row["gross_wins"]   or 0.0
                gl = row["gross_losses"] or 0.0
                pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
                n  = row["total"]
                wr = (row["wins"] / n) if n > 0 else 0.0
                results.append(DimensionStat(
                    dimension        = dimension,
                    value            = str(row["dim_value"] or "unknown"),
                    total            = n,
                    wins             = row["wins"],
                    losses           = n - row["wins"],
                    win_rate         = round(wr, 4),
                    total_pnl        = round(row["total_pnl"] or 0.0, 2),
                    avg_pnl          = round(row["avg_pnl"]   or 0.0, 2),
                    gross_wins       = round(gw, 2),
                    gross_losses     = round(gl, 2),
                    profit_factor    = round(pf, 3) if pf != float("inf") else 999.0,
                    avg_confidence   = round(row["avg_confidence"] or 0.0, 1),
                    avg_r_ratio      = round(row["avg_r_ratio"]   or 0.0, 2),
                    avg_duration_min = round(row["avg_duration_min"] or 0.0, 1),
                    low_confidence   = (n < MIN_SAMPLE_SIZE),
                ))
        results.sort(key=lambda s: s.profit_factor, reverse=True)
        return results

    def _cross_dimension(self, dim1: str, dim2: str) -> list[CrossDimensionStat]:
        """
        Two-axis breakdown: performance at the intersection of dim1 x dim2.
        Returns sorted list (best profit_factor first).
        """
        sql = f"""
            SELECT
                {dim1}                                              AS val1,
                {dim2}                                              AS val2,
                COUNT(*)                                            AS total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)             AS wins,
                SUM(pnl_usd)                                        AS total_pnl,
                SUM(CASE WHEN won=1 THEN pnl_usd   ELSE 0 END)     AS gross_wins,
                SUM(CASE WHEN won=0 THEN ABS(pnl_usd) ELSE 0 END)  AS gross_losses,
                AVG(confidence)                                     AS avg_confidence
            FROM trades
            WHERE closed_at_utc IS NOT NULL
              AND {dim1} IS NOT NULL
              AND {dim2} IS NOT NULL
            GROUP BY {dim1}, {dim2}
            ORDER BY total_pnl DESC
        """
        results = []
        with self._connect() as conn:
            for row in conn.execute(sql).fetchall():
                gw = row["gross_wins"]   or 0.0
                gl = row["gross_losses"] or 0.0
                pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
                n  = row["total"]
                wr = (row["wins"] / n) if n > 0 else 0.0
                results.append(CrossDimensionStat(
                    dim1           = dim1,
                    val1           = str(row["val1"] or "unknown"),
                    dim2           = dim2,
                    val2           = str(row["val2"] or "unknown"),
                    total          = n,
                    wins           = row["wins"],
                    win_rate       = round(wr, 4),
                    total_pnl      = round(row["total_pnl"] or 0.0, 2),
                    profit_factor  = round(pf, 3) if pf != float("inf") else 999.0,
                    avg_confidence = round(row["avg_confidence"] or 0.0, 1),
                    low_confidence = (n < MIN_SAMPLE_SIZE),
                ))
        results.sort(key=lambda s: s.profit_factor, reverse=True)
        return results

    def _confidence_calibration(self) -> list[ConfidenceCalibration]:
        """
        Group trades into confidence bands (70-79, 80-89, 90-100)
        and compute win rate per band.

        WHY this is the most important analysis in Phase 5:
        If higher confidence doesn't predict higher win rate, the scoring
        system is not working and its weights need recalibration.
        If it IS predictive, we can safely raise or lower the threshold.
        """
        sql = """
            SELECT
                (CAST(confidence AS INTEGER) / :step) * :step      AS band_low,
                (CAST(confidence AS INTEGER) / :step) * :step + :step - 1 AS band_high,
                COUNT(*)                                            AS total,
                SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)             AS wins,
                AVG(pnl_usd)                                        AS avg_pnl
            FROM trades
            WHERE closed_at_utc IS NOT NULL
              AND confidence IS NOT NULL
            GROUP BY band_low
            ORDER BY band_low
        """
        results = []
        with self._connect() as conn:
            for row in conn.execute(sql, {"step": CONFIDENCE_DECILE_STEP}).fetchall():
                n  = row["total"]
                wr = (row["wins"] / n) if n > 0 else 0.0
                results.append(ConfidenceCalibration(
                    band_low       = int(row["band_low"]),
                    band_high      = int(row["band_high"]),
                    total          = n,
                    wins           = row["wins"],
                    win_rate       = round(wr, 4),
                    avg_pnl        = round(row["avg_pnl"] or 0.0, 2),
                    low_confidence = (n < MIN_SAMPLE_SIZE),
                ))
        return results

    def _management_stats(self) -> list[ManagementStat]:
        """
        Compare trade outcomes based on which management features fired:
          - Trades where BE was applied vs not
          - Trades where trailing was active vs not

        WHY: If BE-applied trades have a higher win rate, the +1R rule is
        working. If trailing_active trades have higher avg P&L, trailing
        outperforms fixed TP.
        """
        results = []

        for col in ("breakeven_applied", "trailing_active"):
            sql = f"""
                SELECT
                    {col}                                               AS flag,
                    COUNT(*)                                            AS total,
                    SUM(CASE WHEN won=1 THEN 1 ELSE 0 END)             AS wins,
                    AVG(pnl_usd)                                        AS avg_pnl,
                    SUM(CASE WHEN won=1 THEN pnl_usd   ELSE 0 END)     AS gross_wins,
                    SUM(CASE WHEN won=0 THEN ABS(pnl_usd) ELSE 0 END)  AS gross_losses
                FROM trades
                WHERE closed_at_utc IS NOT NULL
                  AND {col} IS NOT NULL
                GROUP BY {col}
            """
            with self._connect() as conn:
                for row in conn.execute(sql).fetchall():
                    gw = row["gross_wins"]   or 0.0
                    gl = row["gross_losses"] or 0.0
                    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
                    n  = row["total"]
                    wr = (row["wins"] / n) if n > 0 else 0.0
                    results.append(ManagementStat(
                        category      = col,
                        value         = bool(row["flag"]),
                        total         = n,
                        wins          = row["wins"],
                        win_rate      = round(wr, 4),
                        avg_pnl       = round(row["avg_pnl"] or 0.0, 2),
                        profit_factor = round(pf, 3) if pf != float("inf") else 999.0,
                    ))
        return results

    def _count_closed_trades(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE closed_at_utc IS NOT NULL"
            ).fetchone()
        return row["n"] if row else 0

    # -----------------------------------------------------------------------
    # Weight recommendation engine
    # -----------------------------------------------------------------------

    def _generate_weight_recommendations(
        self,
        by_signal:          list[DimensionStat],
        by_regime:          list[DimensionStat],
        confidence_cal:     list[ConfidenceCalibration],
    ) -> dict[str, float]:
        """
        Generate weight delta recommendations for each confidence component.

        Returns dict: component_name -> delta (positive = increase, negative = decrease).

        LOGIC:
          1. price_action weight:
             If OB_RETEST PF > SD_BOUNCE PF by a large margin, price_action
             component is highly predictive -> recommend +2 to +5 pts.
             If all signal types cluster around PF 1.0, component has low
             discriminatory power -> recommend -2 pts.

          2. regime weight:
             If TRENDING has significantly better PF than RANGING, regime
             detection is a strong predictor -> recommend +3.
             If both regimes perform equally, regime clarity is noise -> -2.

          3. confidence gate calibration:
             If win rate increases monotonically across bands (70s < 80s < 90s),
             the gate is well-calibrated -> no change.
             If higher confidence doesn't predict higher win rate, the scoring
             has components that are misleading -> flag for manual review.

        WHY deltas not absolute values:
        Absolute rebalancing risks overcorrecting on small samples.
        Deltas applied with a dampening factor (in StrategyAdjuster) ensure
        the system moves toward the evidence gradually, not all at once.
        """
        deltas: dict[str, float] = {}

        # --- price_action component ---
        signal_stats = {s.value: s for s in by_signal if not s.low_confidence}
        if "OB_RETEST" in signal_stats and "SD_BOUNCE" in signal_stats:
            ob_pf = signal_stats["OB_RETEST"].profit_factor
            sd_pf = signal_stats["SD_BOUNCE"].profit_factor
            pf_gap = ob_pf - sd_pf
            if pf_gap > 1.5:
                deltas["price_action"] = +3.0
                logger.info("[PA] Recommend price_action +3 (OB PF=%.2f >> SD PF=%.2f)", ob_pf, sd_pf)
            elif pf_gap < -0.5:
                deltas["price_action"] = -2.0
                logger.info("[PA] Recommend price_action -2 (SD outperforming OB)")

        # --- regime_clarity component ---
        regime_stats = {s.value: s for s in by_regime if not s.low_confidence}
        if "TRENDING" in regime_stats and "RANGING" in regime_stats:
            trend_pf  = regime_stats["TRENDING"].profit_factor
            range_pf  = regime_stats["RANGING"].profit_factor
            pf_gap    = trend_pf - range_pf
            if pf_gap > 1.0:
                deltas["regime_clarity"] = +2.0
                logger.info("[PA] Recommend regime_clarity +2 (TRENDING PF=%.2f >> RANGING PF=%.2f)",
                            trend_pf, range_pf)
            elif pf_gap < -0.5:
                deltas["regime_clarity"] = -2.0

        # --- confidence gate calibration check ---
        cal_with_data = [c for c in confidence_cal if not c.low_confidence]
        if len(cal_with_data) >= 2:
            win_rates = [c.win_rate for c in cal_with_data]
            # Check if win rate is monotonically increasing with confidence
            is_monotonic = all(win_rates[i] <= win_rates[i+1] for i in range(len(win_rates)-1))
            if not is_monotonic:
                logger.warning(
                    "[PA] Confidence calibration is NOT monotonic: %s. "
                    "Recommend reviewing scoring component weights manually.",
                    [(c.band_low, round(c.win_rate * 100, 1)) for c in cal_with_data],
                )
                deltas["_calibration_warning"] = 1.0   # flag for report generator

        return deltas

    # -----------------------------------------------------------------------
    # Database connection
    # -----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.row_factory = sqlite3.Row
        return conn


# ===========================================================================
# Standalone test harness
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile
    import os
    from datetime import datetime, timezone, timedelta

    # We need to import trade_logger to seed a test database
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from learning.trade_logger import TradeLogger, TradeOpenRecord

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("ARCS-FX  --  PatternAnalyzer standalone test")
    print("=" * 60)

    # Create a temporary database with realistic trade data
    tmp_db = os.path.join(tempfile.gettempdir(), "arcs_pa_test.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass

    tl = TradeLogger(db_path=tmp_db)

    # Seed data: 30 trades covering a realistic distribution
    # OB_RETEST in TRENDING is the best setup
    # SD_BOUNCE in RANGING is the worst
    seed_trades = [
        # (id, symbol, direction, signal, pattern, regime, session, conf, entry, sl, tp, r, won, pnl)
        ("S01",  "EURUSD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "OVERLAP", 84, 1.085, 1.082, 1.091, 2.0, True,   18.0),
        ("S02",  "GBPUSD", "SELL", "OB_RETEST", "BEARISH_PIN_BAR",    "TRENDING", "LONDON",  82, 1.270, 1.273, 1.264, 2.0, True,   22.0),
        ("S03",  "USDJPY", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "NY",      79, 149.5, 149.2, 150.1, 2.0, True,   21.0),
        ("S04",  "EURUSD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "OVERLAP", 87, 1.087, 1.084, 1.093, 2.0, True,   17.0),
        ("S05",  "GBPUSD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "LONDON",  76, 1.268, 1.265, 1.274, 2.0, False, -12.0),
        ("S06",  "USDJPY", "SELL", "OB_RETEST", "BEARISH_ENGULFING",  "TRENDING", "NY",      83, 150.0, 150.3, 149.4, 2.0, True,   20.0),
        ("S07",  "EURUSD", "BUY",  "FVG_FILL",  "BULLISH_ENGULFING",  "TRENDING", "OVERLAP", 80, 1.086, 1.083, 1.092, 2.0, True,   16.0),
        ("S08",  "GBPUSD", "SELL", "FVG_FILL",  "BEARISH_PIN_BAR",    "TRENDING", "LONDON",  77, 1.271, 1.274, 1.265, 2.0, True,   19.0),
        ("S09",  "AUDUSD", "BUY",  "FVG_FILL",  "BULLISH_PIN_BAR",    "TRENDING", "NY",      74, 0.641, 0.638, 0.647, 2.0, False, -11.0),
        ("S10",  "USDCHF", "SELL", "FVG_FILL",  "BEARISH_ENGULFING",  "TRENDING", "OVERLAP", 81, 0.890, 0.893, 0.884, 2.0, True,   14.0),
        ("S11",  "EURUSD", "BUY",  "SD_BOUNCE", "BULLISH_ENGULFING",  "RANGING",  "OVERLAP", 74, 1.083, 1.080, 1.089, 2.0, False, -13.0),
        ("S12",  "GBPUSD", "SELL", "SD_BOUNCE", "BEARISH_PIN_BAR",    "RANGING",  "LONDON",  71, 1.274, 1.277, 1.268, 2.0, False, -14.0),
        ("S13",  "AUDUSD", "BUY",  "SD_BOUNCE", "BULLISH_PIN_BAR",    "RANGING",  "ASIAN",   70, 0.639, 0.636, 0.645, 2.0, False, -10.0),
        ("S14",  "NZDUSD", "BUY",  "SD_BOUNCE", "INSIDE_BAR",         "RANGING",  "ASIAN",   72, 0.615, 0.612, 0.621, 2.0, True,    9.0),
        ("S15",  "USDCAD", "SELL", "SD_BOUNCE", "BEARISH_ENGULFING",  "RANGING",  "NY",      73, 1.370, 1.373, 1.364, 2.0, False, -11.0),
        ("S16",  "EURUSD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "OVERLAP", 88, 1.088, 1.085, 1.094, 2.0, True,   19.0),
        ("S17",  "GBPUSD", "BUY",  "OB_RETEST", "BULLISH_PIN_BAR",    "TRENDING", "OVERLAP", 85, 1.267, 1.264, 1.273, 2.0, True,   21.0),
        ("S18",  "USDJPY", "SELL", "FVG_FILL",  "BEARISH_ENGULFING",  "TRENDING", "NY",      78, 150.2, 150.5, 149.6, 2.0, False, -13.0),
        ("S19",  "USDCHF", "BUY",  "SD_BOUNCE", "BULLISH_ENGULFING",  "RANGING",  "LONDON",  71, 0.892, 0.889, 0.898, 2.0, True,   11.0),
        ("S20",  "EURJPY", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "OVERLAP", 83, 162.5, 162.0, 163.5, 2.0, True,   28.0),
        ("S21",  "EURUSD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "OVERLAP", 91, 1.090, 1.087, 1.096, 2.0, True,   18.0),
        ("S22",  "GBPUSD", "SELL", "SD_BOUNCE", "BEARISH_PIN_BAR",    "RANGING",  "LONDON",  70, 1.272, 1.275, 1.266, 2.0, False, -12.0),
        ("S23",  "AUDUSD", "BUY",  "FVG_FILL",  "BULLISH_ENGULFING",  "TRENDING", "NY",      76, 0.643, 0.640, 0.649, 2.0, True,   13.0),
        ("S24",  "USDJPY", "BUY",  "OB_RETEST", "BULLISH_PIN_BAR",    "TRENDING", "NY",      80, 149.8, 149.5, 150.4, 2.0, True,   22.0),
        ("S25",  "NZDUSD", "SELL", "SD_BOUNCE", "BEARISH_ENGULFING",  "RANGING",  "ASIAN",   72, 0.613, 0.616, 0.607, 2.0, False, -10.0),
        ("S26",  "USDCAD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "NY",      78, 1.372, 1.369, 1.378, 2.0, True,   16.0),
        ("S27",  "EURJPY", "SELL", "FVG_FILL",  "BEARISH_ENGULFING",  "TRENDING", "OVERLAP", 82, 163.0, 163.5, 162.0, 2.0, True,   25.0),
        ("S28",  "EURUSD", "BUY",  "SD_BOUNCE", "BULLISH_PIN_BAR",    "RANGING",  "OVERLAP", 73, 1.084, 1.081, 1.090, 2.0, False, -13.0),
        ("S29",  "GBPUSD", "BUY",  "OB_RETEST", "BULLISH_ENGULFING",  "TRENDING", "LONDON",  86, 1.269, 1.266, 1.275, 2.0, True,   20.0),
        ("S30",  "USDJPY", "BUY",  "FVG_FILL",  "BULLISH_ENGULFING",  "TRENDING", "NY",      77, 150.1, 149.8, 150.7, 2.0, False, -12.0),
    ]

    now_utc = datetime.now(timezone.utc)
    print(f"\nSeeding {len(seed_trades)} trades...")

    for i, t in enumerate(seed_trades):
        open_rec = TradeOpenRecord(
            trade_id           = t[0],
            mt5_ticket         = i * 1000,
            symbol             = t[1],
            direction          = t[2],
            entry_price        = t[8],
            sl_price           = t[9],
            initial_sl_price   = t[9],
            tp_price           = t[10],
            lots               = 0.03,
            opened_at_utc      = (now_utc - timedelta(hours=len(seed_trades)-i)).isoformat(),
            signal_type        = t[3],
            pattern            = t[4],
            r_ratio            = t[11],
            confidence         = float(t[7]),
            regime             = t[5],
            session            = t[6],
            risk_pct           = 1.0,
            risk_usd           = 10.0,
            win_rate_at_open   = 0.5,
            daily_loss_at_open = 0.0,
        )
        tl.log_open(open_rec)
        tl.log_close(
            trade_id          = t[0],
            close_price       = t[10] if t[12] else t[9],
            pnl_usd           = t[13],
            won               = t[12],
            close_reason      = "TP_HIT" if t[12] else "SL_HIT",
            breakeven_applied = t[12],
            trailing_active   = t[12] and t[7] >= 82,
        )

    # --- Run analysis ---
    print("\n--- Running full analysis ---")
    pa     = PatternAnalyzer(db_path=tmp_db)
    report = pa.run_full_analysis()

    print(f"\nTotal closed trades: {report.total_closed_trades}")

    print("\n-- By Signal Type --")
    for s in report.by_signal_type:
        flag = " [LOW N]" if s.low_confidence else ""
        print(f"  {s.value:20s} | n={s.total:3d} | "
              f"WR={s.win_rate*100:.0f}% | PF={s.profit_factor:.2f} | "
              f"P&L=${s.total_pnl:.2f}{flag}")

    print("\n-- By Regime --")
    for s in report.by_regime:
        flag = " [LOW N]" if s.low_confidence else ""
        print(f"  {s.value:12s} | n={s.total:3d} | "
              f"WR={s.win_rate*100:.0f}% | PF={s.profit_factor:.2f} | "
              f"P&L=${s.total_pnl:.2f}{flag}")

    print("\n-- By Session --")
    for s in report.by_session:
        flag = " [LOW N]" if s.low_confidence else ""
        print(f"  {s.value:10s} | n={s.total:3d} | "
              f"WR={s.win_rate*100:.0f}% | PF={s.profit_factor:.2f}{flag}")

    print("\n-- Regime x Signal (cross-analysis) --")
    for s in report.regime_x_signal:
        flag = " [LOW N]" if s.low_confidence else ""
        print(f"  {s.val1:10s} x {s.val2:15s} | n={s.total:3d} | "
              f"WR={s.win_rate*100:.0f}% | PF={s.profit_factor:.2f}{flag}")

    print("\n-- Confidence Calibration --")
    for c in report.confidence_calibration:
        flag = " [LOW N]" if c.low_confidence else ""
        print(f"  [{c.band_low}-{c.band_high}]: n={c.total:3d} | "
              f"WR={c.win_rate*100:.0f}% | avg_pnl=${c.avg_pnl:.2f}{flag}")

    print("\n-- Management Stats --")
    for m in report.management_stats:
        print(f"  {m.category:22s} = {str(m.value):5s} | "
              f"n={m.total} | WR={m.win_rate*100:.0f}% | "
              f"avg_pnl=${m.avg_pnl:.2f} | PF={m.profit_factor:.2f}")

    print("\n-- Top Setups --")
    for s in report.top_setups:
        print(f"  {s.value:20s} PF={s.profit_factor:.2f} WR={s.win_rate*100:.0f}%")

    print("\n-- Worst Setups --")
    for s in report.worst_setups:
        print(f"  {s.value:20s} PF={s.profit_factor:.2f} WR={s.win_rate*100:.0f}%")

    print("\n-- Weight Recommendations --")
    for comp, delta in report.weight_recommendations.items():
        print(f"  {comp}: {delta:+.1f}")

    # --- Assertions ---
    assert report.total_closed_trades == 30, "FAILED: expected 30 trades"
    assert len(report.by_signal_type)  >= 3, "FAILED: expected 3+ signal types"
    ob_stat = next(s for s in report.by_signal_type if s.value == "OB_RETEST")
    sd_stat = next(s for s in report.by_signal_type if s.value == "SD_BOUNCE")
    assert ob_stat.profit_factor > sd_stat.profit_factor, \
        f"FAILED: OB_RETEST PF ({ob_stat.profit_factor}) should > SD_BOUNCE PF ({sd_stat.profit_factor})"
    assert ob_stat.win_rate > sd_stat.win_rate, "FAILED: OB win rate should be higher"
    print("\n-> ALL ASSERTIONS PASS")

    # Cleanup
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
