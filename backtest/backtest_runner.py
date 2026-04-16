"""
ARCS-FX -- backtest/backtest_runner.py
Historical simulation of the full signal chain without placing live orders.

WHY BACKTESTING BEFORE LIVE:
Before committing real demo money to the bot, we want answers to:
  - Does the regime detector classify the last 3 months correctly?
  - How often does price action fire a valid signal in each regime?
  - What is the average confidence score distribution?
  - Do circuit breakers ever fire? How many trades per week?
  - Is the 70-point (or 80-point early mode) threshold too tight or too loose?

WHAT THIS SCRIPT DOES:
  1. Fetches historical OHLCV from MT5 (all pairs, H1/M15/M5)
  2. Steps forward bar by bar, simulating the live tick loop
  3. On each step: regime -> news (synthetic) -> PA -> confidence -> risk
  4. Logs every signal to a SimulatedTrade list (no MT5 orders placed)
  5. Outputs a JSON summary and prints a text report to the terminal

WHAT IT DOES NOT DO:
  - Place any orders
  - Connect to a live news feed (news is synthetic: no-blackout, neutral)
  - Model slippage or spread (uses close price as fill price)
  - Compute actual P&L (that requires realistic fill simulation)

HOW TO RUN:
  py -3.11 backtest/backtest_runner.py

OPTIONAL ARGS:
  --days N     : look back N days of history (default 90)
  --pair P     : run on a single pair instead of all pairs
  --early      : use 80-point early mode threshold (default 70)
  --verbose    : print every signal (default: summary only)
"""

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

# ---------------------------------------------------------------------------
# Path setup -- must come before any local imports
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from config import (
    PAIRS, TF_H1, TF_M15, TF_M5,
    CANDLES_H1, CANDLES_M15, CANDLES_M5,
    REGIME_CHAOTIC, REGIME_QUIET,
    CONFIDENCE_MIN, CONFIDENCE_EARLY_MODE,
)
from core.mt5_connection import connect, disconnect
from core import data_fetcher
from core.regime_detector import detect_multi_tf
from engines import price_action
from engines.price_action import diagnose as pa_diagnose
from engines.confidence_score import compute as confidence_compute
from risk.risk_manager import RiskManager, AccountSnapshot
from execution.order_manager import OpenPosition

logger = logging.getLogger("backtest")


# ===========================================================================
# Data classes
# ===========================================================================

@dataclass
class SimulatedSignal:
    """A single bar where the confidence gate passed."""
    symbol:        str
    timestamp_utc: str
    direction:     str
    regime:        str
    signal_type:   str
    pattern:       str
    confidence:    float
    entry_price:   float
    sl_price:      float
    tp_price:      float
    r_ratio:       float
    lots:          float
    risk_pct:      float
    spread_pips:   float
    session:       str


@dataclass
class BacktestSummary:
    """Aggregate statistics from a full backtest run."""
    pairs_tested:      int
    bars_evaluated:    int
    signals_generated: int
    signals_by_regime: dict
    signals_by_session: dict
    avg_confidence:    float
    avg_r_ratio:       float
    signals_by_family: dict
    signals_by_type:   dict
    signals_by_symbol: dict
    chaotic_skips:     int
    quiet_skips:       int
    news_skips:        int      # always 0 in backtest (synthetic news)
    pa_no_signal:      int
    confidence_blocked: int
    risk_blocked:      int
    signals:           list


# ===========================================================================
# Synthetic news (backtest mode -- no live news feed)
# ===========================================================================

class _SyntheticNews:
    """
    Stand-in NewsResult for backtesting.

    Always returns CLEAR (no blackout) and NEUTRAL sentiment (0.0).
    WHY: A proper backtest news feed would require a historical ForexFactory
    database. That is Phase 7 work. For now, this tells us how the bot
    behaves on pure price action signals, ignoring news filtering.
    The real-world expectation is that news filtering will REDUCE signal
    count (some signals will be blocked during blackout windows).
    """

    @staticmethod
    def make():
        from engines.news_engine import NewsResult
        return NewsResult(
            is_blackout         = False,
            blackout_reason     = "",
            minutes_to_next_event = None,
            next_event          = None,
            sentiment_score     = 0.0,
            sentiment_label     = "NEUTRAL",
            sentiment_confidence = 0.5,
            active_events       = [],
            recent_headlines    = [],
            source_quality      = "BACKTEST",
        )


