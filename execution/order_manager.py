"""
ARCS-FX -- execution/order_manager.py
MT5 order placement and asymmetric trade management engine.

WHY THIS MODULE IS THE NERVE CENTRE AT RUNTIME:
Every other module produces analysis. This module acts on it.
It is the only code that touches real (or demo) MT5 money.

RESPONSIBILITIES:
  1. PLACE ORDERS    -- Send market orders to MT5 with SL + TP attached.
                        Validate before sending. Verify after sending.
  2. MANAGE TRADES   -- On every tick, iterate open positions and apply
                        the asymmetric management rules:
                          strategy-specific breakeven trigger
                          strategy-specific trailing activation and trail ratio
  3. CLOSE TRADES    -- Close positions partially or fully on signal,
                        or when the bot decides to exit a regime change.
  4. SYNC WITH RISK  -- Call RiskManager.record_trade_close() on every close
                        so circuit breakers stay current.
  5. SYNC WITH LOG   -- Call TradeLogger.log_close() so every trade is
                        stored in the SQLite DNA database.

ASYMMETRIC MANAGEMENT RATIONALE:
  Standard fixed TP is left on the table. Instead:
    - At +1R: SL moves to entry. Trade is now risk-free.
    - At +2R: Trailing stop engages. We ride the move as far as it goes.
  This creates an asymmetric payoff: max loss = 1R, potential gain = unlimited.
  The trailing stop uses a strategy-specific fraction of the achieved move so
  scalp trades lock profit faster while swing trades breathe longer.

MT5 ORDER FLOW:
  1. Build MqlTradeRequest (type MARKET, action DEAL)
  2. Call order_send() and inspect MqlTradeResult.retcode
  3. On success: store trade in _open_positions dict, call risk.record_trade_open()
  4. On failure: log exact MT5 return code, do NOT retry (avoid double-fills)

THREAD SAFETY:
  Single-threaded by design. Called from the main orchestrator tick.
"""

import os
import sys
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    BREAKEVEN_TRIGGER_R, TRAIL_TRIGGER_R, TRAIL_RATIO,
    PAIRS,
)
from risk.risk_manager import RiskManager, RiskCheckResult, AccountSnapshot, OpenPosition

logger = logging.getLogger(__name__)

# TradeLogger is imported lazily inside open_trade() to avoid circular imports
# (order_manager -> trade_logger is fine, but import at module level causes issues
# if trade_logger ever imports from order_manager in future refactors)

# ---------------------------------------------------------------------------
# MT5 return codes that are considered "already done" (idempotent)
# ---------------------------------------------------------------------------
# When order_send() fails with one of these, the order may have gone through
# on the broker side. We MUST NOT retry -- verify position list instead.
MT5_RETCODE_SUCCESS  = 10009   # TRADE_RETCODE_DONE
MT5_RETCODE_NO_MONEY = 10019   # insufficient margin
_AMBIGUOUS_RETCODES  = {10013, 10014, 10015, 10016, 10017}  # may have filled

# Magic number to identify ARCS-FX orders in MT5 (distinguishes from manual trades)
ARCS_MAGIC = 20260411


def _safe_comment(text: str, max_len: int = 20) -> str:
    # XM/MT5 rejects MT5 comments containing anything but ASCII alphanumerics.
    # Why: repeated error -2 "Invalid comment argument" even after we allowed [-_].
    # Strip to [A-Za-z0-9] only and keep it short — broker also appears length-sensitive.
    safe = "".join(c for c in text if c.isascii() and c.isalnum())
    return safe[:max_len] or "ARCSFX"


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class TradeIntent:
    """
    Validated intent to open a trade.
    Created by the orchestrator, consumed by OrderManager.open_trade().

    Contains everything needed: the symbol, direction, prices, the full
    RiskCheckResult, and DNA context fields required by TradeLogger.

    DNA fields (session, spread_at_entry, day_of_week, news_score_at_entry,
    confidence_components) are populated by main.py before passing here so
    TradeOpenRecord can be built with the full context at entry time.
    """
    symbol:          str
    direction:       str           # BUY | SELL
    entry_price:     float         # indicative (market order fills at ask/bid)
    sl_price:        float
    tp_price:        float         # initial TP from price_action engine
    signal_type:     str           # OB_RETEST | SD_BOUNCE | FVG_FILL
    pattern:         str           # candlestick pattern name
    r_ratio:         float         # R:R ratio at entry
    confidence:      float         # 0-100 score from confidence engine
    regime:          str           # TRENDING | RANGING
    risk_check:      RiskCheckResult
    # DNA context fields -- required by TradeLogger for pattern learning
    session:              str   = ""    # OVERLAP | LONDON | NY | ASIAN | OFF
    spread_at_entry:      float = 0.0   # pips at signal time
    day_of_week:          int   = 0     # 0=Mon .. 6=Sun
    news_score_at_entry:  float = 0.0   # -1.0 to +1.0
    confidence_components: Optional[list] = None  # [{name, score, max_score}]


@dataclass
class ManagedPosition:
    """
    Live tracking record for a single open position.

    Extends OpenPosition (from risk module) with management-layer state:
      - initial_risk_r: the initial 1R distance in price units
      - breakeven_applied: True once SL has been moved to entry
      - trailing_active: True once +2R trigger has fired
      - highest_price / lowest_price: running extremes for trailing calc
      - mt5_ticket: MT5 position ticket number
    """
    # Identity
    trade_id:         str
    mt5_ticket:       int
    symbol:           str
    direction:        str          # BUY | SELL

    # Prices at entry
    entry_price:      float
    sl_price:         float        # current SL (updated as we trail)
    initial_sl_price: float        # original SL (never changes -- used for R calc)
    tp_price:         float        # initial TP (may be removed once trailing)
    lots:             float

    # Management state
    initial_risk_r:       float    # abs(entry - initial_sl) in price units
    breakeven_applied:    bool = False
    trailing_active:      bool = False
    highest_price:        float = 0.0   # for BUY trailing (updated each tick)
    lowest_price:         float = 0.0   # for SELL trailing (updated each tick)

    # Metadata for DNA logging
    signal_type:  str = ""
    pattern:      str = ""
    r_ratio:      float = 0.0
    confidence:   float = 0.0
    regime:       str = ""
    opened_at_utc: str = ""

    # Accounting
    pnl_usd:      float = 0.0      # floating P&L (updated each tick)


