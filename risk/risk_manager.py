"""
ARCS-FX -- risk/risk_manager.py
Production-grade risk management engine.

WHY THIS MODULE IS THE MOST IMPORTANT IN THE BOT:
A good entry can still blow up an account if position sizing is wrong.
Every professional prop desk runs risk management as a first-class system,
not an afterthought. This module:

  1. POSITION SIZING  -- Kelly-inspired adaptive sizing based on rolling win rate.
     Never bet the farm. Never trade flat when your edge is hot.

  2. CIRCUIT BREAKERS -- Hard daily and weekly drawdown limits.
     When you are losing, the market is telling you something. Stop listening
     to your gut and listen to the circuit breaker.

  3. CORRELATION GUARD -- Prevents doubling down on the same underlying move.
     EURUSD + GBPUSD long is NOT two trades. It is one leveraged bet on USD weakness.

  4. CONSECUTIVE LOSS COOLDOWN -- Smart-idle after 3 consecutive losses.
     The most dangerous time to trade is when you are trying to recover losses.
     The bot enforces a 2-hour pause so the regime can reset.

  5. TRADE COUNT CAP -- Hard cap on concurrent open positions.
     A $1,000 account should never have more than 2 live trades. Period.

ALL STATE IS PERSISTED to disk so that circuit breakers survive bot restarts.
If the bot crashes and restarts, it still knows it already lost 2.8% today.

USAGE:
    rm = RiskManager()
    check = rm.evaluate_trade(symbol, confidence_result, price_action_signal,
                              account_info, open_positions)
    if check.approved:
        lots = check.position_size_lots
        rm.record_trade_open(trade_id, symbol, lots, entry_price, sl_price)
    ...
    rm.record_trade_close(trade_id, pnl_usd, won=True)
"""

import os
import sys
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