# ===========================================================================
# Bar slicer
# ===========================================================================

def _slice_at_bar(df: pd.DataFrame, end_idx: int, lookback: int) -> Optional[pd.DataFrame]:
    """
    Return the sub-DataFrame ending at bar `end_idx` with `lookback` bars.

    WHY: In live mode we always have the most recent N candles. To simulate
    this for backtesting, we step forward bar by bar and slice the historical
    data so the engine 'sees' exactly what it would have seen in live mode.

    Returns None if there are not enough bars.
    """
    start_idx = end_idx - lookback
    if start_idx < 0:
        return None
    return df.iloc[start_idx:end_idx].copy()


def _session_at(ts: datetime) -> str:
    """Map a UTC timestamp to a session label."""
    h = ts.hour
    if 13 <= h < 17:
        return "OVERLAP"
    if 7 <= h < 16:
        return "LONDON"
    if 13 <= h < 21:
        return "NY"
    if 0 <= h < 8:
        return "ASIAN"
    return "OFF"


def _signal_family(signal_type: str) -> str:
    """
    Group signal types into higher-level strategy families.

    WHY:
    The bot now mixes multiple entry engines. Looking only at raw signal_type
    is too granular when we want to answer "is scalp or swing doing the work?"
    """
    if not signal_type:
        return "UNKNOWN"

    if signal_type.startswith("SCALP_"):
        return "SCALP"
    if signal_type.startswith("SWING_"):
        return "SWING"
    if signal_type in {"OB_RETEST", "FVG_FILL"}:
        return "TREND"
    if signal_type in {"SD_BOUNCE"}:
        return "MEAN_REVERSION"
    return "OTHER"


# ===========================================================================
# Per-pair backtest
# ===========================================================================