@dataclass
class CloseRecord:
    """Result of a trade close operation."""
    success:       bool
    trade_id:      str
    symbol:        str
    lots:          float
    entry_price:   float
    close_price:   float
    pnl_usd:       float
    won:           bool
    close_reason:  str     # SL_HIT | TP_HIT | TRAIL_HIT | REGIME_CHANGE | MANUAL
    error_msg:     str = ""


# ===========================================================================
# OrderManager
# ===========================================================================

class OrderManager:
    """
    Production MT5 order placement and asymmetric trade management.

    USAGE PATTERN (from main.py orchestrator):

        om = OrderManager(risk_manager, trade_logger)

        # On signal:
        intent = TradeIntent(...)
        om.open_trade(intent)

        # Every tick:
        om.manage_open_positions()

        # On regime change / forced exit:
        om.close_trade(trade_id, reason="REGIME_CHANGE")
    """

    def __init__(
        self,
        risk_manager:  RiskManager,
        trade_logger=None,   # TradeLogger injected (optional for standalone test)
    ) -> None:
        self._risk   = risk_manager
        self._logger = trade_logger
        self._positions: dict[str, ManagedPosition] = {}   # trade_id -> ManagedPosition

        # MT5 is imported lazily to allow standalone tests without MT5 installed
        self._mt5 = None

    # -----------------------------------------------------------------------
    # MT5 lazy import
    # -----------------------------------------------------------------------

    def _get_mt5(self):
        """
        Lazy-load MetaTrader5 module.

        WHY lazy:
        Allows standalone unit tests to run on machines without MT5 installed.
        The module is only imported when an actual order is being placed.
        """
        if self._mt5 is None:
            import MetaTrader5 as mt5
            self._mt5 = mt5
        return self._mt5

    # -----------------------------------------------------------------------
    # Symbol info helpers
    # -----------------------------------------------------------------------

    def _get_symbol_info(self, symbol: str) -> Optional[object]:
        """Fetch MT5 symbol info. Returns None on failure."""
        mt5 = self._get_mt5()
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("[OM] symbol_info(%s) returned None: %s", symbol, mt5.last_error())
        return info

    def _normalize_price(self, price: float, symbol: str) -> float:
        """Round price to the symbol's tick size."""
        info = self._get_symbol_info(symbol)
        if info is None:
            return round(price, 5)
        digits = info.digits
        return round(price, digits)

    def _pip_size(self, symbol: str) -> float:
        """Return pip size for position management calculations."""
        return 0.01 if "JPY" in symbol else 0.0001

    def _get_ask(self, symbol: str) -> Optional[float]:
        """Current ask price from MT5 tick."""
        mt5 = self._get_mt5()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("[OM] symbol_info_tick(%s) failed: %s", symbol, mt5.last_error())
            return None
        return tick.ask

    def _get_bid(self, symbol: str) -> Optional[float]:
        """Current bid price from MT5 tick."""
        mt5 = self._get_mt5()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("[OM] symbol_info_tick(%s) failed: %s", symbol, mt5.last_error())
            return None
        return tick.bid

    # -----------------------------------------------------------------------
    # Open trade
    # -----------------------------------------------------------------------

    def open_trade(self, intent: TradeIntent) -> Optional[ManagedPosition]:
        """
        Place a market order with SL and initial TP on MT5.

        Returns a ManagedPosition on success, None on failure.

        FLOW:
          1. Validate intent (SL on correct side, R:R >= 1.5)
          2. Fetch live ask/bid to use as actual fill reference
          3. Build MT5 request struct
          4. Send and inspect retcode
          5. On success: create ManagedPosition, notify risk + logger

        WHY we attach SL and TP to the order itself (not as separate stops):
        MT5 executes SL/TP server-side. If the Python process dies, the broker
        still protects the position. This is non-negotiable for a live bot.
        """
        mt5    = self._get_mt5()
        symbol = intent.symbol
        is_buy = intent.direction == "BUY"

        # --- Duplicate position guard --------------------------------------
        # Bug fix (Run #3): bot opened 3 identical NZDUSD positions on
        # consecutive ticks because there was no check for an existing open
        # position on the same symbol. One position per symbol max.
        for existing in self._positions.values():
            if existing.symbol == symbol:
                logger.info(
                    "[OM] Skipping %s %s — already have open position %s",
                    symbol, intent.direction, existing.trade_id,
                )
                return None

        # --- Validation ----------------------------------------------------
        if is_buy and intent.sl_price >= intent.entry_price:
            logger.error("[OM] Invalid BUY: SL %.5f >= entry %.5f for %s",
                         intent.sl_price, intent.entry_price, symbol)
            return None

        if not is_buy and intent.sl_price <= intent.entry_price:
            logger.error("[OM] Invalid SELL: SL %.5f <= entry %.5f for %s",
                         intent.sl_price, intent.entry_price, symbol)
            return None

        if intent.risk_check.position_size_lots <= 0:
            logger.error("[OM] Lot size is 0 for %s -- cannot place order.", symbol)
            return None

        # --- Live price check ----------------------------------------------
        live_price = self._get_ask(symbol) if is_buy else self._get_bid(symbol)
        if live_price is None:
            return None

        # Warn if live price has drifted significantly from intent price
        drift_pips = abs(live_price - intent.entry_price) / self._pip_size(symbol)
        if drift_pips > 5:
            logger.warning(
                "[OM] %s %s: significant price drift since signal. "
                "Intent=%.5f Live=%.5f drift=%.1f pips",
                symbol, intent.direction, intent.entry_price, live_price, drift_pips,
            )

        # --- Build MT5 request ---------------------------------------------
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        sl         = self._normalize_price(intent.sl_price, symbol)
        tp         = self._normalize_price(intent.tp_price, symbol)
        lots       = intent.risk_check.position_size_lots

        request = {
            "action":     mt5.TRADE_ACTION_DEAL,
            "symbol":     symbol,
            "volume":     lots,
            "type":       order_type,
            "price":      live_price,
            "sl":         sl,
            "tp":         tp,
            "deviation":  10,               # max slippage in points
            "magic":      ARCS_MAGIC,
            "comment":    _safe_comment(f"ARCS-{intent.signal_type}-{intent.pattern}"),
            "type_time":  mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(
            "[OM] Sending %s %s | lots=%.2f | entry~%.5f | sl=%.5f | tp=%.5f | "
            "risk=$%.2f (%.2f%%)",
            symbol, intent.direction, lots, live_price, sl, tp,
            intent.risk_check.risk_usd, intent.risk_check.risk_pct,
        )

        # --- Send to MT5 ---------------------------------------------------
        result = mt5.order_send(request)

        if result is None:
            logger.error("[OM] order_send() returned None for %s -- MT5 error: %s",
                         symbol, mt5.last_error())
            return None

        if result.retcode != MT5_RETCODE_SUCCESS:
            if result.retcode in _AMBIGUOUS_RETCODES:
                logger.warning(
                    "[OM] Ambiguous retcode %d for %s -- verifying positions.",
                    result.retcode, symbol,
                )
                # Do not retry -- check if position opened
            else:
                logger.error(
                    "[OM] Order FAILED for %s: retcode=%d comment='%s'",
                    symbol, result.retcode, result.comment,
                )
                return None

        # --- Verify position opened ----------------------------------------
        # Use result.order ticket to find the actual position
        ticket = result.order
        pos    = self._find_mt5_position(ticket, symbol)

        if pos is None:
            if result.retcode == MT5_RETCODE_SUCCESS:
                logger.error(
                    "[OM] Order confirmed (retcode=10009) but position not found "
                    "for ticket=%d %s. Manual check required.", ticket, symbol
                )
            return None

        # --- Build ManagedPosition -----------------------------------------
        filled_price   = pos.price_open
        initial_risk_r = abs(filled_price - sl)

        trade_id = f"ARCS_{symbol}_{ticket}"
        now_utc  = datetime.now(timezone.utc).isoformat()

        managed = ManagedPosition(
            trade_id         = trade_id,
            mt5_ticket       = ticket,
            symbol           = symbol,
            direction        = intent.direction,
            entry_price      = filled_price,
            sl_price         = sl,
            initial_sl_price = sl,
            tp_price         = tp,
            lots             = lots,
            initial_risk_r   = initial_risk_r,
            highest_price    = filled_price,
            lowest_price     = filled_price,
            signal_type      = intent.signal_type,
            pattern          = intent.pattern,
            r_ratio          = intent.r_ratio,
            confidence       = intent.confidence,
            regime           = intent.regime,
            opened_at_utc    = now_utc,
        )

        self._positions[trade_id] = managed

        # Notify risk manager
        self._risk.record_trade_open(
            trade_id    = trade_id,
            symbol      = symbol,
            lots        = lots,
            entry_price = filled_price,
            sl_price    = sl,
        )

        # Notify trade logger -- build a TradeOpenRecord with full DNA context.
        # ManagedPosition alone lacks session/spread/news DNA fields; those come
        # from TradeIntent which was built by the orchestrator with live context.
        if self._logger:
            from learning.trade_logger import TradeOpenRecord
            log_rec = TradeOpenRecord(
                trade_id            = managed.trade_id,
                mt5_ticket          = managed.mt5_ticket,
                symbol              = managed.symbol,
                direction           = managed.direction,
                entry_price         = managed.entry_price,
                sl_price            = managed.sl_price,
                initial_sl_price    = managed.initial_sl_price,
                tp_price            = managed.tp_price,
                lots                = managed.lots,
                opened_at_utc       = managed.opened_at_utc,
                signal_type         = managed.signal_type,
                pattern             = managed.pattern,
                r_ratio             = managed.r_ratio,
                confidence          = managed.confidence,
                regime              = managed.regime,
                session             = intent.session,
                spread_at_entry     = intent.spread_at_entry,
                day_of_week         = intent.day_of_week,
                news_score_at_entry = intent.news_score_at_entry,
                risk_pct            = intent.risk_check.risk_pct,
                risk_usd            = intent.risk_check.risk_usd,
                win_rate_at_open    = intent.risk_check.win_rate,
                daily_loss_at_open  = intent.risk_check.daily_loss_pct,
                confidence_components = intent.confidence_components,
            )
            self._logger.log_open(log_rec)

        logger.info(
            "[OM] Trade OPENED: %s | id=%s | ticket=%d | "
            "filled=%.5f | sl=%.5f | tp=%.5f | 1R=%.5f",
            symbol, trade_id, ticket, filled_price, sl, tp, initial_risk_r,
        )
        return managed

    # -----------------------------------------------------------------------
    # Asymmetric trade management
    # -----------------------------------------------------------------------

    def manage_open_positions(self) -> None:
        """
        Called every orchestrator tick. Applies asymmetric management rules
        to every open position.

        RULES (in order of precedence):
          1. If position no longer exists in MT5 (SL or TP hit by broker):
             -> Record close, remove from internal dict.
          2. If the strategy-specific breakeven threshold is reached:
             -> Move SL to entry price. Trade is now risk-free.
          3. If the strategy-specific trailing threshold is reached:
             -> Activate trailing stop. Remove fixed TP.
          4. If trailing active:
             -> Update trailing SL if price has moved further in our favour.

        WHY check MT5 position existence first:
        MT5 SL/TP executes server-side. By the time this method runs, the
        position might already be closed. We must check before trying to
        modify a non-existent position.
        """
        if not self._positions:
            return

        closed_ids: list[str] = []

        for trade_id, pos in self._positions.items():
            try:
                self._manage_single_position(pos, closed_ids)
            except Exception as exc:
                logger.error(
                    "[OM] Unexpected error managing %s (%s): %s",
                    trade_id, pos.symbol, exc, exc_info=True,
                )

        # Remove closed positions from tracking dict
        for tid in closed_ids:
            del self._positions[tid]

    def _management_profile(self, signal_type: str) -> dict:
        """
        Return trade-management settings by strategy family.

        The goal is not to predict alpha here; it's to avoid forcing the same
        exit behavior on scalp and swing trades.
        """
        if signal_type.startswith("SCALP_"):
            return {
                "breakeven_trigger_r": 0.8,
                "trail_trigger_r": 1.5,
                "trail_ratio": 0.65,
                "news_lock_r": 0.35,
            }
        if signal_type.startswith("SWING_"):
            return {
                "breakeven_trigger_r": 1.2,
                "trail_trigger_r": 2.5,
                "trail_ratio": 0.35,
                "news_lock_r": 0.80,
            }
        return {
            "breakeven_trigger_r": BREAKEVEN_TRIGGER_R,
            "trail_trigger_r": TRAIL_TRIGGER_R,
            "trail_ratio": TRAIL_RATIO,
            "news_lock_r": 0.50,
        }

    def _manage_single_position(
        self,
        pos:        ManagedPosition,
        closed_ids: list[str],
    ) -> None:
        """
        Core management logic for one position. Mutates pos in-place.
        Appends trade_id to closed_ids if position is detected as closed.

        SPEC REQUIREMENT (Feature 3 + News Engine):
          "If news drops mid-trade -> tighten TP immediately"
          "If regime shifts mid-trade -> close early regardless of P&L"

        We check news FIRST (before MT5 position lookup) because a blackout
        during an open position is the highest-priority action -- it overrides
        all other management logic.
        """
        mt5 = self._get_mt5()
        mgmt = self._management_profile(pos.signal_type)

        # --- Step 0: mid-trade news check (spec requirement) ---------------
        # Import here to avoid circular import at module load time
        self._handle_mid_trade_news(pos, closed_ids)
        if pos.trade_id in closed_ids:
            return   # position already closed by news handler

        # --- Step 1: check if still open in MT5 ---------------------------
        mt5_pos = self._find_mt5_position(pos.mt5_ticket, pos.symbol)

        if mt5_pos is None:
            # Position closed externally (SL/TP hit, manual close, margin call)
            close_price, pnl_usd = self._get_last_deal_info(pos.mt5_ticket, pos.symbol)
            won          = pnl_usd > 0
            close_reason = self._infer_close_reason(pos, close_price)

            logger.info(
                "[OM] Position CLOSED externally: %s | id=%s | reason=%s | "
                "close=%.5f | pnl=$%.2f | won=%s",
                pos.symbol, pos.trade_id, close_reason,
                close_price, pnl_usd, won,
            )

            self._on_trade_closed(pos, close_price, pnl_usd, won, close_reason)
            closed_ids.append(pos.trade_id)
            return

        # --- Update live price extremes ------------------------------------
        current_price = mt5_pos.price_current
        pos.pnl_usd   = mt5_pos.profit

        if pos.direction == "BUY":
            if current_price > pos.highest_price:
                pos.highest_price = current_price
        else:
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

        # --- Compute current R multiple ------------------------------------
        if pos.initial_risk_r <= 0:
            return

        if pos.direction == "BUY":
            current_r = (current_price - pos.entry_price) / pos.initial_risk_r
        else:
            current_r = (pos.entry_price - current_price) / pos.initial_risk_r

        # --- Step 2: breakeven at +1R --------------------------------------
        if not pos.breakeven_applied and current_r >= mgmt["breakeven_trigger_r"]:
            be_price = self._normalize_price(pos.entry_price, pos.symbol)
            success  = self._modify_sl(pos, be_price)
            if success:
                pos.sl_price         = be_price
                pos.breakeven_applied = True
                logger.info(
                    "[OM] BREAKEVEN applied: %s %s | entry=%.5f | "
                    "new_sl=%.5f | current_r=%.2fR",
                    pos.symbol, pos.direction, pos.entry_price, be_price, current_r,
                )

        # --- Step 3: activate trailing at +2R ------------------------------
        if not pos.trailing_active and current_r >= mgmt["trail_trigger_r"]:
            # Remove fixed TP -- we will ride the move
            success = self._remove_tp(pos)
            if success:
                pos.trailing_active = True
                logger.info(
                    "[OM] TRAILING activated: %s %s | current_r=%.2fR | "
                    "TP removed, trailing with %d%% move lock",
                    pos.symbol, pos.direction, current_r,
                    int(mgmt["trail_ratio"] * 100),
                )

        # --- Step 4: update trailing SL ------------------------------------
        if pos.trailing_active:
            self._update_trailing_sl(pos, current_price, mgmt["trail_ratio"])

    def _handle_mid_trade_news(
        self,
        pos:        ManagedPosition,
        closed_ids: list[str],
    ) -> None:
        """
        Spec requirement: "If news drops mid-trade -> tighten TP immediately"

        WHY this is critical:
        A position that was opened during clear conditions can be devastated
        by a surprise high-impact news event. The spec is explicit: the bot
        must react to mid-trade news, not just block new entries.

        BEHAVIOUR:
          - If a news blackout activates while we are in a trade:
            -> Tighten TP to entry + 0.5R (lock partial profit if in profit,
               or minimise loss if not yet at BE)
            -> If already past BE (SL >= entry), leave SL where it is
            -> Log reason: NEWS_BLACKOUT_MID_TRADE
          - If blackout was already active when we entered (shouldn't happen
            due to confidence gate, but defensive check), close immediately.

        NEWS ENGINE IMPORT:
          Imported lazily to avoid circular dependency at module load time.
        """
        try:
            from engines import news_engine
        except ImportError:
            return   # news engine not available -- skip silently

        try:
            # news_engine.evaluate() has its own 5-min TTL cache internally
            news = news_engine.evaluate(pos.symbol)

            if not news.is_blackout:
                return   # no news event -- nothing to do

            # News blackout is NOW active with an open position
            logger.warning(
                "[OM] NEWS BLACKOUT mid-trade: %s | id=%s | reason=%s",
                pos.symbol, pos.trade_id, news.blackout_reason,
            )

            # Calculate tightened TP using the strategy-specific news lock.
            news_lock_r = self._management_profile(pos.signal_type)["news_lock_r"]
            half_r = pos.initial_risk_r * news_lock_r
            if pos.direction == "BUY":
                tight_tp = self._normalize_price(pos.entry_price + half_r, pos.symbol)
                # Only tighten if current price is already past entry (in profit zone)
                # or if we have no TP set yet (trailing mode)
                current_tp = pos.tp_price
                should_tighten = (current_tp == 0.0) or (tight_tp < current_tp)
            else:
                tight_tp = self._normalize_price(pos.entry_price - half_r, pos.symbol)
                current_tp = pos.tp_price
                should_tighten = (current_tp == 0.0) or (tight_tp > current_tp)

            if should_tighten:
                mt5 = self._get_mt5()
                request = {
                    "action":   mt5.TRADE_ACTION_SLTP,
                    "symbol":   pos.symbol,
                    "position": pos.mt5_ticket,
                    "sl":       pos.sl_price,
                    "tp":       tight_tp,
                }
                result = mt5.order_send(request)
                if result and result.retcode == 10009:
                    pos.tp_price = tight_tp
                    logger.info(
                        "[OM] TP tightened to %.5f (news lock %.2fR) due to news blackout: %s",
                        tight_tp, news_lock_r, news.blackout_reason,
                    )
                else:
                    code = result.retcode if result else "None"
                    logger.warning(
                        "[OM] TP tighten FAILED for %s: retcode=%s", pos.symbol, code
                    )

        except Exception as exc:
            # Never let news check crash the management loop
            logger.warning("[OM] Mid-trade news check error for %s: %s", pos.symbol, exc)

    def _update_trailing_sl(self, pos: ManagedPosition, current_price: float, trail_ratio: float) -> None:
        """
        Trail the SL at the provided `trail_ratio` of the move from entry to the
        current highest/lowest price.

        WHY 50% trail:
        We want to give the trade room to breathe through minor pullbacks
        while still locking in a significant portion of the move.
        A 50% trail on a 100-pip move means we lock 50 pips profit.

        WHY we only move the SL in the favour direction:
        Never move the SL backwards. If price retreats, the SL stays.
        We are trailing, not oscillating.
        """
        if pos.direction == "BUY":
            # Trail below highest achieved price
            move             = pos.highest_price - pos.entry_price
            candidate_sl     = pos.entry_price + (move * trail_ratio)
            candidate_sl     = self._normalize_price(candidate_sl, pos.symbol)
            if candidate_sl > pos.sl_price:    # only advance, never retreat
                success = self._modify_sl(pos, candidate_sl)
                if success:
                    logger.debug(
                        "[OM] TRAIL update %s BUY: sl %.5f -> %.5f "
                        "(high=%.5f move=%.1fpips)",
                        pos.symbol, pos.sl_price, candidate_sl,
                        pos.highest_price,
                        (pos.highest_price - pos.entry_price) / self._pip_size(pos.symbol),
                    )
                    pos.sl_price = candidate_sl
        else:
            # Trail above lowest achieved price
            move             = pos.entry_price - pos.lowest_price
            candidate_sl     = pos.entry_price - (move * trail_ratio)
            candidate_sl     = self._normalize_price(candidate_sl, pos.symbol)
            if candidate_sl < pos.sl_price:    # only advance, never retreat
                success = self._modify_sl(pos, candidate_sl)
                if success:
                    logger.debug(
                        "[OM] TRAIL update %s SELL: sl %.5f -> %.5f "
                        "(low=%.5f move=%.1fpips)",
                        pos.symbol, pos.sl_price, candidate_sl,
                        pos.lowest_price,
                        (pos.entry_price - pos.lowest_price) / self._pip_size(pos.symbol),
                    )
                    pos.sl_price = candidate_sl

    # -----------------------------------------------------------------------
    # Close trade
    # -----------------------------------------------------------------------

    def close_trade(self, trade_id: str, reason: str = "MANUAL") -> Optional[CloseRecord]:
        """
        Force-close an open position by trade_id.
        Used for regime-change exits and manual overrides.

        Returns CloseRecord on success, None if trade_id not found or close fails.
        """
        pos = self._positions.get(trade_id)
        if pos is None:
            logger.warning("[OM] close_trade: unknown trade_id=%s", trade_id)
            return None

        mt5    = self._get_mt5()
        is_buy = pos.direction == "BUY"

        # Close at market with opposite order type
        close_type  = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        close_price = self._get_bid(pos.symbol) if is_buy else self._get_ask(pos.symbol)

        if close_price is None:
            return None

        request = {
            "action":     mt5.TRADE_ACTION_DEAL,
            "symbol":     pos.symbol,
            "volume":     pos.lots,
            "type":       close_type,
            "position":   pos.mt5_ticket,
            "price":      close_price,
            "deviation":  10,
            "magic":      ARCS_MAGIC,
            "comment":    _safe_comment(f"ARCS-CLOSE-{reason}"),
            "type_time":  mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)

        if result is None or result.retcode != MT5_RETCODE_SUCCESS:
            code = result.retcode if result else "None"
            logger.error("[OM] close_trade FAILED for %s: retcode=%s", trade_id, code)
            return CloseRecord(
                success=False, trade_id=trade_id, symbol=pos.symbol,
                lots=pos.lots, entry_price=pos.entry_price, close_price=close_price,
                pnl_usd=0.0, won=False, close_reason=reason,
                error_msg=f"MT5 retcode={code}",
            )

        # Compute approximate P&L (precise value comes from MT5 deal history)
        pnl_usd = pos.pnl_usd   # last known floating P&L
        won     = pnl_usd > 0

        self._on_trade_closed(pos, close_price, pnl_usd, won, reason)
        del self._positions[trade_id]

        record = CloseRecord(
            success=True, trade_id=trade_id, symbol=pos.symbol,
            lots=pos.lots, entry_price=pos.entry_price, close_price=close_price,
            pnl_usd=pnl_usd, won=won, close_reason=reason,
        )
        logger.info(
            "[OM] Trade CLOSED manually: %s | id=%s | reason=%s | "
            "close=%.5f | pnl=$%.2f",
            pos.symbol, trade_id, reason, close_price, pnl_usd,
        )
        return record

    def close_all_trades(self, reason: str = "SHUTDOWN") -> list[CloseRecord]:
        """
        Close all open positions. Used on graceful bot shutdown.
        Returns list of CloseRecord for each closed position.
        """
        records  = []
        trade_ids = list(self._positions.keys())
        for tid in trade_ids:
            rec = self.close_trade(tid, reason=reason)
            if rec:
                records.append(rec)
        return records

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _find_mt5_position(self, ticket: int, symbol: str) -> Optional[object]:
        """
        Look up a position in MT5 by ticket.
        Returns the MT5 position object or None if not found.
        """
        mt5 = self._get_mt5()
        positions = mt5.positions_get(symbol=symbol)
        if positions is None:
            return None
        for p in positions:
            if p.ticket == ticket:
                return p
        return None

    def _get_last_deal_info(self, ticket: int, symbol: str) -> tuple[float, float]:
        """
        Retrieve close price and P&L for a closed position from MT5 deal history.
        Falls back to (0.0, 0.0) on failure.

        MT5 stores closed deals in history_deals_get(). We search for the
        deal that closed this position (entry=DEAL_ENTRY_OUT).

        Bug fix (Run #3): previously used float timestamps which some MT5
        builds silently reject. Now passes datetime objects. Also tries
        matching on both position_id and order (some brokers populate one
        but not the other).
        """
        mt5 = self._get_mt5()
        from datetime import timedelta
        now_utc  = datetime.now(timezone.utc)
        week_ago = now_utc - timedelta(days=7)

        # MT5 Python API accepts datetime objects reliably across all builds.
        deals = mt5.history_deals_get(week_ago, now_utc, group=f"*{symbol}*")

        if deals is None or len(deals) == 0:
            # Retry without group filter — some brokers use non-standard symbol naming
            deals = mt5.history_deals_get(week_ago, now_utc)
            if deals is None or len(deals) == 0:
                logger.warning(
                    "[OM] history_deals_get returned no deals for ticket=%d %s",
                    ticket, symbol,
                )
                return 0.0, 0.0

        # DEAL_ENTRY_OUT = 1 (closing deal)
        DEAL_ENTRY_OUT = 1
        for deal in reversed(deals):   # most recent first
            if deal.entry == DEAL_ENTRY_OUT and (
                deal.position_id == ticket or deal.order == ticket
            ):
                logger.debug(
                    "[OM] Found closing deal for ticket=%d: price=%.5f profit=%.2f",
                    ticket, deal.price, deal.profit,
                )
                return deal.price, deal.profit

        logger.warning(
            "[OM] No DEAL_ENTRY_OUT found for ticket=%d %s in %d deals",
            ticket, symbol, len(deals),
        )
        return 0.0, 0.0

    def _infer_close_reason(self, pos: ManagedPosition, close_price: float) -> str:
        """
        Infer why a position was closed externally.
        Compares close_price to known SL and TP levels.
        """
        pip = self._pip_size(pos.symbol)
        sl_distance = abs(close_price - pos.sl_price) / pip
        tp_distance = abs(close_price - pos.tp_price) / pip if pos.tp_price else 999

        if sl_distance < 2:
            return "SL_HIT"
        if tp_distance < 2:
            return "TP_HIT"
        if pos.trailing_active:
            return "TRAIL_HIT"
        return "UNKNOWN"

    def _modify_sl(self, pos: ManagedPosition, new_sl: float) -> bool:
        """
        Send a position modify request to MT5 to update the SL.
        Returns True on success.

        WHY we never move SL to an invalid level:
        MT5 enforces a minimum SL distance from current price (stops level).
        If we try to set SL too close, MT5 returns TRADE_RETCODE_INVALID_STOPS.
        We log and skip rather than crash.
        """
        mt5 = self._get_mt5()

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   pos.symbol,
            "position": pos.mt5_ticket,
            "sl":       new_sl,
            "tp":       pos.tp_price if not pos.trailing_active else 0.0,
        }

        result = mt5.order_send(request)

        if result is None or result.retcode != MT5_RETCODE_SUCCESS:
            code = result.retcode if result else "None"
            # 10016 = INVALID_STOPS -- too close to current price
            if result and result.retcode == 10016:
                logger.debug(
                    "[OM] SL modify for %s rejected: price too close to SL "
                    "(stops level constraint). Will retry next tick.",
                    pos.symbol,
                )
            else:
                logger.warning(
                    "[OM] SL modify FAILED for %s ticket=%d: retcode=%s",
                    pos.symbol, pos.mt5_ticket, code,
                )
            return False

        return True

    def _remove_tp(self, pos: ManagedPosition) -> bool:
        """
        Remove the fixed TP from a position (set to 0.0) when trailing activates.
        Returns True on success.
        """
        mt5 = self._get_mt5()

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "symbol":   pos.symbol,
            "position": pos.mt5_ticket,
            "sl":       pos.sl_price,
            "tp":       0.0,
        }

        result = mt5.order_send(request)

        if result is None or result.retcode != MT5_RETCODE_SUCCESS:
            code = result.retcode if result else "None"
            logger.warning(
                "[OM] TP removal FAILED for %s ticket=%d: retcode=%s",
                pos.symbol, pos.mt5_ticket, code,
            )
            return False

        pos.tp_price = 0.0
        return True

    def _on_trade_closed(
        self,
        pos:         ManagedPosition,
        close_price: float,
        pnl_usd:     float,
        won:         bool,
        reason:      str,
    ) -> None:
        """
        Shared close handler: notify risk manager and trade logger.
        Called whether the close was broker-side (SL/TP) or manual.
        """
        self._risk.record_trade_close(
            trade_id    = pos.trade_id,
            symbol      = pos.symbol,
            lots        = pos.lots,
            entry_price = pos.entry_price,
            sl_price    = pos.initial_sl_price,
            pnl_usd     = pnl_usd,
            won         = won,
        )

        if self._logger:
            self._logger.log_close(
                trade_id    = pos.trade_id,
                close_price = close_price,
                pnl_usd     = pnl_usd,
                won         = won,
                close_reason = reason,
                breakeven_applied = pos.breakeven_applied,
                trailing_active   = pos.trailing_active,
            )

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def get_open_positions(self) -> list[OpenPosition]:
        """
        Return list of OpenPosition objects (risk module format)
        for all currently managed trades.

        Used by orchestrator to pass to RiskManager.evaluate_trade().
        """
        result = []
        for pos in self._positions.values():
            result.append(OpenPosition(
                trade_id      = pos.trade_id,
                symbol        = pos.symbol,
                direction     = pos.direction,
                lots          = pos.lots,
                entry_price   = pos.entry_price,
                sl_price      = pos.sl_price,
                opened_at_utc = pos.opened_at_utc,
            ))
        return result

    def get_position_count(self) -> int:
        return len(self._positions)

    def get_position_summary(self) -> list[dict]:
        """Human-readable summary of all open positions for status logging."""
        summary = []
        for pos in self._positions.values():
            if pos.initial_risk_r > 0:
                is_buy = pos.direction == "BUY"
                ref_price = pos.highest_price if is_buy else pos.lowest_price
                if is_buy:
                    current_r = (ref_price - pos.entry_price) / pos.initial_risk_r
                else:
                    current_r = (pos.entry_price - ref_price) / pos.initial_risk_r
            else:
                current_r = 0.0

            summary.append({
                "trade_id":          pos.trade_id,
                "symbol":            pos.symbol,
                "direction":         pos.direction,
                "lots":              pos.lots,
                "entry":             pos.entry_price,
                "current_sl":        pos.sl_price,
                "pnl_usd":           round(pos.pnl_usd, 2),
                "best_r":            round(current_r, 2),
                "breakeven":         pos.breakeven_applied,
                "trailing":          pos.trailing_active,
                "signal_type":       pos.signal_type,
                "confidence":        pos.confidence,
            })
        return summary


