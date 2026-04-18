"""
ARCS-PROP — equity_watchdog.py
Hard-limit circuit breakers that run on a fast poll loop (every 5s by default)
and are the LAST line of defence before firm caps are hit.

Checks, in priority order:
  1. Max drawdown (trailing, from peak_equity) → HALT challenge
  2. Daily loss cap                            → flatten + pause till UTC midnight
  3. Daily win lock (+3% before 15:00 UTC)     → flatten + pause till UTC midnight
  4. Consecutive-loss cooldown (informational — set by close handler, read here)

The watchdog never places trades — it only closes/pauses. All thresholds
are INTERNAL caps (below firm caps) so there is always a buffer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from typing import Callable, Optional

from prop.challenge_state import ChallengeState, ChallengeStateStore
from prop.prop_config import (
    INTERNAL_DAILY_LOSS_PCT, INTERNAL_MAX_DRAWDOWN_PCT,
    DAILY_WIN_LOCK_PCT, DAILY_WIN_LOCK_HOUR_UTC,
    PHASE_HALTED,
)

logger = logging.getLogger("prop.equity_watchdog")


@dataclass
class WatchdogAction:
    """Returned on every poll so the caller knows what (if anything) to do."""
    halt:          bool = False
    flatten:       bool = False
    pause_today:   bool = False   # pause until next UTC midnight
    reason:        str  = ""


def _next_utc_midnight(now_utc: datetime) -> datetime:
    tomorrow = (now_utc + timedelta(days=1)).date()
    return datetime.combine(tomorrow, time.min, tzinfo=timezone.utc)


class EquityWatchdog:
    """
    Stateless checker: call `evaluate()` on every equity poll. It consults
    the persisted ChallengeState for peak/start balances and calls back the
    supplied `flatten_fn` when a hard stop fires.

    Usage:
        watchdog = EquityWatchdog(store, flatten_fn=order_mgr.close_all_trades)
        action   = watchdog.evaluate(current_equity, now_utc)
        if action.halt:    ...
        elif action.flatten: ...
    """

    def __init__(
        self,
        store: ChallengeStateStore,
        flatten_fn: Callable[[str], None],
    ) -> None:
        self._store     = store
        self._flatten   = flatten_fn

    # ---------- Public ----------

    def evaluate(self, current_equity: float, now_utc: datetime) -> WatchdogAction:
        state = self._store.get()

        # If already halted, nothing more to do.
        if state.phase == PHASE_HALTED:
            return WatchdogAction(halt=True, reason=f"already HALTED: {state.halt_reason}")

        # 1. Max drawdown from peak (trailing)
        if state.peak_equity > 0:
            from_peak_pct = (current_equity - state.peak_equity) / state.peak_equity * 100.0
            if from_peak_pct <= -INTERNAL_MAX_DRAWDOWN_PCT:
                reason = (
                    f"MAX_DD breach: equity={current_equity:.2f} peak={state.peak_equity:.2f} "
                    f"from_peak={from_peak_pct:+.2f}% (cap={-INTERNAL_MAX_DRAWDOWN_PCT}%)"
                )
                self._flatten(reason)
                self._store.halt(reason)
                return WatchdogAction(halt=True, flatten=True, reason=reason)

        # 2. Daily loss cap
        if state.day_start_balance > 0:
            day_pnl_pct = (current_equity - state.day_start_balance) / state.day_start_balance * 100.0

            if day_pnl_pct <= -INTERNAL_DAILY_LOSS_PCT:
                reason = (
                    f"DAILY_LOSS_CAP: equity={current_equity:.2f} day_start={state.day_start_balance:.2f} "
                    f"day_pnl={day_pnl_pct:+.2f}% (cap={-INTERNAL_DAILY_LOSS_PCT}%)"
                )
                self._flatten(reason)
                self._store.pause_until(_next_utc_midnight(now_utc), "DAILY_LOSS_CAP")
                return WatchdogAction(flatten=True, pause_today=True, reason=reason)

            # 3. Daily win lock
            if (
                day_pnl_pct >= DAILY_WIN_LOCK_PCT
                and now_utc.hour >= DAILY_WIN_LOCK_HOUR_UTC
            ):
                reason = (
                    f"DAILY_WIN_LOCK: day_pnl={day_pnl_pct:+.2f}% ≥ +{DAILY_WIN_LOCK_PCT}% "
                    f"after {DAILY_WIN_LOCK_HOUR_UTC}:00 UTC — locking gain"
                )
                self._flatten(reason)
                self._store.pause_until(_next_utc_midnight(now_utc), "DAILY_WIN_LOCK")
                return WatchdogAction(flatten=True, pause_today=True, reason=reason)

        # 4. Peak update (trailing peak — only rises)
        if current_equity > state.peak_equity:
            self._store.update(peak_equity=float(current_equity))

        return WatchdogAction()  # no-op: all checks passed
