"""
ARCS-FX — main.py
Full orchestrator: Phase 6 complete signal chain.

WIRING ORDER (per tick, per pair):
  1. Refresh OHLCV cache (every DATA_REFRESH_INTERVAL_S)
  2. Manage any open positions (every tick, unconditionally)
  3. For each pair:
       a. Detect regime (H1 + M15)
       b. Skip if CHAOTIC
       c. Evaluate news — skip if blackout
       d. Run price action engine
       e. Get live spread
       f. Compute confidence score (7-component gate)
       g. Risk evaluation (Kelly sizing, circuit breakers, correlation)
       h. Open trade if all gates pass
  4. Weekly report trigger (Sunday 22:00 UTC)

DESIGN RULES (from spec):
  - NEVER trade in CHAOTIC regime or news blackout
  - Log every decision — skip reasons included
  - All parameters come from config.py
  - All module errors are caught; bot never crashes on a single bad tick
  - ARCS_MAGIC = 20260411 identifies bot orders in MT5
"""

import logging
import sys
import os
import signal
import time
import argparse
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path — ensure project root is importable before any local imports
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    MAIN_LOOP_INTERVAL_S, DATA_REFRESH_INTERVAL_S,
    PAIRS, TF_H1, TF_M15, TF_M5,
    REGIME_CHAOTIC, REGIME_QUIET, SESSIONS, LIVE_EARLY_MODE,
    WEEKLY_REPORT_DAY, WEEKLY_REPORT_HOUR_UTC,
)

# Core
from core.mt5_connection import connect, disconnect, is_connected, reconnect
from core import data_fetcher
from core.regime_detector import detect_multi_tf

# Engines
from engines import price_action
from engines.news_engine import evaluate as news_evaluate
from engines.confidence_score import compute as confidence_compute

# Risk
from risk.risk_manager import RiskManager, AccountSnapshot

# Execution
from execution.order_manager import OrderManager, TradeIntent

# Learning
from learning.trade_logger import TradeLogger
from learning.report_generator import ReportGenerator


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


class _NoiseSuppressingFilter(logging.Filter):
    """
    Drop repetitive low-value INFO chatter from the file log while preserving
    warnings, errors, and trading decisions. Regime snapshots are kept here so
    post-mortem debugging has the full picture.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True

        msg = record.getMessage()
        name = record.name

        if name == "werkzeug":
            return False

        if name == "engines.news_engine":
            if "Calendar:" in msg or "NewsEngine: CLEAR" in msg:
                return False

        if name.startswith(("httpx", "huggingface_hub", "transformers")):
            return False

        return True


class _ConsoleNoiseFilter(_NoiseSuppressingFilter):
    """
    Console inherits the file filter, then additionally silences per-pair
    regime INFO snapshots (8 pairs × every tick) that would flood the terminal.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.levelno < logging.WARNING
            and record.name == "core.regime_detector"
        ):
            return False
        return super().filter(record)


