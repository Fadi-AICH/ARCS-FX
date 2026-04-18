"""
ARCS-FX — Adaptive Regime-Conditioned Scalping for Forex
config.py — Single source of truth for ALL bot parameters.

Rule: Never scatter magic numbers in code. Every tunable
value lives here so we can adjust behaviour without
touching logic files.
"""

# ---------------------------------------------------------
# MT5 CONNECTION
# ---------------------------------------------------------
MT5_TIMEOUT_MS = 10_000          # ms to wait for MT5 to respond

# ---------------------------------------------------------
# TRADING PAIRS
# ---------------------------------------------------------
FX_PAIRS = [
    "EURUSD", "GBPUSD", "AUDUSD",
    "USDJPY", "USDCHF", "USDCAD",
    "NZDUSD", "EURJPY",
]
PAIRS = FX_PAIRS

# ---------------------------------------------------------
# TIMEFRAMES  (MetaTrader5 constants are imported at use)
# ---------------------------------------------------------
# H1 -> bias/structure | M15 -> setup | M5 -> entry trigger
TF_H1  = "H1"
TF_M15 = "M15"
TF_M5  = "M5"

CANDLES_H1  = 400    # bars to fetch for H1 analysis (ATR percentile needs 356+ rows)
CANDLES_M15 = 400    # bars to fetch for M15 analysis
CANDLES_M5  = 100    # bars to fetch for M5 trigger check

# ---------------------------------------------------------
# REGIME DETECTION
# ---------------------------------------------------------
ADX_PERIOD          = 14
ADX_TREND_THRESHOLD = 25      # ADX > 25 -> trending
ADX_RANGE_THRESHOLD = 20      # ADX < 20 -> ranging

ATR_PERIOD          = 14
ATR_LOOKBACK_DAYS   = 14      # days used to compute ATR percentile
ATR_LOW_PCT         = 30      # below this -> too quiet, no trade
ATR_HIGH_PCT        = 70      # above this -> too chaotic, no trade
ATR_CHAOS_PCT       = 90      # ≥ this -> chaotic regime, full idle

BB_PERIOD           = 20
BB_STD              = 2.0

# Regime labels (used throughout the codebase)
REGIME_TRENDING_CLEAN    = "TRENDING_CLEAN"
REGIME_TRENDING_EXTENDED = "TRENDING_EXTENDED"
REGIME_RANGING_CLEAN     = "RANGING_CLEAN"
REGIME_QUIET             = "QUIET"
REGIME_CHAOTIC           = "CHAOTIC"

TRENDING_REGIMES = {REGIME_TRENDING_CLEAN, REGIME_TRENDING_EXTENDED}
RANGING_REGIMES  = {REGIME_RANGING_CLEAN}
TRADEABLE_REGIMES = TRENDING_REGIMES | RANGING_REGIMES

# ---------------------------------------------------------
# SPREAD FILTER  (in pips)
# ---------------------------------------------------------
SPREAD_LIMITS = {
    "EURUSD": 1.0,
    "GBPUSD": 1.5,
    "AUDUSD": 1.5,
    "USDJPY": 1.5,
    "USDCHF": 2.0,
    "USDCAD": 2.0,
    "NZDUSD": 2.0,
    "EURJPY": 2.0,
}
SPREAD_DEFAULT_LIMIT = 2.0     # fallback for unlisted pairs

# ---------------------------------------------------------
# SESSION WINDOWS  (UTC hours, inclusive)
# ---------------------------------------------------------
SESSIONS = {
    "ASIAN":   (0,  8),
    "LONDON":  (7,  16),
    "NY":      (13, 21),
    "OVERLAP": (13, 17),   # London / NY overlap — highest priority
}
PREFERRED_SESSION = "OVERLAP"
SCALP_CORE_SESSIONS = {"LONDON", "NY", "OVERLAP"}
SCALP_ASIAN_SYMBOLS = {"USDJPY", "EURJPY", "AUDUSD", "NZDUSD"}
SCALP_ASIAN_SESSIONS = {"ASIAN"}
MEAN_REVERSION_CORE_SESSIONS = {"LONDON", "NY", "OVERLAP"}
MEAN_REVERSION_ASIAN_SYMBOLS = {"USDJPY", "EURJPY", "AUDUSD", "NZDUSD"}

