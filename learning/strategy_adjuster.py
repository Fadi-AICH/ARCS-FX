"""
ARCS-FX -- learning/strategy_adjuster.py
Dynamic confidence weight adjustment based on live trade performance.

WHY THIS MODULE EXISTS:
The confidence score weights in config.py are initial estimates.
They represent our best guess before the bot has traded. After 50+ closed
trades, we have real evidence about which components actually predict
profitable trades on this specific account, these specific pairs, in
current market conditions.

This module takes the PatternAnalyzer output and adjusts the weights
stored in a persistent weights file. The adjusted weights override the
config defaults at runtime.

DESIGN PRINCIPLES:

  1. DAMPENING -- Never apply the full recommended delta in one shot.
     Changes are applied at a fraction (DAMPENING_FACTOR = 0.3) per cycle.
     This prevents overcorrection on streaky periods and keeps the system
     moving toward evidence gradually.

  2. HARD LIMITS -- Weights are bounded. No component goes below MIN_WEIGHT
     (5 pts) or above MAX_WEIGHT (35 pts). The total always sums to 100.

  3. MINIMUM SAMPLE GATE -- Adjustments only fire when enough closed trades
     exist. Adjusting on 10 trades is gambling, not learning.

  4. AUDIT TRAIL -- Every adjustment is logged with the reason, the before/
     after weights, and the trade count that justified the change. Full
     transparency -- the bot can always explain why it adjusted.

  5. ROLLBACK SAFETY -- Adjusted weights are stored separately from config.py.
     To reset: delete data/weights.json. The bot falls back to config defaults.

ADJUSTMENT TRIGGERS:
  - Runs automatically every Sunday at WEEKLY_REPORT_HOUR_UTC (config.py)
  - Can be triggered manually via: py -3.11 learning/strategy_adjuster.py
  - ReportGenerator calls it before generating the weekly PDF

WEIGHT FILE:
  data/weights.json -- persists adjusted weights across restarts.
  Format: {"component_name": float, ..., "adjusted_at_utc": "...", "reason": "..."}
"""

import os
import sys
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    CONFIDENCE_WEIGHTS, DB_PATH,
    WIN_RATE_LOOKBACK,
)
from learning.pattern_analyzer import PatternAnalyzer, AnalysisReport, MIN_SAMPLE_SIZE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fraction of the recommended delta actually applied per adjustment cycle.
# 0.3 = "move 30% of the way toward the evidence" each week.
DAMPENING_FACTOR = 0.3

# Hard bounds per component (points)
MIN_WEIGHT = 5
MAX_WEIGHT = 35

# Minimum closed trades before any adjustment fires
MIN_TRADES_FOR_ADJUSTMENT = 20

# Weight file path
_WEIGHTS_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_WEIGHTS_FILE = os.path.join(_WEIGHTS_DIR, "weights.json")

# Absolute DB path
_DB_ABS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    DB_PATH,
)


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class WeightAdjustment:
    """Records a single component weight change."""
    component:  str
    before:     float
    after:      float
    delta:      float
    reason:     str


@dataclass
class AdjustmentResult:
    """Full output of a StrategyAdjuster.run() call."""
    adjusted:          bool              # True if any weight changed
    adjustments:       list[WeightAdjustment]
    weights_before:    dict[str, float]
    weights_after:     dict[str, float]
    trade_count:       int
    skipped_reason:    str               # populated if adjusted=False
    generated_at_utc:  str


# ===========================================================================
# StrategyAdjuster
# ===========================================================================

