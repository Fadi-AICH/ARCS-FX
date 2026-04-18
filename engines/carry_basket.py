"""
ARCS-FX -- engines/carry_basket.py
Carry factor (rate differential) across the G8 FX universe.

WHY THIS EXISTS:
Carry is the other pillar of AQR's "Value-Momentum-Carry" style premia.
In FX, it is the most consistent long-run return source that isn't
price-based: a currency with a HIGH policy rate tends to appreciate
(or at least pay enough interest to compensate) against a currency
with a LOW policy rate, over multi-week horizons.

The classic FX carry basket:
  LONG the top-3 highest-rate currencies,
  SHORT the bottom-3 lowest-rate currencies.

For a retail bot we don't actually rebalance a full basket -- we use
carry as a DIRECTIONAL BIAS for each pair. If EURUSD's carry is
negative (USD rate > EUR rate) we penalise longs and boost shorts,
holding everything else equal.

RATE SOURCE:
Policy rates change slowly (a few times a year). We hard-code the
current central bank policy rates in a table that is easy to update
from config/memory when rate decisions land. This is much more
reliable than scraping yield data for a tiny edge improvement.

  Last updated: 2026-04-17 (see central bank meeting calendar)
    Fed (USD)    : 4.25 %
    ECB (EUR)    : 2.50 %
    BoE (GBP)    : 4.50 %
    BoJ (JPY)    : 0.50 %
    SNB (CHF)    : 0.50 %
    BoC (CAD)    : 2.75 %
    RBA (AUD)    : 3.85 %
    RBNZ (NZD)   : 3.25 %

  To update: just edit RATES below. The module does the rest.

We also apply a small "volatility haircut" so a high-carry currency
that is CRASHING doesn't get a free boost: if the pair has had a
sharp negative 10-day return, the carry signal is damped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Central bank policy rates (percentage, annualised)
# ---------------------------------------------------------------------------
# Update this when rate decisions land. No data fetch needed.
RATES: dict[str, float] = {
    "USD": 4.25,
    "EUR": 2.50,
    "GBP": 4.50,
    "JPY": 0.50,
    "CHF": 0.50,
    "CAD": 2.75,
    "AUD": 3.85,
    "NZD": 3.25,
}

# A carry differential of this size (%) is "meaningful"
CARRY_STRONG_THRESHOLD = 2.0    # >= 2% differential -> strong carry signal
CARRY_WEAK_THRESHOLD   = 0.5    # <  0.5% differential -> neutral

# Haircut parameters
HAIRCUT_RETURN_LOOKBACK_DAYS = 10
HAIRCUT_RETURN_TRIGGER_PCT   = 0.02  # 2% adverse move within 10d -> haircut
HAIRCUT_FACTOR               = 0.5   # multiply boost by this when triggered


@dataclass
class CarryReading:
    pair: str
    rate_diff_pct: float      # positive -> base-currency carries higher rate
    direction: str            # BULLISH | BEARISH | NEUTRAL (for the pair)
    strength: float           # 0-1 normalised
    haircut_applied: bool
    note: str = ""

    def boost_for_trade(self, trade_direction: str) -> tuple[float, str]:
        """
        * Trade direction agrees with carry direction and strength >= strong: +3
        * Trade direction agrees with carry, weak:                           +1
        * Trade direction contradicts carry, strong:                         -3
        * Trade direction contradicts carry, weak:                           -1
        Boosts are halved if a haircut was applied.
        """
        if self.direction == "NEUTRAL":
            return 0.0, "carry neutral"

        aligned = (
            (trade_direction == "BUY"  and self.direction == "BULLISH")
            or (trade_direction == "SELL" and self.direction == "BEARISH")
        )

        strong = self.strength >= 0.5
        if aligned:
            base = 3.0 if strong else 1.0
        else:
            base = -3.0 if strong else -1.0

        if self.haircut_applied:
            base *= HAIRCUT_FACTOR

        tag = f"carry {self.direction.lower()} diff={self.rate_diff_pct:+.2f}%"
        if self.haircut_applied:
            tag += " [haircut]"
        return round(base, 2), tag


def _split_pair(pair: str) -> Optional[tuple[str, str]]:
    if len(pair) != 6:
        return None
    return pair[:3], pair[3:]


def _recent_return(h1_df: Optional[pd.DataFrame], days: int) -> Optional[float]:
    """Return (last close / close N business days ago) - 1, or None."""
    if h1_df is None or h1_df.empty or "close" not in h1_df.columns:
        return None
    daily = h1_df["close"].resample("1D").last().dropna()
    if len(daily) < days + 1:
        return None
    last  = float(daily.iloc[-1])
    prior = float(daily.iloc[-(days + 1)])
    if prior <= 0 or np.isnan(prior):
        return None
    return (last / prior) - 1.0


def evaluate(pair: str, h1_df: Optional[pd.DataFrame] = None) -> Optional[CarryReading]:
    """
    Compute the carry reading for `pair`.

    If `h1_df` is provided, we apply a volatility haircut when the pair
    has moved sharply against the carry direction recently.
    """
    parts = _split_pair(pair)
    if parts is None:
        return None
    base, quote = parts
    if base not in RATES or quote not in RATES:
        return None

    diff = RATES[base] - RATES[quote]   # positive -> base carries higher
    abs_diff = abs(diff)

    # Direction
    if abs_diff < CARRY_WEAK_THRESHOLD:
        direction = "NEUTRAL"
    elif diff > 0:
        direction = "BULLISH"   # long the pair = long higher-carry
    else:
        direction = "BEARISH"

    # Strength in [0,1], saturating at CARRY_STRONG_THRESHOLD
    strength = min(abs_diff / CARRY_STRONG_THRESHOLD, 1.0)

    # Haircut: if recent move contradicts carry direction AND is large, damp
    haircut = False
    if direction != "NEUTRAL":
        ret10 = _recent_return(h1_df, HAIRCUT_RETURN_LOOKBACK_DAYS)
        if ret10 is not None:
            if direction == "BULLISH" and ret10 < -HAIRCUT_RETURN_TRIGGER_PCT:
                haircut = True
            elif direction == "BEARISH" and ret10 > HAIRCUT_RETURN_TRIGGER_PCT:
                haircut = True

    return CarryReading(
        pair=pair,
        rate_diff_pct=round(diff, 3),
        direction=direction,
        strength=round(strength, 3),
        haircut_applied=haircut,
    )


def basket_ranking() -> list[tuple[str, float]]:
    """
    Return currencies ranked by policy rate (highest first).
    Useful for classic carry-basket diagnostics.
    """
    return sorted(RATES.items(), key=lambda kv: kv[1], reverse=True)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    print("Rate ranking (high -> low):", basket_ranking())
    for p in ["EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCHF", "USDCAD", "NZDUSD", "EURJPY"]:
        r = evaluate(p)
        print(p, r)
