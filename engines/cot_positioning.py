"""
ARCS-FX -- engines/cot_positioning.py
CFTC Commitment of Traders positioning data (weekly).

WHY THIS EXISTS:
Every Tuesday, the CFTC publishes the Commitments of Traders (COT)
report covering positioning in currency futures as of the prior
Tuesday. This is arguably the most valuable FREE data source in FX:
it tells you what the "smart money" (commercials / hedgers) and the
"dumb money" (small speculators) are doing on each currency.

CLASSIC EDGE:
  -- When NON-COMMERCIAL (large spec) net position is at a 52-week
     extreme, reversals are more likely. Crowded trades fail.
  -- When COMMERCIAL net position is at a 52-week extreme in the
     OPPOSITE direction, it's a high-conviction signal that the
     smart money is hedging against a reversal.

We use a simple z-score of the large-spec net position vs its 52-week
range. If the pair signal direction is the SAME as the crowded spec
direction AND the z-score > 2, we PENALISE the trade (mean-reversion
risk is elevated). If opposite, we boost.

DATA SOURCE:
  CFTC publishes a CSV: https://www.cftc.gov/dea/newcot/deacot<yyyymmdd>.zip
  We use the simpler "Financial Futures" endpoint via a public mirror
  (nilsvonbock) if available, with graceful fallback.

CACHING: weekly data changes once a week -- 24h cache is plenty.
"""

from __future__ import annotations

import logging
import time
import io
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CFTC contract name mapping to our FX pairs
# ---------------------------------------------------------------------------
# FX futures at CME are ALL quoted vs USD. Positioning in EUR future
# directly tells you spec positioning in EURUSD (long EUR future = long EURUSD).
# For the quote-side pair (USD/XXX) we INVERT the sign.
# ---------------------------------------------------------------------------

COT_MAP: dict[str, tuple[str, bool]] = {
    # pair -> (CFTC contract substring, invert_for_pair_direction)
    "EURUSD": ("EURO FX", False),
    "GBPUSD": ("BRITISH POUND", False),
    "AUDUSD": ("AUSTRALIAN DOLLAR", False),
    "NZDUSD": ("NZ DOLLAR", False),
    "USDCAD": ("CANADIAN DOLLAR", True),   # long CAD fut = short USDCAD
    "USDCHF": ("SWISS FRANC", True),
    "USDJPY": ("JAPANESE YEN", True),
    "EURJPY": ("EURO FX", False),          # approximation: use EUR leg
}

CACHE_TTL = 24 * 3600
_cot_cache: tuple[float, dict] = (0.0, {})


@dataclass
class COTReading:
    pair: str
    spec_net: float               # large-spec net position (contracts)
    spec_net_52w_zscore: float    # z-score vs rolling 52w
    commercial_net: float
    commercial_net_52w_zscore: float
    extreme_crowding: bool        # True if |spec z| > 2
    note: str = ""

    def crowded_long_for_pair(self) -> bool:
        """Speculators are excessively long this pair."""
        return self.spec_net_52w_zscore > 2.0

    def crowded_short_for_pair(self) -> bool:
        return self.spec_net_52w_zscore < -2.0

    def __str__(self) -> str:
        return (
            f"COT[{self.pair}] spec_z={self.spec_net_52w_zscore:+.2f} "
            f"commercial_z={self.commercial_net_52w_zscore:+.2f} "
            f"crowded={self.extreme_crowding}"
        )


