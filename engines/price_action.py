"""
ARCS-FX — engines/price_action.py
Full Smart Money Concepts (SMC) price action engine.

This is the most complex module in the bot. It implements the
institutional price action framework from scratch — no library
does this correctly for live trading, so we build it ourselves.

WHAT THIS MODULE DETECTS:
  Market Structure:
    - Swing highs / swing lows (the skeleton of all SMC analysis)
    - HH / HL (uptrend) and LH / LL (downtrend)
    - BOS  — Break of Structure: confirms trend continuation
    - CHoCH — Change of Character: early trend reversal warning

  Institutional Footprints:
    - Order Blocks (OB): zones where institutions placed large orders
      (last opposing candle before a strong impulsive move)
    - Fair Value Gaps (FVG): price imbalances between candles that
      act as magnets for price to return to
    - Liquidity Sweeps: stop-hunt moves above swing highs or below
      swing lows that quickly reverse

  Supply & Demand Zones:
    - Strong impulsive origin zones used in ranging regime
    - Distinct from OBs — broader zones, used for mean reversion

  Candlestick Patterns (only valid AT key levels):
    - Engulfing (bullish / bearish)
    - Pin Bar / Hammer / Shooting Star
    - Inside Bar (breakout setup)
    - Doji at key level (no trade)
    - Morning / Evening Star

  Key Levels:
    - Previous Day High / Low (PDH/PDL)
    - Previous Week High / Low
    - Round number magnets (1.1000, 1.1050 etc.)
    - Session highs / lows (London open, NY open)

ENTRY SIGNAL LOGIC:
  TRENDING regime:
    OB retest + BOS confirmation + engulfing/pin bar AT the OB -> BUY/SELL
    Direction locked to H1 market structure (only longs in uptrend)

  RANGING regime:
    S&D zone touch + pin bar or engulfing AT zone boundary -> BUY/SELL
    TP at opposite zone, tight SL beyond zone

  Signal is returned as a PriceActionSignal dataclass with full context
  for DNA tagging and the confidence score engine.
"""

import os
import sys
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    OB_LOOKBACK, FVG_MIN_BODY_RATIO, STRUCTURE_LOOKBACK,
    LIQUIDITY_SWEEP_BUFFER, PATTERN_LOOKBACK,
    REGIME_TRENDING_CLEAN, REGIME_TRENDING_EXTENDED, REGIME_RANGING_CLEAN,
    TRENDING_REGIMES, RANGING_REGIMES,
)
from engines.scalp_engine import scalp_session_allowed, mean_reversion_session_allowed, session_label
from engines.swing_engine import breakout_allowed, reversion_allowed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Data structures
# ---------------------------------------------------------

@dataclass
class SwingPoint:
    index: int
    time: datetime
    price: float
    kind: str          # "HIGH" or "LOW"

@dataclass
class MarketStructure:
    trend: str                              # BULLISH | BEARISH | NEUTRAL
    swing_highs: list[SwingPoint]
    swing_lows: list[SwingPoint]
    last_bos_time: Optional[datetime]
    last_choch_time: Optional[datetime]
    last_bos_direction: str                 # BULLISH | BEARISH | NONE
    structure_points: list[str]             # human-readable trace e.g. ["HH","HL","HH"]

@dataclass
class OrderBlock:
    time: datetime
    top: float
    bottom: float
    mid: float
    direction: str     # BULLISH (demand OB) | BEARISH (supply OB)
    strength: float    # 0–1: impulse size relative to recent ATR
    tested: bool       # has price returned to this OB?
    index: int         # candle index in the source DataFrame

@dataclass
class FairValueGap:
    time: datetime
    top: float
    bottom: float
    mid: float
    direction: str     # BULLISH | BEARISH
    filled: bool       # True once price has traded through the gap

@dataclass
class LiquiditySweep:
    time: datetime
    swept_level: float
    direction: str     # BULLISH_SWEEP (swept lows) | BEARISH_SWEEP (swept highs)
    reversal_confirmed: bool

@dataclass
class SDZone:
    time: datetime
    top: float
    bottom: float
    mid: float
    kind: str          # "SUPPLY" | "DEMAND"
    strength: float    # 0–1
    tested: bool

@dataclass
class PriceActionSignal:
    """
    Complete entry signal returned to the orchestrator.
    Contains everything needed to:
      1. Gate entry via confidence score
      2. Calculate position size (entry/sl/tp)
      3. Tag the trade DNA
    """
    has_signal: bool
    direction: str              # "BUY" | "SELL" | "NONE"
    signal_type: str            # "OB_RETEST" | "SD_BOUNCE" | "FVG_FILL" | etc.
    pattern: str                # candlestick pattern name
    entry_price: float
    sl_price: float
    tp_price: float
    r_ratio: float              # TP distance / SL distance
    strength: float             # 0–1 composite signal quality
    key_level_proximity: float  # 0–1 (1 = price exactly at key level)
    ob: Optional[OrderBlock]
    fvg: Optional[FairValueGap]
    sd_zone: Optional[SDZone]
    sweep: Optional[LiquiditySweep]
    structure: Optional[MarketStructure]
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        if not self.has_signal:
            return "PriceActionSignal: NO SIGNAL"
        return (
            f"PriceActionSignal: {self.direction} {self.signal_type} "
            f"| Pattern={self.pattern} "
            f"| Entry={self.entry_price:.5f} SL={self.sl_price:.5f} TP={self.tp_price:.5f} "
            f"| R={self.r_ratio:.1f} Strength={self.strength:.2f}"
        )


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def evaluate(
    symbol: str,
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    regime: str,
    direction_bias: str,
    atr: float,
) -> PriceActionSignal:
    """
    Full price action evaluation pipeline.

    Args:
        symbol:         trading pair
        h1_df:          H1 OHLCV — for macro structure + OB identification
        m15_df:         M15 OHLCV — for setup confirmation
        m5_df:          M5 OHLCV — for entry trigger and pattern
        regime:         5-state tradeable regime label (quiet / chaotic filtered upstream)
        direction_bias: BULLISH | BEARISH | NEUTRAL from regime detector
        atr:            current ATR value for SL/TP scaling

    Returns:
        PriceActionSignal — always returned, has_signal=False if no setup.
    """
    no_signal = _empty_signal()

    if regime not in (TRENDING_REGIMES | RANGING_REGIMES):
        return no_signal

    # -- Step 0: Session check (OFF = allowed but penalised) --------
    # Run #3 proved OFF-session can produce winners (NZDUSD +$217).
    # Instead of hard-blocking, we let the signal through. The confidence
    # scorer already penalises OFF via spread_session component (0/5),
    # which naturally raises the bar for OFF-session trades.
    current_ts = m5_df.index[-1].to_pydatetime() if len(m5_df.index) else datetime.now(timezone.utc)
    _is_off_session = session_label(current_ts) == "OFF"
    if _is_off_session:
        logger.debug("[%s] OFF session -- signals allowed but penalised by confidence.", symbol)

    # -- Step 1: Build market structure on H1 -------------
    structure = _build_market_structure(h1_df)

    # -- Step 2: Find key SMC elements on H1 --------------
    order_blocks  = _find_order_blocks(h1_df, structure)
    fvgs          = _find_fvgs(h1_df)
    sweeps        = _find_liquidity_sweeps(h1_df, structure)
    sd_zones      = _find_sd_zones(h1_df)
    key_levels    = _find_key_levels(h1_df, symbol)

    # -- Step 2b: M15 setup confirmation (spec: ALL 3 TFs must align) --------
    # M15 must show a BOS or CHoCH in the bias direction, or an OB/zone retest.
    # If M15 structure contradicts the H1 bias, we skip -- no trade.
    # WHY: M15 is the setup confirmation layer. A perfect H1 OB signal with
    # M15 trending the other way is a trap, not an opportunity.
    m15_confirmed, m15_reason = _confirm_m15_setup(m15_df, structure, direction_bias)
    if not m15_confirmed:
        logger.debug(
            "[%s] M15 confirmation failed: %s -- no trade.", symbol, m15_reason
        )
        return no_signal

    # -- Step 3: Identify M5 entry pattern (spec: M5 = precise entry trigger) -
    # M5 provides the exact candle confirmation at the identified level.
    pattern, pattern_strength = _detect_pattern(m5_df)

    # -- Step 4: Route to regime strategy -----------------
    scalp_allowed, scalp_reason = scalp_session_allowed(symbol, current_ts)

    if regime in TRENDING_REGIMES:
        signal = _evaluate_trending(
            symbol, h1_df, m5_df,
            structure, order_blocks, fvgs, sweeps,
            key_levels, direction_bias, pattern, pattern_strength, atr, regime,
        )

        if not signal.has_signal and scalp_allowed:
            signal = _evaluate_scalp_pullback(
                symbol, m15_df, m5_df, key_levels,
                direction_bias, pattern, pattern_strength, atr, regime,
            )
        if not signal.has_signal:
            swing_breakout_ok, _ = breakout_allowed(regime)
            if swing_breakout_ok:
                signal = _evaluate_swing_breakout(
                    symbol, h1_df, m5_df, key_levels,
                    direction_bias, pattern, pattern_strength, atr, current_ts,
                )
    else:
        signal = _evaluate_ranging(
            symbol, h1_df, m5_df,
            structure, sd_zones, key_levels,
            direction_bias, pattern, pattern_strength, atr, current_ts,
        )
        if not signal.has_signal and scalp_allowed:
            signal = _evaluate_scalp_sweep_reversal(
                symbol, m15_df, m5_df, key_levels,
                pattern, pattern_strength, atr, regime,
            )
        if not signal.has_signal:
            swing_reversion_ok, _ = reversion_allowed(regime)
            if swing_reversion_ok:
                signal = _evaluate_swing_reversion(
                    symbol, h1_df, m5_df, key_levels,
                    pattern, pattern_strength, atr,
                )

    if not signal.has_signal and not scalp_allowed:
        logger.debug("[%s] Scalp layer skipped: %s", symbol, scalp_reason)

    if signal.has_signal:
        signal.details.setdefault("regime", regime)
        signal.details.setdefault("scalp_session_allowed", scalp_allowed)
        logger.info("[%s] %s", symbol, signal)
    else:
        logger.debug("[%s] No PA signal in %s regime.", symbol, regime)

    return signal


