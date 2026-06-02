from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
from ta.trend import ADXIndicator

from .config import (
    H1_RANGE_ADX_MAX,
    M1_BREAKOUT_LOOKBACK,
    M1_SWEEP_LOOKBACK,
    MIN_CONFIDENCE,
    M15_RANGE_LOOKBACK,
    PRICE_UNITS,
    TREND_ADX_MIN,
)


@dataclass
class Context:
    symbol: str
    h1_regime: str
    h1_bias: str
    h1_adx: float
    m15_regime: str
    m15_bias: str
    m15_adx: float
    summary: str


@dataclass
class Signal:
    has_signal: bool
    direction: str = "NONE"
    signal_type: str = "NONE"
    confidence: float = 0.0
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    r_ratio: float = 0.0
    notes: str = ""


def _adx(df: pd.DataFrame, window: int = 14) -> tuple[float, float, float]:
    ind = ADXIndicator(df["high"], df["low"], df["close"], window=window, fillna=False)
    return float(ind.adx().iloc[-1]), float(ind.adx_pos().iloc[-1]), float(ind.adx_neg().iloc[-1])


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr_like(df: pd.DataFrame, window: int = 14) -> float:
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.rolling(window).mean().iloc[-1] or 0.0)


def _bias_from_emas(df: pd.DataFrame, fast: int, slow: int) -> str:
    ema_fast = _ema(df["close"], fast).iloc[-1]
    ema_slow = _ema(df["close"], slow).iloc[-1]
    if ema_fast > ema_slow:
        return "BULLISH"
    if ema_fast < ema_slow:
        return "BEARISH"
    return "NEUTRAL"


def analyze_context(symbol: str, h1: pd.DataFrame, m15: pd.DataFrame) -> Context:
    h1_adx, h1_pos, h1_neg = _adx(h1)
    h1_bias = _bias_from_emas(h1, 20, 50)
    h1_regime = "TREND" if h1_adx >= TREND_ADX_MIN else "RANGE"

    m15_adx, _, _ = _adx(m15)
    m15_bias = _bias_from_emas(m15, 20, 50)
    if m15_adx >= TREND_ADX_MIN:
        m15_regime = "TREND"
    elif m15_adx <= H1_RANGE_ADX_MAX:
        m15_regime = "RANGE"
    else:
        m15_regime = "TRANSITION"

    summary = f"H1 {h1_regime}/{h1_bias} ADX {h1_adx:.1f} | M15 {m15_regime}/{m15_bias} ADX {m15_adx:.1f}"
    return Context(symbol=symbol, h1_regime=h1_regime, h1_bias=h1_bias, h1_adx=h1_adx, m15_regime=m15_regime, m15_bias=m15_bias, m15_adx=m15_adx, summary=summary)


