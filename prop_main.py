"""
ARCS-PROP — prop_main.py
Entry point for the prop-firm challenge bot.

Runs entirely separate from ARCS-FX:
  - Its own MT5 login (connect via the same helpers; user must point the MT5
    terminal at the prop-firm account before starting this process).
  - Its own magic number (PROP_MAGIC = 20260418) so challenge trades never
    mix with main-bot trades.
  - Its own logs, DB path, state file, and dashboard port (5001).

Per-tick gate chain (strict — prop filters layer ON TOP of main-bot gates):
  1. Health check + MT5 connection
  2. Challenge state: halted? paused? cooldown expired?
  3. Equity watchdog: max DD / daily loss / daily-win lock / peak update
  4. Phase target hit? → stop (mark_passed) or advance to Phase 2
  5. Refresh OHLCV + manage open positions
  6. Per-pair signal chain (with prop filters bolted in)
  7. Write prop_status.json for dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Main-bot infra we reuse (MT5 connection, data, regime, engines, execution)
from config import TF_H1, TF_M15, TF_M5, REGIME_CHAOTIC
from core.mt5_connection import connect, disconnect, is_connected, reconnect
from core import data_fetcher
from core.instruments import distance_in_units
from core.regime_detector import detect_multi_tf
from engines import price_action
from engines.news_engine import evaluate as news_evaluate
from engines.confidence_score import compute as confidence_compute
from execution.order_manager import OrderManager, TradeIntent
from learning.trade_logger import TradeLogger
from risk.risk_manager import RiskManager, AccountSnapshot
import risk.risk_manager as _rm

# Prop-specific modules
from prop import challenge_state as _cs_mod
from prop.challenge_state import ChallengeStateStore
from prop.dynamic_risk    import compute_risk
from prop.equity_watchdog import EquityWatchdog
from prop.prop_config import (
    ALLOWED_REGIMES, ALLOWED_SIGNAL_TYPES, ALLOWED_SIGNAL_TYPES_WITH_GATE,
    CHALLENGE_ACCOUNT_USD, CHALLENGE_STOP_FLAG,
    CONSECUTIVE_LOSS_COOLDOWN_H, DATA_REFRESH_INTERVAL_S, DASHBOARD_PORT,
    HIGH_IMPACT_KEYWORDS, INTERNAL_MAX_CONCURRENT_POSITIONS,
    INTERNAL_MAX_DAILY_TRADES, INTERNAL_MAX_DAILY_TRADES_P2,
    MAIN_LOOP_INTERVAL_S, MAX_CONSECUTIVE_LOSSES, MIN_RR_RATIO,
    NEWS_BLACKOUT_AFTER_MIN, NEWS_BLACKOUT_BEFORE_MIN,
    PHASE_1, PHASE_2, PHASE_1_TARGET_PCT, PHASE_2_TARGET_PCT,
    PHASE_HALTED, PHASE_PASSED,
    POST_LOSS_COOLDOWN_MIN, PROP_CONFIDENCE_MIN, PROP_EVENTS_PATH,
    PROP_LOG_PATH, PROP_MAGIC, PROP_STATUS_PATH, PROP_SYMBOLS,
    SESSION_WINDOWS, SWING_BREAKOUT_ADX_MAX, SWING_BREAKOUT_ADX_MIN,
)

from datetime import timedelta

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("ARCS-PROP")


def _archive_previous_run_log(path: Path) -> None:
    """Move the previous run's live log to logs/archive/ before starting fresh."""
    if not path.exists() or path.stat().st_size == 0:
        return
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    ts = mtime.strftime("%Y%m%d_%H%M%S")
    dst = archive_dir / f"{path.stem}_{ts}{path.suffix}"
    counter = 1
    while dst.exists():
        dst = archive_dir / f"{path.stem}_{ts}_{counter}{path.suffix}"
        counter += 1
    path.replace(dst)


def _archive_previous_run_logs() -> None:
    """Each prop-bot session starts on clean live log files."""
    Path("logs").mkdir(exist_ok=True)
    _archive_previous_run_log(Path(PROP_LOG_PATH))
    _archive_previous_run_log(Path(PROP_EVENTS_PATH))


