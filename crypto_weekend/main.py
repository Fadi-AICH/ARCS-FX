from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

from .config import (
    ARCHIVE_DIR,
    BREAKEVEN_TRIGGER_R,
    DATA_DIR,
    DB_PATH,
    DASHBOARD_DIR,
    DATA_DIR,
    LOOP_INTERVAL_S,
    LOGS_DIR,
    MAGIC,
    MAX_OPEN_PER_SYMBOL,
    MAX_OPEN_TRADES,
    MICRO_BREAKEVEN_TRIGGER_R,
    MICRO_PARTIAL_TRIGGER_R,
    MICRO_TIME_STOP_MIN,
    MICRO_TRAIL_TRIGGER_R,
    PAIRS,
    PARTIAL_TRIGGER_R,
    PRICE_UNITS,
    PROJECT_NAME,
    REFRESH_INTERVAL_S,
    RISK_PER_TRADE_PCT,
    RUNTIME_LOG,
    SPREAD_LIMITS_UNITS,
    STATUS_PATH,
    TF_H1,
    TF_M1,
    TF_M15,
    TIME_STOP_MIN,
    TRAIL_RATIO,
    TRAIL_TRIGGER_R,
    TRADING_LOG,
    WEB_HOST,
    WEB_PORT,
)
from .mt5_bridge import connect, disconnect
from .storage import TradeStore
from .strategy import find_signal, analyze_context


_RUNNING = True


def _signal_handler(sig, frame):
    global _RUNNING
    _RUNNING = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


class TradingFilter(logging.Filter):
    KEYWORDS = ("OPEN", "CLOSE", "SIGNAL", "Tick complete", "starting up", "Dashboard live")
    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        return any(k in msg for k in self.KEYWORDS)