def _backtest_pair(
    symbol:       str,
    days:         int,
    early_mode:   bool,
    verbose:      bool,
    risk_manager: RiskManager,
    step:         int = 4,
) -> dict:
    """
    Run the full signal chain bar-by-bar for one pair.
    Returns a dict with stats and signal list.
    """
    logger.info("[BT] Fetching %d days of history for %s ...", days, symbol)

    # We need enough bars for indicators: CANDLES_H1 (400) minimum plus
    # the simulation window.  Fetch extra so slicing always has 400+ rows.
    total_bars_h1  = CANDLES_H1 + days * 24
    total_bars_m15 = CANDLES_M15 + days * 24 * 4
    total_bars_m5  = CANDLES_M5  + days * 24 * 12

    h1_full  = data_fetcher.get_ohlcv(symbol, TF_H1,  total_bars_h1)
    m15_full = data_fetcher.get_ohlcv(symbol, TF_M15, total_bars_m15)
    m5_full  = data_fetcher.get_ohlcv(symbol, TF_M5,  total_bars_m5)

    if h1_full is None or m15_full is None or m5_full is None:
        logger.error("[BT] %s: failed to fetch history. Skipping.", symbol)
        return {}

    # Determine how many H1 bars fall within the simulation window
    cutoff_dt   = datetime.now(timezone.utc) - timedelta(days=days)
    sim_h1_df   = h1_full[h1_full.index >= cutoff_dt]
    sim_bars    = len(sim_h1_df)

    logger.info("[BT] %s: %d H1 bars in %d-day window.", symbol, sim_bars, days)

    signals:    list[SimulatedSignal] = []
    chaotic     = 0
    quiet       = 0
    pa_miss     = 0
    conf_block  = 0
    risk_block  = 0
    bars_eval   = 0

    synthetic_news = _SyntheticNews.make()

    # Simulate account -- fixed $1000 for all backtest runs
    account = AccountSnapshot(balance=1000.0, equity=1000.0, margin_free=900.0)

    # Offset into the FULL dataframe to start simulation from
    h1_start_offset = len(h1_full) - sim_bars

    # Pre-sort indexes once so searchsorted works correctly
    m15_idx = m15_full.index
    m5_idx  = m5_full.index

    for i in range(0, sim_bars, step):
        h1_end  = h1_start_offset + i + 1   # exclusive upper bound

        # Align M15 / M5 bars to the same timestamp as this H1 bar
        ts = h1_full.index[h1_end - 1]

        # searchsorted is O(log n) — much faster than boolean mask O(n)
        m15_end = int(m15_idx.searchsorted(ts, side="right"))
        m5_end  = int(m5_idx.searchsorted(ts,  side="right"))

        # Slice each timeframe up to (not including) the current bar
        h1_slice  = _slice_at_bar(h1_full,  h1_end,  CANDLES_H1)
        m15_slice = _slice_at_bar(m15_full, m15_end, CANDLES_M15)
        m5_slice  = _slice_at_bar(m5_full,  m5_end,  CANDLES_M5)

        # Progress report every 200 evaluated bars
        if bars_eval > 0 and bars_eval % 200 == 0:
            pct = round(i / sim_bars * 100)
            print(f"  {symbol}: {pct}% ({i}/{sim_bars} bars, {len(signals)} signals so far)",
                  flush=True)

        if h1_slice is None or m15_slice is None or m5_slice is None:
            continue

        bars_eval += 1

        # -- Regime --
        regime_map = detect_multi_tf(symbol, h1_slice, m15_slice)
        if not regime_map or "H1" not in regime_map:
            continue

        rh1  = regime_map["H1"]
        rm15 = regime_map.get("M15", rh1)

        if rh1.regime == REGIME_CHAOTIC:
            chaotic += 1
            continue
        if rh1.regime == REGIME_QUIET:
            quiet += 1
            continue

        # -- Price action --
        pa = price_action.evaluate(
            symbol         = symbol,
            h1_df          = h1_slice,
            m15_df         = m15_slice,
            m5_df          = m5_slice,
            regime         = rh1.regime,
            direction_bias = rh1.direction,
            atr            = rh1.atr,
        )

        if not pa.has_signal:
            pa_miss += 1
            continue

        # -- Confidence --
        conf = confidence_compute(
            symbol        = symbol,
            regime_result = rh1,
            regime_m15    = rm15,
            pa_signal     = pa,
            news_result   = synthetic_news,
            spread_pips   = 0.8,   # typical EURUSD spread
            early_mode    = early_mode,
            now_utc       = ts.to_pydatetime().replace(tzinfo=timezone.utc),
        )

        if not conf.trade_allowed:
            conf_block += 1
            if verbose:
                logger.info("[BT] %s %s: CONF BLOCKED %.1f (%s)",
                            symbol, ts.strftime("%Y-%m-%d %H:%M"), conf.score, conf.skip_reason)
            continue

        # -- Risk (simplified -- no correlation check in backtest) --
        risk = risk_manager.evaluate_trade(
            symbol         = symbol,
            direction      = pa.direction,
            entry_price    = pa.entry_price,
            sl_price       = pa.sl_price,
            account        = account,
            open_positions = [],   # no real positions in backtest
        )

        if not risk.approved:
            risk_block += 1
            continue

        # -- Record signal --
        now_ts = ts.to_pydatetime().replace(tzinfo=timezone.utc)
        sig = SimulatedSignal(
            symbol        = symbol,
            timestamp_utc = now_ts.isoformat(),
            direction     = pa.direction,
            regime        = rh1.regime,
            signal_type   = pa.signal_type,
            pattern       = pa.pattern,
            confidence    = conf.score,
            entry_price   = pa.entry_price,
            sl_price      = pa.sl_price,
            tp_price      = pa.tp_price,
            r_ratio       = pa.r_ratio,
            lots          = risk.position_size_lots,
            risk_pct      = risk.risk_pct,
            spread_pips   = 0.8,
            session       = _session_at(now_ts),
        )
        signals.append(sig)

        if verbose:
            logger.info(
                "[BT] SIGNAL %s %s %s | conf=%.1f r=%.1f lots=%.2f",
                symbol, pa.direction, pa.signal_type,
                conf.score, pa.r_ratio, risk.position_size_lots,
            )

    logger.info(
        "[BT] %s done: bars=%d signals=%d chaotic=%d pa_miss=%d conf_block=%d risk_block=%d",
        symbol, bars_eval, len(signals), chaotic, pa_miss, conf_block, risk_block,
    )

    return {
        "symbol":        symbol,
        "bars_evaluated": bars_eval,
        "signals":       signals,
        "chaotic_skips": chaotic,
        "quiet_skips":   quiet,
        "pa_no_signal":  pa_miss,
        "confidence_blocked": conf_block,
        "risk_blocked":  risk_block,
    }


# ===========================================================================
# Diagnostic mode  (--diagnose flag)
# ===========================================================================