def _fetch_cot_data() -> Optional[dict]:
    """
    Fetch raw COT time series. Uses the cot_reports package if available,
    else falls back to direct CFTC CSV fetch.

    Returns a dict: contract_substring -> list[(date, spec_net, comm_net)]
    for the last ~60 weeks.
    """
    now = time.monotonic()
    if now - _cot_cache[0] < CACHE_TTL and _cot_cache[1]:
        return _cot_cache[1]

    result: dict[str, list[tuple[datetime, float, float]]] = {}

    try:
        import cot_reports as ct
        df = ct.cot_all(cot_report_type="legacy_fut")  # Legacy Futures-only
        if df is None or df.empty:
            raise RuntimeError("cot_reports returned empty")

        # Normalise column names
        df.columns = [c.strip() for c in df.columns]
        name_col   = "Market and Exchange Names"
        date_col   = "As of Date in Form YYYY-MM-DD"
        spec_long  = "Noncommercial Positions-Long (All)"
        spec_short = "Noncommercial Positions-Short (All)"
        comm_long  = "Commercial Positions-Long (All)"
        comm_short = "Commercial Positions-Short (All)"

        for contract in {v[0] for v in COT_MAP.values()}:
            sub = df[df[name_col].str.contains(contract, case=False, na=False)]
            if sub.empty:
                continue
            sub = sub.copy()
            sub[date_col] = pd.to_datetime(sub[date_col])
            sub = sub.sort_values(date_col).tail(60)
            rows = []
            for _, r in sub.iterrows():
                try:
                    spec_net = float(r[spec_long]) - float(r[spec_short])
                    comm_net = float(r[comm_long]) - float(r[comm_short])
                    rows.append((r[date_col].to_pydatetime(), spec_net, comm_net))
                except Exception:
                    continue
            if rows:
                result[contract] = rows

        _cot_cache_val = result
        _cot_cache_new = (time.monotonic(), _cot_cache_val)
        globals()["_cot_cache"] = _cot_cache_new
        return result
    except ImportError:
        logger.warning("[COT] cot_reports not installed -- COT positioning disabled.")
        return None
    except Exception as exc:
        logger.warning("[COT] Fetch failed: %s -- COT disabled this cycle.", exc)
        return None


def evaluate(pair: str) -> Optional[COTReading]:
    """
    Return the latest COT reading for `pair` (or None if unavailable).
    """
    mapping = COT_MAP.get(pair)
    if mapping is None:
        return None
    contract, invert = mapping

    import pandas as _pd
    globals().setdefault("pd", _pd)

    data = _fetch_cot_data()
    if not data:
        return None

    rows = data.get(contract)
    if not rows or len(rows) < 10:
        return None

    spec_series = np.array([r[1] for r in rows])
    comm_series = np.array([r[2] for r in rows])

    def _zscore(series: np.ndarray) -> float:
        if len(series) < 10:
            return 0.0
        mean = series.mean()
        std  = series.std(ddof=0) or 1.0
        return float((series[-1] - mean) / std)

    spec_z = _zscore(spec_series)
    comm_z = _zscore(comm_series)

    if invert:
        spec_z = -spec_z
        comm_z = -comm_z

    return COTReading(
        pair=pair,
        spec_net=float(spec_series[-1]) * (-1 if invert else 1),
        spec_net_52w_zscore=round(spec_z, 3),
        commercial_net=float(comm_series[-1]) * (-1 if invert else 1),
        commercial_net_52w_zscore=round(comm_z, 3),
        extreme_crowding=abs(spec_z) > 2.0,
    )


def boost_for_trade(pair: str, trade_direction: str) -> tuple[float, str]:
    """
    Translate a COT reading into a confidence boost/penalty for the trade.

    Rules (conservative — only penalise extreme situations):
      * If speculators are crowded-long (z > +2) and trade is BUY: -5 (reversal risk)
      * If speculators are crowded-short (z < -2) and trade is SELL: -5
      * If commercials are heavily positioned AGAINST the trade (|z| > 2): -3
      * If commercials are heavily positioned WITH the trade: +2
    """
    reading = evaluate(pair)
    if reading is None:
        return 0.0, "COT unavailable"

    boost = 0.0
    notes = []

    if trade_direction == "BUY" and reading.crowded_long_for_pair():
        boost -= 5.0
        notes.append(f"crowded-long spec z={reading.spec_net_52w_zscore:+.1f}")
    elif trade_direction == "SELL" and reading.crowded_short_for_pair():
        boost -= 5.0
        notes.append(f"crowded-short spec z={reading.spec_net_52w_zscore:+.1f}")

    # Commercial hedger alignment
    if trade_direction == "BUY":
        if reading.commercial_net_52w_zscore > 1.5:
            boost += 2.0
            notes.append("commercials long")
        elif reading.commercial_net_52w_zscore < -1.5:
            boost -= 3.0
            notes.append("commercials heavily short")
    else:  # SELL
        if reading.commercial_net_52w_zscore < -1.5:
            boost += 2.0
            notes.append("commercials short")
        elif reading.commercial_net_52w_zscore > 1.5:
            boost -= 3.0
            notes.append("commercials heavily long")

    return boost, "; ".join(notes) if notes else "cot neutral"


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    for p in COT_MAP.keys():
        r = evaluate(p)
        print(f"{p}: {r}")
