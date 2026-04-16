"""
ARCS-FX — engines/confidence_score.py
Trade confidence gating system: 0–100 composite score.

WHY THIS MODULE IS THE FINAL GATE:
Every module before this one produces signals in isolation.
The confidence score is the SYNTHESIS — it asks: "given everything
we know right now, how much conviction do we have in this trade?"

If the score is below threshold, the bot does nothing. No exceptions.
This is the mechanism that transforms a "signal" into a "conviction".

SCORE BREAKDOWN (must sum to 100):
  1. Regime clarity         25pts  — how cleanly defined is the regime?
  2. Price action signal    20pts  — OB/S&D signal quality + pattern strength
  3. MTF confluence         15pts  — H1 and M15 agree on regime + direction
  4. News sentiment         15pts  — NLP score and no blackout present
  5. Key level proximity    10pts  — is the signal at a significant level?
  6. Volatility percentile  10pts  — ATR in the tradeable sweet spot
  7. Spread + session        5pts  — is spread acceptable and session active?

THRESHOLD:
  Normal mode:  score >= 70  -> trade
  Early mode:   score >= 80  -> trade (conservative, during initial live testing)

TRANSPARENCY:
  Every component score is logged and stored with the trade DNA.
  The bot never takes a trade without being able to explain why.
  If the score is 69 and the threshold is 70, we skip. Always.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    CONFIDENCE_MIN, CONFIDENCE_EARLY_MODE, CONFIDENCE_WEIGHTS,
    REGIME_QUIET, REGIME_CHAOTIC,
    SPREAD_LIMITS, SPREAD_DEFAULT_LIMIT,
    SESSIONS, NEWS_PAUSE_BEFORE_MIN,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Data structures
# ---------------------------------------------------------

@dataclass
class ComponentScore:
    """Single component of the confidence score."""
    name: str
    raw_score: float        # 0–1 before weighting
    weight: int             # from CONFIDENCE_WEIGHTS
    weighted_score: float   # raw_score x weight
    reason: str             # human-readable explanation

@dataclass
class ConfidenceResult:
    """
    Full confidence evaluation result.
    Passed to the order manager and stored in trade DNA.
    """
    score: float                        # 0–100 final composite score
    threshold: float                    # threshold used (normal or early mode)
    trade_allowed: bool                 # score >= threshold
    components: list[ComponentScore]    # per-component breakdown
    skip_reason: str                    # if not allowed, why
    symbol: str
    timestamp_utc: datetime
    details: dict = field(default_factory=dict)

    def component_map(self) -> dict:
        """Return components as a flat dict for DNA tagging / logging."""
        return {c.name: round(c.weighted_score, 2) for c in self.components}

    def __str__(self) -> str:
        allowed = "TRADE ALLOWED" if self.trade_allowed else f"BLOCKED ({self.skip_reason})"
        component_str = " | ".join(
            f"{c.name}={c.weighted_score:.1f}/{c.weight}"
            for c in self.components
        )
        return (
            f"[{self.symbol}] ConfScore={self.score:.1f}/{self.threshold:.0f} "
            f"-> {allowed}\n"
            f"  {component_str}"
        )


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def compute(
    symbol: str,
    regime_result,          # core.regime_detector.RegimeResult
    regime_m15,             # core.regime_detector.RegimeResult (M15)
    pa_signal,              # engines.price_action.PriceActionSignal
    news_result,            # engines.news_engine.NewsResult
    spread_pips: float,
    early_mode: bool = False,
    now_utc: Optional[datetime] = None,
) -> ConfidenceResult:
    """
    Compute the composite confidence score for a potential trade.

    Args:
        symbol:        trading pair
        regime_result: H1 regime classification result
        regime_m15:    M15 regime classification result (for MTF confluence)
        pa_signal:     price action signal from the PA engine
        news_result:   news engine output
        spread_pips:   current bid-ask spread in pips
        early_mode:    if True, use stricter threshold (CONFIDENCE_EARLY_MODE)
        now_utc:       current time (for session detection)

    Returns:
        ConfidenceResult with final score, gate decision, and full breakdown.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    threshold = CONFIDENCE_EARLY_MODE if early_mode else CONFIDENCE_MIN
    components: list[ComponentScore] = []

    # -- Hard gate: CHAOTIC regime or news blackout --------
    # These are absolute overrides — score is irrelevant.
    if regime_result.regime == REGIME_CHAOTIC:
        return _blocked(symbol, "CHAOTIC regime", threshold, now_utc)
    if regime_result.regime == REGIME_QUIET:
        return _blocked(symbol, "QUIET regime", threshold, now_utc)

    if news_result.is_blackout:
        return _blocked(symbol, f"News blackout: {news_result.blackout_reason}", threshold, now_utc)

    if not pa_signal.has_signal:
        return _blocked(symbol, "No price action signal", threshold, now_utc)

    # -- Component 1: Regime clarity (25pts) --------------
    c1 = _score_regime_clarity(regime_result)
    components.append(c1)

    # -- Component 2: Price action signal (20pts) ---------
    c2 = _score_price_action(pa_signal)
    components.append(c2)

    # -- Component 3: MTF confluence (15pts) --------------
    c3 = _score_mtf_confluence(regime_result, regime_m15, pa_signal)
    components.append(c3)

    # -- Component 4: News sentiment (15pts) --------------
    c4 = _score_news(news_result, pa_signal)
    components.append(c4)

    # -- Component 5: Key level proximity (10pts) ---------
    c5 = _score_key_level(pa_signal)
    components.append(c5)

    # -- Component 6: Volatility percentile (10pts) -------
    c6 = _score_volatility(regime_result)
    components.append(c6)

    # -- Component 7: Spread + session (5pts) -------------
    c7 = _score_spread_session(symbol, spread_pips, now_utc)
    components.append(c7)

    # -- Final score ---------------------------------------
    total = sum(c.weighted_score for c in components)
    total = round(min(total, 100.0), 2)

    trade_allowed = total >= threshold
    skip_reason = "" if trade_allowed else f"Score {total:.1f} below threshold {threshold}"

    result = ConfidenceResult(
        score=total,
        threshold=threshold,
        trade_allowed=trade_allowed,
        components=components,
        skip_reason=skip_reason,
        symbol=symbol,
        timestamp_utc=now_utc,
        details={
            "early_mode": early_mode,
            "regime": regime_result.regime,
            "direction": regime_result.direction,
            "signal_type": pa_signal.signal_type,
            "pattern": pa_signal.pattern,
            "r_ratio": pa_signal.r_ratio,
        },
    )

    logger.info(str(result))
    return result


