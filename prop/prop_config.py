"""
ARCS-PROP — prop_config.py
Single source of truth for ALL prop-challenge parameters.

Rule: this file governs ARCS-PROP ONLY. main.py continues to use the
project-root config.py. Never cross-import constants between the two —
they are intentionally separate so tuning one doesn't affect the other.

FundedNext Stellar Lite $10k reference (2026-04):
  Phase 1 target: +8%, Phase 2 target: +5%
  Daily loss cap: 5% of day-start balance
  Max drawdown:   10% from peak (trailing)
  No time limit, no consistency rule, EAs allowed
"""

from typing import Set

# ---------------------------------------------------------
# CHALLENGE IDENTITY
# ---------------------------------------------------------
CHALLENGE_FIRM       = "FundedNext Stellar Lite"   # display-only
CHALLENGE_ACCOUNT_USD = 10_000.0                   # starting balance

# Phases
PHASE_1 = "PHASE_1"
PHASE_2 = "PHASE_2"
PHASE_PASSED = "PASSED"
PHASE_HALTED = "HALTED"

# ---------------------------------------------------------
# TARGETS (firm-mandated, do not modify lightly)
# ---------------------------------------------------------
PHASE_1_TARGET_PCT   = 8.0     # +8% from start-of-phase balance
PHASE_2_TARGET_PCT   = 5.0     # +5% from start-of-phase balance

# ---------------------------------------------------------
# FIRM CAPS (the walls — must never be touched)
# ---------------------------------------------------------
FIRM_DAILY_LOSS_PCT  = 5.0     # FundedNext daily loss cap
FIRM_MAX_DRAWDOWN_PCT = 10.0   # FundedNext max DD cap

# ---------------------------------------------------------
# ARCS-PROP INTERNAL CAPS (below firm caps — this is our buffer)
# ---------------------------------------------------------
INTERNAL_DAILY_LOSS_PCT   = 2.5    # bot-internal: flatten + pause at -2.5%
INTERNAL_MAX_DRAWDOWN_PCT = 6.0    # bot-internal: flatten + HALT at -6% from peak

INTERNAL_MAX_CONCURRENT_POSITIONS = 2
INTERNAL_MAX_DAILY_TRADES         = 4    # Phase 1
INTERNAL_MAX_DAILY_TRADES_P2      = 3    # Phase 2 (tighter)

# Daily-win lock: if we hit +3% before 15:00 UTC → flatten + pause to protect gain
DAILY_WIN_LOCK_PCT      = 3.0
DAILY_WIN_LOCK_HOUR_UTC = 15

# Consecutive-loss cooldown
MAX_CONSECUTIVE_LOSSES      = 2      # after 2 straight SL hits → pause
CONSECUTIVE_LOSS_COOLDOWN_H = 4      # hours of pause

# Post-loss no-revenge window
POST_LOSS_COOLDOWN_MIN = 60          # no new trade for 60 min after any SL hit

# ---------------------------------------------------------
# DYNAMIC RISK TIERS
# ---------------------------------------------------------
# Growth tiers: as equity approaches the target, shrink risk to lock gains.
# Each entry: (gain_pct_threshold, risk_pct).  Checked in descending order.
RISK_GROWTH_TIERS = [
    (6.0, 0.30),   # at +6% → risk 0.3% per trade
    (4.0, 0.50),   # at +4% → risk 0.5%
    (2.0, 0.75),   # at +2% → risk 0.75%
]
RISK_DEFAULT_PCT = 1.00        # Phase 1 base risk
RISK_DEFAULT_PCT_P2 = 0.50     # Phase 2 base risk (target lower → can risk less)

# Recovery tiers: if equity is below peak, shrink risk further.
# Each entry: (from_peak_pct_threshold_NEGATIVE, risk_pct).  Descending check.
RISK_RECOVERY_TIERS = [
    (-4.5, None),    # None → PAUSE 24h
    (-3.5, 0.25),
    (-2.0, 0.50),
]
RECOVERY_PAUSE_HOURS = 24