# ---------------------------------------------------------
# INSTRUMENT PROFILES
# ---------------------------------------------------------
SYMBOL_PROFILES = {
    "EURUSD": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["EURUSD"],
        "round_level_step": 0.0050,
        "min_stop_units": 10.0,
    },
    "GBPUSD": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["GBPUSD"],
        "round_level_step": 0.0050,
        "min_stop_units": 10.0,
    },
    "AUDUSD": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["AUDUSD"],
        "round_level_step": 0.0050,
        "min_stop_units": 10.0,
    },
    "USDJPY": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["USDJPY"],
        "round_level_step": 0.50,
        "min_stop_units": 10.0,
    },
    "USDCHF": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["USDCHF"],
        "round_level_step": 0.0050,
        "min_stop_units": 10.0,
    },
    "USDCAD": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["USDCAD"],
        "round_level_step": 0.0050,
        "min_stop_units": 10.0,
    },
    "NZDUSD": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["NZDUSD"],
        "round_level_step": 0.0050,
        "min_stop_units": 10.0,
    },
    "EURJPY": {
        "asset_class": "forex",
        "session_mode": "fx",
        "news_mode": "forex_macro",
        "spread_limit": SPREAD_LIMITS["EURJPY"],
        "round_level_step": 0.50,
        "min_stop_units": 10.0,
    },
}

# ---------------------------------------------------------
# CONFIDENCE SCORE GATING
# ---------------------------------------------------------
CONFIDENCE_MIN          = 72    # lowered from 76 after 2026-04-18 dry run (best signal only 69.5 under 76 floor)
CONFIDENCE_EARLY_MODE   = 76    # optional stricter threshold during cautious live-testing
LIVE_EARLY_MODE         = False # Phase 1 rebuild default: use normal mode until analytics stabilise

# Component weights (must sum to 100)
CONFIDENCE_WEIGHTS = {
    "regime_clarity":    25,
    "price_action":      20,
    "mtf_confluence":    15,
    "news_sentiment":    15,
    "key_level":         10,
    "volatility_pct":    10,
    "spread_session":     5,
}

# ---------------------------------------------------------
# NEWS ENGINE
# ---------------------------------------------------------
NEWS_PAUSE_BEFORE_MIN  = 30    # minutes before high-impact event -> idle
NEWS_PAUSE_AFTER_MIN   = 30    # minutes after high-impact event -> idle
HIGH_IMPACT_KEYWORDS   = [
    "NFP", "Non-Farm", "FOMC", "Federal Reserve",
    "CPI", "Inflation", "GDP", "Interest Rate",
    "Employment", "Unemployment",
]
FINBERT_MODEL          = "ProsusAI/finbert"  # HuggingFace model ID
NEWS_SENTIMENT_NEUTRAL_BAND = 0.15           # |score| < this -> treat as neutral

# ForexFactory calendar URL (scraped for event schedule)
FOREXFACTORY_URL = "https://www.forexfactory.com/calendar"

# ---------------------------------------------------------
# RISK ENGINE
# ---------------------------------------------------------
RISK_BASE_PCT          = 1.0   # % of account per trade (default)
RISK_MAX_PCT           = 2.0   # hard ceiling, never exceeded
RISK_HIGH_PCT          = 1.5   # when win rate > WIN_RATE_SCALE_UP
RISK_LOW_PCT           = 0.5   # when win rate < WIN_RATE_SCALE_DOWN

