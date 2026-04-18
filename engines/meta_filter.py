"""
ARCS-FX -- engines/meta_filter.py
The Meta-Filter: the single gate that all trades pass through.

WHY THIS EXISTS:
Raw price-action / SMC signals on small accounts are noise-indistinguishable.
But STACKING several independent edges (macro, vol, event, COT, cross-sectional,
carry, cost) and requiring CONSENSUS before firing a trade dramatically
improves win-rate -- this is the core insight behind Marcos Lopez de Prado's
"meta-labelling" in Advances in Financial Machine Learning (2018).

THIS MODULE:
  * Takes the base confidence score from confidence_score.py
  * Queries EVERY edge module in one place
  * Applies hard veto gates (no-trade windows, macro contradiction, chaos vol)
  * Applies additive boosts/penalties from every soft edge
  * Returns a single MetaDecision: allow/deny + final_score + audit trail

KEY PRINCIPLE:
Any edge that is UNAVAILABLE (network error, missing data) contributes 0 --
we never PENALISE a trade just because an edge was silent. Missing data is
treated as neutral, not negative. This keeps the system robust.

HARD VETOES (block trade regardless of score):
  1. event_flow.no_trade = True   (FOMC impact, NFP impact, weekend gap)
  2. vol_regime.tradeable = False (CHAOS regime)
  3. macro_anchor contradicts trade direction with strength >= 0.6
  4. execution_cost.acceptable = False
  5. final_score < CONFIDENCE_MIN

SOFT BOOSTS/PENALTIES (sum into final_score):
  * macro_anchor aligned:       + strength * 5     (up to +5)
  * macro_anchor neutral:        0
  * macro_anchor contradicts:   -5 (soft if strength < 0.6)
  * event_flow.boost:            as provided (-10..+10)
  * vol_regime defensive:       -3 (don't block, just penalise)
  * cot_positioning:             as provided (-5..+2)
  * cross_momentum:              as provided (-4..+3)
  * carry_basket:                as provided (-3..+3)
  * execution_cost:              as provided (-5..+3)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class MetaDecision:
    pair: str
    trade_direction: str
    base_score: float
    final_score: float
    allowed: bool
    vetoes: list[str] = field(default_factory=list)
    contributions: list[tuple[str, float, str]] = field(default_factory=list)
    # List of (edge_name, delta, note). Full audit trail for logs.

    def add_contribution(self, name: str, delta: float, note: str = "") -> None:
        self.contributions.append((name, round(delta, 2), note))
        self.final_score += delta

    def add_veto(self, name: str, reason: str) -> None:
        self.vetoes.append(f"{name}: {reason}")
        self.allowed = False

    def summary(self) -> str:
        tag = "ALLOW" if self.allowed else "DENY"
        contribs = ", ".join(f"{n}={d:+.1f}" for (n, d, _) in self.contributions)
        vetoes = f" | vetoes={self.vetoes}" if self.vetoes else ""
        return (
            f"META[{self.pair} {self.trade_direction}] {tag} "
            f"base={self.base_score:.1f} -> final={self.final_score:.1f} "
            f"[{contribs}]{vetoes}"
        )


def evaluate_trade(
    pair: str,
    trade_direction: str,
    base_score: float,
    h1_df: Optional[pd.DataFrame] = None,
    tp_pips: Optional[float] = None,
    sl_pips: Optional[float] = None,
    live_spread_pips: Optional[float] = None,
    xmom_result=None,
    fomc_today: bool = False,
    now_utc: Optional[datetime] = None,
    confidence_min: float = 70.0,
    macro_veto_strength: float = 0.6,
) -> MetaDecision:
    """
    Run the full meta-filter stack for a proposed trade.

    Parameters
    ----------
    pair              : "EURUSD" etc.
    trade_direction   : "BUY" or "SELL"
    base_score        : raw confidence score (0..100) from confidence_score.py
    h1_df             : H1 OHLCV DataFrame (used by vol_regime, carry haircut)
    tp_pips, sl_pips  : proposed TP/SL distances (for execution-cost check)
    live_spread_pips  : current broker spread
    xmom_result       : pre-computed CrossMomResult (to avoid recomputation)
    fomc_today        : news engine has flagged today as FOMC
    now_utc           : override for testing
    confidence_min    : reject if final_score < this (default 70)
    macro_veto_strength: macro contradiction blocks if strength >= this
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    decision = MetaDecision(
        pair=pair,
        trade_direction=trade_direction,
        base_score=base_score,
        final_score=base_score,
        allowed=True,
    )

    # ----- 1. Event-flow window check (hard veto candidate) -----------------
    try:
        from engines import event_flow
        ef = event_flow.evaluate(pair, now_utc=now_utc, fomc_today=fomc_today)
        if ef.no_trade:
            decision.add_veto("event_flow", f"toxic window ({','.join(ef.active_windows)})")
        else:
            decision.add_contribution("event_flow", ef.boost, ef.note)
            # Small directional boost if the pair's event-flow bias aligns
            bias = ef.bias_direction
            if bias:
                if (trade_direction == "BUY" and bias == "BULLISH") or \
                   (trade_direction == "SELL" and bias == "BEARISH"):
                    decision.add_contribution("event_flow_bias", 1.5, f"aligned with {bias}")
                elif bias != "NEUTRAL":
                    decision.add_contribution("event_flow_bias", -1.5, f"against {bias}")
    except Exception as exc:
        logger.warning("[META] event_flow failed: %s", exc)

    # ----- 2. Vol regime (hard veto if CHAOS) -------------------------------
    try:
        from engines import vol_regime
        if h1_df is not None and not h1_df.empty:
            vr = vol_regime.classify(pair, h1_df)
            if not vr.tradeable:
                decision.add_veto("vol_regime", f"CHAOS (pct={vr.percentile:.0f})")
            elif vr.defensive_only:
                decision.add_contribution("vol_regime", -3.0, f"HIGH defensive")
            elif vr.regime == "LOW":
                decision.add_contribution("vol_regime", +1.0, "LOW vol -- favoured")
            else:
                decision.add_contribution("vol_regime", 0.0, "NORMAL")
    except Exception as exc:
        logger.warning("[META] vol_regime failed: %s", exc)

    # ----- 3. Macro anchor (hard veto if strong contradiction) --------------
    try:
        from engines import macro_anchor
        ma = macro_anchor.evaluate(pair, now_utc=now_utc)
        if ma.direction == "NEUTRAL":
            decision.add_contribution("macro_anchor", 0.0, "neutral")
        elif ma.aligns_with(trade_direction):
            decision.add_contribution("macro_anchor", ma.strength * 5.0,
                                      f"aligned strength={ma.strength:.2f}")
        else:
            # Contradiction
            if ma.strength >= macro_veto_strength:
                decision.add_veto("macro_anchor",
                                  f"contradicts ({ma.direction}, strength={ma.strength:.2f})")
            else:
                decision.add_contribution("macro_anchor", -5.0,
                                          f"soft contradict strength={ma.strength:.2f}")
    except Exception as exc:
        logger.warning("[META] macro_anchor failed: %s", exc)

    # ----- 4. COT positioning ----------------------------------------------
    try:
        from engines import cot_positioning
        boost, note = cot_positioning.boost_for_trade(pair, trade_direction)
        if boost != 0.0 or note != "COT unavailable":
            decision.add_contribution("cot", boost, note)
    except Exception as exc:
        logger.warning("[META] cot_positioning failed: %s", exc)

    # ----- 5. Cross-sectional momentum -------------------------------------
    if xmom_result is not None:
        try:
            boost, note = xmom_result.boost_for_trade(pair, trade_direction)
            decision.add_contribution("xmom", boost, note)
        except Exception as exc:
            logger.warning("[META] xmom failed: %s", exc)

    # ----- 6. Carry basket --------------------------------------------------
    try:
        from engines import carry_basket
        cr = carry_basket.evaluate(pair, h1_df=h1_df)
        if cr is not None:
            boost, note = cr.boost_for_trade(trade_direction)
            decision.add_contribution("carry", boost, note)
    except Exception as exc:
        logger.warning("[META] carry_basket failed: %s", exc)

    # ----- 7. Execution cost (hard veto candidate) --------------------------
    if tp_pips is not None and sl_pips is not None:
        try:
            from engines import execution_cost
            ca = execution_cost.assess_trade(pair, tp_pips, sl_pips, live_spread_pips)
            decision.add_contribution("cost", ca.boost_for_confidence(),
                                      f"RR_post={ca.rr_after_cost:.2f}")
            if not ca.acceptable:
                decision.add_veto("execution_cost", ca.reason)
        except Exception as exc:
            logger.warning("[META] execution_cost failed: %s", exc)

    # ----- 8. Confidence floor ---------------------------------------------
    if decision.final_score < confidence_min:
        decision.add_veto("confidence_floor",
                          f"final={decision.final_score:.1f} < min={confidence_min:.1f}")

    # Clamp score for sanity (don't let it exceed 100)
    decision.final_score = min(max(decision.final_score, 0.0), 100.0)

    logger.info("[META] %s", decision.summary())
    return decision


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    d = evaluate_trade("EURUSD", "BUY", base_score=72.0,
                       tp_pips=10, sl_pips=5, live_spread_pips=0.9)
    print(d.summary())
