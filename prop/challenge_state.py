"""
ARCS-PROP — challenge_state.py
Persisted state for a single prop-challenge attempt.

Tracks across restarts:
  - Starting balance for the challenge (never changes)
  - Starting balance for the CURRENT phase
  - Starting balance for TODAY (UTC day)
  - Peak equity ever seen (for trailing-DD calc)
  - Current phase (PHASE_1 / PHASE_2 / PASSED / HALTED)
  - Trade counters: today's trades, consecutive losses
  - Cooldown state: when it expires (post-loss, daily-cap, recovery-pause)
  - Halt reason + timestamp if the bot halted itself

Atomic-write pattern (tmp → rename) so a crash mid-save never corrupts state.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prop.prop_config import (
    CHALLENGE_ACCOUNT_USD, PHASE_1, PHASE_2, PHASE_PASSED, PHASE_HALTED,
    PROP_STATE_PATH,
)

logger = logging.getLogger("prop.challenge_state")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ChallengeState:
    """All fields serialise to JSON directly — no nested dataclasses."""

    # Identity
    challenge_started_utc:  str = ""                    # ISO-8601 timestamp
    account_login:          int = 0                     # MT5 login number (for audit)
    phase:                  str = PHASE_1               # PHASE_1 | PHASE_2 | PASSED | HALTED

    # Equity milestones
    start_balance:          float = CHALLENGE_ACCOUNT_USD
    phase_start_balance:    float = CHALLENGE_ACCOUNT_USD
    day_start_balance:      float = CHALLENGE_ACCOUNT_USD
    day_start_date_utc:     str   = ""                  # YYYY-MM-DD
    peak_equity:            float = CHALLENGE_ACCOUNT_USD

    # Counters
    daily_trades_count:     int = 0
    consecutive_losses:     int = 0
    total_trades:           int = 0

    # Cooldown / pause state
    cooldown_until_utc:     Optional[str] = None        # ISO-8601 or None
    pause_reason:           str = ""                    # human-readable

    # Halt state (only set when phase == HALTED)
    halt_reason:            str = ""
    halted_at_utc:          str = ""

    # Last-trade fingerprint (helps diagnostics)
    last_trade_utc:         str = ""
    last_trade_pnl:         float = 0.0
    last_trade_won:         Optional[bool] = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ChallengeStateStore:
    """
    Thread-safe, crash-safe persistence for ChallengeState.

    Usage:
        store = ChallengeStateStore(path="prop/prop_state.json")
        state = store.get()
        state.peak_equity = max(state.peak_equity, new_equity)
        store.save(state)

    All mutations should go through `update()` which handles save + logging.
    """

    def __init__(self, path: str = PROP_STATE_PATH) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._state: ChallengeState = self._load()

    # ----- Public API -----

    def get(self) -> ChallengeState:
        with self._lock:
            return self._state

    def save(self, state: Optional[ChallengeState] = None) -> None:
        """Persist state atomically (tmp file → rename)."""
        with self._lock:
            if state is not None:
                self._state = state
            tmp = self._path.with_suffix(".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(asdict(self._state), f, indent=2)
                os.replace(tmp, self._path)
            except Exception as exc:
                logger.error("Failed to save challenge state: %s", exc)

    def update(self, **fields) -> ChallengeState:
        """Apply field updates and persist atomically."""
        with self._lock:
            for k, v in fields.items():
                if not hasattr(self._state, k):
                    logger.warning("Unknown ChallengeState field: %s", k)
                    continue
                setattr(self._state, k, v)
            self.save()
            return self._state

    # ----- Convenience methods -----

    def bootstrap_if_empty(self, start_balance: float, account_login: int) -> ChallengeState:
        """
        First-run initialisation. Call once on startup. If state already exists,
        this is a no-op — we do NOT overwrite an in-progress challenge.
        """
        with self._lock:
            if self._state.challenge_started_utc:
                return self._state
            now = datetime.now(timezone.utc)
            self._state.challenge_started_utc = now.isoformat()
            self._state.account_login         = int(account_login or 0)
            self._state.start_balance         = float(start_balance)
            self._state.phase_start_balance   = float(start_balance)
            self._state.day_start_balance     = float(start_balance)
            self._state.day_start_date_utc    = now.strftime("%Y-%m-%d")
            self._state.peak_equity           = float(start_balance)
            self._state.phase                 = PHASE_1
            self.save()
            logger.info(
                "ChallengeState bootstrapped: start_balance=%.2f login=%d",
                start_balance, account_login,
            )
            return self._state

    def rollover_day_if_needed(self, current_balance: float, now_utc: datetime) -> bool:
        """
        Reset day-start balance + daily counters when UTC date changes.
        Returns True if a rollover occurred.
        """
        today = now_utc.strftime("%Y-%m-%d")
        with self._lock:
            if self._state.day_start_date_utc == today:
                return False
            prev = self._state.day_start_date_utc or "<never>"
            self._state.day_start_date_utc = today
            self._state.day_start_balance  = float(current_balance)
            self._state.daily_trades_count = 0
            # Clear daily-cap / daily-lock cooldowns but NOT recovery pauses
            # (recovery pause is a function of equity drawdown, not calendar).
            if self._state.pause_reason in ("DAILY_LOSS_CAP", "DAILY_WIN_LOCK"):
                self._state.cooldown_until_utc = None
                self._state.pause_reason       = ""
            self.save()
            logger.info("Day rollover: %s → %s (day_start=%.2f)", prev, today, current_balance)
            return True

    def advance_to_phase_2(self, new_start_balance: float) -> None:
        """Called once Phase 1 target is hit. Resets phase-level counters."""
        with self._lock:
            self._state.phase               = PHASE_2
            self._state.phase_start_balance = float(new_start_balance)
            self._state.peak_equity         = float(new_start_balance)
            self._state.consecutive_losses  = 0
            self.save()
            logger.info("Advanced to PHASE_2. new_start_balance=%.2f", new_start_balance)

    def mark_passed(self) -> None:
        with self._lock:
            self._state.phase = PHASE_PASSED
            self.save()
            logger.info("Challenge PASSED. Both phases complete.")

    def halt(self, reason: str) -> None:
        with self._lock:
            self._state.phase         = PHASE_HALTED
            self._state.halt_reason   = reason
            self._state.halted_at_utc = datetime.now(timezone.utc).isoformat()
            self.save()
            logger.critical("Challenge HALTED: %s", reason)

    def pause_until(self, until_utc: datetime, reason: str) -> None:
        with self._lock:
            self._state.cooldown_until_utc = until_utc.isoformat()
            self._state.pause_reason       = reason
            self.save()
            logger.warning("Paused until %s: %s", until_utc.isoformat(), reason)

    def clear_pause(self) -> None:
        with self._lock:
            self._state.cooldown_until_utc = None
            self._state.pause_reason       = ""
            self.save()

    def is_paused(self, now_utc: datetime) -> tuple[bool, str]:
        """Return (paused, reason). Auto-clears when cooldown expires."""
        with self._lock:
            if not self._state.cooldown_until_utc:
                return False, ""
            try:
                until = datetime.fromisoformat(self._state.cooldown_until_utc)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
            except Exception:
                # Corrupt timestamp → clear it
                self.clear_pause()
                return False, ""
            if now_utc >= until:
                reason = self._state.pause_reason
                self.clear_pause()
                return False, f"(expired: {reason})"
            return True, self._state.pause_reason

    def record_trade_closed(self, pnl_usd: float, won: bool, now_utc: datetime) -> None:
        with self._lock:
            self._state.total_trades    += 1
            self._state.last_trade_utc   = now_utc.isoformat()
            self._state.last_trade_pnl   = float(pnl_usd)
            self._state.last_trade_won   = bool(won)
            if won:
                self._state.consecutive_losses = 0
            else:
                self._state.consecutive_losses += 1
            self.save()

    def record_trade_opened(self) -> None:
        with self._lock:
            self._state.daily_trades_count += 1
            self.save()

    # ----- Internal -----

    def _load(self) -> ChallengeState:
        if not self._path.exists():
            logger.info("No existing challenge state at %s — starting fresh.", self._path)
            return ChallengeState()
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            state = ChallengeState(**{k: v for k, v in data.items() if k in ChallengeState.__dataclass_fields__})
            logger.info(
                "ChallengeState loaded: phase=%s peak=%.2f trades=%d",
                state.phase, state.peak_equity, state.total_trades,
            )
            return state
        except Exception as exc:
            logger.error("Failed to load challenge state (using defaults): %s", exc)
            return ChallengeState()
