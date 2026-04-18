"""
ARCS-PROP Dashboard — Flask backend.

Standalone:  py -3.11 dashboard/prop_app.py
Via bot:     py -3.11 prop_main.py --dashboard

Reads:
  data/prop_status.json   — live snapshot written by prop_main._write_prop_status
  prop/prop_state.json    — persisted challenge state
  data/prop_trades.db     — trade history (separate DB)
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from prop.prop_config import (
    CHALLENGE_STOP_FLAG, DASHBOARD_PORT,
    FIRM_DAILY_LOSS_PCT, FIRM_MAX_DRAWDOWN_PCT,
    INTERNAL_DAILY_LOSS_PCT, INTERNAL_MAX_DAILY_TRADES,
    INTERNAL_MAX_DRAWDOWN_PCT, PHASE_1_TARGET_PCT, PHASE_2_TARGET_PCT,
    PROP_DB_PATH, PROP_STATUS_PATH, PROP_SYMBOLS,
)

BASE_DIR = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = BASE_DIR / "data"
STATUS_PATH = BASE_DIR / PROP_STATUS_PATH
DB_PATH     = BASE_DIR / PROP_DB_PATH
STOP_FLAG   = BASE_DIR / CHALLENGE_STOP_FLAG

STATUS_STALE_AFTER_S = 180

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _db_query(sql, params=()):
    try:
        if not DB_PATH.exists():
            return []
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _is_stale(ts: str) -> bool:
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() > STATUS_STALE_AFTER_S
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("prop_index.html")


@app.route("/api/prop_overview")
def api_prop_overview():
    status = _read_json(STATUS_PATH, default={})
    fresh = not _is_stale(status.get("timestamp", ""))

    phase       = status.get("phase", "PHASE_1")
    target_pct  = status.get("phase_target_pct", PHASE_1_TARGET_PCT)
    phase_gain  = float(status.get("phase_gain_pct", 0.0))
    day_pnl     = float(status.get("day_pnl_pct",   0.0))
    from_peak   = float(status.get("from_peak_pct", 0.0))

    distances = {
        "phase_target_remaining_pct": round(target_pct - phase_gain, 2),
        "daily_internal_remaining_pct": round(INTERNAL_DAILY_LOSS_PCT + day_pnl, 2),
        "daily_firm_remaining_pct":     round(FIRM_DAILY_LOSS_PCT    + day_pnl, 2),
        "max_dd_internal_remaining_pct": round(INTERNAL_MAX_DRAWDOWN_PCT + from_peak, 2),
        "max_dd_firm_remaining_pct":     round(FIRM_MAX_DRAWDOWN_PCT    + from_peak, 2),
    }

    return jsonify({
        "fresh":     fresh,
        "status":    status,
        "distances": distances,
        "firm": {
            "daily_loss_cap_pct":   FIRM_DAILY_LOSS_PCT,
            "max_drawdown_cap_pct": FIRM_MAX_DRAWDOWN_PCT,
        },
        "internal": {
            "daily_loss_cap_pct":   INTERNAL_DAILY_LOSS_PCT,
            "max_drawdown_cap_pct": INTERNAL_MAX_DRAWDOWN_PCT,
            "max_daily_trades":     INTERNAL_MAX_DAILY_TRADES,
            "phase_1_target_pct":   PHASE_1_TARGET_PCT,
            "phase_2_target_pct":   PHASE_2_TARGET_PCT,
            "pairs":                PROP_SYMBOLS,
        },
        "halt_flag_present": STOP_FLAG.exists(),
    })


@app.route("/api/prop_trades")
def api_prop_trades():
    limit = min(int(request.args.get("limit", 50)), 500)
    rows = _db_query(
        "SELECT * FROM trades ORDER BY opened_at_utc DESC LIMIT ?",
        (limit,),
    )
    return jsonify({"trades": rows, "count": len(rows)})


@app.route("/api/prop_equity")
def api_prop_equity():
    rows = _db_query(
        "SELECT closed_at_utc AS ts, pnl_usd FROM trades "
        "WHERE closed_at_utc IS NOT NULL ORDER BY closed_at_utc ASC",
    )
    equity = []
    running = 0.0
    for r in rows:
        try:
            running += float(r.get("pnl_usd") or 0.0)
            equity.append({"ts": r["ts"], "equity_delta": round(running, 2)})
        except Exception:
            continue
    return jsonify({"points": equity})


@app.route("/api/halt", methods=["POST"])
def api_halt():
    """
    Big red HALT button. Writes the CHALLENGE_STOP flag file; the bot's
    tick loop polls this flag and flattens + halts on the next cycle.
    """
    try:
        STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        STOP_FLAG.write_text(
            f"halted via dashboard @ {datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        return jsonify({"ok": True, "flag": str(STOP_FLAG)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/clear_halt", methods=["POST"])
def api_clear_halt():
    try:
        if STOP_FLAG.exists():
            STOP_FLAG.unlink()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Standalone launch
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PROP_DASHBOARD_PORT", DASHBOARD_PORT))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
