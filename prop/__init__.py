"""ARCS-PROP — FundedNext Stellar Lite challenge bot.

Separate from the main ARCS-FX bot:
  - Different MT5 login, magic number, DB, log files, dashboard port.
  - Stricter entry filters, dynamic risk tiers, trailing peak DD watchdog.
"""

from prop.challenge_state import ChallengeState, ChallengeStateStore
from prop.dynamic_risk    import RiskDecision, compute_risk
from prop.equity_watchdog import EquityWatchdog, WatchdogAction

__all__ = [
    "ChallengeState",
    "ChallengeStateStore",
    "RiskDecision",
    "compute_risk",
    "EquityWatchdog",
    "WatchdogAction",
]