# ---------------------------------------------------------
# M15 Setup Confirmation  (GAP 4 fix -- spec: all 3 TFs must align)
# ---------------------------------------------------------

def _confirm_m15_setup(
    m15_df:         pd.DataFrame,
    h1_structure:   MarketStructure,
    direction_bias: str,
) -> tuple[bool, str]:
    """
    Confirm that M15 structure aligns with the H1 bias before allowing entry.

    WHY THIS IS NON-NEGOTIABLE:
    The spec states "ALL 3 timeframes must align before any trade."
    M15 is the setup confirmation layer. An H1 OB signal while M15 is in
    a clear opposing trend is a classic retail trap -- institutions use
    the higher TF to induce entries, then sweep stops.

    CONFIRMATION CRITERIA:
      1. M15 trend is not actively opposing the bias direction.
         (Last 10 M15 candles should not be a clean opposite move)
      2. M15 shows a recent BOS or structure break in the bias direction
         within the last 20 candles.
      3. Fallback: if M15 data is insufficient (<20 candles), allow
         the trade with a warning (don't block on data gaps).

    Returns (confirmed: bool, reason: str)
    """
    if m15_df is None or len(m15_df) < 20:
        return True, "M15 data insufficient -- allowing with H1 bias only"

    closes    = m15_df["close"].values
    highs     = m15_df["high"].values
    lows      = m15_df["low"].values
    last_20_c = closes[-20:]

    # --- Check 1: is M15 in a clean opposing trend? -----------------------
    # A "clean opposing trend" = last 10 closes are monotonically moving
    # against the bias direction (e.g. bias=BULLISH but M15 making LH/LL)
    last_10 = closes[-10:]
    if direction_bias == "BULLISH":
        # Opposing = last 10 M15 closes all declining
        opposing = all(last_10[i] > last_10[i+1] for i in range(len(last_10)-1))
        if opposing:
            return False, "M15 in clean bearish trend opposing BULLISH H1 bias"
    elif direction_bias == "BEARISH":
        # Opposing = last 10 M15 closes all rising
        opposing = all(last_10[i] < last_10[i+1] for i in range(len(last_10)-1))
        if opposing:
            return False, "M15 in clean bullish trend opposing BEARISH H1 bias"

    # --- Check 2: recent M15 BOS in the bias direction --------------------
    # A BOS on M15 = price breaks above the last swing high (BULLISH)
    #               or below the last swing low (BEARISH)
    # We look at the last 20 candles and check if the recent close
    # exceeded the prior 10-candle high/low.
    prior_high = float(highs[-20:-10].max()) if len(highs) >= 20 else float(highs.max())
    prior_low  = float(lows[-20:-10].min())  if len(lows)  >= 20 else float(lows.min())
    recent_high = float(highs[-10:].max())
    recent_low  = float(lows[-10:].min())
    recent_close = float(closes[-1])

    if direction_bias == "BULLISH":
        # M15 BOS: recent close broke above the prior swing high
        if recent_close > prior_high:
            return True, "M15 BOS confirmed in BULLISH direction"
        # M15 at equilibrium (neither BOS nor opposing trend) -- allow
        if recent_close >= prior_low:
            return True, "M15 neutral -- no opposing trend detected"
        # M15 broke below prior low = bearish BOS = opposing H1 bias
        return False, "M15 bearish BOS contradicts BULLISH H1 bias"

    elif direction_bias == "BEARISH":
        if recent_close < prior_low:
            return True, "M15 BOS confirmed in BEARISH direction"
        if recent_close <= prior_high:
            return True, "M15 neutral -- no opposing trend detected"
        return False, "M15 bullish BOS contradicts BEARISH H1 bias"

    # NEUTRAL bias -- no M15 filter applied
    return True, "H1 bias is NEUTRAL -- M15 filter skipped"


# ---------------------------------------------------------
# Market Structure
# ---------------------------------------------------------

def _find_swing_points(df: pd.DataFrame, strength: int = 3) -> tuple[list[SwingPoint], list[SwingPoint]]:
    """
    Identify swing highs and swing lows using a fractal approach.

    A swing high is a candle whose high is higher than the `strength`
    candles on each side. Same logic for swing lows.

    `strength=3` means 3 candles must be lower/higher on both sides
    — this is the institutional standard for significant structure points.

    WHY NOT JUST ROLLING MAX:
    Rolling max finds the highest point in a window but doesn't
    distinguish between a true swing and a micro fluctuation. The
    fractal n-bar pivot method is what professional charting tools use.
    """
    highs: list[SwingPoint] = []
    lows:  list[SwingPoint] = []

    n = len(df)
    for i in range(strength, n - strength):
        candle_high  = df["high"].iloc[i]
        candle_low   = df["low"].iloc[i]

        # Swing high: higher than `strength` bars before and after
        left_highs  = df["high"].iloc[i - strength: i]
        right_highs = df["high"].iloc[i + 1: i + strength + 1]
        if (candle_high > left_highs.max()) and (candle_high > right_highs.max()):
            highs.append(SwingPoint(
                index=i,
                time=df.index[i],
                price=candle_high,
                kind="HIGH",
            ))

        # Swing low: lower than `strength` bars before and after
        left_lows  = df["low"].iloc[i - strength: i]
        right_lows = df["low"].iloc[i + 1: i + strength + 1]
        if (candle_low < left_lows.min()) and (candle_low < right_lows.min()):
            lows.append(SwingPoint(
                index=i,
                time=df.index[i],
                price=candle_low,
                kind="LOW",
            ))

    return highs, lows


def _build_market_structure(df: pd.DataFrame) -> MarketStructure:
    """
    Classify overall market structure and detect BOS / CHoCH events.

    Algorithm:
      1. Find swing highs and lows
      2. Walk through them chronologically, labelling HH/HL/LH/LL
      3. BOS: new HH breaks above previous swing high (uptrend continuation)
             new LL breaks below previous swing low  (downtrend continuation)
      4. CHoCH: in an uptrend, a new LL forms -> potential reversal (bearish CHoCH)
                in a downtrend, a new HH forms -> potential reversal (bullish CHoCH)

    Returns a MarketStructure with the last 20 labelled events and
    BOS/CHoCH timestamps.
    """
    swing_highs, swing_lows = _find_swing_points(df, strength=3)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return MarketStructure(
            trend="NEUTRAL",
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            last_bos_time=None,
            last_choch_time=None,
            last_bos_direction="NONE",
            structure_points=[],
        )

    structure_points = []
    last_bos_time = None
    last_choch_time = None
    last_bos_dir = "NONE"

    # Alternate between examining highs and lows chronologically
    all_swings = sorted(swing_highs + swing_lows, key=lambda s: s.index)

    prev_high_price = None
    prev_low_price  = None
    trend = "NEUTRAL"

    for i, swing in enumerate(all_swings):
        if swing.kind == "HIGH":
            if prev_high_price is not None:
                if swing.price > prev_high_price:
                    label = "HH"
                    if trend == "BULLISH":
                        last_bos_time = swing.time
                        last_bos_dir  = "BULLISH"
                    trend = "BULLISH"
                else:
                    label = "LH"
                    if trend == "BULLISH":      # was bullish, now LH -> CHoCH
                        last_choch_time = swing.time
                        trend = "BEARISH"
            else:
                label = "SH"   # first swing high, just label it
            structure_points.append(label)
            prev_high_price = swing.price

        else:  # LOW
            if prev_low_price is not None:
                if swing.price < prev_low_price:
                    label = "LL"
                    if trend == "BEARISH":
                        last_bos_time = swing.time
                        last_bos_dir  = "BEARISH"
                    trend = "BEARISH"
                else:
                    label = "HL"
                    if trend == "BEARISH":      # was bearish, now HL -> CHoCH
                        last_choch_time = swing.time
                        trend = "BULLISH"
            else:
                label = "SL"
            structure_points.append(label)
            prev_low_price = swing.price

    return MarketStructure(
        trend=trend,
        swing_highs=swing_highs[-20:],
        swing_lows=swing_lows[-20:],
        last_bos_time=last_bos_time,
        last_choch_time=last_choch_time,
        last_bos_direction=last_bos_dir,
        structure_points=structure_points[-20:],
    )


