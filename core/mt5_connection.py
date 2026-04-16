"""
ARCS-FX — core/mt5_connection.py
MT5 bridge: initialise, authenticate, health-check, shutdown.

WHY THIS FILE EXISTS:
MetaTrader5 must be running locally and the Python bridge must
be initialised before ANY data or order call can succeed.
This module owns that lifecycle so the rest of the bot never
worries about connection state.

All credentials come from .env — never hardcoded.
"""

import os
import sys
import logging
import time
from typing import Optional

# Ensure the project root is on sys.path so `config` is always importable,
# whether this module is imported or run directly as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5
from dotenv import load_dotenv

from config import MT5_TIMEOUT_MS

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def connect() -> bool:
    """
    Initialise the MT5 bridge and log into the demo account.

    Returns True on success, False on any failure.
    The caller should halt the bot if False is returned — there
    is nothing useful the bot can do without a live connection.

    Steps:
      1. Load credentials from .env
      2. Validate .env values are present
      3. mt5.initialize()  — starts the bridge process
      4. mt5.login()       — authenticates with the broker server
      5. Print account summary for visual confirmation
    """
    load_dotenv()

    login    = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server   = os.getenv("MT5_SERVER")
    env_flag = os.getenv("TRADING_ENV", "demo").lower()

    # -- Guard: refuse to run outside demo until explicitly cleared --
    if env_flag != "demo":
        logger.critical(
            "TRADING_ENV is '%s' — bot only supports 'demo' mode. "
            "Aborting for safety.", env_flag
        )
        return False

    # -- Guard: all three credentials must be present --
    if not all([login, password, server]):
        logger.critical(
            "Missing MT5 credentials in .env. "
            "Expected MT5_LOGIN, MT5_PASSWORD, MT5_SERVER. "
            "Copy .env.template -> .env and fill in your values."
        )
        return False

    try:
        login_int = int(login)
    except ValueError:
        logger.critical("MT5_LOGIN must be an integer, got: '%s'", login)
        return False

    # -- Step 3: Initialise the MT5 bridge --
    logger.info("Initialising MetaTrader5 bridge ...")
    if not mt5.initialize(timeout=MT5_TIMEOUT_MS):
        err = mt5.last_error()
        logger.critical(
            "mt5.initialize() failed — code %s: %s. "
            "Is MetaTrader 5 installed and running?", err[0], err[1]
        )
        return False

    logger.info("MT5 bridge initialised (version %s)", mt5.version())

    # -- Step 4: Authenticate --
    logger.info("Logging into server '%s' with account %s ...", server, login_int)
    authorised = mt5.login(
        login=login_int,
        password=password,
        server=server,
        timeout=MT5_TIMEOUT_MS,
    )

    if not authorised:
        err = mt5.last_error()
        logger.critical(
            "mt5.login() failed — code %s: %s. "
            "Check your credentials and server name.", err[0], err[1]
        )
        mt5.shutdown()
        return False

    # -- Step 5: Print account summary --
    info = mt5.account_info()
    if info is None:
        logger.error("Logged in but mt5.account_info() returned None.")
        mt5.shutdown()
        return False

    logger.info(
        "Connected successfully!\n"
        "  Account  : %s\n"
        "  Name     : %s\n"
        "  Broker   : %s\n"
        "  Server   : %s\n"
        "  Balance  : %.2f %s\n"
        "  Equity   : %.2f %s\n"
        "  Leverage : 1:%s\n"
        "  Type     : %s",
        info.login,
        info.name,
        info.company,
        info.server,
        info.balance,  info.currency,
        info.equity,   info.currency,
        info.leverage,
        _account_type(info.trade_mode),
    )

    return True


def disconnect() -> None:
    """
    Cleanly shut down the MT5 bridge.
    Call this at bot shutdown or before process exit.
    Idempotent — safe to call even if never connected.
    """
    mt5.shutdown()
    logger.info("MT5 bridge shut down.")


def is_connected() -> bool:
    """
    Lightweight health check — returns True if MT5 terminal
    is still reachable and the account info is readable.

    Used by the main loop to detect silent disconnections.
    """
    try:
        return mt5.account_info() is not None
    except Exception:
        return False


def get_account_info() -> Optional[object]:
    """
    Return the MT5 AccountInfo named-tuple, or None on failure.
    Callers can safely access .balance, .equity, .currency, etc.
    """
    info = mt5.account_info()
    if info is None:
        logger.warning("get_account_info() returned None — connection lost?")
    return info


def reconnect(max_retries: int = 3, delay_s: int = 10) -> bool:
    """
    Attempt to reconnect after a detected disconnection.

    Why: MT5 terminal occasionally drops the bridge (e.g. after
    a PC sleep/wake cycle or network blip).  The main loop calls
    this instead of crashing so unattended overnight runs survive.

    Args:
        max_retries: number of attempts before giving up
        delay_s:     seconds to wait between attempts
    Returns:
        True if reconnection succeeded, False after all retries exhausted
    """
    disconnect()  # clean slate — shutdown any partial state
    for attempt in range(1, max_retries + 1):
        logger.warning(
            "Reconnect attempt %d/%d ...", attempt, max_retries
        )
        if connect():
            logger.info("Reconnected on attempt %d.", attempt)
            return True
        if attempt < max_retries:
            time.sleep(delay_s)

    logger.critical(
        "All %d reconnect attempts failed. "
        "Bot will halt — manual intervention required.", max_retries
    )
    return False


# ---------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------

def _account_type(trade_mode: int) -> str:
    """Convert MT5 ACCOUNT_TRADE_MODE int to a readable string."""
    modes = {
        mt5.ACCOUNT_TRADE_MODE_DEMO:    "DEMO",
        mt5.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
        mt5.ACCOUNT_TRADE_MODE_REAL:    "REAL ⚠️",
    }
    return modes.get(trade_mode, f"UNKNOWN({trade_mode})")


# ---------------------------------------------------------
# Standalone test (run: python core/mt5_connection.py)
# ---------------------------------------------------------
if __name__ == "__main__":
    # Add project root to path so `config` is importable when running
    # this file directly (e.g. `python core/mt5_connection.py`)
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # Configure console logging for the test run
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("  ARCS-FX — MT5 Connection Test")
    print("=" * 60)

    if connect():
        print("\n[PASS] Connection established.")
        info = get_account_info()
        if info:
            print(f"[PASS] Account balance: {info.balance:.2f} {info.currency}")
        print(f"[PASS] Health check: {is_connected()}")
        disconnect()
        print("[PASS] Disconnected cleanly.")
        print("\nPhase 1 connection test: PASSED")
    else:
        print("\n[FAIL] Connection failed — check logs above.")
        sys.exit(1)
