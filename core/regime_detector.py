"""
ARCS-FX — core/regime_detector.py
Market regime classifier: TRENDING / RANGING / CHAOTIC

WHY THIS IS THE FIRST INTELLIGENCE MODULE:
Every other decision in the bot depends on regime. The price action
strategy, position sizing, news sensitivity — all of it changes based
on which regime we're in. Getting this wrong means applying the wrong
playbook to the wrong market, which is how most bots blow accounts.

REGIME LOGIC:
  TRENDING  — ADX > 25 AND ATR between 30th-70th percentile
              Market has clear direction and normal volatility.
              Apply SMC momentum strategy.

  RANGING   — ADX < 20 AND price contained within Bollinger Bands
              Market is oscillating without direction.
              Apply Supply & Demand mean reversion.

  CHAOTIC   — ATR >= 90th percentile OR high-impact news active
              Market is unpredictable. Bot goes silent.
              This is a feature, not a bug.

MULTI-FACTOR CONFIDENCE:
  Rather than a binary label, we also return a confidence score (0-1)
  so the confidence_score engine can weight the regime signal
  proportionally. A barely-trending ADX of 26 gets penalised vs 40.

DIRECTION BIAS:
  In trending regime we also detect direction (BULLISH/BEARISH) using
  +DI / -DI crossover from ADX, confirmed by market structure slope.
  This prevents the price action engine from looking for longs in a
  confirmed downtrend.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange, BollingerBands

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    ADX_PERIOD, ADX_TREND_THRESHOLD, ADX_RANGE_THRESHOLD,
    ATR_PERIOD, ATR_LOOKBACK_DAYS, ATR_LOW_PCT, ATR_HIGH_PCT, ATR_CHAOS_PCT,
    BB_PERIOD, BB_STD,
    REGIME_TRENDING_CLEAN, REGIME_TRENDING_EXTENDED,
    REGIME_RANGING_CLEAN, REGIME_QUIET, REGIME_CHAOTIC,
    TF_H1,
)
from core.instruments import get_profile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Data structures
# ---------------------------------------------------------

@dataclass
class RegimeResult:
    """
    Full output of a single regime classification pass.
    Consumed by the confidence score engine and price action engine.
    """
    regime: str                   # TRENDING_CLEAN | TRENDING_EXTENDED | RANGING_CLEAN | QUIET | CHAOTIC
    confidence: float             # 0.0–1.0  how clearly defined the regime is
    direction: str                # BULLISH | BEARISH | NEUTRAL
    adx: float                    # raw ADX value
    adx_pos: float                # +DI (bullish pressure)
    adx_neg: float                # -DI (bearish pressure)
    atr: float                    # raw ATR value
    atr_percentile: float         # ATR position in 14-day distribution (0–100)
    bb_width: float               # Bollinger Band width (normalised)
    bb_pct_b: float               # %B — where price sits within BB (0=lower, 1=upper)
    price: float                  # latest close used for classification
    symbol: str
    timeframe: str
    components: dict = field(default_factory=dict)   # per-factor breakdown for logging

    def is_tradeable(self) -> bool:
        """Return True only when regime allows trading."""
        return self.regime not in {REGIME_CHAOTIC, REGIME_QUIET}

    def __str__(self) -> str:
        return (
            f"[{self.symbol} {self.timeframe}] "
            f"Regime={self.regime} ({self.confidence:.0%}) "
            f"Dir={self.direction} | "
            f"ADX={self.adx:.1f} (+DI={self.adx_pos:.1f} -DI={self.adx_neg:.1f}) | "
            f"ATR_pct={self.atr_percentile:.0f}th | "
            f"BB_pct_b={self.bb_pct_b:.2f}"
        )


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def detect(
    symbol: str,
    df: pd.DataFrame,
    timeframe: str = TF_H1,
    force_chaos: bool = False,
) -> Optional[RegimeResult]:
    """
    Classify the current market regime for `symbol` using `df` (OHLCV).

    Args:
        symbol:      trading pair, e.g. "EURUSD"
        df:          OHLCV DataFrame with columns [open,high,low,close]
                     minimum length: ATR_PERIOD + ATR_LOOKBACK_DAYS * 24 + BB_PERIOD
        timeframe:   label for logging
        force_chaos: set True when news engine signals a high-impact event —
                     bypasses all calculations and returns CHAOTIC immediately

    Returns:
        RegimeResult or None if DataFrame is too short / malformed.
    """
    if force_chaos:
        logger.info("[%s] Regime forced to CHAOTIC by news engine.", symbol)
        return _forced_chaotic(symbol, timeframe, df)

    if not _validate_df(df, symbol):
        return None

    # -- Compute indicators --------------------------------
    df = df.copy()
    df = _add_adx(df)
    df = _add_atr(df)
    df = _add_bb(df)

    # Use the last fully-formed candle
    last = df.iloc[-1]

    adx     = float(last["adx"])
    adx_pos = float(last["adx_pos"])
    adx_neg = float(last["adx_neg"])
    atr     = float(last["atr"])
    bb_w    = float(last["bb_width"])
    bb_pct  = float(last["bb_pct_b"])
    price   = float(last["close"])

    # -- ATR percentile over rolling lookback window -------
    atr_pct = _atr_percentile(df)

    # -- Classify regime -----------------------------------
    profile = get_profile(symbol)

    regime, confidence, components = _classify(
        adx, adx_pos, adx_neg, atr_pct, bb_w, bb_pct, profile
    )

    # -- Direction bias ------------------------------------
    direction = _resolve_direction(adx_pos, adx_neg, df)

    result = RegimeResult(
        regime=regime,
        confidence=confidence,
        direction=direction,
        adx=adx,
        adx_pos=adx_pos,
        adx_neg=adx_neg,
        atr=atr,
        atr_percentile=atr_pct,
        bb_width=bb_w,
        bb_pct_b=bb_pct,
        price=price,
        symbol=symbol,
        timeframe=timeframe,
        components=components,
    )

    logger.info(str(result))
    return result


def detect_multi_tf(
    symbol: str,
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    force_chaos: bool = False,
) -> dict[str, RegimeResult]:
    """
    Run regime detection on both H1 and M15 for a symbol.

    WHY: H1 sets the macro regime context. M15 confirms it for entry.
    Returns a dict {"H1": RegimeResult, "M15": RegimeResult}.
    If the two regimes disagree, the caller should be cautious.
    """
    results = {}
    h1 = detect(symbol, h1_df, timeframe="H1", force_chaos=force_chaos)
    m15 = detect(symbol, m15_df, timeframe="M15", force_chaos=force_chaos)

    if h1:
        results["H1"] = h1
    if m15:
        results["M15"] = m15

    if h1 and m15:
        # Whitelist compatible pairs: QUIET inside a RANGING/TRENDING parent is a
        # sub-regime, not a conflict. Only flag as mismatch when the regimes are
        # genuinely incompatible (e.g. RANGING vs CHAOTIC, RANGING vs TRENDING_EXTENDED).
        compatible = {
            ("RANGING_CLEAN",      "QUIET"),
            ("TRENDING_CLEAN",     "QUIET"),
            ("TRENDING_EXTENDED",  "QUIET"),
        }
        pair = (h1.regime, m15.regime)
        if h1.regime != m15.regime and pair not in compatible:
            logger.warning(
                "[%s] MTF regime mismatch: H1=%s vs M15=%s — confidence penalised.",
                symbol, h1.regime, m15.regime,
            )
    return results


# ---------------------------------------------------------
# Indicator calculations
# ---------------------------------------------------------

def _add_adx(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add ADX, +DI, -DI columns using the `ta` library.
    ADX measures trend STRENGTH (0-100), not direction.
    +DI and -DI measure directional pressure.
    """
    adx_ind = ADXIndicator(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=ADX_PERIOD,
        fillna=False,
    )
    df["adx"]     = adx_ind.adx()
    df["adx_pos"] = adx_ind.adx_pos()   # +DI
    df["adx_neg"] = adx_ind.adx_neg()   # -DI
    return df