WIN_RATE_LOOKBACK      = 20    # rolling trade window for win-rate calc
WIN_RATE_SCALE_UP      = 0.60  # 60 %+ win rate -> size up
WIN_RATE_SCALE_DOWN    = 0.40  # below 40 % win rate -> size down

MAX_OPEN_TRADES        = 3     # correlation guard hard cap
CORRELATION_THRESHOLD  = 0.7   # skip new trade if correlated above this

# Circuit breakers
DAILY_LOSS_LIMIT_PCT   = 3.0   # % drawdown -> stop for the day
WEEKLY_LOSS_LIMIT_PCT  = 6.0   # % drawdown -> stop for the week
MAX_CONSECUTIVE_LOSSES = 3     # -> 2-hour smart-idle cooldown
CONSECUTIVE_LOSS_COOLDOWN_H = 2  # hours

# ---------------------------------------------------------
# TRADE MANAGEMENT  (asymmetric R-multiple rules)
# ---------------------------------------------------------
BREAKEVEN_TRIGGER_R    = 1.0   # move SL to BE after +1R
TRAIL_TRIGGER_R        = 2.0   # start trailing after +2R
TRAIL_RATIO            = 0.50  # trail at 50 % of remaining move

# ---------------------------------------------------------
# PRICE ACTION ENGINE
# ---------------------------------------------------------
OB_LOOKBACK            = 50    # candles to look back for order blocks
FVG_MIN_BODY_RATIO     = 0.5   # minimum body/range ratio for FVG candle
STRUCTURE_LOOKBACK     = 30    # candles for HH/HL/LH/LL detection
LIQUIDITY_SWEEP_BUFFER = 0.0002  # price buffer for sweep detection (pips eq.)
PATTERN_LOOKBACK       = 5     # candles window for PA pattern detection

# ---------------------------------------------------------
# LEARNING / LOGGING
# ---------------------------------------------------------
DB_PATH                = "data/trades.db"
LOG_DIR                = "logs/"
WEEKLY_REPORT_DAY      = 6     # 0=Mon ... 6=Sun -> generate report on Sunday
WEEKLY_REPORT_HOUR_UTC = 22    # time (UTC) to generate weekly report

# ---------------------------------------------------------
# SCHEDULER
# ---------------------------------------------------------
MAIN_LOOP_INTERVAL_S   = 60    # main orchestrator tick (seconds)
DATA_REFRESH_INTERVAL_S = 300  # how often to refresh OHLCV cache

# ---------------------------------------------------------
# EDGE STACK  (Phase 2: hedge-fund-grade filters)
# ---------------------------------------------------------
# Master switch -- if False every edge module is bypassed and the bot
# reverts to pure confidence-score gating (the legacy behaviour).
EDGE_STACK_ENABLED       = True

# Per-edge master switches (disable an edge in isolation for AB testing)
EDGE_MACRO_ANCHOR        = True
EDGE_VOL_REGIME          = True
EDGE_EVENT_FLOW          = True
EDGE_COT_POSITIONING     = True   # requires cot_reports pkg; auto-disables if missing
EDGE_CROSS_MOMENTUM      = True
EDGE_CARRY_BASKET        = True
EDGE_EXECUTION_COST      = True
EDGE_SIGNAL_CALIBRATOR   = True   # requires 50+ closed trades; no-op until then

# Macro anchor: strength above this BLOCKS a contradictory trade
MACRO_VETO_STRENGTH      = 0.6

# Inverse-vol sizing (vol_regime). When True, risk_manager takes
# min(SL-based lots, inverse_vol_lots) -- caps oversizing in calm vol.
USE_INVERSE_VOL_SIZING   = True
DAILY_VOL_TARGET_PCT     = 0.35  # target daily P&L std per trade, % of balance

# Signal calibrator: minimum P(win) to accept a trade once trained
CALIBRATOR_MIN_PWIN      = 0.52
CALIBRATOR_RETRAIN_H     = 24    # hours between retrains
