"""
ARCS-FX -- engines/macro_anchor.py
Edge 1: Cross-Asset Macro Anchor confirmation.

WHY THIS EXISTS:
Retail FX pattern-trading has no edge in isolation — SMC, OB, FVG are
all derived from price and add no independent information. Every real
systematic FX desk anchors its FX view to cross-asset macro data:
rate differentials, equity risk-on/off, commodity prices, the dollar
index, etc. This module does the same for us.

FOR EACH PAIR we track a MACRO ANCHOR proxy that leads or coincides
with its direction. We allow a trade only when the FX signal direction
AGREES with the anchor's recent direction.

ANCHOR MAP (kept pragmatic for daily retail data):

  EURUSD  -> DX-Y.NYB (US Dollar Index) inverse
            weak dollar = EURUSD up
  GBPUSD  -> DX-Y.NYB inverse + ^FTSE/^GSPC relative
  AUDUSD  -> HG=F (copper) + ^AXJO (Australia 200)
  NZDUSD  -> HG=F (copper) as commodity-beta proxy
  USDCAD  -> CL=F (WTI crude) inverse   (oil up = CAD up = USDCAD down)
  USDJPY  -> ^TNX (US 10y yield)        (yields up = USDJPY up)
  USDCHF  -> DX-Y.NYB direct            (strong dollar = USDCHF up)
  EURJPY  -> ^GSPC (SPX)                (risk-on = yen weak = EURJPY up)

DATA SOURCE: yfinance (free, daily close). Fetched once/hour, cached.

OUTPUT: MacroAnchorResult(direction: BULLISH|BEARISH|NEUTRAL,
                         strength: 0-1,
                         anchors_used: list of tickers + their directions)

HOW IT'S USED:
main.py calls evaluate(pair, desired_direction) BEFORE opening a trade.
If the anchor is NEUTRAL we pass with a small penalty; if the anchor
CONTRADICTS the desired direction we block outright.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anchor map
# ---------------------------------------------------------------------------
# Each entry:  (ticker, weight, inverse_flag)
#   weight       -> relative importance when multiple anchors exist
#   inverse_flag -> True when anchor_up implies pair_down
# ---------------------------------------------------------------------------
ANCHOR_MAP: dict[str, list[tuple[str, float, bool]]] = {
    # Dollar-weakness pairs (long pair = short USD)
    "EURUSD": [("DX-Y.NYB", 1.0, True)],
    "GBPUSD": [("DX-Y.NYB", 0.8, True), ("^FTSE", 0.2, False)],
    "AUDUSD": [("HG=F", 0.6, False), ("DX-Y.NYB", 0.4, True)],
    "NZDUSD": [("HG=F", 0.6, False), ("DX-Y.NYB", 0.4, True)],

    # Dollar-strength pairs (long pair = long USD)
    "USDCAD": [("CL=F", 0.8, True), ("DX-Y.NYB", 0.2, False)],
    "USDCHF": [("DX-Y.NYB", 1.0, False)],

    # Yield / risk-on sensitives
    "USDJPY": [("^TNX", 0.6, False), ("^GSPC", 0.4, False)],
    "EURJPY": [("^GSPC", 0.7, False), ("^TNX", 0.3, False)],
}

# Cache TTL — macro anchors move slowly, 1h is plenty
CACHE_TTL_SECONDS = 3600
LOOKBACK_DAYS = 10  # need ~10 daily bars to compute momentum
MOMENTUM_WINDOW = 5  # 5-day return is the directional signal


@dataclass
class AnchorReading:
    ticker: str
    direction: str       # BULLISH | BEARISH | NEUTRAL
    momentum_pct: float  # raw 5d return
    weight: float


@dataclass
class MacroAnchorResult:
    pair: str
    direction: str                        # BULLISH | BEARISH | NEUTRAL
    strength: float                       # 0.0 - 1.0
    anchors: list[AnchorReading] = field(default_factory=list)
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fresh: bool = True                    # False if served from a degraded source
    note: str = ""

    def aligns_with(self, trade_direction: str) -> bool:
        if self.direction == "NEUTRAL":
            return True  # neutral anchor does not block, just doesn't boost
        if trade_direction == "BUY" and self.direction == "BULLISH":
            return True
        if trade_direction == "SELL" and self.direction == "BEARISH":
            return True
        return False

    def __str__(self) -> str:
        parts = " | ".join(
            f"{a.ticker}={a.direction}({a.momentum_pct:+.2%})"
            for a in self.anchors
        )
        return (
            f"MacroAnchor[{self.pair}] {self.direction} "
            f"strength={self.strength:.2f} :: {parts}"
        )


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_anchor_cache: dict[str, tuple[float, pd.Series]] = {}  # ticker -> (ts, close series)
_result_cache: dict[str, tuple[float, MacroAnchorResult]] = {}  # pair -> (ts, result)


def _fetch_close_series(ticker: str) -> Optional[pd.Series]:
    """
    Fetch the last ~LOOKBACK_DAYS daily close prices for a ticker via yfinance.
    Cached for CACHE_TTL_SECONDS. Returns None on any failure.
    """
    now = time.monotonic()
    cached = _anchor_cache.get(ticker)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    try:
        import yfinance as yf
    except ImportError:
        logger.warning("[MACRO] yfinance not installed -- macro anchors disabled.")
        return None

    try:
        # 20d period to ensure 10+ trading days even over weekends/holidays
        data = yf.download(
            ticker,
            period="20d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if data is None or data.empty:
            logger.warning("[MACRO] %s: empty yfinance response.", ticker)
            return None

        close = data["Close"]
        # yfinance sometimes returns a DataFrame even for a single ticker
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        close = close.dropna().tail(LOOKBACK_DAYS + 2)
        if len(close) < 3:
            logger.warning("[MACRO] %s: not enough close data (%d).", ticker, len(close))
            return None

        _anchor_cache[ticker] = (now, close)
        return close
    except Exception as exc:
        logger.warning("[MACRO] %s fetch error: %s", ticker, exc)
        return None


def _momentum_direction(series: pd.Series) -> tuple[str, float]:
    """
    Classify the short-term momentum of a close series.

    Uses 5-day return with a small dead-zone around zero to avoid whipsaws.
    Returns (direction, return_pct).
    """
    if series is None or len(series) < MOMENTUM_WINDOW + 1:
        return "NEUTRAL", 0.0

    last = float(series.iloc[-1])
    prior = float(series.iloc[-(MOMENTUM_WINDOW + 1)])
    if prior == 0 or np.isnan(prior):
        return "NEUTRAL", 0.0

    ret = (last / prior) - 1.0
    # Dead-zone: ±0.25% is noise for an index / commodity over 5 days
    if ret > 0.0025:
        return "BULLISH", ret
    if ret < -0.0025:
        return "BEARISH", ret
    return "NEUTRAL", ret


def evaluate(pair: str, now_utc: Optional[datetime] = None) -> MacroAnchorResult:
    """
    Return the macro anchor direction for a pair.

    The result is cached for CACHE_TTL_SECONDS so repeated calls per tick
    don't hammer yfinance.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    now = time.monotonic()
    cached = _result_cache.get(pair)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    anchors_cfg = ANCHOR_MAP.get(pair)
    if not anchors_cfg:
        result = MacroAnchorResult(
            pair=pair,
            direction="NEUTRAL",
            strength=0.0,
            note="no anchor mapping for pair",
            fresh=False,
        )
        _result_cache[pair] = (now, result)
        return result

    readings: list[AnchorReading] = []
    signed_score = 0.0
    abs_weight = 0.0

    for ticker, weight, inverse in anchors_cfg:
        series = _fetch_close_series(ticker)
        direction, ret = _momentum_direction(series)

        # Apply inverse flag: if anchor_up means pair_down, flip the direction
        if inverse:
            if direction == "BULLISH":
                direction = "BEARISH"
            elif direction == "BEARISH":
                direction = "BULLISH"

        readings.append(AnchorReading(
            ticker=ticker,
            direction=direction,
            momentum_pct=round(ret, 5),
            weight=weight,
        ))

        if direction == "BULLISH":
            signed_score += weight
        elif direction == "BEARISH":
            signed_score -= weight
        abs_weight += weight

    if abs_weight == 0:
        final_dir, strength = "NEUTRAL", 0.0
    else:
        ratio = signed_score / abs_weight
        strength = min(abs(ratio), 1.0)
        if ratio > 0.35:
            final_dir = "BULLISH"
        elif ratio < -0.35:
            final_dir = "BEARISH"
        else:
            final_dir = "NEUTRAL"

    fresh = any(r.direction != "NEUTRAL" or r.momentum_pct != 0.0 for r in readings) \
            or all(_anchor_cache.get(cfg[0]) is not None for cfg in anchors_cfg)

    result = MacroAnchorResult(
        pair=pair,
        direction=final_dir,
        strength=round(strength, 3),
        anchors=readings,
        timestamp_utc=now_utc,
        fresh=fresh,
    )
    _result_cache[pair] = (now, result)
    logger.info("[MACRO] %s", result)
    return result


def warm_cache(pairs: Optional[list[str]] = None) -> None:
    """Pre-fetch anchor data for all configured pairs (call at startup)."""
    pairs = pairs or list(ANCHOR_MAP.keys())
    tickers = {t for p in pairs for (t, _, _) in ANCHOR_MAP.get(p, [])}
    logger.info("[MACRO] Warming anchor cache for %d tickers ...", len(tickers))
    for t in tickers:
        _fetch_close_series(t)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    warm_cache()
    for p in ANCHOR_MAP.keys():
        r = evaluate(p)
        print(r)
