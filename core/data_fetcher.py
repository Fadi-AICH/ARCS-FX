"""
ARCS-FX — core/data_fetcher.py
Fetches OHLCV bars and live tick data from MetaTrader 5.

WHY THIS FILE EXISTS:
Every analysis module (regime detector, price action engine,
confidence scorer) needs clean OHLCV DataFrames.  Centralising
all MT5 data calls here means:
  - One place to handle MT5 quirks (timezone, column names)
  - Consistent data shape for every consumer
  - Easy to mock out during unit-tests

All returned DataFrames have a UTC-aware DatetimeIndex and
columns: [open, high, low, close, tick_volume, spread].
"""

import os
import sys
import logging
from datetime import datetime, timezone
from typing import Optional

# Ensure project root is on sys.path whether imported or run directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import MetaTrader5 as mt5

from config import (
    TF_H1, TF_M15, TF_M5,
    CANDLES_H1, CANDLES_M15, CANDLES_M5,
    PAIRS,
)
from core.instruments import price_unit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Timeframe mapping  (config strings -> MT5 constants)
# ---------------------------------------------------------
_TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}

# Default number of candles to fetch per timeframe
_DEFAULT_CANDLES = {
    TF_H1:  CANDLES_H1,
    TF_M15: CANDLES_M15,
    TF_M5:  CANDLES_M5,
}


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def get_ohlcv(
    symbol: str,
    timeframe: str,
    n_candles: Optional[int] = None,
) -> Optional[pd.DataFrame]:
    """
    Fetch the most recent `n_candles` closed bars for `symbol`
    on `timeframe`.

    Args:
        symbol:    e.g. "EURUSD"
        timeframe: config string — "M5", "M15", "H1", etc.
        n_candles: how many bars to fetch; defaults to config value
                   for the given timeframe, or 200 if unknown.

    Returns:
        DataFrame with UTC DatetimeIndex and columns
        [open, high, low, close, tick_volume, spread],
        or None on any failure.

    Why we add 1 and drop the last row:
        MT5 returns the CURRENT forming candle as the last row.
        That candle is incomplete (still building), so every
        analysis module would be looking at a partial bar.
        We drop it and only ever work with closed candles.
    """
    tf_const = _resolve_timeframe(timeframe)
    if tf_const is None:
        return None

    if n_candles is None:
        n_candles = _DEFAULT_CANDLES.get(timeframe, 200)

    # Fetch one extra so we can safely drop the incomplete candle
    rates = mt5.copy_rates_from_pos(symbol, tf_const, 0, n_candles + 1)

    if rates is None or len(rates) == 0:
        err = mt5.last_error()
        logger.error(
            "copy_rates_from_pos(%s, %s) failed — %s: %s",
            symbol, timeframe, err[0], err[1],
        )
        return None

    df = pd.DataFrame(rates)

    # Convert MT5 epoch timestamps -> UTC-aware datetime index
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    df.sort_index(inplace=True)

    # Keep only the columns we care about; rename for clarity
    keep = ["open", "high", "low", "close", "tick_volume", "spread"]
    df = df[[c for c in keep if c in df.columns]]

    # Drop the last (still-forming) candle
    df = df.iloc[:-1]

    if df.empty:
        logger.warning("get_ohlcv(%s, %s) — DataFrame is empty after trim.", symbol, timeframe)
        return None

    logger.debug("get_ohlcv(%s, %s) -> %d candles", symbol, timeframe, len(df))
    return df


def get_tick(symbol: str) -> Optional[dict]:
    """
    Return the latest bid/ask tick for `symbol`.

    Returns a dict with keys: bid, ask, spread_pips, time_utc
    or None on failure.

    WHY: Used by spread filter before every order — must be
    called immediately before entry, not cached.

    NOTE: `spread_pips` is a legacy field name kept for compatibility with
    the rest of the bot. For crypto/instrument-aware symbols it represents
    spread in the instrument's practical price unit, not strictly FX pips.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        err = mt5.last_error()
        logger.error(
            "symbol_info_tick(%s) failed — %s: %s",
            symbol, err[0], err[1],
        )
        return None

    pip_size = _get_pip_size(symbol)
    spread_pips = round((tick.ask - tick.bid) / pip_size, 2) if pip_size else None

    return {
        "bid":         tick.bid,
        "ask":         tick.ask,
        "spread_pips": spread_pips,
        "time_utc":    datetime.fromtimestamp(tick.time, tz=timezone.utc),
    }


def get_symbol_info(symbol: str) -> Optional[object]:
    """
    Return MT5 SymbolInfo for `symbol`, or None on failure.
    Useful for lot size constraints, pip value, min volume, etc.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        logger.error("symbol_info(%s) returned None — symbol not found?", symbol)
    return info


