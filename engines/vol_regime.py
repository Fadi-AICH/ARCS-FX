"""
ARCS-FX -- engines/vol_regime.py
Edge 2: Volatility Regime filter + inverse-volatility position sizing.

WHY THIS EXISTS:
The single biggest reason retail systematic FX strategies blow up is
sizing everything the same. On a $5k account, 1% risk on a quiet EURUSD
day is tiny; 1% risk on a chaotic USDJPY BOJ-intervention day is a
coin flip with bad odds.

Real quant desks (AQR, AHL, Two Sigma, BlueCrest) ALL use volatility
targeting. Every position is sized such that its expected DAILY P&L
standard deviation in USD is roughly equal across the book.

This module does two things:

  1. CLASSIFY vol regime for a pair:
       LOW    -- realized vol < 30th percentile of last 90 days
       NORMAL -- 30-70
       HIGH   -- 70-85
       CHAOS  -- > 85

     Only NORMAL is "green light". LOW can be traded with breakout bias.
     HIGH is defensive only. CHAOS is no-trade.

  2. COMPUTE inverse-volatility lot sizing:
       target_daily_risk_usd = account_balance * DAILY_VOL_TARGET_PCT
       daily_usd_per_lot     = daily_range_pips * usd_per_pip_per_lot
       lots                  = target_daily_risk_usd / daily_usd_per_lot

     This sizes small on noisy days, bigger on quiet days -- the exact
     OPPOSITE of what retail pattern-traders do (chase volatility).

WHAT IT REPLACES:
The Kelly-inspired sizing in risk_manager.py keeps working for the
SL-based risk per trade, but this module's inverse-vol size is an
ALTERNATIVE you can toggle via USE_INVERSE_VOL_SIZING in config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
VOL_PERCENTILE_WINDOW_DAYS = 90      # rolling lookback for percentile calc
VOL_LOW_PCT   = 30
VOL_HIGH_PCT  = 70
VOL_CHAOS_PCT = 85

# Target DAILY P&L standard deviation per trade, as % of account balance.
# 0.35% daily vol per position with 2 concurrent positions -> portfolio
# daily vol ~0.5%, annual ~8% at sqrt(252). Sharpe 1 strategy -> ~8% return.
DAILY_VOL_TARGET_PCT = 0.35

# How many daily bars to compute realized vol on
REALIZED_VOL_WINDOW_DAYS = 20


@dataclass
class VolRegimeResult:
    pair: str
    regime: str                 # LOW | NORMAL | HIGH | CHAOS
    percentile: float           # 0-100 current vol percentile
    realized_vol_daily: float   # std of log returns, daily
    daily_range_pips: float     # average daily high-low range in pips
    tradeable: bool             # False if CHAOS
    defensive_only: bool        # True if HIGH
    note: str = ""

    def __str__(self) -> str:
        flag = "OK" if self.tradeable else "NO-TRADE"
        return (
            f"VolRegime[{self.pair}] {self.regime} "
            f"pct={self.percentile:.0f} range={self.daily_range_pips:.1f}p "
            f"-> {flag}"
        )


def _daily_bars_from_h1(h1_df: pd.DataFrame) -> pd.DataFrame:
    """Resample H1 OHLCV to daily bars (UTC)."""
    if h1_df is None or h1_df.empty:
        return pd.DataFrame()
    df = h1_df.copy()
    # h1_df index is already a UTC DatetimeIndex per data_fetcher conventions
    daily = df.resample("1D").agg({
        "open":  "first",
        "high":  "max",
        "low":   "min",
        "close": "last",
    }).dropna()
    return daily


def classify(pair: str, h1_df: pd.DataFrame) -> VolRegimeResult:
    """
    Classify the current volatility regime for a pair.

    Uses H1 data (fastest available without tick noise) resampled to daily.
    """
    daily = _daily_bars_from_h1(h1_df)
    if len(daily) < REALIZED_VOL_WINDOW_DAYS + 10:
        return VolRegimeResult(
            pair=pair,
            regime="NORMAL",
            percentile=50.0,
            realized_vol_daily=0.0,
            daily_range_pips=0.0,
            tradeable=True,
            defensive_only=False,
            note=f"Insufficient daily data ({len(daily)} bars) -- defaulting NORMAL",
        )

    # Daily realized vol (std of log returns)
    log_ret = np.log(daily["close"] / daily["close"].shift(1)).dropna()
    current_vol = float(log_ret.tail(REALIZED_VOL_WINDOW_DAYS).std())

    # Rolling realized vol series for percentile ranking
    rolling_vol = log_ret.rolling(REALIZED_VOL_WINDOW_DAYS).std().dropna()
    hist = rolling_vol.tail(VOL_PERCENTILE_WINDOW_DAYS)
    if len(hist) < 10:
        pct = 50.0
    else:
        pct = float((hist <= current_vol).sum() / len(hist) * 100.0)

    # Average daily range in pips (used for inverse-vol sizing)
    pip_size = 0.01 if "JPY" in pair else 0.0001
    daily_range = (daily["high"] - daily["low"]).tail(REALIZED_VOL_WINDOW_DAYS).mean()
    daily_range_pips = float(daily_range / pip_size) if pip_size > 0 else 0.0

    # Classify
    if pct >= VOL_CHAOS_PCT:
        regime = "CHAOS"
        tradeable, defensive = False, False
    elif pct >= VOL_HIGH_PCT:
        regime = "HIGH"
        tradeable, defensive = True, True
    elif pct <= VOL_LOW_PCT:
        regime = "LOW"
        tradeable, defensive = True, False
    else:
        regime = "NORMAL"
        tradeable, defensive = True, False

    return VolRegimeResult(
        pair=pair,
        regime=regime,
        percentile=round(pct, 1),
        realized_vol_daily=round(current_vol, 6),
        daily_range_pips=round(daily_range_pips, 2),
        tradeable=tradeable,
        defensive_only=defensive,
    )


def inverse_vol_lots(
    pair: str,
    balance_usd: float,
    entry_price: float,
    daily_range_pips: float,
    target_daily_vol_pct: float = DAILY_VOL_TARGET_PCT,
    min_lots: float = 0.01,
    max_lots: float = 5.0,
) -> tuple[float, float]:
    """
    Compute position size by targeting a FIXED daily P&L std in USD.

    Ignores SL distance entirely -- this is vol-targeted sizing, not
    risk-per-trade sizing. The two approaches should be combined:
      - use inverse-vol for the UPPER bound on lots (never exceed this)
      - use SL-based risk-% for the LOWER bound (match trade-level risk)
      - final lots = min(sl_risk_lots, inverse_vol_lots)

    Returns (lots, target_risk_usd).
    """
    if daily_range_pips <= 0 or balance_usd <= 0 or entry_price <= 0:
        return 0.0, 0.0

    target_risk_usd = balance_usd * (target_daily_vol_pct / 100.0)

    # USD per pip per lot
    is_jpy = "JPY" in pair
    if is_jpy:
        usd_per_pip_per_lot = 1000.0 / entry_price
    else:
        usd_per_pip_per_lot = 10.0

    daily_dollar_vol_per_lot = daily_range_pips * usd_per_pip_per_lot
    if daily_dollar_vol_per_lot <= 0:
        return 0.0, 0.0

    raw_lots = target_risk_usd / daily_dollar_vol_per_lot
    # Floor to broker 0.01 step
    lots = max(min_lots, min(max_lots, round(raw_lots, 2)))

    return lots, target_risk_usd


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.mt5_connection import connect, disconnect
    from core.data_fetcher import get_h1

    if connect():
        for pair in ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD"]:
            h1 = get_h1(pair)
            r = classify(pair, h1)
            print(r)
            if r.daily_range_pips > 0:
                lots, risk = inverse_vol_lots(pair, 5000, 1.0 if "JPY" not in pair else 150, r.daily_range_pips)
                print(f"  inv-vol lots @ $5k: {lots:.2f} (target daily risk ${risk:.2f})")
        disconnect()