def _diagnose_pair(
    symbol: str,
    days: int,
    step: int = 1,
) -> None:
    """
    Run a diagnostic pass for one pair and print a breakdown of
    why PA signals are blocked.  Uses step=1 by default to catch
    every bar.

    This does NOT run the full confidence/risk chain -- it only
    instruments the price_action gate to show which sub-condition
    blocks the most bars.
    """
    print(f"\n[DIAG] Fetching {days} days for {symbol} ...")

    total_bars_h1  = CANDLES_H1 + days * 24
    total_bars_m15 = CANDLES_M15 + days * 24 * 4
    total_bars_m5  = CANDLES_M5  + days * 24 * 12

    h1_full  = data_fetcher.get_ohlcv(symbol, TF_H1,  total_bars_h1)
    m15_full = data_fetcher.get_ohlcv(symbol, TF_M15, total_bars_m15)
    m5_full  = data_fetcher.get_ohlcv(symbol, TF_M5,  total_bars_m5)

    if h1_full is None or m15_full is None or m5_full is None:
        print(f"[DIAG] {symbol}: failed to fetch data.")
        return

    from datetime import timedelta
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    sim_h1_df = h1_full[h1_full.index >= cutoff_dt]
    sim_bars  = len(sim_h1_df)

    m15_idx = m15_full.index
    m5_idx  = m5_full.index
    h1_start_offset = len(h1_full) - sim_bars

    counts = {
        "total_non_chaotic": 0,
        "quiet": 0,
        "m15_failed": 0,
        "direction_neutral": 0,
        "no_ob_near_price": 0,
        "no_sd_near_price": 0,
        "pattern_none_ob_present": 0,
        "pattern_none_sd_present": 0,
        "pattern_none": 0,
        "scalp_session_blocked": 0,
        "ob_direction_mismatch": 0,
        "low_rr_or_other": 0,
        "low_rr_or_pattern_mismatch": 0,
    }

    # Pattern frequency counters
    pattern_freq: dict = {}

    # OB and zone count accumulators
    ob_totals   = []
    sd_totals   = []

    for i in range(0, sim_bars, step):
        h1_end = h1_start_offset + i + 1
        ts = h1_full.index[h1_end - 1]

        m15_end = int(m15_idx.searchsorted(ts, side="right"))
        m5_end  = int(m5_idx.searchsorted(ts,  side="right"))

        h1_slice  = _slice_at_bar(h1_full,  h1_end,  CANDLES_H1)
        m15_slice = _slice_at_bar(m15_full, m15_end, CANDLES_M15)
        m5_slice  = _slice_at_bar(m5_full,  m5_end,  CANDLES_M5)

        if h1_slice is None or m15_slice is None or m5_slice is None:
            continue

        regime_map = detect_multi_tf(symbol, h1_slice, m15_slice)
        if not regime_map or "H1" not in regime_map:
            continue

        rh1 = regime_map["H1"]
        if rh1.regime == REGIME_CHAOTIC:
            continue
        if rh1.regime == REGIME_QUIET:
            counts["quiet"] += 1
            continue

        counts["total_non_chaotic"] += 1

        diag = pa_diagnose(
            symbol         = symbol,
            h1_df          = h1_slice,
            m15_df         = m15_slice,
            m5_df          = m5_slice,
            regime         = rh1.regime,
            direction_bias = rh1.direction,
            atr            = rh1.atr,
        )

        reason = diag.get("block_reason", "unknown")
        counts[reason] = counts.get(reason, 0) + 1

        pat = diag.get("pattern", "NONE")
        pattern_freq[pat] = pattern_freq.get(pat, 0) + 1

        ob_totals.append(diag.get("ob_count", 0))
        sd_totals.append(diag.get("sd_zone_count", 0))

    total = counts["total_non_chaotic"] or 1
    print(f"\n[DIAG] {symbol} -- {sim_bars} H1 bars | step={step}")
    print(f"  Non-chaotic bars:       {counts['total_non_chaotic']}")
    print(f"  Quiet bars skipped:     {counts['quiet']}")
    print(f"  Avg OBs found/bar:      {sum(ob_totals)/len(ob_totals):.1f}" if ob_totals else "  Avg OBs found/bar:      n/a")
    print(f"  Avg S&D zones/bar:      {sum(sd_totals)/len(sd_totals):.1f}" if sd_totals else "  Avg S&D zones/bar:      n/a")
    print(f"\n  Block reason breakdown (% of non-chaotic):")
    for reason, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        if reason == "total_non_chaotic" or cnt == 0:
            continue
        print(f"    {reason:<35}: {cnt:>4}  ({cnt/total*100:.0f}%)")
    print(f"\n  Pattern frequency on M5 last candle:")
    for pat, cnt in sorted(pattern_freq.items(), key=lambda x: -x[1]):
        print(f"    {pat:<30}: {cnt:>4}  ({cnt/total*100:.0f}%)")


