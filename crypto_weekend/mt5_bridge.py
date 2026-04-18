import os
import MetaTrader5 as mt5
from dotenv import load_dotenv


def connect(timeout_ms: int = 10000) -> tuple[bool, str]:
    load_dotenv()
    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")

    if not all([login, password, server]):
        return False, "Missing MT5_LOGIN / MT5_PASSWORD / MT5_SERVER in .env"

    if not mt5.initialize(timeout=timeout_ms):
        return False, f"mt5.initialize failed: {mt5.last_error()}"

    if not mt5.login(int(login), password=password, server=server, timeout=timeout_ms):
        err = mt5.last_error()
        mt5.shutdown()
        return False, f"mt5.login failed: {err}"

    return True, "connected"


def disconnect():
    mt5.shutdown()