# -- ensure project root is on sys.path regardless of how this module is loaded
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    RISK_BASE_PCT, RISK_MAX_PCT, RISK_HIGH_PCT, RISK_LOW_PCT,
    WIN_RATE_LOOKBACK, WIN_RATE_SCALE_UP, WIN_RATE_SCALE_DOWN,
    MAX_OPEN_TRADES, CORRELATION_THRESHOLD,
    DAILY_LOSS_LIMIT_PCT, WEEKLY_LOSS_LIMIT_PCT,
    MAX_CONSECUTIVE_LOSSES, CONSECUTIVE_LOSS_COOLDOWN_H,
    PAIRS, DB_PATH,
)
from core.instruments import (
    distance_in_units,
    get_profile,
    risk_value_per_lot,
    round_volume,
    volume_constraints,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pair correlation map  (static, based on known macro relationships)
# ---------------------------------------------------------------------------
# Groups of pairs that share a dominant underlying exposure.
# Within each group, opening two trades in the same direction is considered
# a correlated double-position and will be blocked by the correlation guard.
#
# WHY static correlation instead of rolling Pearson?
# Rolling correlation on price returns is noisy over short windows (< 30 bars).
# The structural relationships below are stable enough for risk management purposes.
# A full dynamic correlation matrix can be added in Phase 5 learning module.
# ---------------------------------------------------------------------------
CORRELATION_GROUPS: list[tuple[str, ...]] = [
    ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"),   # USD-index sensitive, long = USD short
    ("USDCHF", "USDJPY", "USDCAD"),              # USD-index sensitive, long = USD long
    ("EURUSD", "EURJPY"),                         # EUR exposure
    ("USDJPY", "EURJPY"),                         # JPY exposure
]

# State file -- persists circuit breaker counters across restarts
_STATE_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_STATE_FILE = os.path.join(_STATE_DIR, "risk_state.json")


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class TradeRecord:
    """Single closed trade stored in the rolling history window."""
    trade_id:     str
    symbol:       str
    lots:         float
    entry_price:  float
    sl_price:     float
    pnl_usd:      float
    won:          bool
    closed_at_utc: str   # ISO-8601 string for JSON serialisation


@dataclass
class OpenPosition:
    """Represents a currently open MT5 position. Passed in from order_manager."""
    trade_id:    str
    symbol:      str
    direction:   str    # BUY | SELL
    lots:        float
    entry_price: float
    sl_price:    float
    opened_at_utc: str


@dataclass
class AccountSnapshot:
    """Minimal account data needed for risk calculations."""
    balance:     float   # account balance (USD)
    equity:      float   # current equity including floating P&L
    margin_free: float   # free margin available


@dataclass
class RiskCheckResult:
    """
    Full decision record from a single evaluate_trade() call.

    approved: bool          -- True = bot may open this trade
    position_size_lots: float
    risk_pct: float         -- % of balance being risked
    risk_usd: float         -- USD amount being risked
    skip_reason: str        -- populated if approved=False
    win_rate: float         -- rolling win rate at decision time
    consecutive_losses: int
    cooldown_active: bool
    cooldown_until_utc: Optional[str]
    daily_loss_pct: float   -- drawdown so far today
    weekly_loss_pct: float  -- drawdown so far this week
    open_trade_count: int
    correlation_blocked: bool
    circuit_breaker_daily: bool
    circuit_breaker_weekly: bool
    """
    approved:               bool
    position_size_lots:     float
    risk_pct:               float
    risk_usd:               float
    skip_reason:            str
    win_rate:               float
    consecutive_losses:     int
    cooldown_active:        bool
    cooldown_until_utc:     Optional[str]
    daily_loss_pct:         float
    weekly_loss_pct:        float
    open_trade_count:       int
    correlation_blocked:    bool
    circuit_breaker_daily:  bool
    circuit_breaker_weekly: bool


@dataclass
class RiskState:
    """
    Persistent state that survives bot restarts.
    Written to data/risk_state.json after every update.
    """
    # Rolling trade history (last WIN_RATE_LOOKBACK closed trades)
    trade_history: list[dict]           = field(default_factory=list)

    # Day-level accounting
    day_start_balance: float            = 0.0
    day_start_date_utc: str             = ""   # YYYY-MM-DD

    # Week-level accounting
    week_start_balance: float           = 0.0
    week_start_date_utc: str            = ""   # ISO week start (Monday), YYYY-MM-DD

    # Consecutive loss tracker
    consecutive_losses: int             = 0
    cooldown_until_utc: Optional[str]   = None  # ISO-8601 or None


# ===========================================================================
# RiskManager
# ===========================================================================

class RiskManager:
    """
    Stateful risk management engine for ARCS-FX.

    WHY STATEFUL:
    Circuit breakers (daily loss, weekly loss, consecutive losses) must persist
    across ticks and across bot restarts. State is written to disk after every
    mutation so a crash never resets the counters.

    THREAD SAFETY:
    Single-threaded by design (matches the main.py orchestrator).
    Do not share instances across threads without adding a lock.
    """

    def __init__(self) -> None:
        os.makedirs(_STATE_DIR, exist_ok=True)
        self._state: RiskState = self._load_state()
        logger.info("RiskManager initialised. State loaded from %s", _STATE_FILE)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def evaluate_trade(
        self,
        symbol:           str,
        direction:        str,           # BUY | SELL
        entry_price:      float,
        sl_price:         float,
        account:          AccountSnapshot,
        open_positions:   list[OpenPosition],
        max_lots_cap:     float = 0.0,   # if > 0, vol-targeted upper bound on lots
    ) -> RiskCheckResult:
        """
        Master gate: should the bot open this trade?

        Checks are applied in order of severity (cheapest first):
          1. Cooldown (smart-idle after consecutive losses)
          2. Daily circuit breaker
          3. Weekly circuit breaker
          4. Open trade cap
          5. Correlation guard
          6. Position size feasibility (SL distance > 0)

        Returns RiskCheckResult with approved=True and calculated lots
        only when ALL checks pass.

        WHY return a full result even on rejection:
        The confidence engine logs every skip with its reason. Returning
        a detailed struct (not just True/False) lets the logger record
        exactly which gate fired and why.
        """
        # Refresh daily/weekly baselines against current account state
        self._refresh_period_baselines(account.balance)

        # Compute drawdown metrics
        daily_loss_pct  = self._daily_loss_pct(account.balance)
        weekly_loss_pct = self._weekly_loss_pct(account.balance)
        win_rate        = self._rolling_win_rate()
        consec          = self._state.consecutive_losses
        cooldown_until  = self._state.cooldown_until_utc

        # -- Base result frame (rejected by default, filled in below) --
        result = RiskCheckResult(
            approved=False,
            position_size_lots=0.0,
            risk_pct=0.0,
            risk_usd=0.0,
            skip_reason="",
            win_rate=win_rate,
            consecutive_losses=consec,
            cooldown_active=False,
            cooldown_until_utc=cooldown_until,
            daily_loss_pct=daily_loss_pct,
            weekly_loss_pct=weekly_loss_pct,
            open_trade_count=len(open_positions),
            correlation_blocked=False,
            circuit_breaker_daily=False,
            circuit_breaker_weekly=False,
        )

        # --- CHECK 1: smart-idle cooldown -----------------------------------
        if self._is_cooldown_active():
            result.cooldown_active = True
            result.skip_reason = (
                f"Smart-idle cooldown active until {cooldown_until} UTC "
                f"({MAX_CONSECUTIVE_LOSSES} consecutive losses)"
            )
            logger.warning("[RISK] BLOCKED %s -- %s", symbol, result.skip_reason)
            return result

        # --- CHECK 2: daily circuit breaker ---------------------------------
        if daily_loss_pct >= DAILY_LOSS_LIMIT_PCT:
            result.circuit_breaker_daily = True
            result.skip_reason = (
                f"Daily circuit breaker: lost {daily_loss_pct:.2f}% today "
                f"(limit {DAILY_LOSS_LIMIT_PCT}%)"
            )
            logger.warning("[RISK] BLOCKED %s -- %s", symbol, result.skip_reason)
            return result

        # --- CHECK 3: weekly circuit breaker --------------------------------
        if weekly_loss_pct >= WEEKLY_LOSS_LIMIT_PCT:
            result.circuit_breaker_weekly = True
            result.skip_reason = (
                f"Weekly circuit breaker: lost {weekly_loss_pct:.2f}% this week "
                f"(limit {WEEKLY_LOSS_LIMIT_PCT}%)"
            )
            logger.warning("[RISK] BLOCKED %s -- %s", symbol, result.skip_reason)
            return result

        # --- CHECK 4: open trade cap ----------------------------------------
        if len(open_positions) >= MAX_OPEN_TRADES:
            result.skip_reason = (
                f"Max open trades reached: {len(open_positions)}/{MAX_OPEN_TRADES}"
            )
            logger.warning("[RISK] BLOCKED %s -- %s", symbol, result.skip_reason)
            return result

        # --- CHECK 5: correlation guard -------------------------------------
        corr_conflict = self._correlation_conflict(symbol, direction, open_positions)
        if corr_conflict:
            result.correlation_blocked = True
            result.skip_reason = (
                f"Correlation guard: {symbol} {direction} is correlated with "
                f"existing position {corr_conflict}"
            )
            logger.warning("[RISK] BLOCKED %s -- %s", symbol, result.skip_reason)
            return result

        # --- CHECK 6: position sizing ---------------------------------------
        lots, risk_pct, risk_usd = self._calculate_position_size(
            symbol, entry_price, sl_price, account.balance, win_rate
        )

        # Inverse-vol upper bound: caps lots in calm markets where SL-based
        # sizing would otherwise allow oversized positions to meet risk %.
        # The cap can never INCREASE size -- it is a ceiling, not a floor.
        if max_lots_cap > 0 and lots > max_lots_cap:
            risk_per_lot = risk_value_per_lot(symbol, entry_price, sl_price)
            logger.info(
                "[RISK] Inverse-vol cap applied for %s: %.2f -> %.2f lots",
                symbol, lots, max_lots_cap,
            )
            lots = round_volume(symbol, max_lots_cap, mode="down")
            min_volume, _, _ = volume_constraints(symbol)
            if lots >= min_volume and risk_per_lot > 0:
                risk_usd = lots * risk_per_lot
                risk_pct = (risk_usd / account.balance) * 100.0

        if lots <= 0.0:
            result.skip_reason = (
                f"Position size calculation returned 0 lots "
                f"(SL distance too small or balance insufficient)"
            )
            logger.warning("[RISK] BLOCKED %s -- %s", symbol, result.skip_reason)
            return result

        # --- ALL CHECKS PASSED ---------------------------------------------
        result.approved           = True
        result.position_size_lots = lots
        result.risk_pct           = risk_pct
        result.risk_usd           = risk_usd
        result.skip_reason        = ""

        logger.info(
            "[RISK] APPROVED %s %s | lots=%.2f | risk=%.2f%% ($%.2f) | "
            "win_rate=%.1f%% | daily_loss=%.2f%% | weekly_loss=%.2f%%",
            symbol, direction, lots, risk_pct, risk_usd,
            win_rate * 100, daily_loss_pct, weekly_loss_pct,
        )
        return result

    def record_trade_open(
        self,
        trade_id:    str,
        symbol:      str,
        lots:        float,
        entry_price: float,
        sl_price:    float,
    ) -> None:
        """
        Register that a trade was opened.
        Called by order_manager immediately after MT5 order confirmation.
        """
        logger.info(
            "[RISK] Trade opened: id=%s symbol=%s lots=%.2f entry=%.5f sl=%.5f",
            trade_id, symbol, lots, entry_price, sl_price,
        )
        # No state change on open — circuit breakers only update on close.

    def record_trade_close(
        self,
        trade_id:    str,
        symbol:      str,
        lots:        float,
        entry_price: float,
        sl_price:    float,
        pnl_usd:     float,
        won:         bool,
    ) -> None:
        """
        Register that a trade was closed and update all risk state.

        WHY this must be called on EVERY close:
        - Consecutive loss counter tracks back-to-back losses
        - Trade history feeds the rolling win-rate calculator
        - Both feed position sizing for the NEXT trade

        If order_manager can't call this (e.g. MT5 hiccup), the
        next evaluate_trade() call will still use stale counters.
        It is the caller's responsibility to ensure this fires.
        """
        now_utc = datetime.now(timezone.utc)

        record = TradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            lots=lots,
            entry_price=entry_price,
            sl_price=sl_price,
            pnl_usd=pnl_usd,
            won=won,
            closed_at_utc=now_utc.isoformat(),
        )

        # Append to rolling history, keep only last WIN_RATE_LOOKBACK entries
        self._state.trade_history.append(asdict(record))
        if len(self._state.trade_history) > WIN_RATE_LOOKBACK:
            self._state.trade_history = self._state.trade_history[-WIN_RATE_LOOKBACK:]

        # Update consecutive loss counter
        if won:
            if self._state.consecutive_losses > 0:
                logger.info(
                    "[RISK] Win recorded -- consecutive loss streak reset "
                    "(was %d)", self._state.consecutive_losses
                )
            self._state.consecutive_losses = 0
            # Clear any expired cooldown (winning trade = streak broken)
            self._state.cooldown_until_utc = None
        else:
            self._state.consecutive_losses += 1
            logger.warning(
                "[RISK] Loss recorded -- consecutive losses: %d/%d",
                self._state.consecutive_losses, MAX_CONSECUTIVE_LOSSES,
            )
            if self._state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                cooldown_end = now_utc + timedelta(hours=CONSECUTIVE_LOSS_COOLDOWN_H)
                self._state.cooldown_until_utc = cooldown_end.isoformat()
                logger.warning(
                    "[RISK] SMART-IDLE COOLDOWN TRIGGERED: "
                    "%d consecutive losses. Bot paused until %s UTC.",
                    self._state.consecutive_losses,
                    self._state.cooldown_until_utc,
                )

        self._save_state()

        logger.info(
            "[RISK] Trade closed: id=%s won=%s pnl=$%.2f | "
            "history=%d trades | consecutive_losses=%d",
            trade_id, won, pnl_usd,
            len(self._state.trade_history),
            self._state.consecutive_losses,
        )

    def get_status_report(self, account: AccountSnapshot) -> dict:
        """
        Returns a human-readable snapshot of current risk state.
        Used by main.py for periodic status logging.
        """
        self._refresh_period_baselines(account.balance)
        win_rate    = self._rolling_win_rate()
        daily_loss  = self._daily_loss_pct(account.balance)
        weekly_loss = self._weekly_loss_pct(account.balance)

        return {
            "win_rate_pct":        round(win_rate * 100, 1),
            "trade_history_count": len(self._state.trade_history),
            "consecutive_losses":  self._state.consecutive_losses,
            "cooldown_active":     self._is_cooldown_active(),
            "cooldown_until_utc":  self._state.cooldown_until_utc,
            "daily_loss_pct":      round(daily_loss, 2),
            "daily_limit_pct":     DAILY_LOSS_LIMIT_PCT,
            "weekly_loss_pct":     round(weekly_loss, 2),
            "weekly_limit_pct":    WEEKLY_LOSS_LIMIT_PCT,
            "day_start_balance":   self._state.day_start_balance,
            "week_start_balance":  self._state.week_start_balance,
            "current_balance":     account.balance,
            "current_equity":      account.equity,
        }

    # -----------------------------------------------------------------------
    # Position sizing
    # -----------------------------------------------------------------------

    def _calculate_position_size(
        self,
        symbol:      str,
        entry_price: float,
        sl_price:    float,
        balance:     float,
        win_rate:    float,
    ) -> tuple[float, float, float]:
        """
        Kelly-inspired adaptive position sizing.

        Returns (lots, risk_pct, risk_usd).

        SIZING LOGIC:
          Base:    RISK_BASE_PCT (1.0%) of balance per trade
          Scale up:  if rolling win rate >= WIN_RATE_SCALE_UP (60%) -> RISK_HIGH_PCT (1.5%)
          Scale down: if rolling win rate <= WIN_RATE_SCALE_DOWN (40%) -> RISK_LOW_PCT (0.5%)
          Hard cap:  never exceed RISK_MAX_PCT (2.0%) regardless of win rate

        WHY not full Kelly?
        Full Kelly (f* = edge/odds) is theoretically optimal but maximises
        volatility. At 1-2% risk per trade we are well inside the half-Kelly
        zone which still compounds well while keeping drawdowns survivable.
        Full Kelly can be unlocked in Phase 5 once live performance data exists.

        LOT CALCULATION:
          stop distance -> contract-aware risk-per-lot -> lots
        """
        # Determine risk % based on win rate
        if win_rate >= WIN_RATE_SCALE_UP:
            risk_pct = RISK_HIGH_PCT
        elif win_rate <= WIN_RATE_SCALE_DOWN:
            risk_pct = RISK_LOW_PCT
        else:
            # Linear interpolation between low and high based on win rate
            span     = WIN_RATE_SCALE_UP - WIN_RATE_SCALE_DOWN
            position = (win_rate - WIN_RATE_SCALE_DOWN) / span
            risk_pct = RISK_LOW_PCT + position * (RISK_HIGH_PCT - RISK_LOW_PCT)

        risk_pct = min(risk_pct, RISK_MAX_PCT)   # hard cap
        risk_usd = balance * (risk_pct / 100.0)

        # SL distance in price units
        sl_distance = abs(entry_price - sl_price)
        if sl_distance < 1e-9:
            logger.error(
                "[RISK] SL distance near zero for %s (entry=%.5f sl=%.5f)",
                symbol, entry_price, sl_price,
            )
            return 0.0, 0.0, 0.0

        sl_units = distance_in_units(symbol, sl_distance)
        risk_per_lot = risk_value_per_lot(symbol, entry_price, sl_price)

        # Minimum stop-distance floor from the instrument profile.
        # This prevents oversized sizing on symbols whose raw stop is too
        # tight for their normal volatility. The order keeps the original SL;
        # the floor is only used for risk sizing.
        min_stop_units = get_profile(symbol).min_stop_units
        if sl_units < min_stop_units:
            logger.warning(
                "[RISK] SL too tight for %s: %.1f units < %.1f minimum. "
                "Using %.1f units for lot sizing (actual SL unchanged).",
                symbol, sl_units, min_stop_units, min_stop_units,
            )
            if sl_units > 0:
                risk_per_lot *= (min_stop_units / sl_units)
            sl_units = min_stop_units

        if risk_per_lot <= 0:
            logger.error("[RISK] Could not derive risk-per-lot for %s", symbol)
            return 0.0, 0.0, 0.0

        # Lots to risk exactly risk_usd
        raw_lots = risk_usd / risk_per_lot

        # Round to broker-supported volume step.
        lots = round_volume(symbol, raw_lots, mode="down")

        # Enforce minimum viable lot size
        min_volume, _, _ = volume_constraints(symbol)
        if lots < min_volume:
            logger.warning(
                "[RISK] Calculated lots %.4f < %.2f minimum for %s. Skipping.",
                raw_lots, min_volume, symbol,
            )
            return 0.0, 0.0, 0.0

        # Recalculate actual risk with rounded lots
        actual_risk_usd = lots * risk_per_lot
        actual_risk_pct = (actual_risk_usd / balance) * 100.0

        logger.debug(
            "[RISK] Sizing %s: win_rate=%.1f%% risk_pct=%.2f%% "
            "sl_units=%.1f raw_lots=%.4f final_lots=%.2f risk_usd=$%.2f",
            symbol, win_rate * 100, risk_pct,
            sl_units, raw_lots, lots, actual_risk_usd,
        )

        return lots, actual_risk_pct, actual_risk_usd

    # -----------------------------------------------------------------------
    # Win rate
    # -----------------------------------------------------------------------

    def _rolling_win_rate(self) -> float:
        """
        Compute win rate over the last WIN_RATE_LOOKBACK closed trades.

        WHY rolling window instead of all-time:
        The market regime changes. A 65% win rate from 3 months ago in
        RANGING markets is irrelevant during a TRENDING regime. The rolling
        window ensures the sizing adapts to RECENT performance, not history.

        Returns 0.5 (neutral) if fewer than 5 trades have been recorded
        -- not enough data to make a sizing decision.
        """
        history = self._state.trade_history
        if len(history) < 5:
            return 0.5   # default to neutral -- no data yet

        wins = sum(1 for t in history if t["won"])
        return wins / len(history)

    # -----------------------------------------------------------------------
    # Cooldown
    # -----------------------------------------------------------------------

    def _is_cooldown_active(self) -> bool:
        """
        Returns True if the bot is in smart-idle mode due to consecutive losses.

        Auto-expires: if the cooldown_until_utc timestamp has passed,
        the cooldown is cleared and False is returned.
        """
        if self._state.cooldown_until_utc is None:
            return False

        cooldown_end = datetime.fromisoformat(self._state.cooldown_until_utc)
        now_utc      = datetime.now(timezone.utc)

        if now_utc >= cooldown_end:
            logger.info(
                "[RISK] Smart-idle cooldown expired (was until %s). "
                "Resuming normal operation.", self._state.cooldown_until_utc
            )
            self._state.cooldown_until_utc  = None
            self._state.consecutive_losses  = 0
            self._save_state()
            return False

        return True

    # -----------------------------------------------------------------------
    # Circuit breakers
    # -----------------------------------------------------------------------

    def _refresh_period_baselines(self, current_balance: float) -> None:
        """
        Initialise or advance daily / weekly accounting periods.

        WHY we refresh on every evaluate_trade() call:
        The bot might run continuously for days. We need to detect the
        midnight rollover (new day) and Monday rollover (new week) so the
        circuit breakers reset at the correct boundaries.

        Day  boundary: UTC midnight
        Week boundary: UTC Monday 00:00
        """
        now_utc       = datetime.now(timezone.utc)
        today_str     = now_utc.strftime("%Y-%m-%d")
        monday_str    = (now_utc - timedelta(days=now_utc.weekday())).strftime("%Y-%m-%d")

        # --- Daily baseline -------------------------------------------------
        if self._state.day_start_date_utc != today_str:
            if self._state.day_start_date_utc:
                logger.info(
                    "[RISK] New trading day %s. Resetting daily circuit breaker. "
                    "Previous balance: $%.2f -> Current: $%.2f",
                    today_str, self._state.day_start_balance, current_balance,
                )
            self._state.day_start_balance   = current_balance
            self._state.day_start_date_utc  = today_str
            self._save_state()

        # --- Weekly baseline -----------------------------------------------
        if self._state.week_start_date_utc != monday_str:
            if self._state.week_start_date_utc:
                logger.info(
                    "[RISK] New trading week starting %s. Resetting weekly circuit breaker. "
                    "Previous balance: $%.2f -> Current: $%.2f",
                    monday_str, self._state.week_start_balance, current_balance,
                )
            self._state.week_start_balance  = current_balance
            self._state.week_start_date_utc = monday_str
            self._save_state()

    def _daily_loss_pct(self, current_balance: float) -> float:
        """
        How much of today's starting balance has been lost, as a percentage.

        Returns 0.0 if the account is at or above the day-start balance.
        A positive return value means a loss (e.g. 2.5 = lost 2.5% today).
        """
        if self._state.day_start_balance <= 0:
            return 0.0
        delta = self._state.day_start_balance - current_balance
        return max(0.0, (delta / self._state.day_start_balance) * 100.0)

    def _weekly_loss_pct(self, current_balance: float) -> float:
        """
        How much of this week's starting balance has been lost, as a percentage.
        Same sign convention as _daily_loss_pct.
        """
        if self._state.week_start_balance <= 0:
            return 0.0
        delta = self._state.week_start_balance - current_balance
        return max(0.0, (delta / self._state.week_start_balance) * 100.0)

    # -----------------------------------------------------------------------
    # Correlation guard
    # -----------------------------------------------------------------------

    def _correlation_conflict(
        self,
        symbol:         str,
        direction:      str,
        open_positions: list[OpenPosition],
    ) -> Optional[str]:
        """
        Returns the conflicting position's symbol if the new trade is
        correlated with an existing open position, else None.

        CORRELATION LOGIC:
          For each correlation group the proposed symbol belongs to:
            - If there is an open position in ANY other symbol from the same group
              AND that position's direction is the SAME (both BUY or both SELL)
              -> this is a correlated duplicate. Block it.

        WHY same-direction only:
        Long EURUSD + Short GBPUSD = partial hedge. Not ideal but acceptable.
        Long EURUSD + Long GBPUSD = double USD short exposure. BLOCKED.

        WHY static groups instead of rolling correlation matrix:
        See CORRELATION_GROUPS definition at top of file.
        """
        for group in CORRELATION_GROUPS:
            if symbol not in group:
                continue
            for pos in open_positions:
                if pos.symbol in group and pos.symbol != symbol:
                    if pos.direction == direction:
                        logger.debug(
                            "[RISK] Correlation conflict: %s %s vs open %s %s "
                            "(same group: %s)",
                            symbol, direction, pos.symbol, pos.direction, group,
                        )
                        return pos.symbol

        return None

    # -----------------------------------------------------------------------
    # State persistence
    # -----------------------------------------------------------------------

    def _load_state(self) -> RiskState:
        """
        Load risk state from disk. Returns a fresh RiskState if file absent
        or corrupt.

        WHY not raise on corrupt state:
        A corrupt state file on startup should not prevent the bot from running.
        Worst case: circuit breakers reset (conservative -- misses nothing,
        might allow a trade that yesterday's loss counter would have blocked).
        The log entry makes the reset auditable.
        """
        if not os.path.exists(_STATE_FILE):
            logger.info("[RISK] No state file found at %s. Starting fresh.", _STATE_FILE)
            return RiskState()

        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as fh:
                raw = json.load(fh)

            state = RiskState(
                trade_history       = raw.get("trade_history", []),
                day_start_balance   = raw.get("day_start_balance", 0.0),
                day_start_date_utc  = raw.get("day_start_date_utc", ""),
                week_start_balance  = raw.get("week_start_balance", 0.0),
                week_start_date_utc = raw.get("week_start_date_utc", ""),
                consecutive_losses  = raw.get("consecutive_losses", 0),
                cooldown_until_utc  = raw.get("cooldown_until_utc", None),
            )
            logger.info(
                "[RISK] State loaded: %d trades in history, %d consecutive losses, "
                "cooldown=%s",
                len(state.trade_history),
                state.consecutive_losses,
                state.cooldown_until_utc or "none",
            )
            return state

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error(
                "[RISK] State file corrupt (%s). Resetting to fresh state. "
                "Old file preserved as risk_state.json.bak", exc
            )
            import shutil
            shutil.copy(_STATE_FILE, _STATE_FILE + ".bak")
            return RiskState()

    def _save_state(self) -> None:
        """
        Write current state to disk atomically.

        WHY atomic write (write-then-rename):
        If the process is killed mid-write, the old state file remains intact.
        A half-written JSON file would be corrupted and trigger the recovery path.
        """
        tmp_path = _STATE_FILE + ".tmp"
        payload  = {
            "trade_history":       self._state.trade_history,
            "day_start_balance":   self._state.day_start_balance,
            "day_start_date_utc":  self._state.day_start_date_utc,
            "week_start_balance":  self._state.week_start_balance,
            "week_start_date_utc": self._state.week_start_date_utc,
            "consecutive_losses":  self._state.consecutive_losses,
            "cooldown_until_utc":  self._state.cooldown_until_utc,
        }

        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

        os.replace(tmp_path, _STATE_FILE)   # atomic on all POSIX systems and Win32