def find_signal(symbol: str, h1: pd.DataFrame, m15: pd.DataFrame, m1: pd.DataFrame, spread_units: float) -> Signal:
    context = analyze_context(symbol, h1, m15)
    unit = PRICE_UNITS[symbol]
    atr_m1 = max(_atr_like(m1), unit * 2)
    m1_close = m1["close"]
    m1_open = m1["open"]
    m1_high = m1["high"]
    m1_low = m1["low"]
    m1_volume = m1["tick_volume"] if "tick_volume" in m1.columns else pd.Series([0] * len(m1), index=m1.index)

    ema9 = _ema(m1_close, 9)
    ema21 = _ema(m1_close, 21)
    ema50 = _ema(m1_close, 50)
    current = float(m1_close.iloc[-1])
    current_open = float(m1_open.iloc[-1])
    current_high = float(m1_high.iloc[-1])
    current_low = float(m1_low.iloc[-1])
    body = abs(current - current_open)
    candle_range = max(current_high - current_low, unit)
    body_ratio = body / candle_range
    recent_high = float(m1_high.iloc[-M1_BREAKOUT_LOOKBACK:-1].max())
    recent_low = float(m1_low.iloc[-M1_BREAKOUT_LOOKBACK:-1].min())
    sweep_high = float(m1_high.iloc[-M1_SWEEP_LOOKBACK:-1].max())
    sweep_low = float(m1_low.iloc[-M1_SWEEP_LOOKBACK:-1].min())
    vol_ratio = float(m1_volume.iloc[-1] / max(m1_volume.tail(30).mean(), 1.0))
    prev_close = float(m1_close.iloc[-2])
    prev_high = float(m1_high.iloc[-2])
    prev_low = float(m1_low.iloc[-2])
    m1_range_mean = float((m1_high - m1_low).tail(20).mean() or unit)
    breakout_impulse = abs(current - prev_close) / max(m1_range_mean, unit)
    ema_slope = abs(float(ema9.iloc[-1]) - float(ema9.iloc[-4])) / max(unit, atr_m1)

    strict_trend_ok = (
        context.h1_regime == "TREND"
        and context.m15_regime == "TREND"
        and context.h1_bias == context.m15_bias
        and context.h1_bias in {"BULLISH", "BEARISH"}
        and context.h1_adx >= 28.0
        and context.m15_adx >= 24.0
    )
    if not strict_trend_ok:
        return Signal(
            has_signal=False,
            notes=f"{context.summary} | blocked by strict trend filter",
        )

    bias = context.h1_bias

    # Fast flow scalp for weekend momentum continuation.
    if bias == "BULLISH":
        flow_ok = (
            float(ema9.iloc[-1]) >= float(ema21.iloc[-1]) - atr_m1 * 0.1
            and current > float(ema9.iloc[-1])
            and current > prev_high
            and current > prev_close
        )
        if flow_ok and body_ratio >= 0.34 and vol_ratio >= 1.05 and breakout_impulse >= 0.55:
            entry = current
            sl = min(float(m1_low.iloc[-3:-1].min()), float(ema21.iloc[-1]) - atr_m1 * 0.25)
            tp = entry + max((entry - sl) * 1.1, atr_m1 * 2.0)
            conf = 55 + min(vol_ratio * 7, 10) + min(ema_slope * 8, 6) + min(breakout_impulse * 5, 6)
            if context.m15_regime == "TREND":
                conf += 5
            return _signal("BUY", "SCALP_FLOW", conf, entry, sl, tp, context, spread_units)

    if bias == "BEARISH":
        flow_ok = (
            float(ema9.iloc[-1]) <= float(ema21.iloc[-1]) + atr_m1 * 0.1
            and current < float(ema9.iloc[-1])
            and current < prev_low
            and current < prev_close
        )
        if flow_ok and body_ratio >= 0.34 and vol_ratio >= 1.05 and breakout_impulse >= 0.55:
            entry = current
            sl = max(float(m1_high.iloc[-3:-1].max()), float(ema21.iloc[-1]) + atr_m1 * 0.25)
            tp = entry - max((sl - entry) * 1.1, atr_m1 * 2.0)
            conf = 55 + min(vol_ratio * 7, 10) + min(ema_slope * 8, 6) + min(breakout_impulse * 5, 6)
            if context.m15_regime == "TREND":
                conf += 5
            return _signal("SELL", "SCALP_FLOW", conf, entry, sl, tp, context, spread_units)

    # Momentum pulse scalp for fast weekend bursts.
    if bias == "BULLISH":
        pulse_ok = current > recent_high and current > float(ema9.iloc[-1]) and float(ema9.iloc[-1]) >= float(ema21.iloc[-1])
        if pulse_ok and body_ratio >= 0.42 and vol_ratio >= 1.20 and breakout_impulse >= 1.0:
            entry = current
            sl = min(float(m1_low.iloc[-4:-1].min()), float(ema21.iloc[-1]) - atr_m1 * 0.35)
            tp = entry + max((entry - sl) * 1.35, atr_m1 * 2.8)
            conf = 58 + min(vol_ratio * 9, 12) + min(ema_slope * 10, 7) + 6
            if conf >= MIN_CONFIDENCE:
                return _signal("BUY", "SCALP_MOMENTUM_PULSE", conf, entry, sl, tp, context, spread_units)

    if bias == "BEARISH":
        pulse_ok = current < recent_low and current < float(ema9.iloc[-1]) and float(ema9.iloc[-1]) <= float(ema21.iloc[-1])
        if pulse_ok and body_ratio >= 0.42 and vol_ratio >= 1.20 and breakout_impulse >= 1.0:
            entry = current
            sl = max(float(m1_high.iloc[-4:-1].max()), float(ema21.iloc[-1]) + atr_m1 * 0.35)
            tp = entry - max((sl - entry) * 1.35, atr_m1 * 2.8)
            conf = 58 + min(vol_ratio * 9, 12) + min(ema_slope * 10, 7) + 6
            if conf >= MIN_CONFIDENCE:
                return _signal("SELL", "SCALP_MOMENTUM_PULSE", conf, entry, sl, tp, context, spread_units)

    # Breakout burst
    m15_range_high = float(m15["high"].iloc[-M15_RANGE_LOOKBACK:-1].max())
    m15_range_low = float(m15["low"].iloc[-M15_RANGE_LOOKBACK:-1].min())
    if current > m15_range_high and body_ratio >= 0.65 and vol_ratio >= 1.35 and breakout_impulse >= 1.1:
        entry = current
        sl = min(recent_low, m15_range_high - atr_m1 * 0.8)
        tp = entry + max((entry - sl) * 2.1, atr_m1 * 5)
        conf = 60 + min(vol_ratio * 10, 14)
        if conf >= MIN_CONFIDENCE:
            return _signal("BUY", "SCALP_BREAKOUT", conf, entry, sl, tp, context, spread_units)

    if current < m15_range_low and body_ratio >= 0.65 and vol_ratio >= 1.35 and breakout_impulse >= 1.1:
        entry = current
        sl = max(recent_high, m15_range_low + atr_m1 * 0.8)
        tp = entry - max((sl - entry) * 2.1, atr_m1 * 5)
        conf = 60 + min(vol_ratio * 10, 14)
        if conf >= MIN_CONFIDENCE:
            return _signal("SELL", "SCALP_BREAKOUT", conf, entry, sl, tp, context, spread_units)

    diagnostics = []
    if bias in {"BULLISH", "BEARISH"}:
        diagnostics.append(f"bias={bias}")
    diagnostics.append(f"body={body_ratio:.2f}")
    diagnostics.append(f"vol={vol_ratio:.2f}")
    diagnostics.append(f"pulse={breakout_impulse:.2f}")
    return Signal(has_signal=False, notes=f"{context.summary} | {' '.join(diagnostics)}")


def _signal(direction: str, signal_type: str, confidence: float, entry: float, sl: float, tp: float, context: Context, spread_units: float) -> Signal:
    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    r = tp_dist / sl_dist if sl_dist else 0.0
    confidence -= min(spread_units / 20.0, 6.0)
    return Signal(
        has_signal=True,
        direction=direction,
        signal_type=signal_type,
        confidence=round(max(confidence, 0.0), 1),
        entry=round(entry, 5),
        sl=round(sl, 5),
        tp=round(tp, 5),
        r_ratio=round(r, 2),
        notes=context.summary,
    )
