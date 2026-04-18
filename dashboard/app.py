"""
ARCS-FX Dashboard -- Flask backend.

Standalone: py -3.11 dashboard/app.py
Via bot:    py -3.11 main.py --dashboard
"""

import os
import sys
import json
import sqlite3
import calendar as pycalendar
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, render_template, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH  = DATA_DIR / "trades.db"
STATUS_STALE_AFTER_S = 180

# Ignore corrupted/placeholder closes in dashboard analytics until the
# execution/history reconciliation layer can resolve them properly.
VALID_CLOSED_TRADES_WHERE = (
    "closed_at_utc IS NOT NULL "
    "AND NOT ("
    "UPPER(COALESCE(close_reason, '')) = 'UNKNOWN' "
    "AND COALESCE(CAST(close_price AS REAL), 0) <= 0 "
    "AND ABS(COALESCE(pnl_usd, 0)) < 1e-9"
    ")"
)
ANOMALOUS_CLOSED_TRADES_WHERE = (
    "closed_at_utc IS NOT NULL "
    "AND UPPER(COALESCE(close_reason, '')) = 'UNKNOWN' "
    "AND COALESCE(CAST(close_price AS REAL), 0) <= 0 "
    "AND ABS(COALESCE(pnl_usd, 0)) < 1e-9"
)

app = Flask(__name__, template_folder="templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _db_query(sql, params=()):
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _parse_iso_utc(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _status_is_fresh(ts, stale_after_s=STATUS_STALE_AFTER_S):
    dt = _parse_iso_utc(ts)
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age <= stale_after_s


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def api_overview():
    status = _read_json(DATA_DIR / "bot_status.json")
    fresh = _status_is_fresh(status.get("timestamp"))
    status["status_fresh"] = fresh
    status["bot_running_effective"] = bool(status.get("bot_running")) and fresh

    # Aggregate stats from DB
    stats_rows = _db_query(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, "
        "SUM(pnl_usd) as total_pnl "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE}"
    )
    anomaly_rows = _db_query(
        f"SELECT COUNT(*) as total FROM trades WHERE {ANOMALOUS_CLOSED_TRADES_WHERE}"
    )
    if stats_rows:
        s = stats_rows[0]
        total = s.get("total") or 0
        wins  = s.get("wins")  or 0
        anomalies = (anomaly_rows[0].get("total") if anomaly_rows else 0) or 0
        status["db_stats"] = {
            "total_trades":  total,
            "valid_closed_trades": total,
            "anomaly_trades": anomalies,
            "wins":          wins,
            "losses":        total - wins,
            "win_rate":      round(wins / total, 4) if total > 0 else 0,
            "total_pnl_usd": round(s.get("total_pnl") or 0, 2),
        }

    last_trade_rows = _db_query(
        "SELECT symbol, direction, pnl_usd, won, signal_type, session, "
        "confidence, closed_at_utc, close_reason "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "ORDER BY closed_at_utc DESC LIMIT 1"
    )
    status["last_valid_trade"] = last_trade_rows[0] if last_trade_rows else None

    gate_counts = (status.get("gate_stats") or {}).get("counts") or {}
    status["market_pulse"] = {
        "active_setups": max(0, len(status.get("regimes") or {}) - (gate_counts.get("pa_no_signal") or 0)),
        "quiet_pairs": gate_counts.get("regime_quiet") or 0,
        "chaotic_pairs": gate_counts.get("regime_chaotic") or 0,
        "blocked_setups": (
            (gate_counts.get("confidence_blocked") or 0)
            + (gate_counts.get("risk_blocked") or 0)
            + (gate_counts.get("news_blackout") or 0)
        ),
    }

    # Circuit breakers from risk_state.json
    risk = _read_json(DATA_DIR / "risk_state.json")
    if risk:
        cb = risk.get("circuit_breakers") or risk  # support both layouts
        status["circuit_breakers"] = cb

    return jsonify(status)


@app.route("/api/trades")
def api_trades():
    limit = min(int(request.args.get("limit", 20)), 100)
    rows = _db_query(
        f"SELECT * FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "ORDER BY closed_at_utc DESC LIMIT ?",
        (limit,),
    )
    return jsonify(rows)


@app.route("/api/equity")
def api_equity():
    rows = _db_query(
        "SELECT closed_at_utc, pnl_usd FROM trades "
        f"WHERE {VALID_CLOSED_TRADES_WHERE} ORDER BY closed_at_utc"
    )
    cumulative = 0.0
    points = []
    for r in rows:
        cumulative += r.get("pnl_usd") or 0
        points.append({
            "date":       r["closed_at_utc"],
            "pnl":        round(r.get("pnl_usd") or 0, 2),
            "cumulative": round(cumulative, 2),
        })
    return jsonify(points)


@app.route("/api/performance")
def api_performance():
    by_session = _db_query(
        "SELECT session, COUNT(*) as trades, "
        "SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, "
        "ROUND(AVG(pnl_usd), 2) as avg_pnl "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "GROUP BY session ORDER BY trades DESC"
    )
    by_pair = _db_query(
        "SELECT symbol, COUNT(*) as trades, "
        "SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, "
        "ROUND(AVG(pnl_usd), 2) as avg_pnl "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "GROUP BY symbol ORDER BY trades DESC"
    )
    by_day = _db_query(
        "SELECT day_of_week, COUNT(*) as trades, "
        "SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "GROUP BY day_of_week ORDER BY day_of_week"
    )
    by_signal = _db_query(
        "SELECT signal_type, COUNT(*) as trades, "
        "SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) as wins, "
        "ROUND(AVG(r_ratio), 2) as avg_r "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "GROUP BY signal_type ORDER BY trades DESC"
    )
    return jsonify({
        "by_session": by_session,
        "by_pair":    by_pair,
        "by_day":     by_day,
        "by_signal":  by_signal,
    })


@app.route("/api/calendar")
def api_calendar():
    latest_rows = _db_query(
        "SELECT closed_at_utc "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "ORDER BY closed_at_utc DESC LIMIT 1"
    )
    latest_dt = _parse_iso_utc(latest_rows[0]["closed_at_utc"]) if latest_rows else datetime.now(timezone.utc)
    year = int(request.args.get("year", latest_dt.year))
    month = int(request.args.get("month", latest_dt.month))

    rows = _db_query(
        "SELECT SUBSTR(closed_at_utc, 1, 10) as day, "
        "COUNT(*) as trades, "
        "ROUND(SUM(pnl_usd), 2) as pnl "
        f"FROM trades WHERE {VALID_CLOSED_TRADES_WHERE} "
        "AND SUBSTR(closed_at_utc, 1, 7) = ? "
        "GROUP BY SUBSTR(closed_at_utc, 1, 10) "
        "ORDER BY day",
        (f"{year:04d}-{month:02d}",),
    )

    month_matrix = pycalendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    return jsonify({
        "year": year,
        "month": month,
        "month_label": f"{pycalendar.month_name[month]} {year}",
        "weeks": month_matrix,
        "days": rows,
    })


@app.route("/api/weights")
def api_weights():
    return jsonify(_read_json(DATA_DIR / "weights.json", {}))


@app.route("/api/logs")
def api_logs():
    n = min(int(request.args.get("lines", 80)), 300)
    log_path = LOGS_DIR / "arcs_fx.log"
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [ln.rstrip() for ln in lines[-n:]]})
    except Exception as exc:
        return jsonify({"lines": [f"Log unavailable: {exc}"]})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("ARCS_DASHBOARD_PORT", 5000))
    print(f"")
    print(f"  ARCS-FX Dashboard")
    print(f"  http://localhost:{port}")
    print(f"")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
