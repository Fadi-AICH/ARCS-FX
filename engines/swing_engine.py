"""
ARCS-FX -- engines/swing_engine.py
Routing helpers for swing-style setups.

This keeps the first layer of swing separation explicit without forcing a full
rewrite of the price action internals in one jump.
"""

from config import (
    REGIME_TRENDING_CLEAN,
    REGIME_TRENDING_EXTENDED,
    REGIME_RANGING_CLEAN,
)


def breakout_allowed(regime: str) -> tuple[bool, str]:
    """Breakout swings belong to trend regimes only."""
    if regime in {REGIME_TRENDING_CLEAN, REGIME_TRENDING_EXTENDED}:
        return True, f"{regime} supports breakout continuation"
    return False, f"{regime} does not support breakout continuation"


def reversion_allowed(regime: str) -> tuple[bool, str]:
    """Swing reversions belong to clean ranges and extended trends."""
    if regime in {REGIME_RANGING_CLEAN, REGIME_TRENDING_EXTENDED}:
        return True, f"{regime} supports stretched reversion"
    return False, f"{regime} does not support stretched reversion"