# ---------------------------------------------------------
# Component scorers
# ---------------------------------------------------------

def _score_regime_clarity(regime_result) -> ComponentScore:
    """
    Score: How clearly defined is the current regime?

    Full 25pts requires:
      - Regime is TRENDING or RANGING (not transitional)
      - Confidence >= 0.80 (strongly in the regime, not borderline)
      - Direction is not NEUTRAL (we have a bias to trade with)

    WHY THIS IS THE HEAVIEST COMPONENT:
    Trading the wrong strategy for the wrong regime is the primary
    cause of bot failures. A trending strategy in a ranging market
    = death by small cuts. We want to be very sure about regime.
    """
    weight = CONFIDENCE_WEIGHTS["regime_clarity"]
    conf = regime_result.confidence

    # Base score from confidence
    if conf >= 0.80:
        raw = 1.0
        reason = f"Strong {regime_result.regime} regime (conf={conf:.0%})"
    elif conf >= 0.60:
        raw = 0.75
        reason = f"Moderate {regime_result.regime} regime (conf={conf:.0%})"
    elif conf >= 0.40:
        raw = 0.50
        reason = f"Weak {regime_result.regime} regime (conf={conf:.0%})"
    else:
        raw = 0.20
        reason = f"Unclear regime (conf={conf:.0%})"

    # Direction penalty: NEUTRAL = we don't know which way to trade
    if regime_result.direction == "NEUTRAL":
        raw *= 0.70
        reason += " | Direction NEUTRAL (penalty -30%)"

    return ComponentScore(
        name="regime_clarity",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


def _score_price_action(pa_signal) -> ComponentScore:
    """
    Score: How strong is the price action setup?

    Full 20pts requires:
      - Signal type is one of the high-quality setups (OB_RETEST > SD_BOUNCE)
      - Pattern is a strong reversal candle (engulfing > pin bar > inside bar)
      - Signal strength (as scored by the PA engine) is high
      - R:R ratio >= 2.0
    """
    weight = CONFIDENCE_WEIGHTS["price_action"]

    # Signal type quality
    signal_type_scores = {
        "OB_RETEST":            1.00,   # Highest quality — institutional OB with confirmation
        "SD_BOUNCE":            0.78,   # Selective only — useful, but no longer allowed to dominate the book
        "SCALP_PULLBACK":       0.82,   # Intraday continuation after pullback in aligned trend
        "SCALP_SWEEP_REVERSAL": 0.88,   # Intraday liquidity sweep reclaim
        "SWING_BREAKOUT":       0.90,   # Higher-timeframe breakout continuation
        "SWING_REVERSION":      0.84,   # Higher-timeframe stretched reversal
        "FVG_FILL":             0.75,   # Valid but mechanistic
        "NONE":                 0.00,
    }
    type_score = signal_type_scores.get(pa_signal.signal_type, 0.50)

    # Pattern quality
    pattern_scores = {
        "BULLISH_ENGULFING": 1.00,
        "BEARISH_ENGULFING": 1.00,
        "MORNING_STAR":      0.90,
        "EVENING_STAR":      0.90,
        "PIN_BAR_BULLISH":   0.80,
        "PIN_BAR_BEARISH":   0.80,
        "INSIDE_BAR":        0.60,
        "DOJI":              0.00,   # Never trade on doji
        "NONE":              0.30,   # Signal without pattern confirmation is weak
    }
    pattern_score = pattern_scores.get(pa_signal.pattern, 0.40)

    # R:R bonus
    r_ratio = pa_signal.r_ratio
    if r_ratio >= 3.0:
        rr_score = 1.0
    elif r_ratio >= 2.0:
        rr_score = 0.8
    elif r_ratio >= 1.5:
        rr_score = 0.5
    else:
        rr_score = 0.1

    # PA engine's own strength assessment
    pa_strength = pa_signal.strength

    # Weighted combination
    raw = (
        type_score    * 0.35 +
        pattern_score * 0.30 +
        pa_strength   * 0.20 +
        rr_score      * 0.15
    )

    reason = (
        f"{pa_signal.signal_type} | Pattern={pa_signal.pattern} "
        f"| Strength={pa_signal.strength:.2f} | R={r_ratio:.1f}"
    )

    return ComponentScore(
        name="price_action",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


def _score_mtf_confluence(regime_h1, regime_m15, pa_signal) -> ComponentScore:
    """
    Score: Do H1 and M15 agree?

    Full 15pts requires:
      - Same regime on both timeframes
      - Same directional bias
      - Signal direction matches the bias

    WHY MTF CONFLUENCE MATTERS:
    A BUY signal on M5 while H1 is bearish is a counter-trend trade.
    Counter-trend trades have lower win rates and worse drawdowns.
    MTF alignment is the single biggest quality filter after regime.
    """
    weight = CONFIDENCE_WEIGHTS["mtf_confluence"]

    if regime_m15 is None:
        return ComponentScore("mtf_confluence", 0.5, weight, weight * 0.5,
                              "M15 regime data unavailable")

    # Regime agreement
    regime_match = regime_h1.regime == regime_m15.regime

    # Direction agreement
    dir_h1  = regime_h1.direction
    dir_m15 = regime_m15.direction
    direction_match = (dir_h1 == dir_m15) and dir_h1 != "NEUTRAL"

    # Signal direction aligns with H1 bias
    signal_dir = pa_signal.direction
    if dir_h1 == "BULLISH" and signal_dir == "BUY":
        signal_aligned = True
    elif dir_h1 == "BEARISH" and signal_dir == "SELL":
        signal_aligned = True
    else:
        signal_aligned = False

    if regime_match and direction_match and signal_aligned:
        raw = 1.0
        reason = f"Full alignment: H1={dir_h1} M15={dir_m15} Signal={signal_dir}"
    elif regime_match and signal_aligned:
        raw = 0.75
        reason = f"Regime match, direction partial: H1={dir_h1} M15={dir_m15}"
    elif signal_aligned:
        raw = 0.80
        reason = f"Signal aligned with H1; M15 differs but PA filter already passed"
    else:
        raw = 0.35
        reason = f"MTF conflict: H1={dir_h1} M15={dir_m15} Signal={signal_dir}"

    return ComponentScore(
        name="mtf_confluence",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


def _score_news(news_result, pa_signal) -> ComponentScore:
    """
    Score: Does the news environment support this trade direction?

    Full 15pts requires:
      - No blackout active (hard gate already checked, but score = 0 if missed)
      - Sentiment aligns with trade direction OR is neutral
      - Next event is > 30 minutes away (extra safety margin)

    Sentiment alignment:
      BUY  + BULLISH news -> full score
      BUY  + NEUTRAL news -> partial score
      BUY  + BEARISH news -> penalised (counter-sentiment trade)
    """
    weight = CONFIDENCE_WEIGHTS["news_sentiment"]

    # Blackout double-check (should have been caught above, but be safe)
    if news_result.is_blackout:
        return ComponentScore("news_sentiment", 0.0, weight, 0.0, "In news blackout")

    trade_dir = pa_signal.direction
    sentiment = news_result.sentiment_label
    sent_score = news_result.sentiment_score
    sent_conf  = news_result.sentiment_confidence
    mins_to_next = news_result.minutes_to_next_event
    degraded = news_result.source_quality == "DEGRADED"

    # Sentiment alignment
    if degraded:
        alignment = 0.90
        align_reason = "Degraded news source - using near-neutral safety score"
    elif (trade_dir == "BUY"  and sentiment == "BULLISH") or \
         (trade_dir == "SELL" and sentiment == "BEARISH"):
        alignment = 1.0
        align_reason = "Sentiment aligns with trade"
    elif sentiment == "NEUTRAL":
        alignment = 0.85
        align_reason = "Neutral sentiment - acceptable"
    else:
        # Counter-sentiment
        alignment = 0.25
        align_reason = f"Counter-sentiment: trade={trade_dir} vs news={sentiment}"

    # Time-to-event discount: if < 45 min to high-impact event, reduce score
    if mins_to_next is not None:
        if mins_to_next < NEWS_PAUSE_BEFORE_MIN:
            # Should have been blocked, but score to 0 as safety
            time_factor = 0.0
            align_reason += f" | Event in {mins_to_next}min (too close)"
        elif mins_to_next < 45:
            time_factor = mins_to_next / 45   # scale: 0 at 0min, 1 at 45min+
            align_reason += f" | Event approaching in {mins_to_next}min"
        else:
            time_factor = 1.0
    else:
        time_factor = 1.0   # no upcoming event = maximum time factor

    # Degraded news mode should not be treated like weak negative information.
    confidence_factor = 1.0 if degraded else max(0.85, sent_conf)

    raw = alignment * time_factor * confidence_factor

    reason = (
        f"{align_reason} | "
        f"Score={sent_score:+.2f} conf={sent_conf:.2f} | "
        f"NextEvent={mins_to_next}min"
    )

    return ComponentScore(
        name="news_sentiment",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


def _score_key_level(pa_signal) -> ComponentScore:
    """
    Score: Is the signal occurring at a significant price level?

    Full 10pts requires:
      - Price is at or very near a key level (PDH/PDL, weekly H/L, round number)
      - Key level proximity score >= 0.80 (from PA engine)

    WHY: Entries AT key levels have dramatically better fill prices and
    lower chance of being stop-hunted. A level confirms institutional interest.
    """
    weight = CONFIDENCE_WEIGHTS["key_level"]
    klp = pa_signal.key_level_proximity

    if klp >= 0.80:
        raw = 1.0
        reason = f"Price at key level (proximity={klp:.2f})"
    elif klp >= 0.50:
        raw = 0.65
        reason = f"Near key level (proximity={klp:.2f})"
    elif klp >= 0.25:
        raw = 0.35
        reason = f"Weak level proximity (proximity={klp:.2f})"
    else:
        raw = 0.10
        reason = f"No key level nearby (proximity={klp:.2f})"

    return ComponentScore(
        name="key_level",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


def _score_volatility(regime_result) -> ComponentScore:
    """
    Score: Is ATR in the tradeable sweet spot?

    Full 10pts requires ATR percentile between 30th and 70th.
    Below 30th: market too quiet -> no momentum to drive the trade.
    Above 70th: market too explosive -> stop-hunts everywhere.
    """
    weight = CONFIDENCE_WEIGHTS["volatility_pct"]
    atr_pct = regime_result.atr_percentile

    from config import ATR_LOW_PCT, ATR_HIGH_PCT, ATR_CHAOS_PCT

    if ATR_LOW_PCT <= atr_pct <= ATR_HIGH_PCT:
        raw = 1.0
        reason = f"ATR in sweet spot ({atr_pct:.0f}th pct)"
    elif atr_pct < ATR_LOW_PCT:
        # Too quiet: scale from 0 (at 0th) to 0.7 (at 30th)
        raw = (atr_pct / ATR_LOW_PCT) * 0.70
        reason = f"ATR too low ({atr_pct:.0f}th pct) — quiet market"
    elif atr_pct <= ATR_CHAOS_PCT:
        # Elevated: scale from 0.7 (at 70th) down to 0.1 (at 90th)
        raw = 0.70 - ((atr_pct - ATR_HIGH_PCT) / (ATR_CHAOS_PCT - ATR_HIGH_PCT)) * 0.60
        raw = max(raw, 0.10)
        reason = f"ATR elevated ({atr_pct:.0f}th pct) — approaching chaos"
    else:
        raw = 0.0
        reason = f"ATR at chaos level ({atr_pct:.0f}th pct)"

    return ComponentScore(
        name="volatility_pct",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


def _score_spread_session(
    symbol: str,
    spread_pips: float,
    now_utc: datetime,
) -> ComponentScore:
    """
    Score: Is the spread acceptable AND are we in a quality session?

    Session scoring uses a two-tier approach (spec Feature 1):
      1. If the session heatmap has >= MIN_TRADES_PER_CELL data for this
         symbol/hour, use the learned per-pair win-rate heatmap score.
      2. Otherwise fall back to static session preference weights.

    Full 5pts requires:
      - Spread <= configured limit for this pair
      - Heatmap/session score in the high-quality range

    WHY heatmap integration:
      Static session weights treat all London hours equally. The heatmap
      learns that, say, GBPUSD trades well at 08:00 UTC but not 14:00 UTC,
      based on real historical outcomes. This makes the session component
      data-driven rather than assumption-driven.
    """
    weight = CONFIDENCE_WEIGHTS["spread_session"]

    # -- Spread component --------------------------------------------------
    limit = SPREAD_LIMITS.get(symbol, SPREAD_DEFAULT_LIMIT)
    if spread_pips <= limit:
        spread_score = 1.0
        spread_reason = f"Spread OK ({spread_pips:.1f}/{limit:.1f} pips)"
    elif spread_pips <= limit * 1.5:
        spread_score = 0.5
        spread_reason = f"Spread elevated ({spread_pips:.1f}/{limit:.1f} pips)"
    else:
        spread_score = 0.0
        spread_reason = f"Spread too wide ({spread_pips:.1f}/{limit:.1f} pips)"

    # -- Session component (heatmap-aware) ---------------------------------
    hour_utc = now_utc.hour

    # Attempt to use the learned heatmap (lazy import avoids circular deps)
    session_score = 0.0
    session_reason = ""
    heatmap_used = False
    try:
        from learning.session_heatmap import SessionHeatmap
        _hm = SessionHeatmap._get_shared_instance()
        cell = _hm.get_cell(symbol, hour_utc)
        if cell is not None and cell.is_learned:
            session_score = cell.score
            session_reason = (
                f"Heatmap {symbol} h{hour_utc:02d}UTC "
                f"wr={cell.win_rate:.0%} ({cell.trades} trades)"
            )
            heatmap_used = True
    except Exception:
        # Heatmap unavailable: fall through to static scoring below
        pass

    if not heatmap_used:
        # Static fallback: pure session-time preference
        overlap_start, overlap_end = SESSIONS["OVERLAP"]
        london_start,  london_end  = SESSIONS["LONDON"]
        ny_start,      ny_end      = SESSIONS["NY"]

        if overlap_start <= hour_utc < overlap_end:
            session_score = 1.0
            session_reason = "London/NY overlap (optimal)"
        elif london_start <= hour_utc < london_end:
            session_score = 0.75
            session_reason = "London session"
        elif ny_start <= hour_utc < ny_end:
            session_score = 0.75
            session_reason = "NY session"
        else:
            session_score = 0.25
            session_reason = "Asian/off session (low liquidity)"

    raw    = spread_score * 0.60 + session_score * 0.40
    reason = f"{spread_reason} | {session_reason}"

    return ComponentScore(
        name="spread_session",
        raw_score=round(raw, 3),
        weight=weight,
        weighted_score=round(raw * weight, 2),
        reason=reason,
    )


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _blocked(
    symbol: str,
    reason: str,
    threshold: float,
    now_utc: datetime,
) -> ConfidenceResult:
    """Return a zero-score blocked result without running all components."""
    logger.info("[%s] Confidence gate BLOCKED: %s", symbol, reason)
    return ConfidenceResult(
        score=0.0,
        threshold=threshold,
        trade_allowed=False,
        components=[],
        skip_reason=reason,
        symbol=symbol,
        timestamp_utc=now_utc,
    )


# ---------------------------------------------------------
# Standalone test
# ---------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging
    from dotenv import load_dotenv
    from core.mt5_connection import connect, disconnect
    from core.data_fetcher import get_h1, get_m15, get_m5, get_tick
    from core.regime_detector import detect
    from engines.price_action import evaluate as pa_evaluate
    from engines.news_engine import evaluate as news_evaluate

    load_dotenv()
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 70)
    print("  ARCS-FX — Confidence Score Engine Test")
    print("=" * 70)

    if not connect():
        sys.exit(1)

    test_symbols = ["EURUSD", "GBPUSD", "USDJPY"]

    for sym in test_symbols:
        print(f"\n{'-'*70}")
        print(f"  {sym}")
        print(f"{'-'*70}")

        h1  = get_h1(sym)
        m15 = get_m15(sym)
        m5  = get_m5(sym)
        tick = get_tick(sym)

        if not all([h1 is not None, m15 is not None, m5 is not None, tick]):
            print(f"  [SKIP] Missing data")
            continue

        regime_h1  = detect(sym, h1,  timeframe="H1")
        regime_m15 = detect(sym, m15, timeframe="M15")
        news        = news_evaluate(sym)
        spread_pips = tick["spread_pips"] or 2.0

        if regime_h1 is None:
            print(f"  [SKIP] Regime detection failed")
            continue

        pa_signal = pa_evaluate(
            symbol=sym,
            h1_df=h1,
            m15_df=m15,
            m5_df=m5,
            regime=regime_h1.regime,
            direction_bias=regime_h1.direction,
            atr=regime_h1.atr,
        )

        confidence = compute(
            symbol=sym,
            regime_result=regime_h1,
            regime_m15=regime_m15,
            pa_signal=pa_signal,
            news_result=news,
            spread_pips=spread_pips,
            early_mode=True,     # use strict 80-point threshold in testing
        )

        print(f"\n  {confidence}")
        if confidence.trade_allowed:
            print(f"\n  *** TRADE ALLOWED ***")
            print(f"  Direction: {pa_signal.direction}")
            print(f"  Entry:     {pa_signal.entry_price:.5f}")
            print(f"  SL:        {pa_signal.sl_price:.5f}")
            print(f"  TP:        {pa_signal.tp_price:.5f}")
            print(f"  R:R        {pa_signal.r_ratio:.1f}")
        else:
            print(f"  Skip: {confidence.skip_reason}")

    disconnect()
    print("\n\nPhase 2 — Confidence Score test: DONE")
