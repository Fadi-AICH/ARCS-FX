"""
ARCS-PROP — dynamic_risk.py
Pure risk-tier calculator.

Given current equity + peak + phase, returns the % risk to apply on the
next trade, OR None when the rules say "pause and do not trade".

Two independent tier ladders:
  1. GROWTH tiers — shrink risk as we approach the phase target so we lock
     gains and don't give back a pass on the final trade.
  2. RECOVERY tiers — shrink risk (or pause) when equity is below peak, so
     a drawdown can't compound into a blown challenge.

Recovery always wins over growth (the more conservative tier applies).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from prop.prop_config import (
    PHASE_1, PHASE_2,
    RISK_DEFAULT_PCT, RISK_DEFAULT_PCT_P2,
    RISK_GROWTH_TIERS, RISK_RECOVERY_TIERS,
)


@dataclass
class RiskDecision:
    """
    Result of a risk-tier lookup.

    risk_pct : % of equity to risk on the next trade (e.g. 0.75 for 0.75%).
               None ⇒ caller MUST NOT open a new trade and should pause.
    tier     : short label for logging / dashboard readout.
    pause    : True when risk_pct is None because a recovery pause triggered.
    reason   : human-readable explanation of which rule fired.
    """
    risk_pct: Optional[float]
    tier: str
    pause: bool
    reason: str


def _base_risk_for_phase(phase: str) -> float:
    if phase == PHASE_2:
        return RISK_DEFAULT_PCT_P2
    return RISK_DEFAULT_PCT


def _growth_risk(gain_pct: float, base: float) -> tuple[float, str]:
    """
    Walk growth tiers in descending order. Returns (risk_pct, tier_label).
    Tiers are stored as (gain_threshold_pct, risk_pct).
    """
    for threshold, risk in sorted(RISK_GROWTH_TIERS, key=lambda t: -t[0]):
        if gain_pct >= threshold:
            return risk, f"GROWTH>=+{threshold:.1f}%"
    return base, "BASE"


def _recovery_risk(from_peak_pct: float) -> tuple[Optional[float], str]:
    """
    Walk recovery tiers in ascending (most-negative-first) order.
    Tiers: (from_peak_threshold_NEGATIVE, risk_pct_or_None).
    Returns (risk_pct_or_None, tier_label) or (None tier) if no recovery rule applies.
    """
    # Most severe (most negative threshold) should be checked first.
    for threshold, risk in sorted(RISK_RECOVERY_TIERS, key=lambda t: t[0]):
        if from_peak_pct <= threshold:
            label = f"RECOVERY<={threshold:.1f}%"
            return risk, label
    return -1.0, ""  # sentinel: no recovery tier triggered


def compute_risk(
    current_equity: float,
    peak_equity: float,
    phase_start_balance: float,
    phase: str,
) -> RiskDecision:
    """
    Evaluate both tier ladders and return the more conservative decision.

    gain_pct     = (current - phase_start) / phase_start * 100
    from_peak    = (current - peak)        / peak        * 100   (≤ 0)
    """
    if phase_start_balance <= 0 or peak_equity <= 0:
        return RiskDecision(
            risk_pct=None, tier="INVALID", pause=True,
            reason="phase_start_balance or peak_equity not initialised",
        )

    gain_pct      = (current_equity - phase_start_balance) / phase_start_balance * 100.0
    from_peak_pct = (current_equity - peak_equity)         / peak_equity         * 100.0

    base = _base_risk_for_phase(phase)

    # Recovery first — it can force a pause.
    rec_risk, rec_label = _recovery_risk(from_peak_pct)
    if rec_label:
        if rec_risk is None:
            return RiskDecision(
                risk_pct=None, tier=rec_label, pause=True,
                reason=f"from_peak {from_peak_pct:+.2f}% triggers 24h recovery pause",
            )
        return RiskDecision(
            risk_pct=rec_risk, tier=rec_label, pause=False,
            reason=f"from_peak {from_peak_pct:+.2f}% → risk {rec_risk:.2f}%",
        )

    # No recovery rule fired — apply growth tiers (they only ever shrink base).
    grw_risk, grw_label = _growth_risk(gain_pct, base)
    return RiskDecision(
        risk_pct=grw_risk, tier=grw_label, pause=False,
        reason=f"gain {gain_pct:+.2f}% (phase={phase}) → risk {grw_risk:.2f}%",
    )