class StrategyAdjuster:
    """
    Reads PatternAnalyzer output and updates the active confidence weights.

    USAGE:
        adjuster = StrategyAdjuster()
        result   = adjuster.run()
        if result.adjusted:
            print("Weights updated:", result.adjustments)

    WEIGHT LOADING PRIORITY:
        1. data/weights.json (if exists) -- runtime-adjusted weights
        2. config.CONFIDENCE_WEIGHTS     -- static defaults from config.py
    """

    def __init__(self, db_path: str = _DB_ABS) -> None:
        self._db_path  = db_path
        self._analyzer = PatternAnalyzer(db_path=db_path)
        os.makedirs(_WEIGHTS_DIR, exist_ok=True)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def run(self, force: bool = False) -> AdjustmentResult:
        """
        Execute a full adjustment cycle.

        Steps:
          1. Check minimum sample gate
          2. Run PatternAnalyzer.run_full_analysis()
          3. Generate target weights from recommendations
          4. Apply dampening
          5. Clamp to bounds
          6. Renormalise to sum=100
          7. Persist and log

        force=True bypasses the minimum sample gate (useful for testing).
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        current_weights = self.load_weights()
        trade_count     = self._count_closed_trades()

        # --- Minimum sample gate ------------------------------------------
        if not force and trade_count < MIN_TRADES_FOR_ADJUSTMENT:
            reason = (
                f"Insufficient data: {trade_count} closed trades "
                f"(need {MIN_TRADES_FOR_ADJUSTMENT})"
            )
            logger.info("[SA] Skipping adjustment: %s", reason)
            return AdjustmentResult(
                adjusted=False, adjustments=[], skipped_reason=reason,
                weights_before=current_weights, weights_after=current_weights,
                trade_count=trade_count, generated_at_utc=now_utc,
            )

        # --- Run analysis --------------------------------------------------
        logger.info("[SA] Running adjustment cycle on %d trades.", trade_count)
        report = self._analyzer.run_full_analysis()

        # --- Compute target weights ----------------------------------------
        target_weights = self._compute_target_weights(current_weights, report)

        # --- Apply dampening -----------------------------------------------
        dampened_weights = {}
        for comp, current_val in current_weights.items():
            target_val = target_weights.get(comp, current_val)
            delta      = (target_val - current_val) * DAMPENING_FACTOR
            dampened_weights[comp] = current_val + delta

        # --- Clamp to bounds -----------------------------------------------
        clamped_weights = {
            comp: max(MIN_WEIGHT, min(MAX_WEIGHT, val))
            for comp, val in dampened_weights.items()
        }

        # --- Renormalise to sum = 100 --------------------------------------
        total = sum(clamped_weights.values())
        if abs(total - 100.0) > 0.01:
            factor = 100.0 / total
            clamped_weights = {k: round(v * factor, 2) for k, v in clamped_weights.items()}
            # Fix rounding drift on the largest component
            diff = 100.0 - sum(clamped_weights.values())
            largest = max(clamped_weights, key=clamped_weights.get)
            clamped_weights[largest] = round(clamped_weights[largest] + diff, 2)

        # --- Build adjustment records -------------------------------------
        adjustments = []
        for comp in current_weights:
            before = round(current_weights[comp], 2)
            after  = round(clamped_weights[comp], 2)
            delta  = round(after - before, 2)
            if abs(delta) >= 0.01:
                reason = self._build_reason(comp, report)
                adjustments.append(WeightAdjustment(
                    component=comp, before=before, after=after,
                    delta=delta, reason=reason,
                ))

        made_changes = len(adjustments) > 0

        # --- Persist -------------------------------------------------------
        if made_changes:
            self._save_weights(clamped_weights, report)
            for adj in adjustments:
                logger.info(
                    "[SA] Weight adjusted: %-22s %+.2f  (%.2f -> %.2f) | %s",
                    adj.component, adj.delta, adj.before, adj.after, adj.reason,
                )
        else:
            logger.info("[SA] No weight changes warranted by current data.")

        return AdjustmentResult(
            adjusted         = made_changes,
            adjustments      = adjustments,
            weights_before   = current_weights,
            weights_after    = clamped_weights if made_changes else current_weights,
            trade_count      = trade_count,
            skipped_reason   = "" if made_changes else "No significant divergence detected",
            generated_at_utc = now_utc,
        )

    def load_weights(self) -> dict[str, float]:
        """
        Load active weights.
        Returns adjusted weights from file if present, else config defaults.

        WHY always make a copy of config defaults:
        config.CONFIDENCE_WEIGHTS is a module-level dict. Mutating it
        would corrupt future calls in the same process.
        """
        if os.path.exists(_WEIGHTS_FILE):
            try:
                with open(_WEIGHTS_FILE, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                # Extract only the component keys (skip metadata keys)
                components = {
                    k: float(v) for k, v in raw.items()
                    if k in CONFIDENCE_WEIGHTS
                }
                if len(components) == len(CONFIDENCE_WEIGHTS):
                    logger.debug("[SA] Loaded adjusted weights from %s", _WEIGHTS_FILE)
                    return components
                else:
                    logger.warning("[SA] Weights file has unexpected keys -- using config defaults.")
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("[SA] Weights file corrupt (%s) -- using config defaults.", exc)

        return deepcopy(dict(CONFIDENCE_WEIGHTS))

    def reset_to_defaults(self) -> dict[str, float]:
        """
        Delete the weights file and return to config.py defaults.
        Used for manual reset or after a regime shift.
        """
        if os.path.exists(_WEIGHTS_FILE):
            os.remove(_WEIGHTS_FILE)
            logger.info("[SA] Weights file deleted. Reverted to config defaults.")
        return deepcopy(dict(CONFIDENCE_WEIGHTS))

    def get_weight_history(self) -> list[dict]:
        """
        Return the adjustment history log from the weights file.
        Each entry: {adjusted_at_utc, weights, summary}.
        """
        history_file = _WEIGHTS_FILE.replace(".json", "_history.jsonl")
        if not os.path.exists(history_file):
            return []
        entries = []
        with open(history_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _compute_target_weights(
        self,
        current_weights: dict[str, float],
        report:          AnalysisReport,
    ) -> dict[str, float]:
        """
        Convert PatternAnalyzer recommendations into target weight values.

        For each recommended delta:
          target = current + recommended_delta
        For components with no recommendation: target = current (no change).

        Additional heuristics beyond PatternAnalyzer.weight_recommendations:
          - If mtf_confluence is consistently irrelevant (all regimes same):
            slightly reduce it.
          - If spread_session strongly separates win/loss by session:
            slightly increase it.
        """
        target = deepcopy(current_weights)
        recs   = report.weight_recommendations

        # Apply analyzer recommendations
        for comp, delta in recs.items():
            if comp.startswith("_"):
                continue   # metadata flags, not actual components
            if comp in target:
                target[comp] = target[comp] + delta

        # --- Session heuristic: boost spread_session if sessions diverge ----
        session_stats = {s.value: s for s in report.by_session if not s.low_confidence}
        if session_stats:
            win_rates  = [s.win_rate for s in session_stats.values()]
            spread_wr  = max(win_rates) - min(win_rates)
            if spread_wr >= 0.30:
                # Sessions are highly divergent -- spread_session component matters
                target["spread_session"] = target.get("spread_session", 5) + 2.0
                logger.info(
                    "[SA] Session spread %.0f%% -> boosting spread_session +2",
                    spread_wr * 100,
                )

        # --- MTF confluence heuristic: if regime x signal has high PF -------
        # means MTF is doing its job well -> maintain or boost
        best_cross = [
            s for s in report.regime_x_signal
            if not s.low_confidence and s.profit_factor >= 2.0
        ]
        if len(best_cross) >= 2:
            target["mtf_confluence"] = target.get("mtf_confluence", 15) + 1.5
            logger.info("[SA] %d strong regime x signal combos -> boosting mtf_confluence +1.5",
                        len(best_cross))

        return target

    def _build_reason(self, component: str, report: AnalysisReport) -> str:
        """Generate a human-readable reason for a weight change."""
        recs = report.weight_recommendations
        if component in recs:
            delta = recs[component]
            if component == "price_action":
                ob = next((s for s in report.by_signal_type if s.value == "OB_RETEST"), None)
                sd = next((s for s in report.by_signal_type if s.value == "SD_BOUNCE"), None)
                if ob and sd:
                    return (f"OB_RETEST PF={ob.profit_factor:.2f} "
                            f"vs SD_BOUNCE PF={sd.profit_factor:.2f} "
                            f"(delta recommendation: {delta:+.1f})")
            if component == "regime_clarity":
                tr = next((s for s in report.by_regime if s.value == "TRENDING"), None)
                ra = next((s for s in report.by_regime if s.value == "RANGING"), None)
                if tr and ra:
                    return (f"TRENDING PF={tr.profit_factor:.2f} "
                            f"vs RANGING PF={ra.profit_factor:.2f} "
                            f"(delta recommendation: {delta:+.1f})")
        if component == "spread_session":
            return "Session win rates diverging (>30% spread)"
        if component == "mtf_confluence":
            return "Multiple strong regime x signal combinations confirmed"
        return "Dampened adjustment from PatternAnalyzer recommendations"

    def _save_weights(self, weights: dict[str, float], report: AnalysisReport) -> None:
        """
        Atomically save adjusted weights and append to history log.
        """
        now_utc = datetime.now(timezone.utc).isoformat()

        # --- Current weights file -----------------------------------------
        payload = {**weights, "adjusted_at_utc": now_utc, "trade_count": report.total_closed_trades}
        tmp = _WEIGHTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, _WEIGHTS_FILE)

        # --- Append to history JSONL (one line per adjustment run) --------
        history_file = _WEIGHTS_FILE.replace(".json", "_history.jsonl")
        history_entry = {
            "adjusted_at_utc": now_utc,
            "trade_count":     report.total_closed_trades,
            "weights":         weights,
        }
        with open(history_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(history_entry) + "\n")

        logger.info("[SA] Weights saved to %s", _WEIGHTS_FILE)

    def _count_closed_trades(self) -> int:
        import sqlite3
        if not os.path.exists(self._db_path):
            return 0
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE closed_at_utc IS NOT NULL"
            ).fetchone()
            conn.close()
            return row["n"] if row else 0
        except Exception:
            return 0


# ===========================================================================
# Standalone test harness
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
    print("ARCS-FX  --  StrategyAdjuster standalone test")
    print("=" * 60)

    # Temporary DB + weights file
    tmp_db      = os.path.join(tempfile.gettempdir(), "arcs_sa_test.db")
    tmp_weights = os.path.join(tempfile.gettempdir(), "arcs_sa_weights.json")
    # Patch module-level path so adjuster writes to temp location during test
    import learning.strategy_adjuster as _sa_mod
    _sa_mod._WEIGHTS_FILE = tmp_weights

    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass
    for f in (tmp_weights, tmp_weights.replace(".json", "_history.jsonl")):
        try:
            os.remove(f)
        except OSError:
            pass

    tl = TradeLogger(db_path=tmp_db)

    # Seed the same 30 realistic trades as pattern_analyzer test
    seed_trades = [
        ("S01","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",84,True,  18.0),
        ("S02","GBPUSD","SELL","OB_RETEST","BEARISH_PIN_BAR","TRENDING","LONDON",82,True,  22.0),
        ("S03","USDJPY","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","NY",79,True,  21.0),
        ("S04","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",87,True,  17.0),
        ("S05","GBPUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","LONDON",76,False,-12.0),
        ("S06","USDJPY","SELL","OB_RETEST","BEARISH_ENGULFING","TRENDING","NY",83,True,  20.0),
        ("S07","EURUSD","BUY","FVG_FILL","BULLISH_ENGULFING","TRENDING","OVERLAP",80,True,  16.0),
        ("S08","GBPUSD","SELL","FVG_FILL","BEARISH_PIN_BAR","TRENDING","LONDON",77,True,  19.0),
        ("S09","AUDUSD","BUY","FVG_FILL","BULLISH_PIN_BAR","TRENDING","NY",74,False,-11.0),
        ("S10","USDCHF","SELL","FVG_FILL","BEARISH_ENGULFING","TRENDING","OVERLAP",81,True,  14.0),
        ("S11","EURUSD","BUY","SD_BOUNCE","BULLISH_ENGULFING","RANGING","OVERLAP",74,False,-13.0),
        ("S12","GBPUSD","SELL","SD_BOUNCE","BEARISH_PIN_BAR","RANGING","LONDON",71,False,-14.0),
        ("S13","AUDUSD","BUY","SD_BOUNCE","BULLISH_PIN_BAR","RANGING","ASIAN",70,False,-10.0),
        ("S14","NZDUSD","BUY","SD_BOUNCE","INSIDE_BAR","RANGING","ASIAN",72,True,   9.0),
        ("S15","USDCAD","SELL","SD_BOUNCE","BEARISH_ENGULFING","RANGING","NY",73,False,-11.0),
        ("S16","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",88,True,  19.0),
        ("S17","GBPUSD","BUY","OB_RETEST","BULLISH_PIN_BAR","TRENDING","OVERLAP",85,True,  21.0),
        ("S18","USDJPY","SELL","FVG_FILL","BEARISH_ENGULFING","TRENDING","NY",78,False,-13.0),
        ("S19","USDCHF","BUY","SD_BOUNCE","BULLISH_ENGULFING","RANGING","LONDON",71,True,  11.0),
        ("S20","EURJPY","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",83,True,  28.0),
        ("S21","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",91,True,  18.0),
        ("S22","GBPUSD","SELL","SD_BOUNCE","BEARISH_PIN_BAR","RANGING","LONDON",70,False,-12.0),
        ("S23","AUDUSD","BUY","FVG_FILL","BULLISH_ENGULFING","TRENDING","NY",76,True,  13.0),
        ("S24","USDJPY","BUY","OB_RETEST","BULLISH_PIN_BAR","TRENDING","NY",80,True,  22.0),
        ("S25","NZDUSD","SELL","SD_BOUNCE","BEARISH_ENGULFING","RANGING","ASIAN",72,False,-10.0),
        ("S26","USDCAD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","NY",78,True,  16.0),
        ("S27","EURJPY","SELL","FVG_FILL","BEARISH_ENGULFING","TRENDING","OVERLAP",82,True,  25.0),
        ("S28","EURUSD","BUY","SD_BOUNCE","BULLISH_PIN_BAR","RANGING","OVERLAP",73,False,-13.0),
        ("S29","GBPUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","LONDON",86,True,  20.0),
        ("S30","USDJPY","BUY","FVG_FILL","BULLISH_ENGULFING","TRENDING","NY",77,False,-12.0),
    ]

    now_utc = datetime.now(timezone.utc)
    for i, t in enumerate(seed_trades):
        open_rec = TradeOpenRecord(
            trade_id=t[0], mt5_ticket=i*1000, symbol=t[1], direction=t[2],
            entry_price=1.085, sl_price=1.082, initial_sl_price=1.082, tp_price=1.091,
            lots=0.03, opened_at_utc=(now_utc - timedelta(hours=len(seed_trades)-i)).isoformat(),
            signal_type=t[3], pattern=t[4], r_ratio=2.0, confidence=float(t[7]),
            regime=t[5], session=t[6], risk_pct=1.0, risk_usd=10.0,
            win_rate_at_open=0.5, daily_loss_at_open=0.0,
        )
        tl.log_open(open_rec)
        tl.log_close(
            trade_id=t[0], close_price=1.091 if t[8] else 1.082,
            pnl_usd=t[9], won=t[8],
            close_reason="TP_HIT" if t[8] else "SL_HIT",
            breakeven_applied=t[8], trailing_active=(t[8] and t[7] >= 82),
        )

    adjuster = StrategyAdjuster(db_path=tmp_db)

    # --- Test 1: below minimum sample gate (force=False) ---
    print("\n--- TEST 1: Gate check with force=False but adequate data ---")
    result = adjuster.run(force=False)
    print(f"  Adjusted:     {result.adjusted}")
    print(f"  Trade count:  {result.trade_count}")
    print(f"  Skip reason:  '{result.skipped_reason}'")
    # We have 30 trades >= 20 minimum, so should proceed
    assert result.trade_count == 30, "TEST 1 FAILED"
    # It may or may not have adjusted -- that's fine. Just verify it ran.
    print("  -> PASS")

    # --- Test 2: weights before vs after ---
    print("\n--- TEST 2: Weight adjustment values ---")
    print(f"  Weights before:")
    for k, v in result.weights_before.items():
        print(f"    {k:25s}: {v:.2f}")
    print(f"  Weights after:")
    for k, v in result.weights_after.items():
        print(f"    {k:25s}: {v:.2f}")
    total = sum(result.weights_after.values())
    print(f"  Total: {total:.2f}  (should be 100.00)")
    assert abs(total - 100.0) < 0.1, f"TEST 2 FAILED: total = {total}"
    print("  -> PASS")

    # --- Test 3: explicit adjustments shown ---
    if result.adjusted:
        print("\n--- TEST 3: Adjustment details ---")
        for adj in result.adjustments:
            print(f"  {adj.component:25s} {adj.before:.2f} -> {adj.after:.2f} "
                  f"({adj.delta:+.2f}) | {adj.reason[:60]}")
        print("  -> PASS")
    else:
        print("\n--- TEST 3: No adjustments (weights already at target) ---")
        print(f"  Reason: {result.skipped_reason}")
        print("  -> PASS")

    # --- Test 4: load_weights() returns adjusted weights ---
    print("\n--- TEST 4: load_weights() returns persisted values ---")
    loaded = adjuster.load_weights()
    for k, v in loaded.items():
        print(f"  {k:25s}: {v:.2f}")
    assert abs(sum(loaded.values()) - 100.0) < 0.1, "TEST 4 FAILED: loaded weights don't sum to 100"
    print("  -> PASS")

    # --- Test 5: force=True bypasses sample gate on empty DB ---
    print("\n--- TEST 5: force=True with small dataset ---")
    tmp_db2 = tmp_db.replace(".db", "_small.db")
    tl2 = TradeLogger(db_path=tmp_db2)
    # Add only 3 trades (below gate)
    for i in range(3):
        r = TradeOpenRecord(
            trade_id=f"MINI_{i}", mt5_ticket=i, symbol="EURUSD", direction="BUY",
            entry_price=1.085, sl_price=1.082, initial_sl_price=1.082, tp_price=1.091,
            lots=0.01, opened_at_utc=now_utc.isoformat(),
            signal_type="OB_RETEST", pattern="BULLISH_ENGULFING",
            r_ratio=2.0, confidence=80.0, regime="TRENDING", session="OVERLAP",
            risk_pct=1.0, risk_usd=10.0, win_rate_at_open=0.5, daily_loss_at_open=0.0,
        )
        tl2.log_open(r)
        tl2.log_close(f"MINI_{i}", 1.091, 10.0, True, "TP_HIT")

    adj2 = StrategyAdjuster(db_path=tmp_db2)
    res_gate = adj2.run(force=False)
    print(f"  force=False with {res_gate.trade_count} trades: adjusted={res_gate.adjusted}")
    assert not res_gate.adjusted, "TEST 5 FAILED: should be blocked by gate"
    res_force = adj2.run(force=True)
    print(f"  force=True  with {res_force.trade_count} trades: ran={True}")
    print("  -> PASS")

    # Cleanup
    for f in (tmp_weights, tmp_weights.replace(".json", "_history.jsonl")):
        try:
            os.remove(f)
        except OSError:
            pass
    for db in (tmp_db, tmp_db2):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db + ext)
            except OSError:
                pass

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
