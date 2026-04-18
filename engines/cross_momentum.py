"""
ARCS-FX -- engines/cross_momentum.py
Cross-sectional momentum (CSM) across the G8 FX universe.

WHY THIS EXISTS:
Cross-sectional momentum is one of the THREE founding factors of AQR's
"Style Premia" framework (Asness, Moskowitz, Pedersen 2013: "Value and
Momentum Everywhere"). The edge is remarkably robust across FX,
equities, bonds, commodities going back 100+ years.

The idea in FX terms:
  * Rank ALL tradeable pairs by their recent (3m/6m/12m) return.
  * LONG the top-quartile performers, SHORT the bottom-quartile.
  * Rebalance weekly or monthly.
  * The cross-section cancels out the dollar factor: you're not
    betting on USD strength, you're betting on PERSISTENCE.

Why this works after decades of being known:
  - Behavioural: under-reaction to rate/macro news.
  - Structural: slow-moving capital (pension funds, reserve managers)
    creates persistent flows.

WHAT THIS MODULE DOES:
We run the ranking on our 8-pair universe using H1 data (resampled to
daily) and return a per-pair bias score in [-1, +1]:
  +1  -> strongest positive momentum (long bias)
  -1  -> strongest negative momentum (short bias)
   0  -> middle of the pack

We then convert this into a small confidence BOOST in the meta filter:
  * Trade direction AGREES with momentum-quartile: +3
  * Trade direction CONTRADICTS momentum-quartile: -4

HOW IT'S USED:
main.py calls evaluate_universe(pair_to_h1) once per cycle, passing a
dict of pair -> H1 dataframe. It returns a CrossMomResult that can be
queried per pair via .get_bias(pair).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# Lookback windows (in daily bars). We combine 3 horizons.
MOM_LOOKBACKS_DAYS = [20, 60, 120]   # ~1m / 3m / 6m
MOM_WEIGHTS        = [0.5, 0.3, 0.2] # shorter horizon dominates (12w style)

# Quartile thresholds on the normalised rank [0,1]
TOP_QUARTILE    = 0.75
BOTTOM_QUARTILE = 0.25


@dataclass
class PairMomentum:
    pair: str
    combined_return: float   # weighted sum of lookback returns
    rank: float              # 0..1, 1 = strongest
    bias_score: float        # [-1,+1], derived from rank
    quartile: str            # TOP | MID | BOTTOM


@dataclass
class CrossMomResult:
    entries: dict[str, PairMomentum] = field(default_factory=dict)
    universe_size: int = 0
    note: str = ""

    def get_bias(self, pair: str) -> Optional[PairMomentum]:
        return self.entries.get(pair)

    def boost_for_trade(self, pair: str, trade_direction: str) -> tuple[float, str]:
        """
        Convert the bias into a confidence boost/penalty.
          * Long in TOP-quartile  -> +3
          * Short in BOTTOM-quartile -> +3
          * Long in BOTTOM-quartile -> -4 (fighting the trend-of-trends)
          * Short in TOP-quartile -> -4
          * MID -> 0
        """
        pm = self.entries.get(pair)
        if pm is None:
            return 0.0, "xmom unavailable"

        if pm.quartile == "TOP":
            if trade_direction == "BUY":
                return 3.0, f"xmom top-quartile (rank={pm.rank:.2f})"
            return -4.0, f"xmom SELL vs top-quartile (rank={pm.rank:.2f})"

        if pm.quartile == "BOTTOM":
            if trade_direction == "SELL":
                return 3.0, f"xmom bottom-quartile (rank={pm.rank:.2f})"
            return -4.0, f"xmom BUY vs bottom-quartile (rank={pm.rank:.2f})"

        return 0.0, "xmom mid-pack"


def _daily_close_from_h1(h1_df: pd.DataFrame) -> pd.Series:
    """Extract a daily close series from H1 data."""
    if h1_df is None or h1_df.empty or "close" not in h1_df.columns:
        return pd.Series(dtype=float)
    daily = h1_df["close"].resample("1D").last().dropna()
    return daily


def _weighted_return(close: pd.Series) -> Optional[float]:
    """
    Weighted average of returns over [20, 60, 120] day lookbacks.
    Returns None if the series is too short.
    """
    min_bars = max(MOM_LOOKBACKS_DAYS) + 1
    if len(close) < min_bars:
        return None

    last = float(close.iloc[-1])
    if last <= 0:
        return None

    total = 0.0
    used_weight = 0.0
    for lb, w in zip(MOM_LOOKBACKS_DAYS, MOM_WEIGHTS):
        prior = float(close.iloc[-(lb + 1)])
        if prior <= 0 or np.isnan(prior):
            continue
        ret = (last / prior) - 1.0
        total += ret * w
        used_weight += w

    if used_weight == 0:
        return None
    return total / used_weight


def evaluate_universe(pair_to_h1: dict[str, pd.DataFrame]) -> CrossMomResult:
    """
    Build a cross-sectional momentum ranking across all pairs with data.

    `pair_to_h1` is a dict of pair -> H1 OHLCV DataFrame
    (the same cache main.py already maintains).
    """
    if not pair_to_h1:
        return CrossMomResult(note="no data")

    per_pair_return: dict[str, float] = {}
    for pair, h1 in pair_to_h1.items():
        close = _daily_close_from_h1(h1)
        r = _weighted_return(close)
        if r is not None:
            per_pair_return[pair] = r

    if len(per_pair_return) < 4:
        return CrossMomResult(
            universe_size=len(per_pair_return),
            note=f"universe too small ({len(per_pair_return)})",
        )

    # Rank (higher return -> higher rank)
    sorted_pairs = sorted(per_pair_return.items(), key=lambda kv: kv[1])
    n = len(sorted_pairs)
    entries: dict[str, PairMomentum] = {}
    for i, (pair, ret) in enumerate(sorted_pairs):
        rank = i / (n - 1) if n > 1 else 0.5
        # Bias score: map [0,1] to [-1,+1]
        bias = (rank - 0.5) * 2.0

        if rank >= TOP_QUARTILE:
            q = "TOP"
        elif rank <= BOTTOM_QUARTILE:
            q = "BOTTOM"
        else:
            q = "MID"

        entries[pair] = PairMomentum(
            pair=pair,
            combined_return=round(ret, 5),
            rank=round(rank, 3),
            bias_score=round(bias, 3),
            quartile=q,
        )

    logger.info(
        "[XMOM] %d pairs | top=%s | bottom=%s",
        n,
        [p for p, pm in entries.items() if pm.quartile == "TOP"],
        [p for p, pm in entries.items() if pm.quartile == "BOTTOM"],
    )

    return CrossMomResult(entries=entries, universe_size=n)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.mt5_connection import connect, disconnect
    from core.data_fetcher import get_h1
    from config import PAIRS

    if connect():
        cache = {p: get_h1(p) for p in PAIRS}
        res = evaluate_universe(cache)
        for p, pm in res.entries.items():
            print(f"  {p}: rank={pm.rank:.2f} quartile={pm.quartile} r={pm.combined_return:+.3%}")
        disconnect()