# ---------------------------------------------------------
# Order Blocks
# ---------------------------------------------------------

def _find_order_blocks(
    df: pd.DataFrame,
    structure: MarketStructure,
) -> list[OrderBlock]:
    """
    Find Order Blocks — zones where institutions placed orders.

    Definition:
      BULLISH OB: The LAST BEARISH candle immediately before a strong
                  bullish impulse that creates a BOS (breaks a swing high).
                  This is the "institutional demand" zone.

      BEARISH OB: The LAST BULLISH candle immediately before a strong
                  bearish impulse that creates a BOS (breaks a swing low).
                  This is the "institutional supply" zone.

    Why the LAST opposing candle:
      Institutions don't place all their orders in one candle. They layer
      orders. But the LAST bearish candle before a bullish explosion is
      where unfilled orders most likely sit — price will return there.

    Strength scoring:
      Measured by the impulse size (close-to-close move of the following
      3 candles) relative to recent ATR. Larger impulse = stronger OB.
    """
    obs: list[OrderBlock] = []
    n = len(df)
    lookback = min(OB_LOOKBACK, n - 5)

    # Recent ATR for strength normalisation
    recent_ranges = (df["high"] - df["low"]).tail(20)
    avg_range = recent_ranges.mean() if len(recent_ranges) > 0 else 0.001

    swing_high_prices = {s.index for s in structure.swing_highs}
    swing_low_prices  = {s.index for s in structure.swing_lows}

    for i in range(n - lookback, n - 4):
        # Look ahead 3 candles for an impulse move
        impulse_window = df.iloc[i + 1: i + 4]
        if len(impulse_window) < 3:
            continue

        candle = df.iloc[i]
        body   = candle["close"] - candle["open"]
        is_bearish = body < 0
        is_bullish = body > 0

        # -- Bullish OB: last bearish candle before bullish impulse --
        if is_bearish:
            # Impulse: measure how far price EXTENDED upward (max high vs first open)
            # Using max high is correct -- we care about the full upward reach,
            # not net close-to-open which can be dampened by retracement wicks.
            bullish_impulse = (
                float(impulse_window["high"].max()) - float(impulse_window["open"].iloc[0])
            )
            if bullish_impulse > avg_range * 1.0:   # impulse must be at least 1.0x ATR
                ob_top    = float(candle["high"])
                ob_bottom = float(candle["low"])

                strength = min(bullish_impulse / (avg_range * 3), 1.0)
                tested = _is_ob_tested(df, i, ob_top, ob_bottom, "BULLISH")

                obs.append(OrderBlock(
                    time=df.index[i],
                    top=ob_top,
                    bottom=ob_bottom,
                    mid=(ob_top + ob_bottom) / 2,
                    direction="BULLISH",
                    strength=round(strength, 3),
                    tested=tested,
                    index=i,
                ))

        # -- Bearish OB: last bullish candle before bearish impulse --
        elif is_bullish:
            # Impulse: how far price EXTENDED downward (first open vs min low)
            bearish_impulse = (
                float(impulse_window["open"].iloc[0]) - float(impulse_window["low"].min())
            )
            if bearish_impulse > avg_range * 1.0:
                ob_top    = candle["high"]
                ob_bottom = candle["low"]

                strength = min(bearish_impulse / (avg_range * 3), 1.0)
                tested = _is_ob_tested(df, i, ob_top, ob_bottom, "BEARISH")

                obs.append(OrderBlock(
                    time=df.index[i],
                    top=ob_top,
                    bottom=ob_bottom,
                    mid=(ob_top + ob_bottom) / 2,
                    direction="BEARISH",
                    strength=round(strength, 3),
                    tested=tested,
                    index=i,
                ))

    # Return strongest OBs, most recent first, excluding already-tested ones
    obs = [ob for ob in obs if not ob.tested]
    obs.sort(key=lambda o: (o.strength, o.index), reverse=True)
    return obs[:10]


def _is_ob_tested(
    df: pd.DataFrame,
    ob_index: int,
    top: float,
    bottom: float,
    direction: str,
) -> bool:
    """
    Check if this OB has been INVALIDATED — price closed through the zone.

    An OB is invalidated (not worth trading) only when price has closed
    BEYOND the zone boundary, confirming the institutional level was broken.

    Note: A retest (price touches the OB and bounces) does NOT invalidate.
    Only a close through the bottom (bullish OB) or top (bearish OB) does.
    """
    subsequent = df.iloc[ob_index + 4:]   # skip the impulse window itself
    if subsequent.empty:
        return False

    if direction == "BULLISH":
        # Invalidated if price closed BELOW the OB bottom (zone broken)
        return bool((subsequent["close"] < bottom).any())
    else:
        # Invalidated if price closed ABOVE the OB top (zone broken)
        return bool((subsequent["close"] > top).any())


# ---------------------------------------------------------
# Fair Value Gaps
# ---------------------------------------------------------

def _find_fvgs(df: pd.DataFrame) -> list[FairValueGap]:
    """
    Detect Fair Value Gaps (FVG) — price imbalances between candles.

    A bullish FVG exists when candle[i-1].high < candle[i+1].low
    There is a gap in price — no trading occurred between these levels.
    Price is drawn back to fill this gap (it acts as a magnet).

    A bearish FVG is the mirror: candle[i-1].low > candle[i+1].high

    Why FVGs matter:
    Institutions create FVGs when they execute large orders. The gap
    represents unfilled orders. Price returns to fill those orders.
    """
    fvgs: list[FairValueGap] = []
    n = len(df)

    for i in range(1, n - 1):
        prev  = df.iloc[i - 1]
        curr  = df.iloc[i]
        nxt   = df.iloc[i + 1]

        # Body size filter — weak doji candles don't create valid FVGs
        curr_body = abs(curr["close"] - curr["open"])
        curr_range = curr["high"] - curr["low"]
        if curr_range > 0 and curr_body / curr_range < FVG_MIN_BODY_RATIO:
            continue

        # Bullish FVG
        if prev["high"] < nxt["low"]:
            gap_top    = nxt["low"]
            gap_bottom = prev["high"]
            filled     = _is_fvg_filled(df, i, gap_top, gap_bottom, "BULLISH")

            fvgs.append(FairValueGap(
                time=df.index[i],
                top=gap_top,
                bottom=gap_bottom,
                mid=(gap_top + gap_bottom) / 2,
                direction="BULLISH",
                filled=filled,
            ))

        # Bearish FVG
        elif prev["low"] > nxt["high"]:
            gap_top    = prev["low"]
            gap_bottom = nxt["high"]
            filled     = _is_fvg_filled(df, i, gap_top, gap_bottom, "BEARISH")

            fvgs.append(FairValueGap(
                time=df.index[i],
                top=gap_top,
                bottom=gap_bottom,
                mid=(gap_top + gap_bottom) / 2,
                direction="BEARISH",
                filled=filled,
            ))

    unfilled = [f for f in fvgs if not f.filled]
    return unfilled[-15:]   # keep most recent 15 unfilled FVGs


def _is_fvg_filled(
    df: pd.DataFrame,
    fvg_index: int,
    top: float,
    bottom: float,
    direction: str,
) -> bool:
    """FVG is filled when a subsequent candle trades through the gap."""
    subsequent = df.iloc[fvg_index + 2:]
    if subsequent.empty:
        return False
    if direction == "BULLISH":
        return bool((subsequent["low"] <= bottom).any())
    else:
        return bool((subsequent["high"] >= top).any())


# ---------------------------------------------------------
# Liquidity Sweeps
# ---------------------------------------------------------