def get_h1(symbol: str) -> Optional[pd.DataFrame]:
    """Convenience wrapper — fetch H1 OHLCV for `symbol`."""
    return get_ohlcv(symbol, TF_H1, CANDLES_H1)


def get_m15(symbol: str) -> Optional[pd.DataFrame]:
    """Convenience wrapper — fetch M15 OHLCV for `symbol`."""
    return get_ohlcv(symbol, TF_M15, CANDLES_M15)


def get_m5(symbol: str) -> Optional[pd.DataFrame]:
    """Convenience wrapper — fetch M5 OHLCV for `symbol`."""
    return get_ohlcv(symbol, TF_M5, CANDLES_M5)


def fetch_all_pairs(timeframe: str) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for every pair in config.PAIRS for a given timeframe.

    Returns a dict: { "EURUSD": DataFrame, "GBPUSD": DataFrame, ... }
    Pairs that fail are omitted — the bot continues with the rest.
    """
    result = {}
    for symbol in PAIRS:
        df = get_ohlcv(symbol, timeframe)
        if df is not None:
            result[symbol] = df
        else:
            logger.warning("Skipping %s on %s — data unavailable.", symbol, timeframe)
    return result


# ---------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------

def _resolve_timeframe(tf_str: str) -> Optional[int]:
    """Convert config timeframe string to MT5 TIMEFRAME_* constant."""
    const = _TF_MAP.get(tf_str.upper())
    if const is None:
        logger.error(
            "Unknown timeframe '%s'. Valid values: %s",
            tf_str, list(_TF_MAP.keys()),
        )
    return const


def _get_pip_size(symbol: str) -> float:
    """
    Return the instrument's practical price unit.

    FX pairs still map to classic pip sizes. Non-FX symbols reuse the
    same downstream field names for compatibility, but the returned value
    is instrument-aware via `core.instruments.price_unit()`.
    """
    info = mt5.symbol_info(symbol)
    return price_unit(symbol, info)


# ---------------------------------------------------------
# Standalone test (run: python core/data_fetcher.py)
# ---------------------------------------------------------
if __name__ == "__main__":
    import sys
    import logging as _logging
    from core.mt5_connection import connect, disconnect

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("  ARCS-FX — Data Fetcher Test")
    print("=" * 60)

    if not connect():
        print("[FAIL] Could not connect to MT5.")
        sys.exit(1)

    # Test 1: OHLCV fetch
    print("\n[TEST 1] Fetching EURUSD H1 ...")
    df = get_h1("EURUSD")
    if df is not None:
        print(f"[PASS] {len(df)} candles fetched.")
        print(df.tail(3).to_string())
    else:
        print("[FAIL] H1 fetch returned None.")

    # Test 2: Live tick
    print("\n[TEST 2] Fetching EURUSD live tick ...")
    tick = get_tick("EURUSD")
    if tick:
        print(f"[PASS] Bid={tick['bid']}  Ask={tick['ask']}  "
              f"Spread={tick['spread_pips']} pips  "
              f"Time={tick['time_utc']}")
    else:
        print("[FAIL] Tick fetch returned None.")

    # Test 3: All pairs on M15
    print("\n[TEST 3] Fetching all pairs on M15 ...")
    all_data = fetch_all_pairs(TF_M15)
    for sym, data in all_data.items():
        print(f"  {sym}: {len(data)} candles  "
              f"last close={data['close'].iloc[-1]:.5f}")

    disconnect()
    print("\nPhase 1 data fetcher test: DONE")