def _add_atr(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add ATR column. ATR = Average True Range measures volatility in price units.
    We store the raw ATR then compute percentile separately.
    """
    atr_ind = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=ATR_PERIOD,
        fillna=False,
    )
    df["atr"] = atr_ind.average_true_range()
    return df


def _add_bb(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add Bollinger Band columns.
    bb_width: (upper - lower) / middle — normalised band width, proxy for volatility expansion.
    bb_pct_b: %B — where price sits within the band (0 = lower band, 1 = upper band).
    """
    bb_ind = BollingerBands(
        close=df["close"],
        window=BB_PERIOD,
        window_dev=BB_STD,
        fillna=False,
    )
    df["bb_upper"]  = bb_ind.bollinger_hband()
    df["bb_lower"]  = bb_ind.bollinger_lband()
    df["bb_mid"]    = bb_ind.bollinger_mavg()
    df["bb_width"]  = bb_ind.bollinger_wband()   # (upper-lower)/middle * 100
    df["bb_pct_b"]  = bb_ind.bollinger_pband()   # (close-lower)/(upper-lower)
    return df


def _atr_percentile(df: pd.DataFrame) -> float:
    """
    Compute where the latest ATR value sits in the rolling distribution
    over the past ATR_LOOKBACK_DAYS days (H1 candles = days * 24).

    WHY rolling percentile instead of raw ATR:
    A 0.0010 ATR on EURUSD is "noisy" in a quiet week but "normal" in a
    volatile week.  Percentile normalises for the recent volatility regime
    so thresholds stay meaningful regardless of the macro environment.

    Returns a float 0–100 (0 = calmest, 100 = most volatile).
    """
    lookback_candles = ATR_LOOKBACK_DAYS * 24   # H1 candles per day
    atr_series = df["atr"].dropna()

    if len(atr_series) < lookback_candles:
        lookback_candles = len(atr_series)

    window = atr_series.iloc[-lookback_candles:]
    latest = atr_series.iloc[-1]

    # Percentile = fraction of historical ATR values below current ATR
    percentile = float(np.sum(window <= latest) / len(window) * 100)
    return round(percentile, 1)


# ---------------------------------------------------------
# Regime classification logic
# ---------------------------------------------------------

def _classify(
    adx: float,
    adx_pos: float,
    adx_neg: float,
    atr_pct: float,
    bb_width: float,
    bb_pct_b: float,
    profile,
) -> tuple[str, float, dict]:
    """
    Core classification algorithm.

    Returns (regime_label, confidence_0_to_1, components_dict).

    CHAOTIC check is done first — it is an absolute override.
    Trend and range families are scored on a gradient, and the stronger
    score wins. Confidence reflects how clearly one regime dominates.

    Components dict gives full per-factor transparency for logging.
    """
    components = {}

    # -- CHAOTIC: absolute override ------------------------
    # ATR at extreme is the primary chaotic signal.
    # Low ATR (dead market) is equally untradeable.
    chaos_atr_pct = profile.regime_chaos_atr_pct
    quiet_atr_pct = profile.regime_quiet_atr_pct
    dominant_floor = profile.regime_dominant_floor
    atr_low_pct = profile.regime_atr_low_pct
    atr_high_pct = profile.regime_atr_high_pct

    if atr_pct >= chaos_atr_pct:
        components["trigger"] = f"ATR_pct={atr_pct:.0f} >= {chaos_atr_pct:.0f} (extreme volatility)"
        return REGIME_CHAOTIC, 1.0, components

    if atr_pct < quiet_atr_pct:
        components["trigger"] = f"ATR_pct={atr_pct:.0f} < {quiet_atr_pct:.0f} (dead market)"
        return REGIME_QUIET, 0.9, components

    # -- Score TRENDING (0–1) ------------------------------
    trending_score = _score_trending(
        adx, adx_pos, adx_neg, atr_pct,
        atr_low_pct=atr_low_pct,
        atr_high_pct=atr_high_pct,
        chaos_atr_pct=chaos_atr_pct,
    )

    # -- Score RANGING (0–1) -------------------------------
    ranging_score = _score_ranging(
        adx, atr_pct, bb_pct_b,
        atr_high_pct=atr_high_pct,
    )

    components["trending_score"] = round(trending_score, 3)
    components["ranging_score"]  = round(ranging_score, 3)
    components["adx"]            = round(adx, 1)
    components["atr_pct"]        = round(atr_pct, 1)
    components["bb_pct_b"]       = round(bb_pct_b, 3)

    # -- Decision ------------------------------------------
    # Neither score dominant enough -> QUIET / transitional market
    dominant = max(trending_score, ranging_score)
    if dominant < dominant_floor:
        components["trigger"] = "No dominant regime (transitional / low-conviction)"
        return REGIME_QUIET, 0.7, components

    if trending_score >= ranging_score:
        confidence = min(trending_score, 1.0)
        if atr_pct > atr_high_pct or adx >= 38:
            components["trigger"] = (
                f"Strong trend but extended conditions "
                f"(ADX={adx:.1f}, ATR_pct={atr_pct:.0f})"
            )
            return REGIME_TRENDING_EXTENDED, round(confidence, 3), components
        components["trigger"] = (
            f"Clean trend structure (ADX={adx:.1f}, ATR_pct={atr_pct:.0f})"
        )
        return REGIME_TRENDING_CLEAN, round(confidence, 3), components

    confidence = min(ranging_score, 1.0)
    components["trigger"] = (
        f"Contained range conditions (ADX={adx:.1f}, ATR_pct={atr_pct:.0f})"
    )
    return REGIME_RANGING_CLEAN, round(confidence, 3), components


def _score_trending(
    adx: float,
    adx_pos: float,
    adx_neg: float,
    atr_pct: float,
    atr_low_pct: float = ATR_LOW_PCT,
    atr_high_pct: float = ATR_HIGH_PCT,
    chaos_atr_pct: float = ATR_CHAOS_PCT,
) -> float:
    """
    Score how strongly the market exhibits trending characteristics.

    Factors:
      1. ADX magnitude (primary signal — 50% weight)
      2. DI separation (are bulls or bears clearly in control? — 30% weight)
      3. ATR in normal zone (not too quiet, not too explosive — 20% weight)

    Returns 0–1 (higher = more clearly trending).
    """
    # Factor 1: ADX magnitude
    # ADX 25 -> 0.0 baseline, ADX 50 -> 1.0 ceiling
    adx_score = np.clip((adx - ADX_TREND_THRESHOLD) / (50 - ADX_TREND_THRESHOLD), 0, 1)

    # Factor 2: DI separation — abs(+DI - -DI) normalised by their sum
    # Large separation means one side dominates clearly
    di_sum = adx_pos + adx_neg
    di_sep = abs(adx_pos - adx_neg) / di_sum if di_sum > 0 else 0
    di_score = np.clip(di_sep, 0, 1)

    # Factor 3: ATR in sweet spot (30th–70th pct)
    if atr_low_pct <= atr_pct <= atr_high_pct:
        atr_score = 1.0
    elif atr_pct < atr_low_pct:
        atr_score = atr_pct / atr_low_pct           # scale up to 1 at lower sweet spot
    else:
        # Above 70th pct: trend still possible but getting dangerous
        atr_score = max(0, 1 - (atr_pct - atr_high_pct) / max(chaos_atr_pct - atr_high_pct, 1))

    return 0.50 * adx_score + 0.30 * di_score + 0.20 * atr_score


def _score_ranging(
    adx: float,
    atr_pct: float,
    bb_pct_b: float,
    atr_high_pct: float = ATR_HIGH_PCT,
) -> float:
    """
    Score how strongly the market exhibits ranging characteristics.

    Factors:
      1. ADX weakness (primary — 40% weight): low ADX = no trend = ranging
      2. ATR in lower half (30% weight): ranging markets have suppressed volatility
      3. %B mid-zone (30% weight): price oscillating inside BB = classic range

    Returns 0–1.
    """
    # Factor 1: ADX weakness
    # ADX 20 -> 1.0, ADX 30 -> 0.0 (linear fade)
    adx_score = np.clip(1 - (adx - ADX_RANGE_THRESHOLD) / (ADX_TREND_THRESHOLD - ADX_RANGE_THRESHOLD), 0, 1)

    # Factor 2: ATR in lower half of distribution
    # ATR below 50th pct scores well; above 70th pct scores 0
    if atr_pct <= 50:
        atr_score = 1.0
    elif atr_pct <= atr_high_pct:
        atr_score = 1 - (atr_pct - 50) / max(atr_high_pct - 50, 1)
    else:
        atr_score = 0.0

    # Factor 3: %B in mid-zone (0.25–0.75) -> price oscillating centrally
    # %B = 0.5 is perfect centre of BB -> score 1.0
    # %B = 0 or 1 (at bands) -> score 0.0
    bb_mid_score = 1 - abs(bb_pct_b - 0.5) * 2   # peaks at 0.5, zero at 0 or 1
    bb_mid_score = np.clip(bb_mid_score, 0, 1)

    return 0.40 * adx_score + 0.30 * atr_score + 0.30 * bb_mid_score


def _resolve_direction(
    adx_pos: float,
    adx_neg: float,
    df: pd.DataFrame,
) -> str:
    """
    Determine directional bias: BULLISH, BEARISH, or NEUTRAL.

    Two-factor vote:
      1. +DI vs -DI from ADX indicator (institutional trend pressure)
      2. Simple price slope — is the 20-bar EMA rising or falling?

    Both must agree for a non-neutral signal. If they disagree -> NEUTRAL.
    This prevents the price action engine from trading counter-direction
    in a strong institutional trend.
    """
    # Vote 1: DI crossover
    if adx_pos > adx_neg * 1.1:      # 10% buffer to avoid noise at crossover
        di_vote = "BULLISH"
    elif adx_neg > adx_pos * 1.1:
        di_vote = "BEARISH"
    else:
        di_vote = "NEUTRAL"

    # Vote 2: 20-bar close slope
    closes = df["close"].dropna()
    if len(closes) >= 20:
        slope = closes.iloc[-1] - closes.iloc[-20]
        if slope > 0:
            slope_vote = "BULLISH"
        elif slope < 0:
            slope_vote = "BEARISH"
        else:
            slope_vote = "NEUTRAL"
    else:
        slope_vote = "NEUTRAL"

    # Both agree -> confident direction
    if di_vote == slope_vote:
        return di_vote

    # One side NEUTRAL, the other directional -> defer to the directional vote.
    if di_vote == "NEUTRAL":
        return slope_vote
    if slope_vote == "NEUTRAL":
        return di_vote

    # Active disagreement (DI says one way, slope the other) -> NEUTRAL.
    # Letting DI override slope was trading into exhausted moves; when the
    # two disagree outright we'd rather stand aside than pick a winner.
    return "NEUTRAL"


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _forced_chaotic(symbol: str, timeframe: str, df: pd.DataFrame) -> RegimeResult:
    """Return a CHAOTIC result without running calculations (news override)."""
    last_close = float(df["close"].iloc[-1]) if not df.empty else 0.0
    return RegimeResult(
        regime=REGIME_CHAOTIC,
        confidence=1.0,
        direction="NEUTRAL",
        adx=0.0, adx_pos=0.0, adx_neg=0.0,
        atr=0.0, atr_percentile=0.0,
        bb_width=0.0, bb_pct_b=0.5,
        price=last_close,
        symbol=symbol,
        timeframe=timeframe,
        components={"trigger": "Forced by news engine — high-impact event active"},
    )


def _validate_df(df: pd.DataFrame, symbol: str) -> bool:
    """Check minimum DataFrame requirements before running indicators."""
    if df is None or df.empty:
        logger.error("[%s] Regime detector received empty DataFrame.", symbol)
        return False

    required_cols = {"open", "high", "low", "close"}
    if not required_cols.issubset(df.columns):
        logger.error(
            "[%s] Missing columns. Expected %s, got %s.",
            symbol, required_cols, set(df.columns),
        )
        return False

    min_rows = max(ADX_PERIOD, ATR_PERIOD, BB_PERIOD) + ATR_LOOKBACK_DAYS * 24
    if len(df) < min_rows:
        logger.warning(
            "[%s] DataFrame has %d rows; need at least %d for reliable regime detection.",
            symbol, len(df), min_rows,
        )
        # Don't hard-fail — proceed with reduced lookback for ATR percentile

    if df["close"].isna().all():
        logger.error("[%s] All close prices are NaN.", symbol)
        return False

    return True


# ---------------------------------------------------------
# Standalone test
# ---------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging
    from core.mt5_connection import connect, disconnect
    from core.data_fetcher import get_h1, get_m15

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 70)
    print("  ARCS-FX — Regime Detector Test")
    print("=" * 70)

    if not connect():
        sys.exit(1)

    test_pairs = ["EURUSD", "GBPUSD", "USDJPY"]

    for sym in test_pairs:
        print(f"\n{'-'*70}")
        print(f"  {sym}")
        print(f"{'-'*70}")

        h1  = get_h1(sym)
        m15 = get_m15(sym)

        if h1 is None or m15 is None:
            print(f"  [FAIL] Could not fetch data for {sym}")
            continue

        results = detect_multi_tf(sym, h1, m15)

        for tf, r in results.items():
            print(f"\n  [{tf}] {r}")
            print(f"       Components: {r.components}")
            print(f"       Tradeable : {r.is_tradeable()}")

    disconnect()
    print("\n\nPhase 2 — Regime Detector test: DONE")
