"""
ARCS-FX -- engines/scalp_engine.py
Session policy helpers for scalp-style entries.

This module is intentionally small and explicit: scalp setups should fire in
liquid windows by default, with a narrow Asian-session allowance for symbols
that naturally trade there.
"""

from __future__ import annotations

from datetime import datetime

from config import (
    SESSIONS,
    SCALP_CORE_SESSIONS,
    SCALP_ASIAN_SYMBOLS,
    SCALP_ASIAN_SESSIONS,
    MEAN_REVERSION_CORE_SESSIONS,
    MEAN_REVERSION_ASIAN_SYMBOLS,
)
from core.instruments import display_session, session_mode


def session_label(ts: datetime) -> str:
    """Map a UTC timestamp to the project's session labels."""
    hour = ts.hour

    overlap_start, overlap_end = SESSIONS["OVERLAP"]
    london_start, london_end = SESSIONS["LONDON"]
    ny_start, ny_end = SESSIONS["NY"]
    asian_start, asian_end = SESSIONS["ASIAN"]

    if overlap_start <= hour < overlap_end:
        return "OVERLAP"
    if london_start <= hour < london_end:
        return "LONDON"
    if ny_start <= hour < ny_end:
        return "NY"
    if asian_start <= hour < asian_end:
        return "ASIAN"
    return "OFF"


def scalp_session_allowed(symbol: str, ts: datetime) -> tuple[bool, str]:
    """
    Return whether scalp setups should be allowed for `symbol` at `ts`.

    Policy:
    - London / NY / overlap are always eligible.
    - Asian session is allowed only for pairs with natural JPY/AUD/NZD flow.
    - OFF session is disallowed for scalps.
    """
    if session_mode(symbol) == "always_on":
        return True, f"{display_session(symbol) or '24/7'} instrument trades around the clock"

    session = session_label(ts)

    if session in SCALP_CORE_SESSIONS:
        return True, f"{session} session supports scalp liquidity"

    if session in SCALP_ASIAN_SESSIONS and symbol in SCALP_ASIAN_SYMBOLS:
        return True, f"{symbol} allowed to scalp during Asian session"

    if session == "ASIAN":
        return False, f"{symbol} scalp disabled in Asian session"

    return False, f"{session} session disabled for scalp setups"


def mean_reversion_session_allowed(symbol: str, ts: datetime) -> tuple[bool, str]:
    """
    Mean reversion should stay selective and liquidity-aware.

    We allow it in the main liquid sessions, and only allow Asian-session
    participation for symbols with a natural Asian flow profile.
    """
    if session_mode(symbol) == "always_on":
        return True, f"{display_session(symbol) or '24/7'} instrument trades around the clock"

    session = session_label(ts)

    if session in MEAN_REVERSION_CORE_SESSIONS:
        return True, f"{session} session supports mean reversion liquidity"

    if session == "ASIAN" and symbol in MEAN_REVERSION_ASIAN_SYMBOLS:
        return True, f"{symbol} allowed to mean-revert during Asian session"

    if session == "ASIAN":
        return False, f"{symbol} mean reversion disabled in Asian session"

    return False, f"{session} session disabled for mean reversion"