def _setup_logging() -> None:
    _archive_previous_run_logs()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    fh = RotatingFileHandler(
        PROP_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    eh = RotatingFileHandler(
        PROP_EVENTS_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    eh.setFormatter(fmt)
    eh.addFilter(_PropEventsFilter())
    root.addHandler(eh)

    for noisy in ("werkzeug", "httpx", "huggingface_hub", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class _PropEventsFilter(logging.Filter):
    KEYWORDS = (
        "PROP starting", "PROP online", "Dashboard live",
        "PA signal", "BLACKOUT", "All gates PASSED", "Risk BLOCKED",
        "HALT", "PAUSE", "PASSED", "PHASE_2",
        "open_trade() failed", "Tick complete", "Shutdown",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        return any(k in msg for k in self.KEYWORDS)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(sig, frame):
    global _running
    logger.info("Shutdown signal received (%s). Stopping ...", sig)
    _running = False


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_ohlcv_cache: dict = {}
_cache_last_refresh: float = 0.0
_regime_cache: dict = {}
_last_signal: dict = {}


def _refresh_ohlcv_if_stale() -> None:
    global _cache_last_refresh
    now = time.monotonic()
    if now - _cache_last_refresh < DATA_REFRESH_INTERVAL_S:
        return

    logger.info("Refreshing OHLCV cache for %d pairs ...", len(PROP_SYMBOLS))
    for symbol in PROP_SYMBOLS:
        for tf, getter in [
            (TF_H1,  data_fetcher.get_h1),
            (TF_M15, data_fetcher.get_m15),
            (TF_M5,  data_fetcher.get_m5),
        ]:
            df = getter(symbol)
            if df is not None:
                _ohlcv_cache[(symbol, tf)] = df
            else:
                _ohlcv_cache.pop((symbol, tf), None)
                logger.warning("OHLCV unavailable: %s %s", symbol, tf)

    _cache_last_refresh = now
    _last_signal.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_account_snapshot() -> Optional[AccountSnapshot]:
    try:
        import MetaTrader5 as mt5
        info = mt5.account_info()
        if info is None:
            logger.error("mt5.account_info() returned None: %s", mt5.last_error())
            return None
        return AccountSnapshot(
            balance=info.balance, equity=info.equity, margin_free=info.margin_free,
        )
    except Exception as exc:
        logger.exception("Failed to get account snapshot: %s", exc)
        return None


def _in_session(now_utc: datetime) -> bool:
    h = now_utc.hour
    return any(start <= h < end for start, end in SESSION_WINDOWS)


def _news_blackout(symbol: str, now_utc: datetime) -> tuple[bool, str]:
    """
    Use the shared news engine but tighten the filter: any high-impact
    keyword inside the prop blackout window vetoes.
    """
    try:
        result = news_evaluate(symbol, now_utc)
    except Exception as exc:
        logger.warning("[%s] news engine error (fail-closed): %s", symbol, exc)
        return True, "news-engine-error"

    if result.is_blackout:
        reason = (result.blackout_reason or "").upper()
        if any(kw.upper() in reason for kw in HIGH_IMPACT_KEYWORDS):
            return True, result.blackout_reason
        return True, result.blackout_reason  # be strict: any blackout vetoes
    return False, ""


def _phase_target_pct(phase: str) -> float:
    return PHASE_1_TARGET_PCT if phase == PHASE_1 else PHASE_2_TARGET_PCT


def _override_risk_pct(risk_pct: float) -> None:
    """
    Force RiskManager to size every trade at exactly `risk_pct` by overriding
    the module-level constants it imported at startup. Call before every
    evaluate_trade() and restore after.
    """
    _rm.RISK_BASE_PCT = risk_pct
    _rm.RISK_HIGH_PCT = risk_pct
    _rm.RISK_LOW_PCT  = risk_pct
    _rm.RISK_MAX_PCT  = max(_rm.RISK_MAX_PCT, risk_pct)


# ---------------------------------------------------------------------------
# Dashboard status writer
# ---------------------------------------------------------------------------

def _write_prop_status(
    store: ChallengeStateStore,
    order_manager: OrderManager,
    current_equity: float,
    current_risk_pct: Optional[float],
    risk_tier: str,
    now_utc: datetime,
) -> None:
    try:
        import MetaTrader5 as mt5
        state = store.get()

        account_data: dict = {}
        info = mt5.account_info()
        if info:
            account_data = {
                "login":       int(info.login),
                "balance":     round(float(info.balance),     2),
                "equity":      round(float(info.equity),      2),
                "margin_free": round(float(info.margin_free), 2),
            }

        positions = []
        mt5_pos_map = {p.ticket: p for p in (mt5.positions_get() or [])}
        for pos in order_manager.get_open_positions():
            mp      = mt5_pos_map.get(pos.mt5_ticket)
            pnl_usd = round(float(mp.profit), 2) if mp else 0.0
            positions.append({
                "trade_id":    pos.trade_id,
                "symbol":      pos.symbol,
                "direction":   pos.direction,
                "entry_price": pos.entry_price,
                "sl_price":    pos.sl_price,
                "tp_price":    pos.tp_price,
                "lots":        pos.lots,
                "pnl_usd":     pnl_usd,
                "signal_type": pos.signal_type,
                "confidence":  pos.confidence,
            })

        target_pct = _phase_target_pct(state.phase)
        phase_gain_pct = (
            (current_equity - state.phase_start_balance) / state.phase_start_balance * 100.0
            if state.phase_start_balance > 0 else 0.0
        )
        day_pnl_pct = (
            (current_equity - state.day_start_balance) / state.day_start_balance * 100.0
            if state.day_start_balance > 0 else 0.0
        )
        from_peak_pct = (
            (current_equity - state.peak_equity) / state.peak_equity * 100.0
            if state.peak_equity > 0 else 0.0
        )

        status = {
            "timestamp":        now_utc.isoformat(),
            "bot_running":      True,
            "account":          account_data,
            "phase":            state.phase,
            "phase_target_pct": target_pct,
            "phase_gain_pct":   round(phase_gain_pct, 2),
            "day_pnl_pct":      round(day_pnl_pct,    2),
            "from_peak_pct":    round(from_peak_pct,  2),
            "peak_equity":      round(state.peak_equity,       2),
            "start_balance":    round(state.start_balance,     2),
            "day_start_balance": round(state.day_start_balance, 2),
            "phase_start_balance": round(state.phase_start_balance, 2),
            "daily_trades_count": state.daily_trades_count,
            "consecutive_losses": state.consecutive_losses,
            "total_trades":     state.total_trades,
            "risk_pct":         current_risk_pct,
            "risk_tier":        risk_tier,
            "cooldown_until":   state.cooldown_until_utc,
            "pause_reason":     state.pause_reason,
            "halt_reason":      state.halt_reason,
            "halted_at":        state.halted_at_utc,
            "regimes":          _regime_cache,
            "open_positions":   positions,
        }

        os.makedirs(os.path.dirname(PROP_STATUS_PATH) or ".", exist_ok=True)
        tmp_path = PROP_STATUS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp_path, PROP_STATUS_PATH)
    except Exception as exc:
        logger.debug("_write_prop_status failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------

def _check_phase_progression(
    store: ChallengeStateStore,
    order_manager: OrderManager,
    current_equity: float,
) -> bool:
    """
    If current equity crosses the phase target, flatten everything and
    either advance to Phase 2 or mark the challenge PASSED.
    Returns True when a transition occurred (caller should skip new entries).
    """
    state = store.get()
    if state.phase not in (PHASE_1, PHASE_2) or state.phase_start_balance <= 0:
        return False

    gain_pct = (current_equity - state.phase_start_balance) / state.phase_start_balance * 100.0
    target   = _phase_target_pct(state.phase)

    if gain_pct < target:
        return False

    reason = f"PHASE_TARGET_HIT ({state.phase} +{gain_pct:.2f}% ≥ +{target:.1f}%)"
    logger.warning(reason)
    try:
        order_manager.close_all_trades(reason=reason)
    except Exception as exc:
        logger.exception("close_all_trades on phase target failed: %s", exc)

    if state.phase == PHASE_1:
        store.advance_to_phase_2(current_equity)
        logger.critical("PHASE_1 PASSED. Advanced to PHASE_2 at equity %.2f", current_equity)
    else:
        store.mark_passed()
        logger.critical("CHALLENGE PASSED. Equity %.2f", current_equity)
    return True


# ---------------------------------------------------------------------------
# Per-pair signal chain (prop-filtered)
# ---------------------------------------------------------------------------

def _process_pair(
    symbol: str,
    now_utc: datetime,
    store: ChallengeStateStore,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    current_equity: float,
) -> None:
    state = store.get()

    # ---- Prop gate: symbol whitelist (already enforced by PROP_SYMBOLS loop) -
    # ---- Prop gate: session window ------------------------------------------
    if not _in_session(now_utc):
        return

    # ---- Prop gate: daily trade cap -----------------------------------------
    daily_cap = INTERNAL_MAX_DAILY_TRADES_P2 if state.phase == PHASE_2 else INTERNAL_MAX_DAILY_TRADES
    if state.daily_trades_count >= daily_cap:
        logger.debug("[%s] daily trade cap hit (%d).", symbol, daily_cap)
        return

    # ---- Prop gate: concurrency cap -----------------------------------------
    if len(order_manager.get_open_positions()) >= INTERNAL_MAX_CONCURRENT_POSITIONS:
        return

    # ---- OHLCV --------------------------------------------------------------
    h1_df  = _ohlcv_cache.get((symbol, TF_H1))
    m15_df = _ohlcv_cache.get((symbol, TF_M15))
    m5_df  = _ohlcv_cache.get((symbol, TF_M5))
    if h1_df is None or m15_df is None or m5_df is None:
        return

    # ---- Regime -------------------------------------------------------------
    try:
        regime_map = detect_multi_tf(symbol, h1_df, m15_df)
    except Exception as exc:
        logger.exception("[%s] Regime error: %s", symbol, exc)
        return
    if not regime_map or "H1" not in regime_map:
        return

    regime_h1  = regime_map["H1"]
    regime_m15 = regime_map.get("M15", regime_h1)

    _regime_cache[symbol] = {
        "regime":     regime_h1.regime,
        "confidence": round(regime_h1.confidence * 100),
        "direction":  regime_h1.direction,
        "adx":        round(float(regime_h1.adx), 1),
        "atr_pct":    round(float(regime_h1.atr_percentile)),
        "updated":    now_utc.isoformat(),
    }

    if regime_h1.regime == REGIME_CHAOTIC:
        return
    if regime_h1.regime not in ALLOWED_REGIMES:
        logger.debug("[%s] regime %s not in ALLOWED_REGIMES", symbol, regime_h1.regime)
        return

    # ---- News blackout ------------------------------------------------------
    blackout, reason = _news_blackout(symbol, now_utc)
    if blackout:
        logger.info("[%s] News BLACKOUT: %s — skipping.", symbol, reason)
        return

    # ---- Live tick / spread -------------------------------------------------
    tick = data_fetcher.get_tick(symbol)
    spread_pips = tick["spread_pips"] if tick and tick.get("spread_pips") is not None else 0.0

    # ---- Price action -------------------------------------------------------
    try:
        pa_signal = price_action.evaluate(
            symbol=symbol, h1_df=h1_df, m15_df=m15_df, m5_df=m5_df,
            regime=regime_h1.regime, direction_bias=regime_h1.direction,
            atr=regime_h1.atr,
        )
    except Exception as exc:
        logger.exception("[%s] PA error: %s", symbol, exc)
        return
    if not pa_signal.has_signal:
        _last_signal.pop(symbol, None)
        return

    # ---- Prop gate: signal type whitelist -----------------------------------
    st = pa_signal.signal_type
    if st in ALLOWED_SIGNAL_TYPES:
        pass  # SWING_REVERSION always allowed
    elif st in ALLOWED_SIGNAL_TYPES_WITH_GATE:
        # SWING_BREAKOUT: only in TRENDING_CLEAN with ADX in band
        adx = float(regime_h1.adx or 0.0)
        if regime_h1.regime != "TRENDING_CLEAN" or not (SWING_BREAKOUT_ADX_MIN <= adx <= SWING_BREAKOUT_ADX_MAX):
            logger.info("[%s] SWING_BREAKOUT gated (regime=%s adx=%.1f)", symbol, regime_h1.regime, adx)
            return
    else:
        logger.debug("[%s] signal type %s not whitelisted for prop", symbol, st)
        return

    # ---- Prop gate: R:R minimum --------------------------------------------
    if pa_signal.r_ratio < MIN_RR_RATIO:
        logger.info("[%s] R:R %.2f < %.2f — skipping.", symbol, pa_signal.r_ratio, MIN_RR_RATIO)
        return

    # ---- Dedup within same H1 bar ------------------------------------------
    try:
        h1_bar_ts = str(h1_df.index[-1])
    except Exception:
        h1_bar_ts = ""
    sig_key = (pa_signal.direction, pa_signal.signal_type, h1_bar_ts)
    if _last_signal.get(symbol) == sig_key:
        return
    _last_signal[symbol] = sig_key

    logger.info("[%s] PA signal: %s %s | entry=%.5f sl=%.5f tp=%.5f R=%.2f",
                symbol, pa_signal.direction, pa_signal.signal_type,
                pa_signal.entry_price, pa_signal.sl_price,
                pa_signal.tp_price, pa_signal.r_ratio)

    # ---- Confidence ---------------------------------------------------------
    try:
        conf_result = confidence_compute(
            symbol=symbol, regime_result=regime_h1, regime_m15=regime_m15,
            pa_signal=pa_signal, news_result=news_evaluate(symbol, now_utc),
            spread_pips=spread_pips, early_mode=False, now_utc=now_utc,
        )
    except Exception as exc:
        logger.exception("[%s] Confidence error: %s", symbol, exc)
        return

    if conf_result.score < PROP_CONFIDENCE_MIN:
        logger.info("[%s] Confidence %.1f < %.1f — skipping.",
                    symbol, conf_result.score, PROP_CONFIDENCE_MIN)
        return
    if not conf_result.trade_allowed:
        logger.info("[%s] Confidence gate: %s", symbol, conf_result.skip_reason)
        return

    # ---- Dynamic risk sizing ------------------------------------------------
    decision = compute_risk(
        current_equity      = current_equity,
        peak_equity         = state.peak_equity,
        phase_start_balance = state.phase_start_balance,
        phase               = state.phase,
    )
    if decision.pause or decision.risk_pct is None:
        logger.warning("[%s] Dynamic-risk PAUSE (%s) — %s",
                       symbol, decision.tier, decision.reason)
        store.pause_until(
            now_utc + timedelta(hours=24),
            f"RECOVERY_24H ({decision.tier})",
        )
        return

    # ---- Account snapshot + risk eval --------------------------------------
    account = _get_account_snapshot()
    if account is None:
        return

    _override_risk_pct(decision.risk_pct)
    try:
        risk_result = risk_manager.evaluate_trade(
            symbol=symbol, direction=pa_signal.direction,
            entry_price=pa_signal.entry_price, sl_price=pa_signal.sl_price,
            account=account, open_positions=order_manager.get_open_positions(),
        )
    except Exception as exc:
        logger.exception("[%s] Risk error: %s", symbol, exc)
        return

    if not risk_result.approved:
        logger.info("[%s] Risk BLOCKED: %s", symbol, risk_result.skip_reason)
        return

    # ---- Build TradeIntent and fire ----------------------------------------
    intent = TradeIntent(
        symbol              = symbol,
        direction           = pa_signal.direction,
        entry_price         = pa_signal.entry_price,
        sl_price            = pa_signal.sl_price,
        tp_price            = pa_signal.tp_price,
        signal_type         = pa_signal.signal_type,
        pattern             = pa_signal.pattern,
        r_ratio             = pa_signal.r_ratio,
        confidence          = conf_result.score,
        regime              = regime_h1.regime,
        risk_check          = risk_result,
        session             = "PROP",
        spread_at_entry     = spread_pips,
        day_of_week         = now_utc.weekday(),
        news_score_at_entry = 0.0,
        confidence_components = [
            {"name": c.name, "score": round(c.weighted_score, 2), "max_score": c.weight}
            for c in conf_result.components
        ],
    )

    logger.info(
        "[%s] All gates PASSED. Opening %s. Lots=%.2f Risk=%.2f%% Tier=%s Conf=%.1f",
        symbol, pa_signal.direction, risk_result.position_size_lots,
        decision.risk_pct, decision.tier, conf_result.score,
    )

    try:
        order_manager.open_trade(intent)
        store.record_trade_opened()
    except Exception as exc:
        logger.exception("[%s] open_trade() failed: %s", symbol, exc)


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def _tick(
    store: ChallengeStateStore,
    watchdog: EquityWatchdog,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    trade_logger: TradeLogger,
) -> None:
    global _running

    # Kill-switch file
    if Path(CHALLENGE_STOP_FLAG).exists():
        logger.critical("CHALLENGE_STOP flag present — halting.")
        try:
            order_manager.close_all_trades(reason="CHALLENGE_STOP_FLAG")
        except Exception:
            pass
        store.halt("CHALLENGE_STOP flag file")
        _running = False
        return

    # Health
    if not is_connected():
        logger.warning("MT5 connection lost — reconnecting ...")
        if not reconnect():
            logger.critical("Reconnect failed. Halting bot.")
            _running = False
            return

    now_utc = datetime.now(timezone.utc)
    account = _get_account_snapshot()
    if account is None:
        return
    current_equity = account.equity

    # Day rollover (resets day_start + daily counters)
    store.rollover_day_if_needed(current_equity, now_utc)

    state = store.get()
    if state.phase == PHASE_HALTED:
        logger.critical("Bot is HALTED: %s", state.halt_reason)
        _running = False
        return
    if state.phase == PHASE_PASSED:
        logger.info("Challenge already PASSED — bot idle (Ctrl-C to exit).")
        return

    # Pause check
    paused, pause_reason = store.is_paused(now_utc)
    if paused:
        logger.info("Paused (%s) — standing by.", pause_reason)
        _write_prop_status(store, order_manager, current_equity, None, "PAUSED", now_utc)
        return

    # Equity watchdog (circuit breakers)
    action = watchdog.evaluate(current_equity, now_utc)
    if action.halt:
        logger.critical("Watchdog HALT: %s", action.reason)
        _running = False
        return
    if action.flatten or action.pause_today:
        logger.warning("Watchdog flatten/pause: %s", action.reason)
        _write_prop_status(store, order_manager, current_equity, None, "PAUSED", now_utc)
        return

    # Phase target check (flattens and transitions if hit)
    if _check_phase_progression(store, order_manager, current_equity):
        _write_prop_status(store, order_manager, current_equity, None, "TRANSITION", now_utc)
        return

    # Manage open positions (trailing, BE, partials, close detection)
    try:
        order_manager.manage_open_positions()
    except Exception as exc:
        logger.exception("manage_open_positions error: %s", exc)

    # OHLCV refresh + signal chain
    _refresh_ohlcv_if_stale()

    # Risk tier readout (for dashboard — compute once per tick for display)
    dec = compute_risk(
        current_equity=current_equity,
        peak_equity=state.peak_equity,
        phase_start_balance=state.phase_start_balance,
        phase=state.phase,
    )
    for symbol in PROP_SYMBOLS:
        try:
            _process_pair(symbol, now_utc, store, risk_manager, order_manager, current_equity)
        except Exception as exc:
            logger.exception("[%s] _process_pair unhandled: %s", symbol, exc)

    _write_prop_status(store, order_manager, current_equity, dec.risk_pct, dec.tier, now_utc)

    logger.info(
        "Tick complete — %s UTC | phase=%s equity=%.2f peak=%.2f trades_today=%d open=%d risk_tier=%s",
        now_utc.strftime("%H:%M:%S"),
        state.phase, current_equity, state.peak_equity,
        state.daily_trades_count, len(order_manager.get_open_positions()),
        dec.tier,
    )


# ---------------------------------------------------------------------------
# TradeLogger hook — after a trade closes, update challenge_state counters
# ---------------------------------------------------------------------------

def _wrap_trade_logger_close(store: ChallengeStateStore, trade_logger: TradeLogger) -> None:
    """
    Monkey-patch trade_logger.log_close so every close updates challenge state
    (total_trades, consecutive_losses, cooldowns).
    """
    orig_log_close = trade_logger.log_close

    def _patched(**kwargs):
        result = orig_log_close(**kwargs)
        try:
            pnl = float(kwargs.get("pnl_usd", 0.0))
            won = bool(kwargs.get("won", False))
            now_utc = datetime.now(timezone.utc)
            store.record_trade_closed(pnl_usd=pnl, won=won, now_utc=now_utc)

            if not won:
                # Post-loss revenge cooldown
                store.pause_until(
                    now_utc + timedelta(minutes=POST_LOSS_COOLDOWN_MIN),
                    "POST_LOSS_COOLDOWN",
                )
                # Consecutive-loss lockout
                state = store.get()
                if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    store.pause_until(
                        now_utc + timedelta(hours=CONSECUTIVE_LOSS_COOLDOWN_H),
                        f"CONSECUTIVE_LOSS_X{state.consecutive_losses}",
                    )
        except Exception as exc:
            logger.exception("challenge_state close hook failed: %s", exc)
        return result

    trade_logger.log_close = _patched  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    global _running

    parser = argparse.ArgumentParser(description="ARCS-PROP prop-challenge bot")
    parser.add_argument("--dashboard", action="store_true",
                        help=f"Launch dashboard (http://localhost:{DASHBOARD_PORT})")
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT,
                        help=f"Dashboard port (default: {DASHBOARD_PORT})")
    args = parser.parse_args()

    _setup_logging()
    logger.info("=" * 60)
    logger.info("  ARCS-PROP starting up — %s UTC (magic=%d)",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                PROP_MAGIC)
    logger.info("=" * 60)

    if not connect():
        logger.critical("MT5 connection failed.")
        sys.exit(1)

    os.makedirs("data", exist_ok=True)

    # Challenge state (persisted) + bootstrap on first run
    store = ChallengeStateStore()
    snapshot = _get_account_snapshot()
    start_balance = snapshot.balance if snapshot else CHALLENGE_ACCOUNT_USD
    try:
        import MetaTrader5 as mt5
        login = int(getattr(mt5.account_info(), "login", 0))
    except Exception:
        login = 0
    store.bootstrap_if_empty(start_balance=start_balance, account_login=login)

    # Core modules — OrderManager retagged to PROP_MAGIC via module constant
    import execution.order_manager as _om_mod
    _om_mod.ARCS_MAGIC = PROP_MAGIC

    trade_logger  = TradeLogger()
    risk_manager  = RiskManager()
    order_manager = OrderManager(risk_manager, trade_logger)

    _wrap_trade_logger_close(store, trade_logger)

    watchdog = EquityWatchdog(store, flatten_fn=lambda r: order_manager.close_all_trades(reason=r))

    logger.info("PROP online. Pairs=%s cap_daily=%d confidence_min=%.0f",
                PROP_SYMBOLS,
                INTERNAL_MAX_DAILY_TRADES_P2 if store.get().phase == PHASE_2 else INTERNAL_MAX_DAILY_TRADES,
                PROP_CONFIDENCE_MIN)

    # Dashboard
    if args.dashboard:
        def _start_dashboard():
            try:
                os.environ.setdefault("PROP_DASHBOARD_PORT", str(args.port))
                from dashboard.prop_app import app as dash_app
                dash_app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
            except Exception as exc:
                logger.error("Dashboard failed to start: %s", exc)

        threading.Thread(target=_start_dashboard, daemon=True, name="PropDashboard").start()
        time.sleep(1.2)
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")
        logger.info("Dashboard live at http://localhost:%d", args.port)

    # Main loop
    global _cache_last_refresh
    _cache_last_refresh = 0.0
    while _running:
        try:
            _tick(store, watchdog, risk_manager, order_manager, trade_logger)
        except Exception as exc:
            logger.exception("Unhandled exception in tick: %s", exc)
        time.sleep(MAIN_LOOP_INTERVAL_S)

    logger.info("Shutdown — closing all positions ...")
    try:
        closed = order_manager.close_all_trades(reason="SHUTDOWN")
        logger.info("Closed %d position(s).", len(closed))
    except Exception as exc:
        logger.exception("Shutdown close error: %s", exc)
    disconnect()
    logger.info("ARCS-PROP stopped cleanly.")


if __name__ == "__main__":
    run()