def _archive_log(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    path.replace(ARCHIVE_DIR / f"{path.stem}_{stamp}{path.suffix}")


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _archive_log(RUNTIME_LOG)
    _archive_log(TRADING_LOG)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s -- %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(RUNTIME_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    th = logging.FileHandler(TRADING_LOG, encoding="utf-8")
    th.setFormatter(fmt)
    th.addFilter(TradingFilter())
    root.addHandler(th)


logger = logging.getLogger(PROJECT_NAME)


TF_MAP = {
    TF_H1: mt5.TIMEFRAME_H1,
    TF_M15: mt5.TIMEFRAME_M15,
    TF_M1: mt5.TIMEFRAME_M1,
}


def _safe_comment(text: str, max_len: int = 18) -> str:
    safe = "".join(c for c in text if c.isascii() and c.isalnum())
    return safe[:max_len] or "ARCSCRYPTO"


def get_ohlcv(symbol: str, timeframe: str, n: int) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, TF_MAP[timeframe], 0, n + 1)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df.iloc[:-1]


def get_tick(symbol: str):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    unit = PRICE_UNITS[symbol]
    spread_price = tick.ask - tick.bid
    return {
        "bid": tick.bid,
        "ask": tick.ask,
        "spread_price": round(spread_price, 6),
        "spread_units": round(spread_price / unit, 3),
        "time": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
    }


class CryptoWeekendBot:
    def __init__(self):
        self.store = TradeStore(DB_PATH)
        self.last_refresh = 0.0
        self.cache: dict[tuple[str, str], pd.DataFrame] = {}
        self.last_signal_bar: dict[tuple[str, str, str], str] = {}
        self.pair_states: dict[str, dict] = {}
        self.open_meta: dict[int, dict] = {}
        self.reject_cooldown_until: dict[str, datetime] = {}

    def refresh(self):
        now = time.monotonic()
        if now - self.last_refresh < REFRESH_INTERVAL_S:
            return
        loaded = 0
        for symbol in PAIRS:
            for tf, bars in ((TF_H1, 320), (TF_M15, 320), (TF_M1, 360)):
                df = get_ohlcv(symbol, tf, bars)
                if df is not None:
                    self.cache[(symbol, tf)] = df
                    loaded += 1
        self.last_refresh = now
        logger.info("Refresh complete: %d/%d timeframes loaded.", loaded, len(PAIRS) * 3)

    def risk_lots(self, symbol: str, entry: float, sl: float) -> float:
        info = mt5.symbol_info(symbol)
        account = mt5.account_info()
        if info is None or account is None:
            return 0.0
        risk_usd = account.balance * (RISK_PER_TRADE_PCT / 100.0)
        dist = abs(entry - sl)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
        tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
        if tick_size <= 0 or tick_value <= 0 or dist <= 0:
            return 0.0
        money_per_lot = (dist / tick_size) * tick_value
        if money_per_lot <= 0:
            return 0.0
        raw = risk_usd / money_per_lot
        step = float(getattr(info, "volume_step", 0.01) or 0.01)
        min_vol = float(getattr(info, "volume_min", step) or step)
        max_vol = float(getattr(info, "volume_max", 100.0) or 100.0)
        lots = max(min_vol, min(max_vol, (raw // step) * step))
        return round(lots, max(len(f"{step:.8f}".rstrip("0").split(".")[-1]), 2))

    def open_counts(self):
        positions = mt5.positions_get() or []
        bot_positions = [p for p in positions if getattr(p, "magic", 0) == MAGIC]
        total = len(bot_positions)
        by_symbol = {}
        for p in bot_positions:
            by_symbol[p.symbol] = by_symbol.get(p.symbol, 0) + 1
        return total, by_symbol

    def _normalize_trade_levels(self, symbol: str, direction: str, entry: float, sl: float, tp: float):
        info = mt5.symbol_info(symbol)
        if info is None:
            return entry, sl, tp

        tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
        point = float(getattr(info, "point", tick_size) or tick_size or 0.0)
        step = tick_size if tick_size > 0 else point
        stops_level = float(getattr(info, "trade_stops_level", 0.0) or 0.0)
        freeze_level = float(getattr(info, "trade_freeze_level", 0.0) or 0.0)
        min_dist = max(stops_level, freeze_level) * point
        if min_dist <= 0:
            min_dist = step * 4
        min_dist = max(min_dist, step * 2)

        risk_dist = max(abs(entry - sl), min_dist)
        reward_dist = max(abs(tp - entry), risk_dist * 1.1, min_dist * 1.1)

        if direction == "BUY":
            sl = min(sl, entry - min_dist)
            tp = max(tp, entry + min_dist)
            sl = min(sl, entry - risk_dist)
            tp = max(tp, entry + reward_dist)
        else:
            sl = max(sl, entry + min_dist)
            tp = min(tp, entry - min_dist)
            sl = max(sl, entry + risk_dist)
            tp = min(tp, entry - reward_dist)

        if step > 0:
            entry = round(entry / step) * step
            sl = round(sl / step) * step
            tp = round(tp / step) * step

        digits = int(getattr(info, "digits", 5) or 5)
        return round(entry, digits), round(sl, digits), round(tp, digits)

    def maybe_open(self, symbol: str, signal):
        total_open, by_symbol = self.open_counts()
        if total_open >= MAX_OPEN_TRADES or by_symbol.get(symbol, 0) >= MAX_OPEN_PER_SYMBOL:
            self.pair_states.setdefault(symbol, {})["last_reason"] = "Position cap reached"
            return

        m1_df = self.cache.get((symbol, TF_M1))
        if m1_df is None or m1_df.empty:
            return
        bar_key = str(m1_df.index[-1])
        dedup_key = (symbol, signal.direction, signal.signal_type)
        if self.last_signal_bar.get(dedup_key) == bar_key:
            self.pair_states.setdefault(symbol, {})["last_reason"] = "Duplicate M1 bar"
            return

        cooldown_until = self.reject_cooldown_until.get(symbol)
        if cooldown_until and cooldown_until > datetime.now(timezone.utc):
            self.pair_states.setdefault(symbol, {})["last_reason"] = "Cooling down after broker reject"
            return

        tick = get_tick(symbol)
        if not tick or tick["spread_units"] > SPREAD_LIMITS_UNITS[symbol]:
            self.pair_states.setdefault(symbol, {})["last_reason"] = "Spread too wide"
            return

        entry = tick["ask"] if signal.direction == "BUY" else tick["bid"]
        entry, sl, tp = self._normalize_trade_levels(symbol, signal.direction, entry, signal.sl, signal.tp)
        lots = self.risk_lots(symbol, entry, sl)
        if lots <= 0:
            self.pair_states.setdefault(symbol, {})["last_reason"] = "Lot sizing failed"
            return

        order_type = mt5.ORDER_TYPE_BUY if signal.direction == "BUY" else mt5.ORDER_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": order_type,
            "price": entry,
            "sl": sl,
            "tp": tp,
            "volume": lots,
            "magic": MAGIC,
            "comment": _safe_comment(f"ACR{signal.signal_type[:12]}"),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = getattr(result, "retcode", None)
            self.pair_states.setdefault(symbol, {})["last_reason"] = f"order failed {retcode}"
            if retcode == 10016:
                self.reject_cooldown_until[symbol] = datetime.now(timezone.utc) + timedelta(seconds=45)
            logger.warning("[%s] OPEN FAIL retcode=%s", symbol, getattr(result, "retcode", None))
            return

        positions = mt5.positions_get(symbol=symbol) or []
        pos = next(
            (
                p for p in positions
                if getattr(p, "magic", 0) == MAGIC and abs(float(p.price_open) - float(entry)) <= max(PRICE_UNITS[symbol] * 4, 0.0001)
            ),
            None,
        )
        if pos is None:
            pos = next((p for p in positions if getattr(p, "magic", 0) == MAGIC), None)
        if pos is None:
            self.pair_states.setdefault(symbol, {})["last_reason"] = "opened but not found"
            return

        trade_id = f"CRYPTO_{symbol}_{pos.ticket}"
        self.open_meta[pos.ticket] = {
            "trade_id": trade_id,
            "symbol": symbol,
            "direction": signal.direction,
            "signal_type": signal.signal_type,
            "entry_price": pos.price_open,
            "initial_sl": sl,
            "tp_price": tp,
            "lots": pos.volume,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "breakeven": False,
            "partial": False,
            "trailing": False,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "close_attempts": 0,
        }
        self.store.log_open({
            "trade_id": trade_id,
            "ticket": pos.ticket,
            "symbol": symbol,
            "direction": signal.direction,
            "signal_type": signal.signal_type,
            "confidence": signal.confidence,
            "session": "WEEKEND_CRYPTO",
            "entry_price": pos.price_open,
            "sl_price": sl,
            "tp_price": tp,
            "lots": pos.volume,
            "opened_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        self.last_signal_bar[dedup_key] = bar_key
        self.pair_states.setdefault(symbol, {})["last_signal"] = signal.signal_type
        self.pair_states.setdefault(symbol, {})["last_reason"] = f"Opened {signal.signal_type}"
        logger.info("[%s] OPEN %s %s lots=%.2f entry=%.5f sl=%.5f tp=%.5f conf=%.1f", symbol, signal.direction, signal.signal_type, pos.volume, pos.price_open, sl, tp, signal.confidence)

    def _modify_sl(self, ticket: int, symbol: str, sl: float, tp: float):
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": sl,
            "tp": tp,
        })
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    def _partial_close(self, pos, volume: float):
        order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = pos.price_current if order_type == mt5.ORDER_TYPE_SELL else pos.price_current
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "type": order_type,
            "volume": volume,
            "price": price,
            "magic": MAGIC,
            "comment": "ACRPartial",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        })
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE

    def manage_positions(self):
        positions = mt5.positions_get() or []
        live = {p.ticket: p for p in positions if getattr(p, "magic", 0) == MAGIC}

        for ticket, meta in list(self.open_meta.items()):
            pos = live.get(ticket)
            if pos is None:
                close_price, pnl = self.lookup_close(ticket, meta["symbol"])
                if close_price == 0.0 and pnl == 0.0:
                    meta["close_attempts"] = meta.get("close_attempts", 0) + 1
                    if meta["close_attempts"] < 8:
                        continue
                self.store.log_close(meta["trade_id"], close_price, pnl, "EXIT", meta["mae_r"], meta["mfe_r"])
                logger.info("[%s] CLOSE %s pnl=$%.2f", meta["symbol"], meta["trade_id"], pnl)
                del self.open_meta[ticket]
                continue

            risk_dist = abs(meta["entry_price"] - meta["initial_sl"])
            if risk_dist <= 0:
                continue
            current_r = ((pos.price_current - meta["entry_price"]) / risk_dist) if meta["direction"] == "BUY" else ((meta["entry_price"] - pos.price_current) / risk_dist)
            meta["mfe_r"] = max(meta["mfe_r"], current_r)
            meta["mae_r"] = min(meta["mae_r"], current_r)

            is_micro = meta.get("signal_type") in {
                "SCALP_MOMENTUM_PULSE",
                "SCALP_EMA_SNAP",
                "SCALP_FLOW",
                "SCALP_FLIP",
            }
            breakeven_trigger = MICRO_BREAKEVEN_TRIGGER_R if is_micro else BREAKEVEN_TRIGGER_R
            partial_trigger = MICRO_PARTIAL_TRIGGER_R if is_micro else PARTIAL_TRIGGER_R
            trail_trigger = MICRO_TRAIL_TRIGGER_R if is_micro else TRAIL_TRIGGER_R
            time_stop_min = MICRO_TIME_STOP_MIN if is_micro else TIME_STOP_MIN

            if not meta["breakeven"] and current_r >= breakeven_trigger:
                if self._modify_sl(ticket, meta["symbol"], meta["entry_price"], pos.tp):
                    meta["breakeven"] = True

            if not meta["partial"] and current_r >= partial_trigger:
                info = mt5.symbol_info(meta["symbol"])
                min_vol = float(getattr(info, "volume_min", 0.01) or 0.01)
                close_lots = round(max(min_vol, pos.volume / 2.0), 2)
                if pos.volume - close_lots >= min_vol and self._partial_close(pos, close_lots):
                    meta["partial"] = True

            if current_r >= trail_trigger:
                if meta["direction"] == "BUY":
                    new_sl = meta["entry_price"] + ((pos.price_current - meta["entry_price"]) * TRAIL_RATIO)
                    if new_sl > pos.sl:
                        self._modify_sl(ticket, meta["symbol"], new_sl, pos.tp)
                else:
                    new_sl = meta["entry_price"] - ((meta["entry_price"] - pos.price_current) * TRAIL_RATIO)
                    if pos.sl == 0 or new_sl < pos.sl:
                        self._modify_sl(ticket, meta["symbol"], new_sl, pos.tp)

            opened = datetime.fromisoformat(meta["opened_at"])
            if meta["mfe_r"] < 0.5 and (datetime.now(timezone.utc) - opened) > timedelta(minutes=time_stop_min):
                order_type = mt5.ORDER_TYPE_SELL if meta["direction"] == "BUY" else mt5.ORDER_TYPE_BUY
                price = pos.price_current
                result = mt5.order_send({
                    "action": mt5.TRADE_ACTION_DEAL,
                    "position": ticket,
                    "symbol": meta["symbol"],
                    "type": order_type,
                    "volume": pos.volume,
                    "price": price,
                    "magic": MAGIC,
                    "comment": "ACRTimeStop",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                })
                if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                    logger.info("[%s] TIME STOP %s", meta["symbol"], meta["trade_id"])

    def lookup_close(self, ticket: int, symbol: str):
        start = datetime.now(timezone.utc) - timedelta(days=3)
        deals = mt5.history_deals_get(start, datetime.now(timezone.utc), group=f"*{symbol}*") or []
        for deal in reversed(deals):
            if getattr(deal, "position_id", 0) == ticket and getattr(deal, "entry", None) == mt5.DEAL_ENTRY_OUT:
                return float(deal.price), float(deal.profit)
        return 0.0, 0.0

    def evaluate_pairs(self):
        self.pair_states = {}
        opened = 0
        pa = 0
        for symbol in PAIRS:
            h1 = self.cache.get((symbol, TF_H1))
            m15 = self.cache.get((symbol, TF_M15))
            m1 = self.cache.get((symbol, TF_M1))
            state = self.pair_states.setdefault(symbol, {})
            if h1 is None or m15 is None or m1 is None:
                state["last_reason"] = "Missing data"
                continue
            tick = get_tick(symbol)
            spread_units = tick["spread_units"] if tick else 999
            spread_price = tick["spread_price"] if tick else 999
            state["spread_units"] = spread_units
            state["spread_price"] = spread_price
            context = analyze_context(symbol, h1, m15)
            state["context"] = context.__dict__

            if spread_units > SPREAD_LIMITS_UNITS[symbol]:
                state["last_reason"] = f"Spread too wide ({spread_units:.1f}u > {SPREAD_LIMITS_UNITS[symbol]:.1f}u)"
                continue

            signal = find_signal(symbol, h1, m15, m1, spread_units)
            if signal.has_signal:
                pa += 1
                state["last_signal"] = signal.signal_type
                state["last_reason"] = signal.notes
                self.maybe_open(symbol, signal)
                if "Opened" in state.get("last_reason", ""):
                    opened += 1
            else:
                state["last_signal"] = "NO_SETUP"
                state["last_reason"] = signal.notes or "No valid M1 scalp"
        logger.info("Tick complete | open_positions=%d | opened=%d | pa=%d", len(self.open_meta), opened, pa)

    def write_status(self):
        account = mt5.account_info()
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_running": True,
            "account": {
                "balance": round(float(account.balance), 2) if account else 0.0,
                "equity": round(float(account.equity), 2) if account else 0.0,
                "margin_free": round(float(account.margin_free), 2) if account else 0.0,
            },
            "open_positions": list(self.open_meta.values()),
            "pair_states": self.pair_states,
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, STATUS_PATH)

    def run(self, with_dashboard: bool = False):
        ok, msg = connect()
        if not ok:
            logger.error(msg)
            return 1
        logger.info("%s connected.", PROJECT_NAME)

        if with_dashboard:
            from .dashboard.app import app
            thread = threading.Thread(target=lambda: app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False), daemon=True)
            thread.start()
            logger.info("Dashboard live at http://%s:%d", WEB_HOST, WEB_PORT)

        while _RUNNING:
            try:
                self.refresh()
                self.manage_positions()
                self.evaluate_pairs()
                self.write_status()
            except Exception as exc:
                logger.exception("Loop error: %s", exc)
            time.sleep(LOOP_INTERVAL_S)

        disconnect()
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dashboard", action="store_true")
    args = parser.parse_args()

    setup_logging()
    logger.info("============================================================")
    logger.info("  %s starting up -- %s UTC", PROJECT_NAME, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("============================================================")

    bot = CryptoWeekendBot()
    raise SystemExit(bot.run(with_dashboard=args.dashboard))


if __name__ == "__main__":
    main()