# ===========================================================================
# Standalone test harness
# ===========================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("ARCS-FX  --  RiskManager standalone test")
    print("=" * 60)

    rm = RiskManager()

    # Simulate a $1,000 demo account
    account = AccountSnapshot(balance=1000.0, equity=1000.0, margin_free=900.0)

    # Refresh baselines
    rm._refresh_period_baselines(account.balance)

    # --- Test 1: normal trade approval ------------------------------------
    print("\n--- TEST 1: Normal trade (no history, neutral win rate) ---")
    result = rm.evaluate_trade(
        symbol="EURUSD",
        direction="BUY",
        entry_price=1.08500,
        sl_price=1.08200,   # 30-pip SL
        account=account,
        open_positions=[],
    )
    print(f"  Approved:      {result.approved}")
    print(f"  Lots:          {result.position_size_lots}")
    print(f"  Risk %:        {result.risk_pct:.2f}%")
    print(f"  Risk USD:      ${result.risk_usd:.2f}")
    print(f"  Win rate:      {result.win_rate * 100:.1f}%")
    print(f"  Skip reason:   '{result.skip_reason}'")
    assert result.approved, "TEST 1 FAILED: should be approved"
    assert result.position_size_lots > 0, "TEST 1 FAILED: lots must be > 0"
    print("  -> PASS")

    # --- Test 2: correlation guard ----------------------------------------
    print("\n--- TEST 2: Correlation guard (GBPUSD BUY with EURUSD BUY open) ---")
    open_pos = [
        OpenPosition(
            trade_id="T001", symbol="EURUSD", direction="BUY",
            lots=0.03, entry_price=1.08500, sl_price=1.08200,
            opened_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    ]
    result2 = rm.evaluate_trade(
        symbol="GBPUSD",
        direction="BUY",
        entry_price=1.27000,
        sl_price=1.26700,
        account=account,
        open_positions=open_pos,
    )
    print(f"  Approved:      {result2.approved}")
    print(f"  Correlation:   {result2.correlation_blocked}")
    print(f"  Skip reason:   '{result2.skip_reason}'")
    assert not result2.approved,          "TEST 2 FAILED: should be blocked"
    assert result2.correlation_blocked,   "TEST 2 FAILED: correlation_blocked should be True"
    print("  -> PASS")

    # --- Test 3: open trade cap -------------------------------------------
    print("\n--- TEST 3: Open trade cap (2 positions already open) ---")
    two_positions = [
        OpenPosition("T001", "EURUSD", "BUY", 0.03, 1.08500, 1.08200,
                     datetime.now(timezone.utc).isoformat()),
        OpenPosition("T002", "USDCHF", "SELL", 0.03, 0.89000, 0.89300,
                     datetime.now(timezone.utc).isoformat()),
    ]
    result3 = rm.evaluate_trade(
        symbol="AUDUSD",
        direction="BUY",
        entry_price=0.64000,
        sl_price=0.63700,
        account=account,
        open_positions=two_positions,
    )
    print(f"  Approved:      {result3.approved}")
    print(f"  Skip reason:   '{result3.skip_reason}'")
    assert not result3.approved, "TEST 3 FAILED: should be blocked by trade cap"
    print("  -> PASS")

    # --- Test 4: consecutive loss cooldown ---------------------------------
    print("\n--- TEST 4: Consecutive loss cooldown ---")
    for i in range(MAX_CONSECUTIVE_LOSSES):
        rm.record_trade_close(
            trade_id=f"LOSS_{i}", symbol="EURUSD", lots=0.03,
            entry_price=1.08500, sl_price=1.08200,
            pnl_usd=-30.0, won=False,
        )
        print(f"  Recorded loss {i+1}/{MAX_CONSECUTIVE_LOSSES}")

    result4 = rm.evaluate_trade(
        symbol="USDJPY",
        direction="BUY",
        entry_price=149.500,
        sl_price=149.200,
        account=account,
        open_positions=[],
    )
    print(f"  Approved:      {result4.approved}")
    print(f"  Cooldown:      {result4.cooldown_active}")
    print(f"  Until:         {result4.cooldown_until_utc}")
    print(f"  Skip reason:   '{result4.skip_reason}'")
    assert not result4.approved,    "TEST 4 FAILED: should be in cooldown"
    assert result4.cooldown_active, "TEST 4 FAILED: cooldown_active should be True"
    print("  -> PASS")

    # --- Test 5: win rate scaling ------------------------------------------
    print("\n--- TEST 5: Win rate scaling (6 wins -> RISK_HIGH_PCT) ---")
    # Reset cooldown by clearing state directly (test only)
    rm._state.cooldown_until_utc  = None
    rm._state.consecutive_losses  = 0
    rm._state.trade_history       = []

    for i in range(6):
        rm.record_trade_close(
            trade_id=f"WIN_{i}", symbol="EURUSD", lots=0.03,
            entry_price=1.08500, sl_price=1.08200,
            pnl_usd=45.0, won=True,
        )

    result5 = rm.evaluate_trade(
        symbol="EURUSD",
        direction="BUY",
        entry_price=1.08500,
        sl_price=1.08200,   # 30-pip SL
        account=account,
        open_positions=[],
    )
    print(f"  Approved:      {result5.approved}")
    print(f"  Lots:          {result5.position_size_lots}")
    print(f"  Risk %:        {result5.risk_pct:.2f}%  (expect ~{RISK_HIGH_PCT}%)")
    print(f"  Win rate:      {result5.win_rate * 100:.1f}%")
    assert result5.approved, "TEST 5 FAILED: should be approved"
    print("  -> PASS")

    # --- Test 6: status report --------------------------------------------
    print("\n--- TEST 6: Status report ---")
    report = rm.get_status_report(account)
    for k, v in report.items():
        print(f"  {k}: {v}")
    print("  -> PASS")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
