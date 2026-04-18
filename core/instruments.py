"""
Shared instrument metadata and contract math.

This module lets the existing FX bot extend to crypto without forking the
strategy logic or scattering symbol-specific conditionals everywhere.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from config import SPREAD_DEFAULT_LIMIT, SYMBOL_PROFILES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstrumentProfile:
    asset_class: str = "forex"
    session_mode: str = "fx"
    news_mode: str = "forex_macro"
    price_unit: Optional[float] = None
    round_level_step: Optional[float] = None
    spread_limit: float = SPREAD_DEFAULT_LIMIT
    min_stop_units: float = 10.0
    display_session: str = ""
    regime_quiet_atr_pct: float = 10.0
    regime_chaos_atr_pct: float = 90.0
    regime_dominant_floor: float = 0.35
    regime_atr_low_pct: float = 30.0
    regime_atr_high_pct: float = 70.0


def get_profile(symbol: str) -> InstrumentProfile:
    raw = SYMBOL_PROFILES.get(symbol, {})
    return InstrumentProfile(
        asset_class=raw.get("asset_class", "forex"),
        session_mode=raw.get("session_mode", "fx"),
        news_mode=raw.get("news_mode", "forex_macro"),
        price_unit=raw.get("price_unit"),
        round_level_step=raw.get("round_level_step"),
        spread_limit=raw.get("spread_limit", SPREAD_DEFAULT_LIMIT),
        min_stop_units=raw.get("min_stop_units", 10.0),
        display_session=raw.get("display_session", ""),
        regime_quiet_atr_pct=raw.get("regime_quiet_atr_pct", 10.0),
        regime_chaos_atr_pct=raw.get("regime_chaos_atr_pct", 90.0),
        regime_dominant_floor=raw.get("regime_dominant_floor", 0.35),
        regime_atr_low_pct=raw.get("regime_atr_low_pct", 30.0),
        regime_atr_high_pct=raw.get("regime_atr_high_pct", 70.0),
    )


def is_crypto(symbol: str) -> bool:
    return get_profile(symbol).asset_class == "crypto"


def session_mode(symbol: str) -> str:
    return get_profile(symbol).session_mode


def news_mode(symbol: str) -> str:
    return get_profile(symbol).news_mode


def display_session(symbol: str) -> str:
    profile = get_profile(symbol)
    if profile.display_session:
        return profile.display_session
    if profile.session_mode == "always_on":
        return "CRYPTO"
    return ""


def _get_mt5_symbol_info(symbol: str):
    try:
        import MetaTrader5 as mt5
    except Exception:
        return None

    try:
        return mt5.symbol_info(symbol)
    except Exception:
        return None


def price_unit(symbol: str, info=None) -> float:
    """
    Canonical distance unit for spread / dedup / stop math.

    For forex this behaves like legacy pip logic.
    For crypto we can override explicitly in config so units make practical
    sense (e.g. BTC spread in dollars instead of 0.01 quote increments).
    """
    profile = get_profile(symbol)
    if profile.price_unit and profile.price_unit > 0:
        return float(profile.price_unit)

    info = info or _get_mt5_symbol_info(symbol)
    if info is None:
        return 0.0001

    if info.digits in (3, 5):
        return info.point * 10
    if info.digits in (2, 4):
        return info.point
    return max(float(info.point), 1e-8)


def round_level_step(symbol: str) -> float:
    profile = get_profile(symbol)
    if profile.round_level_step and profile.round_level_step > 0:
        return float(profile.round_level_step)
    return 0.50 if "JPY" in symbol else 0.0050


def distance_in_units(symbol: str, distance: float, info=None) -> float:
    unit = price_unit(symbol, info)
    if unit <= 0:
        return 0.0
    return distance / unit


def volume_constraints(symbol: str, info=None) -> tuple[float, float, float]:
    info = info or _get_mt5_symbol_info(symbol)
    if info is None:
        return 0.01, 100.0, 0.01

    min_volume = float(getattr(info, "volume_min", 0.01) or 0.01)
    max_volume = float(getattr(info, "volume_max", 100.0) or 100.0)
    step = float(getattr(info, "volume_step", 0.01) or 0.01)
    return min_volume, max_volume, step


def round_volume(symbol: str, volume: float, info=None, mode: str = "down") -> float:
    min_volume, max_volume, step = volume_constraints(symbol, info)
    if step <= 0:
        step = 0.01

    scaled = volume / step
    if mode == "nearest":
        rounded = round(scaled) * step
    elif mode == "up":
        rounded = math.ceil(scaled) * step
    else:
        rounded = math.floor(scaled) * step

    rounded = max(min_volume, rounded)
    rounded = min(max_volume, rounded)

    decimals = max(0, len(f"{step:.8f}".rstrip("0").split(".")[-1]))
    return round(rounded, decimals)


def risk_value_per_lot(symbol: str, entry_price: float, sl_price: float, info=None) -> float:
    """
    Money risk per 1.0 lot for the given stop distance.

    Preferred path uses MT5 contract metadata. If unavailable, we fall back to
    the original FX-style approximation so current forex behaviour stays stable.
    """
    distance = abs(entry_price - sl_price)
    if distance <= 0:
        return 0.0

    info = info or _get_mt5_symbol_info(symbol)
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0) if info else 0.0
    tick_value_candidates = []
    if info is not None:
        for attr in ("trade_tick_value", "trade_tick_value_profit", "trade_tick_value_loss"):
            value = float(getattr(info, attr, 0.0) or 0.0)
            if value > 0:
                tick_value_candidates.append(value)

    tick_value = max(tick_value_candidates) if tick_value_candidates else 0.0
    if tick_size > 0 and tick_value > 0:
        ticks = distance / tick_size
        return ticks * tick_value

    unit_distance = distance_in_units(symbol, distance, info)
    if unit_distance <= 0:
        return 0.0

    if symbol.endswith("JPY"):
        value_per_unit = 1000.0 / entry_price if entry_price > 0 else 8.0
    else:
        value_per_unit = 10.0
    return unit_distance * value_per_unit