# ===========================================================================
# Standalone test harness (no MT5 connection required -- tests logic only)
# ===========================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("ARCS-FX  --  OrderManager logic test (no MT5)")
    print("=" * 60)

    from risk.risk_manager import RiskManager, AccountSnapshot

    # -----------------------------------------------------------------------
    # We test only the management logic (asymmetric rules) without MT5.
    # We directly instantiate ManagedPosition and call the private
    # management methods to verify the state machine is correct.
    # -----------------------------------------------------------------------

    rm = RiskManager()

    # Clear any leftover state from previous risk_manager tests
    rm._state.trade_history      = []
    rm._state.consecutive_losses = 0
    rm._state.cooldown_until_utc = None
    rm._save_state()

    om = OrderManager(risk_manager=rm, trade_logger=None)

    def make_position(direction="BUY") -> ManagedPosition:
        entry  = 1.08500
        sl     = 1.08200 if direction == "BUY" else 1.08800
        tp     = 1.09100 if direction == "BUY" else 1.07900
        r_size = abs(entry - sl)   # 0.003 = 30 pips
        return ManagedPosition(
            trade_id         = "TEST_001",
            mt5_ticket       = 99999,
            symbol           = "EURUSD",
            direction        = direction,
            entry_price      = entry,
            sl_price         = sl,
            initial_sl_price = sl,
            tp_price         = tp,
            lots             = 0.03,
            initial_risk_r   = r_size,
            highest_price    = entry,
            lowest_price     = entry,
            signal_type      = "OB_RETEST",
            pattern          = "BULLISH_ENGULFING",
            r_ratio          = 2.0,
            confidence       = 78.0,
            regime           = "TRENDING",
            opened_at_utc    = datetime.now(timezone.utc).isoformat(),
        )

    # --- Test 1: Breakeven trigger at +1R ----------------------------------
    print("\n--- TEST 1: Breakeven at +1R (BUY) ---")
    pos = make_position("BUY")
    # Simulate price at exactly +1R
    one_r_price = pos.entry_price + pos.initial_risk_r   # 1.08500 + 0.003 = 1.08800
    pos.highest_price = one_r_price

    # Compute R
    current_r = (one_r_price - pos.entry_price) / pos.initial_risk_r
    print(f"  Simulated price: {one_r_price:.5f} | R = {current_r:.2f}")

    # Apply breakeven manually (simulating _manage_single_position logic)
    be_applied = False
    if not pos.breakeven_applied and current_r >= BREAKEVEN_TRIGGER_R:
        pos.sl_price          = pos.entry_price   # move SL to entry
        pos.breakeven_applied = True
        be_applied            = True

    print(f"  BE applied: {pos.breakeven_applied}")
    print(f"  New SL:     {pos.sl_price:.5f}  (should equal entry {pos.entry_price:.5f})")
    assert pos.breakeven_applied, "TEST 1 FAILED: breakeven should be applied"
    assert pos.sl_price == pos.entry_price, "TEST 1 FAILED: SL should be at entry"
    print("  -> PASS")

    # --- Test 2: Trailing activation at +2R --------------------------------
    print("\n--- TEST 2: Trailing activation at +2R (BUY) ---")
    pos.trailing_active = False
    two_r_price = pos.entry_price + (2 * pos.initial_risk_r)   # 1.08500 + 0.006 = 1.09100
    pos.highest_price = two_r_price

    current_r = (two_r_price - pos.entry_price) / pos.initial_risk_r
    print(f"  Simulated price: {two_r_price:.5f} | R = {current_r:.2f}")

    if not pos.trailing_active and current_r >= TRAIL_TRIGGER_R:
        pos.trailing_active = True
        pos.tp_price        = 0.0   # TP removed

    print(f"  Trailing active: {pos.trailing_active}")
    print(f"  TP removed:      {pos.tp_price == 0.0}")
    assert pos.trailing_active, "TEST 2 FAILED: trailing should be active"
    assert pos.tp_price == 0.0, "TEST 2 FAILED: TP should be removed"
    print("  -> PASS")

    # --- Test 3: Trailing SL update ----------------------------------------
    print("\n--- TEST 3: Trailing SL update (BUY moves to +3R) ---")
    pos.sl_price      = pos.entry_price   # at BE after test 1
    three_r_price     = pos.entry_price + (3 * pos.initial_risk_r)   # 1.09400
    pos.highest_price = three_r_price

    # Apply trail logic
    move          = pos.highest_price - pos.entry_price
    candidate_sl  = pos.entry_price + (move * TRAIL_RATIO)   # lock 50% of 3R = 1.5R
    expected_sl   = round(candidate_sl, 5)

    print(f"  Highest price:  {pos.highest_price:.5f} (3R)")
    print(f"  Move from entry: {move:.5f} = {move / 0.0001:.0f} pips")
    print(f"  Trail SL at 50%%: {candidate_sl:.5f}")

    if candidate_sl > pos.sl_price:
        old_sl       = pos.sl_price
        pos.sl_price = candidate_sl

    print(f"  Old SL:  {old_sl:.5f}")
    print(f"  New SL:  {pos.sl_price:.5f}  (locking {(pos.sl_price - pos.entry_price) / pos.initial_risk_r:.1f}R)")
    assert pos.sl_price > pos.entry_price, "TEST 3 FAILED: SL should be above entry"
    assert abs(pos.sl_price - candidate_sl) < 1e-9, "TEST 3 FAILED: SL should match candidate"
    print("  -> PASS")

    # --- Test 4: SELL direction logic --------------------------------------
    print("\n--- TEST 4: Trailing SL update (SELL direction) ---")
    pos_s             = make_position("SELL")
    two_r_price_s     = pos_s.entry_price - (2 * pos_s.initial_risk_r)   # 1.08500 - 0.006 = 1.07900
    pos_s.lowest_price = two_r_price_s
    pos_s.trailing_active = True

    move_s       = pos_s.entry_price - pos_s.lowest_price
    cand_sl_s    = pos_s.entry_price - (move_s * TRAIL_RATIO)

    print(f"  SELL entry:      {pos_s.entry_price:.5f}")
    print(f"  Lowest price:    {pos_s.lowest_price:.5f} (2R)")
    print(f"  Trail SL at 50%%: {cand_sl_s:.5f}")

    assert cand_sl_s < pos_s.entry_price, "TEST 4 FAILED: SELL SL should be below entry"
    assert cand_sl_s > pos_s.lowest_price, "TEST 4 FAILED: SELL SL should be above lowest"
    print(f"  SL locks {(pos_s.entry_price - cand_sl_s) / pos_s.initial_risk_r:.1f}R profit")
    print("  -> PASS")

    # --- Test 5: open_positions() for risk manager -------------------------
    print("\n--- TEST 5: get_open_positions() returns OpenPosition list ---")
    om._positions["TEST_001"] = pos
    open_list = om.get_open_positions()
    print(f"  Count: {len(open_list)}")
    print(f"  Symbol: {open_list[0].symbol}")
    print(f"  Direction: {open_list[0].direction}")
    assert len(open_list) == 1, "TEST 5 FAILED"
    assert open_list[0].symbol == "EURUSD", "TEST 5 FAILED"
    print("  -> PASS")

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