# ===========================================================================
# Aggregate summary
# ===========================================================================

def _build_summary(pair_results: list[dict]) -> BacktestSummary:
    """Aggregate per-pair results into a single BacktestSummary."""
    all_signals: list[SimulatedSignal] = []
    total_bars   = 0
    chaotic_tot  = 0
    quiet_tot    = 0
    pa_miss_tot  = 0
    conf_tot     = 0
    risk_tot     = 0

    for r in pair_results:
        if not r:
            continue
        all_signals.extend(r.get("signals", []))
        total_bars   += r.get("bars_evaluated", 0)
        chaotic_tot  += r.get("chaotic_skips", 0)
        quiet_tot    += r.get("quiet_skips", 0)
        pa_miss_tot  += r.get("pa_no_signal", 0)
        conf_tot     += r.get("confidence_blocked", 0)
        risk_tot     += r.get("risk_blocked", 0)

    by_regime: dict[str, int] = {}
    by_session: dict[str, int] = {}
    by_family: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    confidences: list[float] = []
    r_ratios: list[float] = []

    for s in all_signals:
        by_regime[s.regime]   = by_regime.get(s.regime, 0) + 1
        by_session[s.session] = by_session.get(s.session, 0) + 1
        family = _signal_family(s.signal_type)
        by_family[family] = by_family.get(family, 0) + 1
        by_type[s.signal_type] = by_type.get(s.signal_type, 0) + 1
        by_symbol[s.symbol] = by_symbol.get(s.symbol, 0) + 1
        confidences.append(s.confidence)
        r_ratios.append(s.r_ratio)

    return BacktestSummary(
        pairs_tested       = len([r for r in pair_results if r]),
        bars_evaluated     = total_bars,
        signals_generated  = len(all_signals),
        signals_by_regime  = by_regime,
        signals_by_session = by_session,
        signals_by_family  = by_family,
        signals_by_type    = by_type,
        signals_by_symbol  = by_symbol,
        avg_confidence     = round(sum(confidences) / len(confidences), 1) if confidences else 0,
        avg_r_ratio        = round(sum(r_ratios) / len(r_ratios), 2) if r_ratios else 0,
        chaotic_skips      = chaotic_tot,
        quiet_skips        = quiet_tot,
        news_skips         = 0,
        pa_no_signal       = pa_miss_tot,
        confidence_blocked = conf_tot,
        risk_blocked       = risk_tot,
        signals            = [asdict(s) for s in all_signals],
    )


# ===========================================================================
# Text report printer
# ===========================================================================

