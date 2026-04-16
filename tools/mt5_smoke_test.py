"""
ARCS-FX — tools/mt5_smoke_test.py

One-shot smoke test: sends a 0.01-lot EURUSD BUY at market, prints the
retcode, then closes the position. Used to verify the Python -> MT5
execution path without waiting for a real signal.

Run:  py -3.11 tools/mt5_smoke_test.py

If the open succeeds (retcode=10009), Algo Trading is enabled and the bot
is cleared to trade. If it fails, the retcode tells us exactly why.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

SYMBOL = "EURUSD"
LOTS   = 0.01        # XM minimum — ~$0.10 risk on a 10-pip stop


def main() -> int:
    login    = int(os.getenv("MT5_LOGIN"))
    password = os.getenv("MT5_PASSWORD")
    server   = os.getenv("MT5_SERVER")

    print(f"Connecting to {server} as {login} ...")
    if not mt5.initialize(login=login, password=password, server=server, timeout=15000):
        print(f"initialize() FAILED: {mt5.last_error()}")
        return 1

    info = mt5.account_info()
    print(f"Connected. Balance={info.balance} Equity={info.equity} TradeAllowed={info.trade_allowed}")

    if not info.trade_allowed:
        print("!! account_info.trade_allowed is False — MT5 terminal still blocks trading.")
        print("   Check: Algo Trading button (red square = ON) + Options > Expert Advisors.")
        mt5.shutdown()
        return 2

    if not mt5.symbol_select(SYMBOL, True):
        print(f"symbol_select({SYMBOL}) failed: {mt5.last_error()}")
        mt5.shutdown()
        return 3

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        print(f"no tick for {SYMBOL}")
        mt5.shutdown()
        return 4

    ask = tick.ask
    print(f"{SYMBOL} tick: bid={tick.bid} ask={tick.ask}")

    open_req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       LOTS,
        "type":         mt5.ORDER_TYPE_BUY,
        "price":        ask,
        "deviation":    20,
        "magic":        20260411,
        "comment":      "ARCSsmoketest",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    print(f"Sending BUY {LOTS} {SYMBOL} @ {ask} ...")
    result = mt5.order_send(open_req)

    if result is None:
        print(f"order_send returned None: {mt5.last_error()}")
        mt5.shutdown()
        return 5

    print(f"retcode={result.retcode} comment='{result.comment}' deal={result.deal} order={result.order}")

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"!! OPEN FAILED — retcode={result.retcode}")
        print("   10027 = AutoTrading disabled by client")
        print("   10030 = Unsupported filling mode (try ORDER_FILLING_FOK)")
        print("   10018 = Market is closed")
        print("   10019 = Not enough money")
        mt5.shutdown()
        return 6

    print("OPEN OK — position is live. Sleeping 2s before close ...")
    time.sleep(2)

    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        print("no open position found to close (odd) — exiting")
        mt5.shutdown()
        return 7

    pos = positions[-1]
    close_tick = mt5.symbol_info_tick(SYMBOL)
    close_req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       pos.volume,
        "type":         mt5.ORDER_TYPE_SELL,
        "position":     pos.ticket,
        "price":        close_tick.bid,
        "deviation":    20,
        "magic":        20260411,
        "comment":      "ARCSsmokeclose",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    print(f"Closing ticket {pos.ticket} ...")
    close_result = mt5.order_send(close_req)
    print(f"close retcode={close_result.retcode} comment='{close_result.comment}'")

    mt5.shutdown()

    if close_result.retcode == mt5.TRADE_RETCODE_DONE:
        print("\nSMOKE TEST PASSED. Bot-to-MT5 execution path is working.")
        return 0

    print("\nOPEN worked but CLOSE failed — partial success.")
    return 8


if __name__ == "__main__":
    sys.exit(main())
