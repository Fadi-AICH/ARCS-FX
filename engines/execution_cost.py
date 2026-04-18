"""
ARCS-FX -- engines/execution_cost.py
Trading-cost model: the single most-ignored reason retail bots die.

WHY THIS EXISTS:
On a $5k account scalping with 4-10 pip targets, EVERY pip of cost
matters. A profitable signal on paper is a NEGATIVE-EXPECTANCY trade
once you add:

  * Spread        : 0.5 - 2.0 pips (quoted)
  * Slippage      : 0.2 - 1.5 pips (entry + exit)
  * Commission    : 0.0 - 0.5 pip equivalent
  * Swap (rollover): small but negative most of the time

Rule of thumb: REAL cost is the bid/ask spread x 1.7 (to account for
slippage on entry and exit). A 1.0 pip spread on EURUSD costs ~1.7p
per round trip.

THIS MODULE DOES:
  1. Estimate the all-in round-trip cost in pips for a pair.
  2. Compare that cost to the EXPECTED WIN in pips (distance to TP).
  3. Compute a cost-adjusted expectancy:
         E = p_win * (tp_pips - cost) - (1-p_win) * (sl_pips + cost)
  4. Return True/False for "is this trade worth taking?" AND a
     boost/penalty to feed the confidence score.

HOW IT'S USED:
main.py calls assess_trade(...) AFTER all signals have been generated
but BEFORE execution. If the trade fails the cost test it is rejected
regardless of confidence score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (pips, round-trip)
# ---------------------------------------------------------------------------
SLIPPAGE_MULTIPLIER   = 1.7   # effective cost = spread * multiplier
COMMISSION_PIPS       = 0.0   # XM "Ultra Low" accounts: no commission
MIN_EDGE_PIPS         = 1.5   # TP - cost must exceed this, else reject
MIN_RR_AFTER_COST     = 1.2   # R:R after subtracting cost from TP / adding to SL

# Default fallback spreads (pips) if broker feed missing, per pair
DEFAULT_SPREADS = {
    "EURUSD": 0.8,
    "GBPUSD": 1.2,
    "AUDUSD": 1.3,
    "NZDUSD": 1.8,
    "USDJPY": 1.0,
    "USDCAD": 1.7,
    "USDCHF": 1.8,
    "EURJPY": 1.5,
}


@dataclass
class CostAssessment:
    pair: str
    spread_pips: float
    effective_cost_pips: float
    tp_pips: float
    sl_pips: float
    net_tp_pips: float          # tp_pips - effective_cost
    net_sl_pips: float          # sl_pips + effective_cost
    rr_after_cost: float
    expectancy_per_unit: float  # assuming 50% win-rate (signal-neutral)
    acceptable: bool
    reason: str = ""

    def boost_for_confidence(self) -> float:
        """
        Translate the cost picture into a confidence boost:
          * rr_after_cost >= 2.0 -> +3
          * rr_after_cost 1.5-2.0 -> +1
          * rr_after_cost 1.2-1.5 -> 0
          * rr_after_cost < 1.2  -> -5 (also usually blocks trade)
        """
        rr = self.rr_after_cost
        if rr >= 2.0:
            return 3.0
        if rr >= 1.5:
            return 1.0
        if rr >= 1.2:
            return 0.0
        return -5.0

    def __str__(self) -> str:
        return (
            f"COST[{self.pair}] spread={self.spread_pips:.1f}p "
            f"eff={self.effective_cost_pips:.1f}p "
            f"RR_post={self.rr_after_cost:.2f} "
            f"accept={self.acceptable}"
        )


def assess_trade(
    pair: str,
    tp_pips: float,
    sl_pips: float,
    live_spread_pips: Optional[float] = None,
) -> CostAssessment:
    """
    Is this trade worth the cost of taking?

    Parameters
    ----------
    pair           : "EURUSD" etc.
    tp_pips        : distance from entry to TP, in pips
    sl_pips        : distance from entry to SL, in pips
    live_spread_pips: current broker spread in pips (preferred).
                     Falls back to DEFAULT_SPREADS if None.
    """
    spread = (
        live_spread_pips
        if live_spread_pips is not None and live_spread_pips > 0
        else DEFAULT_SPREADS.get(pair, 2.0)
    )

    eff_cost = spread * SLIPPAGE_MULTIPLIER + COMMISSION_PIPS

    net_tp = max(tp_pips - eff_cost, 0.0)
    net_sl = sl_pips + eff_cost  # SL moves against you by the cost too

    if net_sl <= 0:
        rr = 0.0
    else:
        rr = net_tp / net_sl

    # Signal-neutral expectancy (50% win) -- a trade must be profitable
    # BEFORE we add any signal edge.
    exp_pips = 0.5 * net_tp - 0.5 * net_sl

    acceptable = True
    reason = "ok"
    if net_tp < MIN_EDGE_PIPS:
        acceptable = False
        reason = f"net_tp {net_tp:.1f}p < min_edge {MIN_EDGE_PIPS:.1f}p"
    elif rr < MIN_RR_AFTER_COST:
        acceptable = False
        reason = f"RR {rr:.2f} < {MIN_RR_AFTER_COST:.2f} after cost"

    return CostAssessment(
        pair=pair,
        spread_pips=round(spread, 2),
        effective_cost_pips=round(eff_cost, 2),
        tp_pips=round(tp_pips, 1),
        sl_pips=round(sl_pips, 1),
        net_tp_pips=round(net_tp, 1),
        net_sl_pips=round(net_sl, 1),
        rr_after_cost=round(rr, 2),
        expectancy_per_unit=round(exp_pips, 2),
        acceptable=acceptable,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    for (p, tp, sl, spread) in [
        ("EURUSD", 8, 5, 0.9),
        ("USDCAD", 10, 6, 2.0),
        ("USDJPY", 4, 3, 1.2),
        ("EURJPY", 12, 7, 1.5),
    ]:
        print(assess_trade(p, tp, sl, spread))