# ---------------------------------------------------------
# ENTRY FILTERS (A+ only — stricter than main.py)
# ---------------------------------------------------------
# Regime whitelist (H1)
ALLOWED_REGIMES: Set[str] = {"RANGING_CLEAN", "TRENDING_CLEAN"}

# Confidence floor for prop challenge (main.py uses 72; we want fewer, higher-quality)
PROP_CONFIDENCE_MIN = 78.0

# Session windows (UTC hours, half-open [start, end))
SESSION_WINDOWS = [
    (7,  10),    # London open
    (13, 16),    # NY open (includes overlap)
]

# Signal type whitelist
ALLOWED_SIGNAL_TYPES: Set[str] = {
    "SWING_REVERSION",        # proven 67% WR in main-bot forensics
}
ALLOWED_SIGNAL_TYPES_WITH_GATE: Set[str] = {
    "SWING_BREAKOUT",         # allowed only in TRENDING_CLEAN with ADX in band
}
SWING_BREAKOUT_ADX_MIN = 20.0
SWING_BREAKOUT_ADX_MAX = 35.0

# R:R minimum
MIN_RR_RATIO = 2.0

# Symbol whitelist (no crypto during challenge — slippage risk too high)
PROP_SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
]

# News blackout
NEWS_BLACKOUT_BEFORE_MIN = 60
NEWS_BLACKOUT_AFTER_MIN  = 60
HIGH_IMPACT_KEYWORDS = [
    "NFP", "Non-Farm", "Payrolls",
    "FOMC", "Fed Funds", "Federal Reserve", "Powell",
    "CPI", "Inflation",
    "ECB", "Lagarde",
    "Interest Rate",
    "Employment", "Unemployment",
]

# ---------------------------------------------------------
# TRADE MANAGEMENT
# ---------------------------------------------------------
SL_ATR_MULT        = 1.0     # SL = 1×ATR(14) beyond structure (driven by PA engine already)
TP_ATR_MULT        = 2.0     # TP = 2×ATR(14) minimum → 2R target
BREAKEVEN_TRIGGER_R = 1.0    # move SL to BE at +1.0R
PARTIAL_CLOSE_R     = 1.5    # close 50% at +1.5R
PARTIAL_CLOSE_FRAC  = 0.50
TRAIL_TRIGGER_R     = 1.5    # start chandelier trail after partial (inherited from main)

# Time-stop: if +0.5R not reached in X min → close
TIME_STOP_SWING_MIN      = 240   # 4h for SWING_* types
TIME_STOP_SCALP_MIN      = 60    # 1h for any SCALP_* (not expected to fire in prop mode)
TIME_STOP_PROGRESS_R     = 0.5

# ---------------------------------------------------------
# SCHEDULER
# ---------------------------------------------------------
MAIN_LOOP_INTERVAL_S     = 60
DATA_REFRESH_INTERVAL_S  = 300
EQUITY_POLL_INTERVAL_S   = 5     # watchdog poll (equity reads are cheap, make them frequent)

# ---------------------------------------------------------
# IDENTITY / STORAGE
# ---------------------------------------------------------
# Distinct magic number from main bot (main uses 20260411).
# Orders tagged with this are managed by ARCS-PROP ONLY.
PROP_MAGIC = 20260418

DASHBOARD_PORT = 5001

# Filesystem paths
PROP_STATE_PATH    = "prop/prop_state.json"
PROP_DB_PATH       = "data/prop_trades.db"
PROP_STATUS_PATH   = "data/prop_status.json"
PROP_LOG_PATH      = "logs/prop/prop_fx.log"
PROP_EVENTS_PATH   = "logs/prop/prop_events.log"
CHALLENGE_STOP_FLAG = "CHALLENGE_STOP"   # presence of this file halts the bot