def _print_report(summary: BacktestSummary, days: int, early_mode: bool) -> None:
    """Print a concise backtest summary to the terminal."""
    threshold = CONFIDENCE_EARLY_MODE if early_mode else CONFIDENCE_MIN

    print("\n" + "=" * 60)
    print("  ARCS-FX Backtest Summary")
    print(f"  Period: {days} days | Threshold: {threshold} | Pairs: {summary.pairs_tested}")
    print("=" * 60)
    print(f"  Bars evaluated:      {summary.bars_evaluated:>6}")
    print(f"  Chaotic skips:       {summary.chaotic_skips:>6}")
    print(f"  Quiet skips:         {summary.quiet_skips:>6}")
    print(f"  PA no-signal:        {summary.pa_no_signal:>6}")
    print(f"  Confidence blocked:  {summary.confidence_blocked:>6}")
    print(f"  Risk blocked:        {summary.risk_blocked:>6}")
    print(f"  Signals generated:   {summary.signals_generated:>6}")
    print("-" * 60)
    print(f"  Avg confidence:      {summary.avg_confidence:>6.1f}")
    print(f"  Avg R:R ratio:       {summary.avg_r_ratio:>6.2f}")
    print("-" * 60)
    print("  Signals by regime:")
    for regime, count in sorted(summary.signals_by_regime.items()):
        print(f"    {regime:<12}: {count}")
    print("  Signals by session:")
    for session, count in sorted(summary.signals_by_session.items()):
        print(f"    {session:<12}: {count}")
    print("  Signals by family:")
    for family, count in sorted(summary.signals_by_family.items(), key=lambda x: (-x[1], x[0])):
        print(f"    {family:<20}: {count}")
    print("  Signals by type:")
    for signal_type, count in sorted(summary.signals_by_type.items(), key=lambda x: (-x[1], x[0])):
        print(f"    {signal_type:<20}: {count}")
    print("  Signals by symbol:")
    for symbol, count in sorted(summary.signals_by_symbol.items(), key=lambda x: (-x[1], x[0])):
        print(f"    {symbol:<12}: {count}")
    print("=" * 60)

    # Expected signals per week
    if days > 0:
        per_week = summary.signals_generated / days * 7
        print(f"  Expected signals/week: {per_week:.1f}")
        if summary.signals_by_family:
            print("  Expected signals/week by family:")
            for family, count in sorted(summary.signals_by_family.items(), key=lambda x: (-x[1], x[0])):
                fam_per_week = count / days * 7
                print(f"    {family:<20}: {fam_per_week:.1f}")
    print("=" * 60 + "\n")


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    logging.basicConfig(
        level=logging.ERROR,     # suppress all per-bar noise
        format="%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Keep backtest-level progress visible, silence noisy sub-loggers
    logging.getLogger("backtest").setLevel(logging.INFO)
    logging.getLogger("core.regime_detector").setLevel(logging.ERROR)
    logging.getLogger("engines.price_action").setLevel(logging.ERROR)
    logging.getLogger("engines.confidence_score").setLevel(logging.ERROR)
    logging.getLogger("engines.news_engine").setLevel(logging.ERROR)
    logging.getLogger("risk.risk_manager").setLevel(logging.ERROR)

    parser = argparse.ArgumentParser(description="ARCS-FX Backtest Runner")
    parser.add_argument("--days",     type=int,  default=90,    help="Lookback days (default 90)")
    parser.add_argument("--pair",     type=str,  default=None,  help="Single pair to test")
    parser.add_argument("--early",    action="store_true",       help="Use 80-point early mode threshold")
    parser.add_argument("--verbose",  action="store_true",       help="Print every signal")
    parser.add_argument("--step",     type=int,  default=4,
                        help="Evaluate every Nth H1 bar (default 4 = every 4h). "
                             "Use 1 for exhaustive but slow scan.")
    parser.add_argument("--diagnose", action="store_true",
                        help="PA diagnostic mode: break down why signals are blocked. "
                             "Runs on first pair (or --pair) for --days window at --step 1.")
    args = parser.parse_args()

    pairs = [args.pair.upper()] if args.pair else PAIRS

    if not connect():
        print("[FAIL] Could not connect to MT5.")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Diagnostic mode  (--diagnose)
    # -----------------------------------------------------------------------
    if args.diagnose:
        diag_pairs = [args.pair.upper()] if args.pair else pairs[:2]
        diag_step  = args.step if args.step != 4 else 1   # default step=1 for diagnose
        print("=" * 60)
        print(f"  ARCS-FX PA Diagnostic Mode")
        print(f"  Pairs: {diag_pairs} | Days: {args.days} | Step: {diag_step}")
        print("=" * 60)
        for sym in diag_pairs:
            try:
                _diagnose_pair(sym, args.days, step=diag_step)
            except Exception as exc:
                print(f"[DIAG] {sym} error: {exc}")
        disconnect()
        return

    print("=" * 60)
    print("  ARCS-FX Backtest Runner")
    print(f"  Days: {args.days} | Pairs: {len(pairs)} | EarlyMode: {args.early} | Step: every {args.step}h")
    print("=" * 60)

    # RiskManager instance (reused across pairs to share state)
    risk_manager = RiskManager()

    pair_results = []
    for symbol in pairs:
        try:
            result = _backtest_pair(
                symbol       = symbol,
                days         = args.days,
                early_mode   = args.early,
                verbose      = args.verbose,
                risk_manager = risk_manager,
                step         = args.step,
            )
            pair_results.append(result)
        except Exception as exc:
            logger.exception("[BT] %s: fatal error -- %s", symbol, exc)
            pair_results.append({})

    disconnect()

    summary = _build_summary(pair_results)
    _print_report(summary, args.days, args.early)

    # Save results to file
    os.makedirs("logs", exist_ok=True)
    out_path = f"logs/backtest_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(asdict(summary), f, indent=2, default=str)

    print(f"Full results saved: {out_path}")


if __name__ == "__main__":
    main()