def _find_liquidity_sweeps(
    df: pd.DataFrame,
    structure: MarketStructure,
) -> list[LiquiditySweep]:
    """
    Detect liquidity sweeps — stop hunts above swing highs or below swing lows.

    A sweep occurs when price briefly violates a swing level by a small
    buffer (stop-hunt) and then quickly reverses, closing back inside.

    WHY THIS MATTERS:
    Institutions grab retail stop-losses to fuel their own entry.
    A sweep of sell-side liquidity (below swing lows) followed by a
    reversal is a BULLISH signal — institutions bought the dip.
    A sweep of buy-side liquidity (above swing highs) followed by
    reversal is BEARISH — institutions sold the top.
    """
    sweeps: list[LiquiditySweep] = []
    n = len(df)

    for swing in structure.swing_highs[-10:]:
        level = swing.price
        # Look for candles after the swing that spike above but close below
        for i in range(swing.index + 1, min(swing.index + 20, n)):
            candle = df.iloc[i]
            if candle["high"] > level + LIQUIDITY_SWEEP_BUFFER:
                # Spike above — is there a bearish close below the level?
                reversal = candle["close"] < level
                sweeps.append(LiquiditySweep(
                    time=df.index[i],
                    swept_level=level,
                    direction="BEARISH_SWEEP",
                    reversal_confirmed=reversal,
                ))
                break

    for swing in structure.swing_lows[-10:]:
        level = swing.price
        for i in range(swing.index + 1, min(swing.index + 20, n)):
            candle = df.iloc[i]
            if candle["low"] < level - LIQUIDITY_SWEEP_BUFFER:
                reversal = candle["close"] > level
                sweeps.append(LiquiditySweep(
                    time=df.index[i],
                    swept_level=level,
                    direction="BULLISH_SWEEP",
                    reversal_confirmed=reversal,
                ))
                break

    # Only return confirmed reversals — unconfirmed sweeps are just noise
    return [s for s in sweeps if s.reversal_confirmed]


# ---------------------------------------------------------
# Supply & Demand Zones
# ---------------------------------------------------------

def _find_sd_zones(df: pd.DataFrame) -> list[SDZone]:
    """
    Find Supply and Demand zones — origin points of strong impulsive moves.

    Definition:
      DEMAND zone: Area where price consolidated then launched UP strongly.
                   (Base before rally)
      SUPPLY zone: Area where price consolidated then dropped DOWN strongly.
                   (Base before drop)

    These differ from Order Blocks in that they capture broader
    consolidation zones, not just a single candle.  Used primarily
    in RANGING regime for mean reversion targets.

    Detection algorithm:
      1. Find consolidation areas (low range candles in sequence)
      2. Measure the subsequent impulse
      3. Strong impulse after consolidation -> valid S&D zone
    """
    zones: list[SDZone] = []
    n = len(df)

    avg_range = (df["high"] - df["low"]).tail(50).mean()
    if avg_range == 0:
        return zones

    for i in range(3, n - 5):
        # Consolidation: 2–3 candles with below-average range
        consol_window = df.iloc[i - 2: i + 1]
        avg_consol_range = (consol_window["high"] - consol_window["low"]).mean()

        if avg_consol_range > avg_range * 0.7:
            continue   # not a consolidation — range too wide

        # Measure post-consolidation impulse (next 3 candles)
        impulse_window = df.iloc[i + 1: i + 4]
        if len(impulse_window) < 3:
            continue

        impulse_move = impulse_window["close"].iloc[-1] - consol_window["close"].iloc[-1]
        strength = min(abs(impulse_move) / (avg_range * 3), 1.0)

        if abs(impulse_move) < avg_range * 1.2:
            continue   # impulse not strong enough to define a zone

        zone_top    = consol_window["high"].max()
        zone_bottom = consol_window["low"].min()

        if impulse_move > 0:
            kind = "DEMAND"
        else:
            kind = "SUPPLY"

        tested = _is_zone_tested(df, i, zone_top, zone_bottom, kind)

        zones.append(SDZone(
            time=df.index[i],
            top=zone_top,
            bottom=zone_bottom,
            mid=(zone_top + zone_bottom) / 2,
            kind=kind,
            strength=round(strength, 3),
            tested=tested,
        ))

    # Return untested zones, strongest first
    zones = [z for z in zones if not z.tested]
    zones.sort(key=lambda z: z.strength, reverse=True)
    return zones[:8]


def _is_zone_tested(
    df: pd.DataFrame,
    zone_index: int,
    top: float,
    bottom: float,
    kind: str,
) -> bool:
    """
    Zone is INVALIDATED if price closed through its boundary.

    A demand zone is invalidated when price closes BELOW the zone bottom.
    A supply zone is invalidated when price closes ABOVE the zone top.
    A retest (price enters and bounces) does NOT invalidate the zone.
    """
    subsequent = df.iloc[zone_index + 4:]
    if subsequent.empty:
        return False
    if kind == "DEMAND":
        return bool((subsequent["close"] < bottom).any())
    else:
        return bool((subsequent["close"] > top).any())


# ---------------------------------------------------------
# Key Levels
# ---------------------------------------------------------

