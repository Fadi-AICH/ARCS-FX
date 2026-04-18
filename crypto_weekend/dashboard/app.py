import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template

from ..config import DATA_DIR, DB_PATH, STATUS_PATH

app = Flask(__name__, template_folder="templates")


def _read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _db(sql: str, params=()):
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/overview")
def overview():
    status = _read_json(STATUS_PATH, {})
    summary = _db(
        """
        SELECT COUNT(*) total,
               SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
               SUM(COALESCE(pnl_usd, 0)) total_pnl
        FROM trades
        WHERE closed_at_utc IS NOT NULL
        """
    )[0]
    open_rows = _db("SELECT * FROM trades WHERE closed_at_utc IS NULL ORDER BY opened_at_utc DESC")
    latest_rows = _db("SELECT * FROM trades ORDER BY opened_at_utc DESC LIMIT 12")
    status["summary"] = {
        "total": int(summary.get("total") or 0),
        "wins": int(summary.get("wins") or 0),
        "win_rate": round((summary.get("wins") or 0) / (summary.get("total") or 1), 4) if (summary.get("total") or 0) else 0.0,
        "total_pnl": round(float(summary.get("total_pnl") or 0.0), 2),
    }
    status["open_trade_rows"] = open_rows
    status["recent_rows"] = latest_rows
    return jsonify(status)


@app.route("/api/equity")
def equity():
    rows = _db("SELECT closed_at_utc, pnl_usd FROM trades WHERE closed_at_utc IS NOT NULL ORDER BY closed_at_utc")
    running = 0.0
    pts = []
    for row in rows:
        running += float(row.get("pnl_usd") or 0.0)
        pts.append({"x": row["closed_at_utc"], "y": round(running, 2), "pnl": round(float(row.get("pnl_usd") or 0.0), 2)})
    return jsonify(pts)


@app.route("/api/trades")
def trades():
    return jsonify(_db("SELECT * FROM trades ORDER BY opened_at_utc DESC LIMIT 40"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)