class _TradingEventsFilter(logging.Filter):
    """
    Send only high-signal trading lifecycle events to a compact companion log.
    """

    KEYWORDS = (
        "starting up",
        "Bot online",
        "All modules initialised",
        "Dashboard live",
        "PA signal",
        "News BLACKOUT",
        "All gates PASSED",
        "Risk BLOCKED",
        "open_trade() failed",
        "Tick complete",
        "Shutdown",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True

        msg = record.getMessage()
        name = record.name

        if name in {
            "execution.order_manager",
            "engines.price_action",
            "engines.confidence_score",
        }:
            return True

        if name == "engines.news_engine" and "BLACKOUT" in msg:
            return True

        if name == "ARCS-FX":
            return any(keyword in msg for keyword in self.KEYWORDS)

        return False


def _archive_previous_run_log(path: Path) -> None:
    """
    Archive the previous run's live log before a new bot session starts.

    The dashboard continues to read the stable live paths (`arcs_fx.log` and
    `trading_events.log`), while each restart leaves behind a timestamped file
    in `logs/archive/` for post-run review.
    """

    if not path.exists() or path.stat().st_size == 0:
        return

    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    timestamp = mtime.strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"{path.stem}_{timestamp}{path.suffix}"

    counter = 1
    while archive_path.exists():
        archive_path = archive_dir / f"{path.stem}_{timestamp}_{counter}{path.suffix}"
        counter += 1

    path.replace(archive_path)


def _archive_previous_run_logs() -> None:
    """Start each bot run with fresh live logs and archive the previous run."""
    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    _archive_previous_run_log(logs_dir / "arcs_fx.log")
    _archive_previous_run_log(logs_dir / "trading_events.log")


def _setup_logging() -> None:
    """Configure root logger with clean runtime and trading-event logs."""
    from logging.handlers import RotatingFileHandler
    _archive_previous_run_logs()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    file_filter    = _NoiseSuppressingFilter()
    console_filter = _ConsoleNoiseFilter()

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.addFilter(console_filter)
    root.addHandler(ch)

    fh = RotatingFileHandler(
        "logs/arcs_fx.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.addFilter(file_filter)
    root.addHandler(fh)

    trading_fh = RotatingFileHandler(
        "logs/trading_events.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    trading_fh.setFormatter(fmt)
    trading_fh.addFilter(_TradingEventsFilter())
    root.addHandler(trading_fh)

    # Reduce third-party noise that is not actionable during live monitoring.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    logging.getLogger("transformers").setLevel(logging.WARNING)


logger = logging.getLogger("ARCS-FX")


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
# OHLCV cache
# ---------------------------------------------------------------------------
# Refreshed every DATA_REFRESH_INTERVAL_S seconds (5 minutes by default).
# All 3 timeframes for all pairs are fetched in one batch refresh so individual
# ticks (60s) can run without hitting MT5 on every pair on every loop.
# ---------------------------------------------------------------------------

_ohlcv_cache: dict = {}            # key: (symbol, tf) -> pd.DataFrame
_cache_last_refresh: float = 0.0   # time.monotonic() of last refresh
_regime_cache: dict = {}           # symbol -> regime summary dict for dashboard
_gate_stats: dict = {}


def _new_gate_stats(now_utc: datetime) -> dict:
    """Create a fresh per-tick gate analytics snapshot."""
    return {
        "timestamp": now_utc.isoformat(),
        "counts": {
            "pairs_total": len(PAIRS),
            "ohlcv_missing": 0,
            "regime_chaotic": 0,
            "regime_quiet": 0,
            "news_blackout": 0,
            "pa_no_signal": 0,
            "confidence_blocked": 0,
            "risk_blocked": 0,
            "trade_opened": 0,
            "execution_error": 0,
        },
        "by_symbol": {},
    }


def _gate_bump(symbol: str, gate: str, reason: str) -> None:
    """Increment per-tick analytics for the gate that blocked or passed."""
    global _gate_stats

    counts = _gate_stats.setdefault("counts", {})
    counts[gate] = counts.get(gate, 0) + 1

    symbol_stats = _gate_stats.setdefault("by_symbol", {}).setdefault(
        symbol,
        {"events": []},
    )
    if len(symbol_stats["events"]) < 5:
        symbol_stats["events"].append({"gate": gate, "reason": reason})


def _refresh_ohlcv_if_stale() -> None:
    """Rebuild the OHLCV cache for all pairs and timeframes if TTL expired."""
    global _cache_last_refresh

    now = time.monotonic()
    if now - _cache_last_refresh < DATA_REFRESH_INTERVAL_S:
        return

    logger.info("Refreshing OHLCV cache for %d pairs ...", len(PAIRS))
    fetched = 0
    for symbol in PAIRS:
        for tf, getter in [
            (TF_H1,  data_fetcher.get_h1),
            (TF_M15, data_fetcher.get_m15),
            (TF_M5,  data_fetcher.get_m5),
        ]:
            df = getter(symbol)
            if df is not None:
                _ohlcv_cache[(symbol, tf)] = df
                fetched += 1
            else:
                # Remove stale entry so downstream code doesn't use old data
                _ohlcv_cache.pop((symbol, tf), None)
                logger.warning("OHLCV unavailable: %s %s -- skipped.", symbol, tf)

    _cache_last_refresh = now
    logger.info("OHLCV cache refresh complete: %d/%d timeframes loaded.",
                fetched, len(PAIRS) * 3)


# ---------------------------------------------------------------------------
# Account snapshot
# ---------------------------------------------------------------------------

def _get_account_snapshot() -> Optional[AccountSnapshot]:
    """
    Fetch live account balance/equity from MT5.

    Returns None on failure. Callers should skip the trade rather than
    proceeding with stale or zero account data -- sizing would be wrong.
    """
    try:
        import MetaTrader5 as mt5
        info = mt5.account_info()
        if info is None:
            logger.error("mt5.account_info() returned None: %s", mt5.last_error())
            return None
        return AccountSnapshot(
            balance=info.balance,
            equity=info.equity,
            margin_free=info.margin_free,
        )
    except Exception as exc:
        logger.exception("Failed to get account snapshot: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def _current_session(now_utc: datetime) -> str:
    """
    Return the trading session name for the given UTC time.

    Priority: OVERLAP > LONDON > NY > ASIAN > OFF
    WHY priority: OVERLAP (13-17 UTC) is also inside LONDON and NY ranges.
    We return the narrowest/highest-quality session first.
    """
    h = now_utc.hour
    ol_s, ol_e = SESSIONS["OVERLAP"]
    lo_s, lo_e = SESSIONS["LONDON"]
    ny_s, ny_e = SESSIONS["NY"]
    as_s, as_e = SESSIONS["ASIAN"]

    if ol_s <= h < ol_e:
        return "OVERLAP"
    if lo_s <= h < lo_e:
        return "LONDON"
    if ny_s <= h < ny_e:
        return "NY"
    if as_s <= h < as_e:
        return "ASIAN"
    return "OFF"


# ---------------------------------------------------------------------------
# Orphan detection on startup
# ---------------------------------------------------------------------------

def _recover_orphan_trades(order_manager: OrderManager, trade_logger: TradeLogger) -> None:
    """
    On bot restart, detect trades that were open but never closed in the DB.

    WHY: If the bot crashes between opening a trade and receiving the close
    event, the DB row has opened_at_utc set but closed_at_utc NULL. These
    'orphans' need to be reconciled with the live MT5 position list so the
    order_manager knows they exist and can manage them.
    """
    try:
        orphans = trade_logger.get_open_trades()
        if not orphans:
            return

        logger.warning("%d orphaned trade(s) found in DB on startup. Checking MT5 ...", len(orphans))
        import MetaTrader5 as mt5
        live_tickets = {p.ticket for p in (mt5.positions_get() or [])}

        for row in orphans:
            ticket = row.get("mt5_ticket", 0)
            trade_id = row.get("trade_id", "")
            if ticket in live_tickets:
                logger.info("Orphan %s (ticket=%d) is LIVE -- re-adopting into order manager.", trade_id, ticket)
                # Rebuild a minimal ManagedPosition so the manager can track it
                from execution.order_manager import ManagedPosition
                entry  = float(row.get("entry_price", 0))
                sl     = float(row.get("sl_price", 0))
                tp     = float(row.get("tp_price", 0))
                lots   = float(row.get("lots", 0))
                direction = row.get("direction", "BUY")

                managed = ManagedPosition(
                    trade_id         = trade_id,
                    mt5_ticket       = ticket,
                    symbol           = row.get("symbol", ""),
                    direction        = direction,
                    entry_price      = entry,
                    sl_price         = sl,
                    initial_sl_price = float(row.get("initial_sl_price", sl)),
                    tp_price         = tp,
                    lots             = lots,
                    initial_risk_r   = abs(entry - sl),
                    highest_price    = entry,
                    lowest_price     = entry,
                    signal_type      = row.get("signal_type", ""),
                    pattern          = row.get("pattern", ""),
                    r_ratio          = float(row.get("r_ratio", 0)),
                    confidence       = float(row.get("confidence", 0)),
                    regime           = row.get("regime", ""),
                    opened_at_utc    = row.get("opened_at_utc", ""),
                )
                # pylint: disable=protected-access
                order_manager._positions[trade_id] = managed
            else:
                logger.warning(
                    "Orphan %s (ticket=%d) NOT in MT5 positions -- marking closed with UNKNOWN_CLOSE.",
                    trade_id, ticket,
                )
                trade_logger.log_close(
                    trade_id          = trade_id,
                    close_price       = float(row.get("entry_price", 0)),
                    pnl_usd           = 0.0,
                    won               = False,
                    close_reason      = "UNKNOWN_CLOSE",
                    breakeven_applied = False,
                    trailing_active   = False,
                )
    except Exception as exc:
        logger.exception("Orphan recovery failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Weekly report trigger
# ---------------------------------------------------------------------------

_last_report_date: str = ""


def _maybe_trigger_weekly_report(report_gen: ReportGenerator, now_utc: datetime) -> None:
    """
    Trigger the weekly report on Sunday at WEEKLY_REPORT_HOUR_UTC (22:00 UTC).

    WHY Sunday evening: markets are closed, no active trades, full week of
    data is available. The report also triggers strategy weight adjustment.
    We track the date string to prevent re-triggering on the same Sunday.
    """
    global _last_report_date

    if now_utc.weekday() != WEEKLY_REPORT_DAY:   # 6 = Sunday
        return
    if now_utc.hour != WEEKLY_REPORT_HOUR_UTC:
        return

    date_str = now_utc.strftime("%Y-%m-%d")
    if _last_report_date == date_str:
        return   # already generated this Sunday

    logger.info("=== Weekly report trigger (Sunday %s 22:00 UTC) ===", date_str)
    try:
        path = report_gen.generate_weekly_report()
        _last_report_date = date_str
        logger.info("Weekly report saved: %s", path)
    except Exception as exc:
        logger.exception("Weekly report generation failed: %s", exc)


# ---------------------------------------------------------------------------
# Dashboard status writer
# ---------------------------------------------------------------------------

def _write_bot_status(order_manager: OrderManager, now_utc: datetime) -> None:
    """
    Write a snapshot of bot state to data/bot_status.json.

    The dashboard reads this file every 15 seconds. Atomic write (tmp->rename)
    so the dashboard never reads a half-written file.
    """
    try:
        import MetaTrader5 as mt5

        # Account
        account_data: dict = {}
        info = mt5.account_info()
        if info:
            account_data = {
                "balance":     round(float(info.balance),    2),
                "equity":      round(float(info.equity),     2),
                "margin_free": round(float(info.margin_free),2),
                "pnl_usd":     round(float(info.equity) - float(info.balance), 2),
            }

        # Open positions -- enrich with live MT5 profit + current price
        mt5_pos_map = {p.ticket: p for p in (mt5.positions_get() or [])}
        positions = []
        for pos in order_manager.get_open_positions():
            mp      = mt5_pos_map.get(pos.mt5_ticket)
            pnl_usd = round(float(mp.profit),        2) if mp else 0.0
            curr_px = round(float(mp.price_current), 5) if mp else pos.entry_price
            r_cur   = 0.0
            if pos.initial_sl_price and pos.initial_sl_price != pos.entry_price:
                pips_per_r = abs(pos.entry_price - pos.initial_sl_price)
                pips_moved = (curr_px - pos.entry_price) if pos.direction == "BUY" \
                             else (pos.entry_price - curr_px)
                r_cur = round(pips_moved / pips_per_r, 2)

            positions.append({
                "trade_id":     pos.trade_id,
                "symbol":       pos.symbol,
                "direction":    pos.direction,
                "entry_price":  pos.entry_price,
                "current_price":curr_px,
                "sl_price":     pos.sl_price,
                "tp_price":     pos.tp_price,
                "lots":         pos.lots,
                "pnl_usd":      pnl_usd,
                "r_current":    r_cur,
                "signal_type":  pos.signal_type,
                "confidence":   pos.confidence,
                "r_ratio":      pos.r_ratio,
                "session":      getattr(pos, "session", ""),
                "opened_at_utc":pos.opened_at_utc,
            })

        status = {
            "timestamp":      now_utc.isoformat(),
            "bot_running":    True,
            "session":        _current_session(now_utc),
            "account":        account_data,
            "regimes":        _regime_cache,
            "open_positions": positions,
            "gate_stats":     _gate_stats,
        }

        os.makedirs("data", exist_ok=True)
        tmp_path = os.path.join("data", "bot_status.tmp")
        dst_path = os.path.join("data", "bot_status.json")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
        os.replace(tmp_path, dst_path)

    except Exception as exc:
        logger.debug("_write_bot_status failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Per-pair signal chain
# ---------------------------------------------------------------------------

def _process_pair(
    symbol: str,
    now_utc: datetime,
    risk_manager: RiskManager,
    order_manager: OrderManager,
    trade_logger: TradeLogger,
) -> None:
    """
    Full signal evaluation for one pair on one tick.

    Gate order (spec-mandated):
      1. OHLCV available
      2. Regime not CHAOTIC
      3. News not in blackout
      4. Price action has a valid signal
      5. Confidence score >= threshold
      6. Risk check approved
      7. Open trade

    Every rejected gate is logged with a reason. The bot never silently
    skips -- every skip is traceable in arcs_fx.log.
    """
    # -- Gate 1: OHLCV cache -----------------------------------------------
    h1_df  = _ohlcv_cache.get((symbol, TF_H1))
    m15_df = _ohlcv_cache.get((symbol, TF_M15))
    m5_df  = _ohlcv_cache.get((symbol, TF_M5))

    if h1_df is None or m15_df is None or m5_df is None:
        _gate_bump(symbol, "ohlcv_missing", "OHLCV cache miss")
        logger.debug("[%s] OHLCV cache miss -- skipping.", symbol)
        return

    # -- Gate 2: Regime detection ------------------------------------------
    try:
        regime_map = detect_multi_tf(symbol, h1_df, m15_df)
    except Exception as exc:
        logger.exception("[%s] Regime detection error: %s", symbol, exc)
        return

    if not regime_map or "H1" not in regime_map:
        logger.debug("[%s] Regime detection returned no result.", symbol)
        return

    regime_h1  = regime_map["H1"]
    regime_m15 = regime_map.get("M15", regime_h1)

    # Cache regime for dashboard (always, even if CHAOTIC so UI shows red)
    _regime_cache[symbol] = {
        "regime":     regime_h1.regime,
        "confidence": round(regime_h1.confidence * 100),
        "direction":  regime_h1.direction,
        "adx":        round(float(regime_h1.adx), 1),
        "atr_pct":    round(float(regime_h1.atr_percentile)),
        "trigger":    regime_h1.components.get("trigger", ""),
        "updated":    now_utc.isoformat(),
    }

    if regime_h1.regime == REGIME_CHAOTIC:
        _gate_bump(symbol, "regime_chaotic", f"ATR_pct={regime_h1.atr_percentile:.0f}")
        logger.info("[%s] CHAOTIC regime (ATR_pct=%.0f) -- bot silent.",
                    symbol, regime_h1.atr_percentile)
        return
    if regime_h1.regime == REGIME_QUIET:
        _gate_bump(symbol, "regime_quiet", regime_h1.components.get("trigger", "quiet regime"))
        logger.info("[%s] QUIET regime (%s) -- standing by.",
                    symbol, regime_h1.components.get("trigger", "low-volatility / transitional"))
        return

    logger.debug("[%s] Regime: %s (conf=%.0f%%) Dir=%s ATR_pct=%.0f",
                 symbol, regime_h1.regime, regime_h1.confidence * 100,
                 regime_h1.direction, regime_h1.atr_percentile)

    # -- Gate 3: News evaluation -------------------------------------------
    try:
        news_result = news_evaluate(symbol, now_utc)
    except Exception as exc:
        logger.exception("[%s] News engine error: %s", symbol, exc)
        return

    if news_result.is_blackout:
        _gate_bump(symbol, "news_blackout", news_result.blackout_reason)
        logger.info("[%s] News BLACKOUT: %s -- skipping.", symbol, news_result.blackout_reason)
        return

    # -- Gate 4: Live spread -----------------------------------------------
    tick = data_fetcher.get_tick(symbol)
    spread_pips = tick["spread_pips"] if tick and tick.get("spread_pips") is not None else 0.0

    # -- Gate 5: Price action evaluation -----------------------------------
    try:
        pa_signal = price_action.evaluate(
            symbol         = symbol,
            h1_df          = h1_df,
            m15_df         = m15_df,
            m5_df          = m5_df,
            regime         = regime_h1.regime,
            direction_bias = regime_h1.direction,
            atr            = regime_h1.atr,
        )
    except Exception as exc:
        logger.exception("[%s] Price action engine error: %s", symbol, exc)
        return

    if not pa_signal.has_signal:
        _gate_bump(symbol, "pa_no_signal", f"{regime_h1.regime} regime produced no PA setup")
        logger.debug("[%s] No PA signal.", symbol)
        return

    logger.info("[%s] PA signal: %s %s | entry=%.5f sl=%.5f tp=%.5f R=%.1f",
                symbol, pa_signal.direction, pa_signal.signal_type,
                pa_signal.entry_price, pa_signal.sl_price,
                pa_signal.tp_price, pa_signal.r_ratio)

    # -- Gate 6: Confidence score ------------------------------------------
    try:
        conf_result = confidence_compute(
            symbol        = symbol,
            regime_result = regime_h1,
            regime_m15    = regime_m15,
            pa_signal     = pa_signal,
            news_result   = news_result,
            spread_pips   = spread_pips,
            early_mode    = LIVE_EARLY_MODE,
            now_utc       = now_utc,
        )
    except Exception as exc:
        logger.exception("[%s] Confidence engine error: %s", symbol, exc)
        return

    logger.info("[%s] %s", symbol, conf_result)

    if not conf_result.trade_allowed:
        _gate_bump(symbol, "confidence_blocked", conf_result.skip_reason)
        return

    # -- Gate 7: Risk evaluation -------------------------------------------
    account = _get_account_snapshot()
    if account is None:
        logger.error("[%s] Cannot read account -- skipping trade.", symbol)
        return

    open_positions = order_manager.get_open_positions()

    try:
        risk_result = risk_manager.evaluate_trade(
            symbol         = symbol,
            direction      = pa_signal.direction,
            entry_price    = pa_signal.entry_price,
            sl_price       = pa_signal.sl_price,
            account        = account,
            open_positions = open_positions,
        )
    except Exception as exc:
        logger.exception("[%s] Risk evaluation error: %s", symbol, exc)
        return

    if not risk_result.approved:
        _gate_bump(symbol, "risk_blocked", risk_result.skip_reason)
        logger.info("[%s] Risk BLOCKED: %s", symbol, risk_result.skip_reason)
        return

    # -- Gate 8: Build TradeIntent and open trade --------------------------
    session = _current_session(now_utc)

    intent = TradeIntent(
        symbol               = symbol,
        direction            = pa_signal.direction,
        entry_price          = pa_signal.entry_price,
        sl_price             = pa_signal.sl_price,
        tp_price             = pa_signal.tp_price,
        signal_type          = pa_signal.signal_type,
        pattern              = pa_signal.pattern,
        r_ratio              = pa_signal.r_ratio,
        confidence           = conf_result.score,
        regime               = regime_h1.regime,
        risk_check           = risk_result,
        session              = session,
        spread_at_entry      = spread_pips,
        day_of_week          = now_utc.weekday(),
        news_score_at_entry  = news_result.sentiment_score,
        confidence_components = [
            {
                "name":      c.name,
                "score":     round(c.weighted_score, 2),
                "max_score": c.weight,
            }
            for c in conf_result.components
        ],
    )

    logger.info(
        "[%s] All gates PASSED. Opening %s trade. "
        "Lots=%.2f Conf=%.1f Session=%s Spread=%.1f",
        symbol, pa_signal.direction,
        risk_result.position_size_lots,
        conf_result.score, session, spread_pips,
    )

    try:
        order_manager.open_trade(intent)
        _gate_bump(symbol, "trade_opened", f"{pa_signal.direction} {pa_signal.signal_type}")
    except Exception as exc:
        _gate_bump(symbol, "execution_error", str(exc))
        logger.exception("[%s] open_trade() failed: %s", symbol, exc)


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

def _tick(
    risk_manager:  RiskManager,
    order_manager: OrderManager,
    trade_logger:  TradeLogger,
    report_gen:    ReportGenerator,
) -> None:
    """
    Single orchestrator tick -- called every MAIN_LOOP_INTERVAL_S seconds.

    Sequence:
      1. Health check (MT5 reconnect if needed)
      2. Refresh OHLCV cache if stale
      3. Manage open positions (trailing, breakeven, close detection)
      4. Process each pair through the full signal chain
      5. Weekly report trigger (Sunday 22:00 UTC)
    """
    global _running, _gate_stats

    # -- 1. Health check ---------------------------------------------------
    if not is_connected():
        logger.warning("MT5 connection lost -- attempting reconnect ...")
        if not reconnect():
            logger.critical("Reconnect failed. Halting bot.")
            _running = False
            return

    now_utc = datetime.now(timezone.utc)
    _gate_stats = _new_gate_stats(now_utc)

    # -- 2. OHLCV cache refresh (rate-limited) -----------------------------
    _refresh_ohlcv_if_stale()

    # -- 3. Manage existing open positions (every tick, unconditionally) ---
    try:
        order_manager.manage_open_positions()
    except Exception as exc:
        logger.exception("manage_open_positions() error: %s", exc)

    # -- 4. Per-pair signal chain ------------------------------------------
    for symbol in PAIRS:
        try:
            _process_pair(
                symbol        = symbol,
                now_utc       = now_utc,
                risk_manager  = risk_manager,
                order_manager = order_manager,
                trade_logger  = trade_logger,
            )
        except Exception as exc:
            logger.exception("[%s] Unhandled error in _process_pair: %s", symbol, exc)

    # -- 5. Weekly report --------------------------------------------------
    _maybe_trigger_weekly_report(report_gen, now_utc)

    # -- 6. Dashboard status file (non-fatal) ------------------------------
    _write_bot_status(order_manager, now_utc)

    counts = _gate_stats.get("counts", {})

    logger.info(
        "Tick complete -- %s UTC | open_positions=%d | opened=%d quiet=%d chaotic=%d pa=%d conf=%d risk=%d",
        now_utc.strftime("%H:%M:%S"),
        len(order_manager.get_open_positions()),
        counts.get("trade_opened", 0),
        counts.get("regime_quiet", 0),
        counts.get("regime_chaotic", 0),
        counts.get("pa_no_signal", 0),
        counts.get("confidence_blocked", 0),
        counts.get("risk_blocked", 0),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """
    Boot the bot, wire all modules, run the main loop, shut down cleanly.

    Module initialisation order:
      TradeLogger   -- must be first (risk manager and order manager log to it)
      RiskManager   -- depends on TradeLogger for win rate history
      OrderManager  -- depends on RiskManager and TradeLogger
      ReportGenerator -- depends on all of the above
    """
    global _running

    parser = argparse.ArgumentParser(description="ARCS-FX trading bot")
    parser.add_argument("--dashboard", action="store_true",
                        help="Launch live dashboard (http://localhost:5000)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Dashboard port (default: 5000)")
    args = parser.parse_args()

    _setup_logging()
    logger.info("=" * 60)
    logger.info("  ARCS-FX starting up -- %s UTC",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("=" * 60)

    # -- Connect to MT5 ----------------------------------------------------
    if not connect():
        logger.critical("Initial MT5 connection failed. Exiting.")
        sys.exit(1)

    # -- Initialise modules ------------------------------------------------
    os.makedirs("data", exist_ok=True)

    trade_logger  = TradeLogger()
    risk_manager  = RiskManager()
    order_manager = OrderManager(risk_manager, trade_logger)
    report_gen    = ReportGenerator()   # uses default DB_PATH from config

    logger.info("All modules initialised.")

    # -- Orphan recovery on restart ----------------------------------------
    _recover_orphan_trades(order_manager, trade_logger)

    # -- Force first OHLCV cache population --------------------------------
    # Set last_refresh to 0 so the first tick immediately fetches data.
    global _cache_last_refresh
    _cache_last_refresh = 0.0

    logger.info("Bot online. Tick interval: %ds, OHLCV refresh: %ds",
                MAIN_LOOP_INTERVAL_S, DATA_REFRESH_INTERVAL_S)

    # -- Launch dashboard if requested ------------------------------------
    if args.dashboard:
        def _start_dashboard():
            try:
                os.environ.setdefault("ARCS_DASHBOARD_PORT", str(args.port))
                from dashboard.app import app as dash_app
                dash_app.run(
                    host="0.0.0.0",
                    port=args.port,
                    debug=False,
                    use_reloader=False,
                )
            except Exception as exc:
                logger.error("Dashboard failed to start: %s", exc)

        dash_thread = threading.Thread(
            target=_start_dashboard, daemon=True, name="Dashboard"
        )
        dash_thread.start()
        time.sleep(1.2)   # let Flask bind the port
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")
        logger.info("Dashboard live at http://localhost:%d", args.port)

    # -- Main loop ---------------------------------------------------------
    while _running:
        try:
            _tick(risk_manager, order_manager, trade_logger, report_gen)
        except Exception as exc:
            logger.exception("Unhandled exception in main loop: %s", exc)
            # Log and continue -- one bad tick must never kill the bot

        time.sleep(MAIN_LOOP_INTERVAL_S)

    # -- Graceful shutdown -------------------------------------------------
    logger.info("Shutdown signal received -- closing all positions ...")
    try:
        closed = order_manager.close_all_trades(reason="SHUTDOWN")
        logger.info("Shutdown: %d position(s) closed.", len(closed))
    except Exception as exc:
        logger.exception("Error during shutdown close: %s", exc)

    disconnect()
    logger.info("ARCS-FX stopped cleanly.")


if __name__ == "__main__":
    run()