def _find_key_levels(df: pd.DataFrame, symbol: str) -> list[float]:
    """
    Compile a list of significant price levels.

    Levels:
      1. Previous Day High (PDH) and Low (PDL)
      2. Previous Week High and Low
      3. Round number levels (every 50 pips / 0.0050 for standard pairs)
      4. Session highs / lows (identified from volume-implied activity)

    Why key levels matter:
    These are the prices where retail traders cluster their orders.
    Institutions know this and use these levels as liquidity pools.
    Entries near key levels have higher probability and better R:R.
    """
    levels: list[float] = []

    if df.empty or len(df) < 24:
        return levels

    # Previous Day H/L (last 24 H1 candles ≈ 1 trading day)
    prev_day = df.iloc[-48:-24]   # day before last
    if not prev_day.empty:
        levels.append(float(prev_day["high"].max()))   # PDH
        levels.append(float(prev_day["low"].min()))    # PDL

    # Previous Week H/L (last ~120 H1 candles ≈ 5 trading days)
    prev_week = df.iloc[-240:-120] if len(df) >= 240 else df.iloc[:len(df)//2]
    if not prev_week.empty:
        levels.append(float(prev_week["high"].max()))
        levels.append(float(prev_week["low"].min()))

    # Round numbers: every 0.0050 for standard pairs, every 0.50 for JPY pairs
    current_price = float(df["close"].iloc[-1])
    is_jpy = "JPY" in symbol
    pip_step = 0.50 if is_jpy else 0.0050

    # Generate round levels within ±1% of current price
    lower = current_price * 0.99
    upper = current_price * 1.01
    level = round(lower / pip_step) * pip_step
    while level <= upper:
        levels.append(round(level, 5))
        level += pip_step

    return sorted(set(levels))


def _key_level_proximity(price: float, key_levels: list[float], atr: float) -> float:
    """
    Score 0–1 for how close `price` is to the nearest key level.
    1.0 = price is exactly at a key level.
    0.0 = price is > 1 ATR away from any level.
    """
    if not key_levels or atr == 0:
        return 0.0
    min_dist = min(abs(price - lvl) for lvl in key_levels)
    proximity = max(0.0, 1.0 - min_dist / atr)
    return round(proximity, 3)


# ---------------------------------------------------------
# Candlestick Pattern Detection
# ---------------------------------------------------------

def _detect_pattern(df: pd.DataFrame) -> tuple[str, float]:
    """
    Detect the most recent candlestick pattern on the last few M5 candles.

    Scans the last 2 trigger bars (last and prev) and returns the strongest
    pattern found. Using a multi-bar window prevents the common case where
    a strong rejection formed one bar ago but the current bar is a small
    continuation — previously counted as PATTERN_NONE despite valid setup.

    Stale patterns (prev bar) are returned at 0.8x strength to prefer
    fresh triggers when both exist.

    Returns (pattern_name, strength_0_to_1). "NONE" with 0.0 if nothing.
    """
    if df is None or len(df) < 3:
        return "NONE", 0.0

    # Fresh trigger: evaluate (last, prev, prev2).
    fresh = _detect_pattern_window(df, offset=0)
    if fresh[0] != "NONE":
        return fresh

    # Stale trigger: evaluate (prev, prev2, prev3) if available.
    if len(df) >= 4:
        stale_name, stale_strength = _detect_pattern_window(df, offset=1)
        if stale_name != "NONE":
            return stale_name, round(stale_strength * 0.8, 3)

    return "NONE", 0.0


def _detect_pattern_window(df: pd.DataFrame, offset: int) -> tuple[str, float]:
    """
    Core single-bar pattern detector evaluated at df.iloc[-1-offset] as "last",
    df.iloc[-2-offset] as "prev", df.iloc[-3-offset] as "prev2".
    """
    idx_last  = -1 - offset
    idx_prev  = -2 - offset
    idx_prev2 = -3 - offset
    if abs(idx_prev2) > len(df):
        return "NONE", 0.0

    last   = df.iloc[idx_last]
    prev   = df.iloc[idx_prev]
    prev2  = df.iloc[idx_prev2]

    open_  = float(last["open"])
    close_ = float(last["close"])
    high_  = float(last["high"])
    low_   = float(last["low"])
    body   = close_ - open_
    candle_range = high_ - low_

    if candle_range == 0:
        return "NONE", 0.0

    body_ratio  = abs(body) / candle_range
    upper_wick  = high_ - max(open_, close_)
    lower_wick  = min(open_, close_) - low_
    upper_ratio = upper_wick / candle_range
    lower_ratio = lower_wick / candle_range

    prev_body  = float(prev["close"]) - float(prev["open"])
    prev_range = float(prev["high"])  - float(prev["low"])

    # -- Engulfing -----------------------------------------
    # Current candle body fully engulfs previous candle body
    if abs(body) > abs(prev_body) * 1.0:
        if (body > 0 and prev_body < 0          # bullish engulfing
                and close_ > float(prev["open"])
                and open_  < float(prev["close"])):
            strength = min(abs(body) / (candle_range * 0.7 + 1e-9), 1.0)
            return "BULLISH_ENGULFING", round(strength * body_ratio, 3)

        if (body < 0 and prev_body > 0          # bearish engulfing
                and close_ < float(prev["open"])
                and open_  > float(prev["close"])):
            strength = min(abs(body) / (candle_range * 0.7 + 1e-9), 1.0)
            return "BEARISH_ENGULFING", round(strength * body_ratio, 3)

    # -- Pin Bar / Hammer / Shooting Star -----------------
    # Long wick ≥ 2x body, small body at one end
    if lower_ratio >= 0.60 and body_ratio <= 0.30:
        # Long lower wick -> bullish rejection (Hammer / Pin Bar)
        return "PIN_BAR_BULLISH", round(lower_ratio, 3)

    if upper_ratio >= 0.60 and body_ratio <= 0.30:
        # Long upper wick -> bearish rejection (Shooting Star / Pin Bar)
        return "PIN_BAR_BEARISH", round(upper_ratio, 3)

    # -- Doji ----------------------------------------------
    # Body < 10% of range -> indecision -> NO TRADE
    if body_ratio < 0.10:
        return "DOJI", 0.0   # Doji = wait, never trade

    # -- Inside Bar ----------------------------------------
    # Current candle fully inside previous candle's range -> breakout setup
    if (high_ < float(prev["high"]) and low_ > float(prev["low"])):
        return "INSIDE_BAR", round(1 - body_ratio, 3)   # tighter = stronger

    # -- Morning Star (bullish 3-candle reversal) ----------
    if prev2 is not None:
        p2_body = float(prev2["close"]) - float(prev2["open"])
        if (p2_body < 0 and abs(prev_body) < abs(p2_body) * 0.4
                and body > 0 and close_ > float(prev2["open"]) * 0.5 + float(prev2["close"]) * 0.5):
            return "MORNING_STAR", 0.75

    # -- Evening Star (bearish 3-candle reversal) ----------
    if prev2 is not None:
        p2_body = float(prev2["close"]) - float(prev2["open"])
        if (p2_body > 0 and abs(prev_body) < abs(p2_body) * 0.4
                and body < 0 and close_ < float(prev2["open"]) * 0.5 + float(prev2["close"]) * 0.5):
            return "EVENING_STAR", 0.75

    return "NONE", 0.0


# ---------------------------------------------------------
# Strategy Evaluators
# ---------------------------------------------------------

def _evaluate_trending(
    symbol: str,
    h1_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    structure: MarketStructure,
    order_blocks: list[OrderBlock],
    fvgs: list[FairValueGap],
    sweeps: list[LiquiditySweep],
    key_levels: list[float],
    direction_bias: str,
    pattern: str,
    pattern_strength: float,
    atr: float,
    regime: str,
) -> PriceActionSignal:
    """
    Trending regime strategy: SMC momentum entry.

    Setup requirements (all must be true):
      1. Price is near or inside a valid Order Block (OB)
      2. OB direction matches H1 structure trend + direction_bias
      3. A bullish/bearish candlestick pattern confirms at the OB
      4. An FVG or liquidity sweep adds confluence (optional, boosts score)

    Entry:
      BUY : at OB mid-price (after confirmation candle closes)
      SL  : 2 pips below OB bottom (bullish) / above OB top (bearish)
      TP  : next swing high (bullish) / swing low (bearish)

    WHY WE WAIT FOR THE OB RETEST:
    We never chase a move. We wait for price to return to the origin
    of the impulse (the OB) and confirm the institutional interest
    is still there via a pattern. This is the SMC way.
    """
    current_price = float(m5_df["close"].iloc[-1])

    for ob in order_blocks:
        # Direction alignment check
        if direction_bias == "BULLISH" and ob.direction != "BULLISH":
            continue
        if direction_bias == "BEARISH" and ob.direction != "BEARISH":
            continue
        if direction_bias == "NEUTRAL":
            continue    # don't trade without directional confirmation

        # Price proximity to OB (OB_RETEST family: 2.3 ATR window).
        # Tuned up from 2.0 to recover "OB present but not near price" leaks
        # (~14% of non-chaotic bars in diag).
        in_ob = (ob.bottom <= current_price <= ob.top)
        near_ob = abs(current_price - ob.mid) <= atr * 2.3

        if not (in_ob or near_ob):
            continue

        # Pattern must be directionally aligned
        bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
        bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

        if ob.direction == "BULLISH" and pattern not in bullish_patterns:
            continue
        if ob.direction == "BEARISH" and pattern not in bearish_patterns:
            continue
        if pattern == "DOJI":
            continue    # never trade on doji confirmation

        # Calculate entry, SL, TP
        pip_buffer = atr * 0.15   # small buffer beyond OB for SL

        if ob.direction == "BULLISH":
            entry = ob.mid
            sl    = ob.bottom - pip_buffer
            # TP: next swing high above entry
            tp = _next_swing_target(structure.swing_highs, entry, direction="UP")
            if tp is None or tp <= entry:
                tp = entry + atr * 3
            direction = "BUY"
        else:
            entry = ob.mid
            sl    = ob.top + pip_buffer
            tp = _next_swing_target(structure.swing_lows, entry, direction="DOWN")
            if tp is None or tp >= entry:
                tp = entry - atr * 3
            direction = "SELL"

        # R:R calculation
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0

        if r_ratio < 1.5:
            continue    # minimum 1.5R — poor setups rejected here

        # Confluence boosts
        fvg_confluence  = _price_near_fvg(current_price, fvgs, atr)
        sweep_confluence = any(
            abs(current_price - s.swept_level) < atr * 0.3
            for s in sweeps
        )
        klp = _key_level_proximity(current_price, key_levels, atr)

        if regime == REGIME_TRENDING_EXTENDED:
            if pattern_strength < 0.55:
                continue
            if not (fvg_confluence > 0 or sweep_confluence or klp >= 0.35):
                continue
            if r_ratio < 1.8:
                continue

        strength = _compute_trending_strength(
            ob.strength, pattern_strength, fvg_confluence,
            sweep_confluence, klp, r_ratio,
        )

        return PriceActionSignal(
            has_signal=True,
            direction=direction,
            signal_type="OB_RETEST",
            pattern=pattern,
            entry_price=round(entry, 5),
            sl_price=round(sl, 5),
            tp_price=round(tp, 5),
            r_ratio=round(r_ratio, 2),
            strength=round(strength, 3),
            key_level_proximity=klp,
            ob=ob,
            fvg=fvgs[0] if fvgs else None,
            sd_zone=None,
            sweep=sweeps[0] if sweeps else None,
            structure=structure,
            details={
                "ob_direction": ob.direction,
                "ob_strength": ob.strength,
                "fvg_confluence": fvg_confluence,
                "sweep_confluence": sweep_confluence,
                "pattern_strength": pattern_strength,
            },
        )

    return _empty_signal()


def _evaluate_ranging(
    symbol: str,
    h1_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    structure: MarketStructure,
    sd_zones: list[SDZone],
    key_levels: list[float],
    direction_bias: str,
    pattern: str,
    pattern_strength: float,
    atr: float,
    current_ts: datetime,
) -> PriceActionSignal:
    """
    Ranging regime strategy: Supply & Demand mean reversion.

    Setup requirements:
      1. Price is near a valid S&D zone boundary
      2. A reversal pattern confirms at the zone
      3. Opposite zone exists as a TP target (defines the trade)

    Entry:
      BUY  : at Demand zone top after bullish pattern
      SELL : at Supply zone bottom after bearish pattern
      SL   : just beyond the zone (invalidation level)
      TP   : mid-point of the opposite zone (conservative) or zone boundary (aggressive)
    """
    current_price = float(m5_df["close"].iloc[-1])
    mr_allowed, mr_reason = mean_reversion_session_allowed(symbol, current_ts)
    if not mr_allowed:
        return _empty_signal()

    bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
    bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

    demand_zones = [z for z in sd_zones if z.kind == "DEMAND"]
    supply_zones = [z for z in sd_zones if z.kind == "SUPPLY"]
    recent_range_high = float(h1_df["high"].tail(30).max())
    recent_range_low = float(h1_df["low"].tail(30).min())
    recent_closes = h1_df["close"].tail(48)
    h1_mean = float(recent_closes.mean()) if len(recent_closes) else current_price
    h1_std = float(recent_closes.std(ddof=0)) if len(recent_closes) > 1 else 0.0
    z_score = (current_price - h1_mean) / h1_std if h1_std > 1e-9 else 0.0

    # -- BUY from Demand zone ------------------------------
    if pattern in bullish_patterns and direction_bias != "BEARISH":
        for dz in demand_zones:
            # SD_BOUNCE family: 2.4 ATR window. Tuned up from 2.0 —
            # no_sd_near_price was the largest leak (~22%) in the diag run.
            near_demand = (
                abs(current_price - dz.top) <= atr * 2.4
                or current_price <= recent_range_low + atr * 0.8
            )

            if not near_demand:
                continue

            zone_edge_extension = current_price <= recent_range_low + atr * 0.5
            if z_score > -0.60 and not zone_edge_extension:
                continue
            if dz.strength < 0.58 and not zone_edge_extension:
                continue

            # Need a supply zone above as TP target
            supply_above = [s for s in supply_zones if s.bottom > dz.top]
            target_supply = min(supply_above, key=lambda s: s.bottom) if supply_above else None

            entry = dz.top
            sl    = dz.bottom - atr * 0.15
            tp    = target_supply.bottom if target_supply else (recent_range_high - atr * 0.10)

            sl_dist = entry - sl
            tp_dist = tp - entry
            r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0

            if r_ratio < 1.5:
                continue

            klp = _key_level_proximity(current_price, key_levels, atr)
            strength = _compute_ranging_strength(
                dz.strength, pattern_strength, klp, r_ratio
            )

            return PriceActionSignal(
                has_signal=True,
                direction="BUY",
                signal_type="SD_BOUNCE",
                pattern=pattern,
                entry_price=round(entry, 5),
                sl_price=round(sl, 5),
                tp_price=round(tp, 5),
                r_ratio=round(r_ratio, 2),
                strength=round(strength, 3),
                key_level_proximity=klp,
                ob=None,
                fvg=None,
                sd_zone=dz,
                sweep=None,
                structure=structure,
                details={
                    "zone_strength": dz.strength,
                    "target_supply": round(target_supply.mid, 5) if target_supply else round(recent_range_high, 5),
                    "range_fallback": target_supply is None,
                    "z_score": round(z_score, 2),
                    "session_filter": mr_reason,
                },
            )

    # -- SELL from Supply zone -----------------------------
    if pattern in bearish_patterns and direction_bias != "BULLISH":
        for sz in supply_zones:
            # SD_BOUNCE family: 2.4 ATR window (matches demand side).
            near_supply = (
                abs(current_price - sz.bottom) <= atr * 2.4
                or current_price >= recent_range_high - atr * 0.8
            )

            if not near_supply:
                continue

            zone_edge_extension = current_price >= recent_range_high - atr * 0.5
            if z_score < 0.60 and not zone_edge_extension:
                continue
            if sz.strength < 0.58 and not zone_edge_extension:
                continue

            demand_below = [d for d in demand_zones if d.top < sz.bottom]
            target_demand = max(demand_below, key=lambda d: d.top) if demand_below else None

            entry = sz.bottom
            sl    = sz.top + atr * 0.15
            tp    = target_demand.top if target_demand else (recent_range_low + atr * 0.10)

            sl_dist = sl - entry
            tp_dist = entry - tp
            r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0

            if r_ratio < 1.5:
                continue

            klp = _key_level_proximity(current_price, key_levels, atr)
            strength = _compute_ranging_strength(
                sz.strength, pattern_strength, klp, r_ratio
            )

            return PriceActionSignal(
                has_signal=True,
                direction="SELL",
                signal_type="SD_BOUNCE",
                pattern=pattern,
                entry_price=round(entry, 5),
                sl_price=round(sl, 5),
                tp_price=round(tp, 5),
                r_ratio=round(r_ratio, 2),
                strength=round(strength, 3),
                key_level_proximity=klp,
                ob=None,
                fvg=None,
                sd_zone=sz,
                sweep=None,
                structure=structure,
                details={
                    "zone_strength": sz.strength,
                    "target_demand": round(target_demand.mid, 5) if target_demand else round(recent_range_low, 5),
                    "range_fallback": target_demand is None,
                    "z_score": round(z_score, 2),
                    "session_filter": mr_reason,
                },
            )

    return _empty_signal()


def _evaluate_scalp_pullback(
    symbol: str,
    m15_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    key_levels: list[float],
    direction_bias: str,
    pattern: str,
    pattern_strength: float,
    atr: float,
    regime: str,
) -> PriceActionSignal:
    """
    Intraday continuation scalp.

    Purpose:
      Increase trade frequency during clean directional markets without
      abandoning structure. We require:
        - H1 directional bias
        - M5 fast EMA aligned with slow EMA
        - Price pulling back into the fast EMA area
        - A directional pattern on M5
    """
    if m5_df is None or len(m5_df) < 50 or direction_bias not in {"BULLISH", "BEARISH"}:
        return _empty_signal()

    closes = m5_df["close"]
    highs = m5_df["high"]
    lows = m5_df["low"]
    ema_fast = closes.ewm(span=20, adjust=False).mean()
    ema_slow = closes.ewm(span=50, adjust=False).mean()

    current_price = float(closes.iloc[-1])
    current_ema_fast = float(ema_fast.iloc[-1])
    current_ema_slow = float(ema_slow.iloc[-1])
    last_high = float(highs.tail(12).max())
    last_low = float(lows.tail(12).min())
    klp = _key_level_proximity(current_price, key_levels, atr)

    bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
    bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

    if direction_bias == "BULLISH":
        trend_ok = current_ema_fast > current_ema_slow and current_price >= current_ema_slow
        pullback_ok = abs(current_price - current_ema_fast) <= atr * 0.40 or abs(float(lows.iloc[-1]) - current_ema_fast) <= atr * 0.40
        if not (trend_ok and pullback_ok and pattern in bullish_patterns):
            return _empty_signal()

        entry = current_price
        sl = last_low - atr * 0.10
        tp = max(last_high + atr * 0.20, entry + atr * 1.8)
        direction = "BUY"
    else:
        trend_ok = current_ema_fast < current_ema_slow and current_price <= current_ema_slow
        pullback_ok = abs(current_price - current_ema_fast) <= atr * 0.40 or abs(float(highs.iloc[-1]) - current_ema_fast) <= atr * 0.40
        if not (trend_ok and pullback_ok and pattern in bearish_patterns):
            return _empty_signal()

        entry = current_price
        sl = last_high + atr * 0.10
        tp = min(last_low - atr * 0.20, entry - atr * 1.8)
        direction = "SELL"

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0.0
    if r_ratio < 1.5:
        return _empty_signal()

    ema_gap = abs(current_ema_fast - current_ema_slow)
    trend_strength = min(ema_gap / max(atr * 0.6, 1e-9), 1.0)
    strength = _compute_scalp_strength(
        base_quality=0.80,
        pattern_strength=pattern_strength,
        trend_strength=trend_strength,
        klp=klp,
        r_ratio=r_ratio,
    )

    return PriceActionSignal(
        has_signal=True,
        direction=direction,
        signal_type="SCALP_PULLBACK",
        pattern=pattern,
        entry_price=round(entry, 5),
        sl_price=round(sl, 5),
        tp_price=round(tp, 5),
        r_ratio=round(r_ratio, 2),
        strength=round(strength, 3),
        key_level_proximity=klp,
        ob=None,
        fvg=None,
        sd_zone=None,
        sweep=None,
        structure=None,
        details={
            "ema_fast": round(current_ema_fast, 5),
            "ema_slow": round(current_ema_slow, 5),
            "trend_strength": round(trend_strength, 3),
        },
    )


def _evaluate_scalp_sweep_reversal(
    symbol: str,
    m15_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    key_levels: list[float],
    pattern: str,
    pattern_strength: float,
    atr: float,
    regime: str,
) -> PriceActionSignal:
    """
    Intraday liquidity sweep reversal.

    Purpose:
      Give ranging conditions a more active setup family by trading reclaims
      after stop-run moves beyond recent M15 range edges.
    """
    if m15_df is None or m5_df is None or len(m15_df) < 20 or len(m5_df) < 20:
        return _empty_signal()

    bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
    bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

    recent_m15_high = float(m15_df["high"].iloc[-17:-1].max())
    recent_m15_low = float(m15_df["low"].iloc[-17:-1].min())
    last = m5_df.iloc[-1]
    current_price = float(last["close"])
    klp = _key_level_proximity(current_price, key_levels, atr)

    # Bullish reclaim after sell-side sweep
    if float(last["low"]) < recent_m15_low and current_price > recent_m15_low and pattern in bullish_patterns:
        entry = current_price
        sl = float(last["low"]) - atr * 0.10
        tp = max(entry + atr * 1.6, recent_m15_high - atr * 0.10)
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0.0
        if r_ratio >= 1.5:
            strength = _compute_scalp_strength(
                base_quality=0.88,
                pattern_strength=pattern_strength,
                trend_strength=0.70,
                klp=klp,
                r_ratio=r_ratio,
            )
            return PriceActionSignal(
                has_signal=True,
                direction="BUY",
                signal_type="SCALP_SWEEP_REVERSAL",
                pattern=pattern,
                entry_price=round(entry, 5),
                sl_price=round(sl, 5),
                tp_price=round(tp, 5),
                r_ratio=round(r_ratio, 2),
                strength=round(strength, 3),
                key_level_proximity=klp,
                ob=None,
                fvg=None,
                sd_zone=None,
                sweep=None,
                structure=None,
                details={
                    "swept_level": round(recent_m15_low, 5),
                    "range_edge": "LOW",
                },
            )

    # Bearish reclaim after buy-side sweep
    if float(last["high"]) > recent_m15_high and current_price < recent_m15_high and pattern in bearish_patterns:
        entry = current_price
        sl = float(last["high"]) + atr * 0.10
        tp = min(entry - atr * 1.6, recent_m15_low + atr * 0.10)
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0.0
        if r_ratio >= 1.5:
            strength = _compute_scalp_strength(
                base_quality=0.88,
                pattern_strength=pattern_strength,
                trend_strength=0.70,
                klp=klp,
                r_ratio=r_ratio,
            )
            return PriceActionSignal(
                has_signal=True,
                direction="SELL",
                signal_type="SCALP_SWEEP_REVERSAL",
                pattern=pattern,
                entry_price=round(entry, 5),
                sl_price=round(sl, 5),
                tp_price=round(tp, 5),
                r_ratio=round(r_ratio, 2),
                strength=round(strength, 3),
                key_level_proximity=klp,
                ob=None,
                fvg=None,
                sd_zone=None,
                sweep=None,
                structure=None,
                details={
                    "swept_level": round(recent_m15_high, 5),
                    "range_edge": "HIGH",
                },
            )

    return _empty_signal()


def _evaluate_swing_breakout(
    symbol: str,
    h1_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    key_levels: list[float],
    direction_bias: str,
    pattern: str,
    pattern_strength: float,
    atr: float,
    current_ts: datetime,
) -> PriceActionSignal:
    """
    Higher-timeframe breakout continuation.

    Goal:
      Add slower continuation entries when H1 structure is pushing through
      a recent range boundary and M5 confirms with a directional candle.
    """
    if h1_df is None or m5_df is None or len(h1_df) < 40 or direction_bias not in {"BULLISH", "BEARISH"}:
        return _empty_signal()

    bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
    bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

    h1_close = h1_df["close"]
    h1_high = h1_df["high"]
    h1_low = h1_df["low"]
    current_price = float(m5_df["close"].iloc[-1])
    recent_high = float(h1_high.iloc[-25:-5].max())
    recent_low = float(h1_low.iloc[-25:-5].min())
    h1_ema_fast = float(h1_close.ewm(span=20, adjust=False).mean().iloc[-1])
    h1_ema_slow = float(h1_close.ewm(span=50, adjust=False).mean().iloc[-1])
    klp = _key_level_proximity(current_price, key_levels, atr)

    # Gate policy: breakout + directional pattern are non-negotiable (thesis
    # + trigger). Of the two confluence checks {retest, trend_ok}, we now
    # require only one — 3-of-4 instead of 4-of-4. This lets us enter when
    # the EMA hasn't crossed yet but price has cleanly broken, or when price
    # has already moved past the retest window but trend alignment is strong.
    if direction_bias == "BULLISH":
        breakout = float(h1_close.iloc[-1]) > recent_high
        retest = current_price >= recent_high - atr * 0.35
        trend_ok = h1_ema_fast > h1_ema_slow
        if not (breakout and pattern in bullish_patterns and (retest or trend_ok)):
            return _empty_signal()
        entry = current_price
        sl = recent_high - atr * 0.60
        tp = entry + atr * 3.2
        direction = "BUY"
    else:
        breakout = float(h1_close.iloc[-1]) < recent_low
        retest = current_price <= recent_low + atr * 0.35
        trend_ok = h1_ema_fast < h1_ema_slow
        if not (breakout and pattern in bearish_patterns and (retest or trend_ok)):
            return _empty_signal()
        entry = current_price
        sl = recent_low + atr * 0.60
        tp = entry - atr * 3.2
        direction = "SELL"

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0.0
    if r_ratio < 1.8:
        return _empty_signal()

    trend_strength = min(abs(h1_ema_fast - h1_ema_slow) / max(atr * 0.8, 1e-9), 1.0)
    strength = _compute_scalp_strength(
        base_quality=0.92,
        pattern_strength=pattern_strength,
        trend_strength=trend_strength,
        klp=klp,
        r_ratio=r_ratio,
    )

    return PriceActionSignal(
        has_signal=True,
        direction=direction,
        signal_type="SWING_BREAKOUT",
        pattern=pattern,
        entry_price=round(entry, 5),
        sl_price=round(sl, 5),
        tp_price=round(tp, 5),
        r_ratio=round(r_ratio, 2),
        strength=round(strength, 3),
        key_level_proximity=klp,
        ob=None,
        fvg=None,
        sd_zone=None,
        sweep=None,
        structure=None,
        details={
            "recent_high": round(recent_high, 5),
            "recent_low": round(recent_low, 5),
            "trend_strength": round(trend_strength, 3),
        },
    )


def _evaluate_swing_reversion(
    symbol: str,
    h1_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    key_levels: list[float],
    pattern: str,
    pattern_strength: float,
    atr: float,
) -> PriceActionSignal:
    """
    Higher-timeframe stretched reversal.

    Goal:
      Capture larger H1 dislocations after the market stretches far from
      its mean and prints a reversal candle on M5.
    """
    if h1_df is None or m5_df is None or len(h1_df) < 40:
        return _empty_signal()

    bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
    bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

    h1_close = h1_df["close"]
    current_price = float(m5_df["close"].iloc[-1])
    h1_mean = float(h1_close.rolling(20).mean().iloc[-1])
    h1_std = float(h1_close.rolling(20).std().iloc[-1] or 0.0)
    if h1_std <= 0:
        return _empty_signal()

    z_score = (float(h1_close.iloc[-1]) - h1_mean) / h1_std
    klp = _key_level_proximity(current_price, key_levels, atr)
    last_m5_high = float(m5_df["high"].tail(10).max())
    last_m5_low = float(m5_df["low"].tail(10).min())

    if z_score <= -1.8 and pattern in bullish_patterns:
        entry = current_price
        sl = last_m5_low - atr * 0.15
        tp = min(h1_mean, entry + atr * 2.8)
        direction = "BUY"
    elif z_score >= 1.8 and pattern in bearish_patterns:
        entry = current_price
        sl = last_m5_high + atr * 0.15
        tp = max(h1_mean, entry - atr * 2.8)
        direction = "SELL"
    else:
        return _empty_signal()

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    r_ratio = tp_dist / sl_dist if sl_dist > 0 else 0.0
    if r_ratio < 1.5:
        return _empty_signal()

    stretch_strength = min(abs(z_score) / 3.0, 1.0)
    strength = _compute_scalp_strength(
        base_quality=0.84,
        pattern_strength=pattern_strength,
        trend_strength=stretch_strength,
        klp=klp,
        r_ratio=r_ratio,
    )

    return PriceActionSignal(
        has_signal=True,
        direction=direction,
        signal_type="SWING_REVERSION",
        pattern=pattern,
        entry_price=round(entry, 5),
        sl_price=round(sl, 5),
        tp_price=round(tp, 5),
        r_ratio=round(r_ratio, 2),
        strength=round(strength, 3),
        key_level_proximity=klp,
        ob=None,
        fvg=None,
        sd_zone=None,
        sweep=None,
        structure=None,
        details={
            "z_score": round(z_score, 3),
            "h1_mean": round(h1_mean, 5),
        },
    )


# ---------------------------------------------------------
# Strength scorers
# ---------------------------------------------------------

def _compute_trending_strength(
    ob_strength: float,
    pattern_strength: float,
    fvg_confluence: float,
    sweep_confluence: bool,
    klp: float,
    r_ratio: float,
) -> float:
    score = (
        ob_strength      * 0.30 +
        pattern_strength * 0.25 +
        fvg_confluence   * 0.15 +
        (0.15 if sweep_confluence else 0.0) +
        klp              * 0.10 +
        min((r_ratio - 1.5) / 3.5, 1.0) * 0.10  # bonus for high R
    )
    return min(score, 1.0)


def _compute_ranging_strength(
    zone_strength: float,
    pattern_strength: float,
    klp: float,
    r_ratio: float,
) -> float:
    score = (
        zone_strength    * 0.40 +
        pattern_strength * 0.35 +
        klp              * 0.15 +
        min((r_ratio - 1.5) / 3.5, 1.0) * 0.10
    )
    return min(score, 1.0)


def _compute_scalp_strength(
    base_quality: float,
    pattern_strength: float,
    trend_strength: float,
    klp: float,
    r_ratio: float,
) -> float:
    score = (
        base_quality     * 0.35 +
        pattern_strength * 0.25 +
        trend_strength   * 0.20 +
        klp              * 0.10 +
        min((r_ratio - 1.5) / 2.5, 1.0) * 0.10
    )
    return min(score, 1.0)


# ---------------------------------------------------------
# Utilities
# ---------------------------------------------------------

def _next_swing_target(
    swings: list[SwingPoint],
    entry: float,
    direction: str,
) -> Optional[float]:
    """Find the nearest swing point above (UP) or below (DOWN) the entry."""
    if direction == "UP":
        candidates = [s.price for s in swings if s.price > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [s.price for s in swings if s.price < entry]
        return max(candidates) if candidates else None


def _price_near_fvg(
    price: float,
    fvgs: list[FairValueGap],
    atr: float,
) -> float:
    """Return 0–1 score for whether price is near an unfilled FVG."""
    if not fvgs:
        return 0.0
    for fvg in fvgs:
        if fvg.bottom <= price <= fvg.top:
            return 1.0   # price inside an FVG — maximum confluence
        dist = min(abs(price - fvg.top), abs(price - fvg.bottom))
        if dist < atr * 0.3:
            return 0.6
    return 0.0


def _empty_signal() -> PriceActionSignal:
    return PriceActionSignal(
        has_signal=False,
        direction="NONE",
        signal_type="NONE",
        pattern="NONE",
        entry_price=0.0,
        sl_price=0.0,
        tp_price=0.0,
        r_ratio=0.0,
        strength=0.0,
        key_level_proximity=0.0,
        ob=None, fvg=None, sd_zone=None, sweep=None, structure=None,
    )


# ---------------------------------------------------------
# Diagnostic API  (used by backtest --diagnose mode)
# ---------------------------------------------------------

def diagnose(
    symbol: str,
    h1_df: pd.DataFrame,
    m15_df: pd.DataFrame,
    m5_df: pd.DataFrame,
    regime: str,
    direction_bias: str,
    atr: float,
) -> dict:
    """
    Return a diagnostic breakdown of why evaluate() would return no signal.

    Called by backtest_runner.py --diagnose mode to identify which gate
    is blocking signals. Does NOT return a trade signal -- analysis only.

    Returns a dict with:
      regime, direction_bias, m15_confirmed, m15_reason,
      ob_count, obs_near_price, sd_zone_count, sd_near_price,
      pattern, block_reason
    """
    result = {
        "regime": regime,
        "direction_bias": direction_bias,
        "m15_confirmed": False,
        "m15_reason": "",
        "ob_count": 0,
        "obs_near_price": 0,
        "obs_direction_match": 0,
        "sd_zone_count": 0,
        "sd_near_price": 0,
        "pattern": "NONE",
        "block_reason": "unknown",
    }

    if regime not in (TRENDING_REGIMES | RANGING_REGIMES):
        result["block_reason"] = "regime_not_eligible"
        return result

    structure   = _build_market_structure(h1_df)
    obs         = _find_order_blocks(h1_df, structure)
    sd_zones    = _find_sd_zones(h1_df)
    pattern, _  = _detect_pattern(m5_df)

    current_price = float(m5_df["close"].iloc[-1]) if m5_df is not None and len(m5_df) > 0 else 0.0

    m15_ok, m15_reason = _confirm_m15_setup(m15_df, structure, direction_bias)

    result["m15_confirmed"]  = m15_ok
    result["m15_reason"]     = m15_reason
    result["ob_count"]       = len(obs)
    result["sd_zone_count"]  = len(sd_zones)
    result["pattern"]        = pattern

    if not m15_ok:
        result["block_reason"] = "m15_failed"
        return result

    if direction_bias == "NEUTRAL":
        result["block_reason"] = "direction_neutral"
        return result

    current_ts = m5_df.index[-1].to_pydatetime() if len(m5_df.index) else datetime.now(timezone.utc)
    scalp_allowed, _ = scalp_session_allowed(symbol, current_ts)
    mean_reversion_allowed, _ = mean_reversion_session_allowed(symbol, current_ts)

    # Count OBs near price
    bullish_patterns = {"BULLISH_ENGULFING", "PIN_BAR_BULLISH", "MORNING_STAR"}
    bearish_patterns = {"BEARISH_ENGULFING", "PIN_BAR_BEARISH", "EVENING_STAR"}

    obs_near = 0
    obs_dir  = 0
    for ob in obs:
        in_ob   = (ob.bottom <= current_price <= ob.top)
        near_ob = (abs(current_price - ob.mid) <= atr * 1.5)
        if in_ob or near_ob:
            obs_near += 1
            dir_ok = (direction_bias == "BULLISH" and ob.direction == "BULLISH") or \
                     (direction_bias == "BEARISH" and ob.direction == "BEARISH")
            if dir_ok:
                obs_dir += 1
    result["obs_near_price"]      = obs_near
    result["obs_direction_match"] = obs_dir

    # Count S&D zones near price
    sd_near = 0
    demand_zones = [z for z in sd_zones if z.kind == "DEMAND"]
    supply_zones = [z for z in sd_zones if z.kind == "SUPPLY"]
    for dz in demand_zones:
        if abs(current_price - dz.top) <= atr * 1.5:
            sd_near += 1
    for sz in supply_zones:
        if abs(current_price - sz.bottom) <= atr * 1.5:
            sd_near += 1
    result["sd_near_price"] = sd_near

    # Determine the blocking reason
    if pattern == "NONE":
        if regime in TRENDING_REGIMES and obs_dir > 0:
            result["block_reason"] = "pattern_none_ob_present"
        elif regime in RANGING_REGIMES and sd_near > 0:
            result["block_reason"] = "pattern_none_sd_present"
        elif regime in TRENDING_REGIMES and obs_near == 0:
            result["block_reason"] = "no_ob_near_price"
        elif regime in RANGING_REGIMES and not mean_reversion_allowed:
            result["block_reason"] = "mean_reversion_session_blocked"
        elif regime in RANGING_REGIMES and sd_near == 0:
            result["block_reason"] = "no_sd_near_price"
        elif not scalp_allowed:
            result["block_reason"] = "scalp_session_blocked"
        else:
            result["block_reason"] = "pattern_none"
    elif regime in TRENDING_REGIMES:
        if obs_near == 0:
            result["block_reason"] = "no_ob_near_price"
        elif obs_dir == 0:
            result["block_reason"] = "ob_direction_mismatch"
        else:
            result["block_reason"] = "low_rr_or_other"
    elif regime in RANGING_REGIMES:
        if not mean_reversion_allowed:
            result["block_reason"] = "mean_reversion_session_blocked"
        elif sd_near == 0:
            result["block_reason"] = "no_sd_near_price"
        else:
            result["block_reason"] = "low_rr_or_pattern_mismatch"

    return result


# ---------------------------------------------------------
# Standalone test
# ---------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging
    from core.mt5_connection import connect, disconnect
    from core.data_fetcher import get_h1, get_m15, get_m5
    from core.regime_detector import detect

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 70)
    print("  ARCS-FX — Price Action Engine Test")
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
        m5  = get_m5(sym)

        if h1 is None or m15 is None or m5 is None:
            print(f"  [SKIP] Missing data for {sym}")
            continue

        regime_result = detect(sym, h1, timeframe="H1")
        if regime_result is None:
            continue

        atr = regime_result.atr
        signal = evaluate(
            symbol=sym,
            h1_df=h1,
            m15_df=m15,
            m5_df=m5,
            regime=regime_result.regime,
            direction_bias=regime_result.direction,
            atr=atr,
        )

        print(f"  Regime  : {regime_result.regime} ({regime_result.direction})")
        print(f"  Signal  : {signal}")

        # Show market structure details
        structure = _build_market_structure(h1)
        print(f"  Structure: trend={structure.trend} | points={structure.structure_points[-8:]}")
        print(f"  OBs found: {len(_find_order_blocks(h1, structure))}")
        print(f"  FVGs found: {len(_find_fvgs(h1))}")
        print(f"  S&D zones: {len(_find_sd_zones(h1))}")

    disconnect()
    print("\n\nPhase 2 — Price Action Engine test: DONE")
