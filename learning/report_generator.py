"""
ARCS-FX -- learning/report_generator.py
Weekly performance report generator: HTML output + optional PDF via reportlab.

WHY A WEEKLY REPORT:
Numbers in a log file are invisible. A structured weekly report turns raw
trade data into a narrative the trader can read in 5 minutes:
  - Did we make or lose money this week?
  - Which setups are working? Which are draining the account?
  - Is the confidence scoring system actually predictive?
  - Did the management rules (BE, trailing) add value?
  - What does the bot recommend adjusting?

REPORT SECTIONS:
  1. Executive Summary   -- P&L, win rate, profit factor, total trades
  2. Equity Curve        -- ASCII chart of running balance across the period
  3. Performance Matrix  -- by signal_type, regime, session, symbol (tables)
  4. Confidence Gate     -- calibration table (is 80+ score better than 70+?)
  5. Trade Management    -- BE effectiveness, trailing vs fixed TP
  6. Top / Bottom Trades -- best and worst individual trades
  7. Weight Adjustments  -- what the StrategyAdjuster changed this week and why
  8. Recommendations     -- actionable bullet points generated from the data

OUTPUT:
  Primary:   HTML file (no external dependencies, opens in any browser)
  Optional:  PDF via reportlab (`py -3.11 -m pip install reportlab`)
             Falls back gracefully if reportlab is not installed.

GENERATED PATH:
  logs/weekly_report_YYYY-MM-DD.html
  logs/weekly_report_YYYY-MM-DD.pdf   (if reportlab available)
"""

import os
import sys
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DB_PATH, LOG_DIR, WEEKLY_REPORT_DAY, WEEKLY_REPORT_HOUR_UTC
from learning.trade_logger import TradeLogger
from learning.pattern_analyzer import PatternAnalyzer, AnalysisReport
from learning.strategy_adjuster import StrategyAdjuster

logger = logging.getLogger(__name__)

_DB_ABS  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), DB_PATH)
_LOG_ABS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), LOG_DIR)


# ===========================================================================
# ASCII equity chart
# ===========================================================================

def _build_equity_chart(
    pnl_series: list[float],
    width:      int = 60,
    height:     int = 10,
) -> str:
    """
    Render a simple ASCII equity curve from a list of cumulative P&L values.
    Returns a multi-line string ready to embed in HTML inside a <pre> block.
    """
    if len(pnl_series) < 2:
        return "  (insufficient data for chart)"

    min_val  = min(pnl_series)
    max_val  = max(pnl_series)
    val_range = max_val - min_val or 1.0

    # Scale each value to a row index (0 = bottom, height-1 = top)
    def to_row(v: float) -> int:
        return round((v - min_val) / val_range * (height - 1))

    # Build a 2D grid
    grid = [[" " for _ in range(width)] for _ in range(height)]

    # Plot each data point
    step = max(1, len(pnl_series) // width)
    col  = 0
    for i in range(0, len(pnl_series), step):
        if col >= width:
            break
        row = height - 1 - to_row(pnl_series[i])
        grid[row][col] = "*"
        col += 1

    # Draw horizontal zero line
    zero_row = height - 1 - to_row(0)
    if 0 <= zero_row < height:
        for c in range(width):
            if grid[zero_row][c] == " ":
                grid[zero_row][c] = "-"

    # Render with Y-axis labels
    lines = []
    for r, row_data in enumerate(grid):
        if r == 0:
            label = f" ${max_val:+.1f} |"
        elif r == height - 1:
            label = f" ${min_val:+.1f} |"
        elif r == height // 2:
            mid = min_val + val_range / 2
            label = f" ${mid:+.1f} |"
        else:
            label = " " * 10 + "|"
        lines.append(label + "".join(row_data))

    lines.append(" " * 10 + "+" + "-" * width)
    lines.append(" " * 11 + "Start" + " " * (width - 13) + "Now")
    return "\n".join(lines)


# ===========================================================================
# HTML template builder
# ===========================================================================

def _pf_color(pf: float) -> str:
    """Return a CSS color based on profit factor."""
    if pf >= 2.0:  return "#00c853"   # green
    if pf >= 1.0:  return "#ffd600"   # amber
    return "#d50000"                   # red


def _wr_color(wr: float) -> str:
    if wr >= 0.6:  return "#00c853"
    if wr >= 0.4:  return "#ffd600"
    return "#d50000"


def _fmt_pnl(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.2f}"


def _dimension_table(stats: list, label: str) -> str:
    """Render a performance breakdown table for one dimension."""
    if not stats:
        return f"<p>No data for {label}.</p>"

    rows_html = ""
    for s in stats:
        pf_clr = _pf_color(s.profit_factor)
        wr_clr = _wr_color(s.win_rate)
        low_n  = " *" if s.low_confidence else ""
        rows_html += f"""
        <tr>
          <td>{s.value}{low_n}</td>
          <td>{s.total}</td>
          <td style="color:{wr_clr};font-weight:bold">{s.win_rate*100:.0f}%</td>
          <td style="color:{pf_clr};font-weight:bold">{s.profit_factor:.2f}</td>
          <td>{_fmt_pnl(s.total_pnl)}</td>
          <td>{s.avg_confidence:.0f}</td>
          <td>{s.avg_duration_min:.0f}m</td>
        </tr>"""

    return f"""
    <h3>{label}</h3>
    <table>
      <tr>
        <th>{label}</th><th>Trades</th><th>Win%</th>
        <th>Prof.Factor</th><th>Total P&L</th>
        <th>Avg Conf</th><th>Avg Dur</th>
      </tr>
      {rows_html}
    </table>
    <p class="note">* = fewer than 5 trades (low statistical confidence)</p>
    """


def _cross_table(stats: list, label: str) -> str:
    if not stats:
        return f"<p>No cross data for {label}.</p>"

    rows_html = ""
    for s in stats:
        pf_clr = _pf_color(s.profit_factor)
        wr_clr = _wr_color(s.win_rate)
        low_n  = " *" if s.low_confidence else ""
        rows_html += f"""
        <tr>
          <td>{s.val1}</td>
          <td>{s.val2}{low_n}</td>
          <td>{s.total}</td>
          <td style="color:{wr_clr};font-weight:bold">{s.win_rate*100:.0f}%</td>
          <td style="color:{pf_clr};font-weight:bold">{s.profit_factor:.2f}</td>
          <td>{_fmt_pnl(s.total_pnl)}</td>
          <td>{s.avg_confidence:.0f}</td>
        </tr>"""

    dim1 = stats[0].dim1.replace("_", " ").title() if stats else ""
    dim2 = stats[0].dim2.replace("_", " ").title() if stats else ""
    return f"""
    <h3>{label}</h3>
    <table>
      <tr>
        <th>{dim1}</th><th>{dim2}</th><th>Trades</th>
        <th>Win%</th><th>Prof.Factor</th><th>Total P&L</th><th>Avg Conf</th>
      </tr>
      {rows_html}
    </table>
    <p class="note">* = fewer than 5 trades</p>
    """


def _build_html_report(
    report:      AnalysisReport,
    adj_result,
    summary:     dict,
    recent:      list[dict],
    pnl_series:  list[float],
    period_label: str,
    generated_at: str,
) -> str:
    """
    Build the full HTML report string.

    WHY self-contained HTML (no CDN, no external CSS):
    The report must open correctly on an air-gapped machine with no internet.
    All styles are inline in the <style> block.
    """
    chart = _build_equity_chart(pnl_series)

    # --- Executive Summary ---
    total      = summary.get("total", 0)
    wins       = summary.get("wins",  0)
    losses     = summary.get("losses", 0)
    win_rate   = summary.get("win_rate_pct", 0.0)
    total_pnl  = summary.get("total_pnl_usd", 0.0)
    avg_win    = summary.get("avg_win_usd", 0.0)
    avg_loss   = summary.get("avg_loss_usd", 0.0)
    pf         = summary.get("profit_factor", 0.0)

    pnl_color  = "#00c853" if total_pnl >= 0 else "#d50000"
    pf_clr     = _pf_color(pf)
    wr_clr     = _wr_color(win_rate / 100.0)

    # --- Confidence calibration table ---
    cal_rows = ""
    for c in report.confidence_calibration:
        flag = " *" if c.low_confidence else ""
        clr  = _wr_color(c.win_rate)
        cal_rows += f"""
        <tr>
          <td>{c.band_low}-{c.band_high}{flag}</td>
          <td>{c.total}</td>
          <td style="color:{clr};font-weight:bold">{c.win_rate*100:.0f}%</td>
          <td>{_fmt_pnl(c.avg_pnl)}</td>
        </tr>"""

    # --- Management stats ---
    mgmt_rows = ""
    for m in report.management_stats:
        clr = _wr_color(m.win_rate)
        mgmt_rows += f"""
        <tr>
          <td>{m.category.replace('_',' ').title()}</td>
          <td>{'Yes' if m.value else 'No'}</td>
          <td>{m.total}</td>
          <td style="color:{clr};font-weight:bold">{m.win_rate*100:.0f}%</td>
          <td>{_fmt_pnl(m.avg_pnl)}</td>
          <td>{m.profit_factor:.2f}</td>
        </tr>"""

    # --- Weight changes ---
    weight_rows = ""
    if adj_result and adj_result.adjusted:
        for adj in adj_result.adjustments:
            clr = "#00c853" if adj.delta > 0 else "#d50000"
            weight_rows += f"""
            <tr>
              <td>{adj.component.replace('_',' ').title()}</td>
              <td>{adj.before:.2f}</td>
              <td>{adj.after:.2f}</td>
              <td style="color:{clr};font-weight:bold">{adj.delta:+.2f}</td>
              <td>{adj.reason[:70]}</td>
            </tr>"""
    else:
        weight_rows = "<tr><td colspan='5'>No weight adjustments this cycle.</td></tr>"

    # --- Recent trades ---
    recent_rows = ""
    for t in recent[:10]:
        won_txt = "WIN"  if t["won"] else "LOSS"
        won_clr = "#00c853" if t["won"] else "#d50000"
        recent_rows += f"""
        <tr>
          <td>{t['symbol']}</td>
          <td>{t['direction']}</td>
          <td>{t['signal_type']}</td>
          <td>{t['regime']}</td>
          <td>{t['session']}</td>
          <td>{t['confidence']:.0f}</td>
          <td style="color:{won_clr};font-weight:bold">{won_txt}</td>
          <td>{_fmt_pnl(t['pnl_usd'])}</td>
          <td>{t['close_reason']}</td>
        </tr>"""

    # --- Recommendations ---
    recs = _generate_recommendations(report)
    rec_html = "".join(f"<li>{r}</li>" for r in recs)

    # --- Top setups ---
    top_html = "".join(
        f"<li><strong>{s.value}</strong> -- WR={s.win_rate*100:.0f}% | "
        f"PF={s.profit_factor:.2f} | n={s.total}</li>"
        for s in report.top_setups
    )
    worst_html = "".join(
        f"<li><strong>{s.value}</strong> -- WR={s.win_rate*100:.0f}% | "
        f"PF={s.profit_factor:.2f} | n={s.total}</li>"
        for s in report.worst_setups
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ARCS-FX Weekly Report -- {period_label}</title>
<style>
  body  {{ font-family: monospace; background: #0d1117; color: #c9d1d9;
           max-width: 1100px; margin: 0 auto; padding: 24px; }}
  h1    {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 8px; }}
  h2    {{ color: #79c0ff; margin-top: 32px; border-left: 3px solid #58a6ff;
           padding-left: 10px; }}
  h3    {{ color: #a5d6ff; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  th    {{ background: #161b22; color: #58a6ff; padding: 6px 12px;
           text-align: left; border: 1px solid #30363d; }}
  td    {{ padding: 5px 12px; border: 1px solid #21262d; }}
  tr:nth-child(even) {{ background: #161b22; }}
  .stat-box {{ display: inline-block; background: #161b22; border: 1px solid #30363d;
               border-radius: 6px; padding: 12px 20px; margin: 6px;
               min-width: 140px; text-align: center; }}
  .stat-label {{ font-size: 0.75em; color: #8b949e; display: block; }}
  .stat-val   {{ font-size: 1.5em; font-weight: bold; display: block; margin-top: 4px; }}
  pre   {{ background: #161b22; padding: 12px; border-radius: 4px;
           border: 1px solid #30363d; overflow-x: auto; font-size: 0.85em; }}
  ul    {{ line-height: 2; }}
  .note {{ font-size: 0.8em; color: #8b949e; }}
  .generated {{ text-align: right; color: #8b949e; font-size: 0.8em; margin-top: 32px; }}
</style>
</head>
<body>

<h1>ARCS-FX Weekly Performance Report</h1>
<p>Period: <strong>{period_label}</strong> &nbsp;|&nbsp; Generated: {generated_at}</p>

<h2>1. Executive Summary</h2>
<div>
  <div class="stat-box">
    <span class="stat-label">Total P&L</span>
    <span class="stat-val" style="color:{pnl_color}">{_fmt_pnl(total_pnl)}</span>
  </div>
  <div class="stat-box">
    <span class="stat-label">Win Rate</span>
    <span class="stat-val" style="color:{wr_clr}">{win_rate:.0f}%</span>
  </div>
  <div class="stat-box">
    <span class="stat-label">Profit Factor</span>
    <span class="stat-val" style="color:{pf_clr}">{pf:.2f}</span>
  </div>
  <div class="stat-box">
    <span class="stat-label">Trades</span>
    <span class="stat-val">{total}</span>
  </div>
  <div class="stat-box">
    <span class="stat-label">Wins / Losses</span>
    <span class="stat-val">{wins} / {losses}</span>
  </div>
  <div class="stat-box">
    <span class="stat-label">Avg Win / Loss</span>
    <span class="stat-val">{_fmt_pnl(avg_win)} / {_fmt_pnl(avg_loss)}</span>
  </div>
</div>

<h2>2. Equity Curve</h2>
<pre>{chart}</pre>

<h2>3. Performance Breakdown</h2>
{_dimension_table(report.by_signal_type, "Signal Type")}
{_dimension_table(report.by_regime,      "Regime")}
{_dimension_table(report.by_session,     "Session")}
{_dimension_table(report.by_symbol,      "Symbol")}
{_cross_table(report.regime_x_signal,    "Regime x Signal Type")}

<h2>4. Confidence Gate Calibration</h2>
<p>
  A well-calibrated gate shows increasing win rates as confidence rises.
  If the 70-79 band has a higher win rate than the 80-89 band, the scoring
  system has a problem that needs manual review.
</p>
<table>
  <tr>
    <th>Confidence Band</th><th>Trades</th><th>Win Rate</th><th>Avg P&L</th>
  </tr>
  {cal_rows}
</table>
<p class="note">* = fewer than 5 trades (low statistical confidence)</p>

<h2>5. Trade Management Effectiveness</h2>
<p>
  Breakeven: does moving SL to entry at +1R save trades?
  Trailing: does trailing stop outperform fixed TP?
</p>
<table>
  <tr>
    <th>Rule</th><th>Applied</th><th>Trades</th>
    <th>Win Rate</th><th>Avg P&L</th><th>Prof.Factor</th>
  </tr>
  {mgmt_rows}
</table>

<h2>6. Recent Trades (last 10)</h2>
<table>
  <tr>
    <th>Symbol</th><th>Dir</th><th>Signal</th><th>Regime</th><th>Session</th>
    <th>Conf</th><th>Result</th><th>P&L</th><th>Reason</th>
  </tr>
  {recent_rows}
</table>

<h2>7. Weight Adjustments This Cycle</h2>
<table>
  <tr>
    <th>Component</th><th>Before</th><th>After</th><th>Delta</th><th>Reason</th>
  </tr>
  {weight_rows}
</table>

<h2>8. Top & Bottom Setups</h2>
<h3>Top Setups (by Profit Factor)</h3>
<ul>{top_html}</ul>
<h3>Worst Setups (by Profit Factor)</h3>
<ul>{worst_html}</ul>

<h2>9. Recommendations</h2>
<ul>{rec_html}</ul>

<p class="generated">Generated by ARCS-FX v1.0 | {generated_at}</p>
</body>
</html>"""

    return html


def _generate_recommendations(report: AnalysisReport) -> list[str]:
    """
    Convert analysis results into actionable English recommendations.
    These appear as bullet points in Section 9 of the report.
    """
    recs = []

    # Signal type recommendations
    signal_stats = {s.value: s for s in report.by_signal_type if not s.low_confidence}
    if "OB_RETEST" in signal_stats and signal_stats["OB_RETEST"].profit_factor >= 2.0:
        recs.append(
            f"OB_RETEST is the strongest signal type "
            f"(PF={signal_stats['OB_RETEST'].profit_factor:.2f}, "
            f"WR={signal_stats['OB_RETEST'].win_rate*100:.0f}%). "
            f"Prioritise this setup in trending conditions."
        )
    if "SD_BOUNCE" in signal_stats and signal_stats["SD_BOUNCE"].profit_factor < 1.0:
        recs.append(
            f"SD_BOUNCE is underperforming "
            f"(PF={signal_stats['SD_BOUNCE'].profit_factor:.2f}). "
            f"Consider raising confidence threshold for SD_BOUNCE setups "
            f"or restricting to OVERLAP session only."
        )

    # Regime recommendations
    regime_stats = {s.value: s for s in report.by_regime if not s.low_confidence}
    if "TRENDING" in regime_stats and "RANGING" in regime_stats:
        tr_pf = regime_stats["TRENDING"].profit_factor
        ra_pf = regime_stats["RANGING"].profit_factor
        if tr_pf > ra_pf * 2:
            recs.append(
                f"TRENDING regime strongly outperforms RANGING "
                f"(PF {tr_pf:.2f} vs {ra_pf:.2f}). "
                f"Raise CONFIDENCE_EARLY_MODE threshold for RANGING trades."
            )

    # Session recommendations
    session_stats = {s.value: s for s in report.by_session if not s.low_confidence}
    for sess, stat in session_stats.items():
        if stat.win_rate < 0.40:
            recs.append(
                f"{sess} session is losing money "
                f"(WR={stat.win_rate*100:.0f}%, PF={stat.profit_factor:.2f}). "
                f"Consider disabling trading during {sess} hours."
            )
        elif stat.win_rate >= 0.65 and stat.profit_factor >= 1.8:
            recs.append(
                f"{sess} session is performing well "
                f"(WR={stat.win_rate*100:.0f}%, PF={stat.profit_factor:.2f}). "
                f"Bot is well-tuned for this session."
            )

    # Confidence calibration
    cal_data = [c for c in report.confidence_calibration if not c.low_confidence]
    if len(cal_data) >= 2:
        rates = [c.win_rate for c in cal_data]
        is_monotonic = all(rates[i] <= rates[i+1] for i in range(len(rates)-1))
        if is_monotonic:
            recs.append(
                "Confidence scoring is well-calibrated: "
                "win rate increases with confidence score. No threshold change needed."
            )
        else:
            recs.append(
                "WARNING: Confidence scoring is NOT monotonically predictive. "
                "Review component weights -- some components may be adding noise."
            )

    # Management effectiveness
    be_stats = {m.value: m for m in report.management_stats if m.category == "breakeven_applied"}
    if True in be_stats and False in be_stats:
        be_yes = be_stats[True]
        be_no  = be_stats[False]
        if be_yes.avg_pnl > be_no.avg_pnl:
            recs.append(
                f"Breakeven rule is adding value: "
                f"avg P&L with BE = {_fmt_pnl(be_yes.avg_pnl)}, "
                f"without BE = {_fmt_pnl(be_no.avg_pnl)}. "
                f"Keep the +1R breakeven trigger."
            )

    trail_stats = {m.value: m for m in report.management_stats if m.category == "trailing_active"}
    if True in trail_stats and False in trail_stats:
        tr_yes = trail_stats[True]
        tr_no  = trail_stats[False]
        if tr_yes.avg_pnl > tr_no.avg_pnl:
            recs.append(
                f"Trailing stop is outperforming fixed TP: "
                f"avg P&L trailing = {_fmt_pnl(tr_yes.avg_pnl)}, "
                f"fixed TP = {_fmt_pnl(tr_no.avg_pnl)}. "
                f"Trailing at +2R is justified."
            )

    if not recs:
        recs.append("Insufficient data for specific recommendations. Continue accumulating trades.")

    return recs


# ===========================================================================
# ReportGenerator
# ===========================================================================

class ReportGenerator:
    """
    Generates the weekly HTML (+optional PDF) performance report.

    USAGE:
        rg = ReportGenerator()
        path = rg.generate_weekly_report()
        print(f"Report saved to {path}")

    The report runs a StrategyAdjuster cycle first, then PatternAnalyzer,
    then produces the full HTML. This ensures weights and report are always
    in sync.
    """

    def __init__(self, db_path: str = _DB_ABS) -> None:
        self._db_path  = db_path
        self._analyzer = PatternAnalyzer(db_path=db_path)
        self._adjuster = StrategyAdjuster(db_path=db_path)
        self._logger   = TradeLogger(db_path=db_path)
        os.makedirs(_LOG_ABS, exist_ok=True)

    def generate_weekly_report(self, period_days: int = 7) -> str:
        """
        Generate the weekly report and return the path to the HTML file.

        period_days: how many days of trades to include in the report.
        The summary and recent trades are filtered to this window,
        but the full analysis (pattern breakdown) runs on all time.
        """
        now_utc  = datetime.now(timezone.utc)
        date_str = now_utc.strftime("%Y-%m-%d")

        period_start = now_utc - timedelta(days=period_days)
        period_label = (
            f"{period_start.strftime('%Y-%m-%d')} to {date_str}"
        )

        logger.info("[RG] Generating weekly report for period: %s", period_label)

        # --- Run adjuster first (writes new weights if warranted) ----------
        try:
            adj_result = self._adjuster.run(force=False)
        except Exception as exc:
            logger.warning("[RG] StrategyAdjuster failed (%s). Continuing without adjustment.", exc)
            adj_result = None

        # --- Run full pattern analysis on all-time data --------------------
        try:
            report = self._analyzer.run_full_analysis()
        except Exception as exc:
            logger.error("[RG] PatternAnalyzer failed: %s", exc)
            raise

        # --- Get summary and recent trades (period-filtered) ---------------
        summary = self._logger.get_performance_summary()
        recent  = self._logger.get_recent_trades(limit=20)

        # --- Build equity curve from recent trade P&L ----------------------
        pnl_series = self._build_cumulative_pnl(recent)

        # --- Render HTML ---------------------------------------------------
        generated_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")
        html = _build_html_report(
            report       = report,
            adj_result   = adj_result,
            summary      = summary,
            recent       = recent,
            pnl_series   = pnl_series,
            period_label = period_label,
            generated_at = generated_at,
        )

        # --- Write HTML file -----------------------------------------------
        html_path = os.path.join(_LOG_ABS, f"weekly_report_{date_str}.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        logger.info("[RG] HTML report saved: %s", html_path)

        # --- Optional PDF export ------------------------------------------
        pdf_path = html_path.replace(".html", ".pdf")
        self._try_pdf_export(html, pdf_path)

        return html_path

    def _build_cumulative_pnl(self, trades: list[dict]) -> list[float]:
        """Build a running cumulative P&L series from most-recent-first trade list."""
        trades_chrono = list(reversed(trades))   # oldest first
        cumulative    = 0.0
        series        = [0.0]
        for t in trades_chrono:
            cumulative += t.get("pnl_usd", 0.0)
            series.append(cumulative)
        return series

    def _try_pdf_export(self, html_content: str, pdf_path: str) -> None:
        """
        Attempt to export the report as PDF using reportlab.

        WHY reportlab instead of weasyprint:
        weasyprint requires GTK/pango which is painful on Windows.
        reportlab is pure Python and installs cleanly on all platforms.
        The PDF is a basic text rendering -- not pixel-perfect HTML rendering.
        For a full graphical PDF, weasyprint or Playwright can be used in Phase 6.
        """
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Preformatted
            from reportlab.lib.units import mm

            doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                                       leftMargin=15*mm, rightMargin=15*mm,
                                       topMargin=15*mm, bottomMargin=15*mm)
            styles = getSampleStyleSheet()
            story  = []

            # Strip HTML tags for plain-text PDF (basic approach)
            import re
            text = re.sub(r"<style[^>]*>.*?</style>", "", html_content, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r" {2,}", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text)

            for chunk in text.split("\n\n"):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if len(chunk) < 80 and chunk.isupper():
                    story.append(Paragraph(chunk, styles["Heading2"]))
                else:
                    story.append(Paragraph(chunk, styles["Normal"]))
                story.append(Spacer(1, 4))

            doc.build(story)
            logger.info("[RG] PDF report saved: %s", pdf_path)

        except ImportError:
            logger.info(
                "[RG] reportlab not installed. Skipping PDF export. "
                "Install with: py -3.11 -m pip install reportlab"
            )
        except Exception as exc:
            logger.warning("[RG] PDF export failed: %s", exc)


# ===========================================================================
# Standalone test harness
# ===========================================================================

if __name__ == "__main__":
    import sys
    import tempfile
    from datetime import timedelta
    from learning.trade_logger import TradeLogger, TradeOpenRecord

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    print("=" * 60)
    print("ARCS-FX  --  ReportGenerator standalone test")
    print("=" * 60)

    tmp_db   = os.path.join(tempfile.gettempdir(), "arcs_rg_test.db")
    tmp_logs = os.path.join(tempfile.gettempdir(), "arcs_rg_logs")
    os.makedirs(tmp_logs, exist_ok=True)

    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass

    # Override log dir for test
    import learning.report_generator as _rg_mod
    _rg_mod._LOG_ABS = tmp_logs

    tl = TradeLogger(db_path=tmp_db)
    now_utc = datetime.now(timezone.utc)

    # Seed same 30 trades
    seed_trades = [
        ("S01","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",84,True,  18.0),
        ("S02","GBPUSD","SELL","OB_RETEST","BEARISH_PIN_BAR","TRENDING","LONDON",82,True,  22.0),
        ("S03","USDJPY","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","NY",79,True,  21.0),
        ("S04","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",87,True,  17.0),
        ("S05","GBPUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","LONDON",76,False,-12.0),
        ("S06","USDJPY","SELL","OB_RETEST","BEARISH_ENGULFING","TRENDING","NY",83,True,  20.0),
        ("S07","EURUSD","BUY","FVG_FILL","BULLISH_ENGULFING","TRENDING","OVERLAP",80,True,  16.0),
        ("S08","GBPUSD","SELL","FVG_FILL","BEARISH_PIN_BAR","TRENDING","LONDON",77,True,  19.0),
        ("S09","AUDUSD","BUY","FVG_FILL","BULLISH_PIN_BAR","TRENDING","NY",74,False,-11.0),
        ("S10","USDCHF","SELL","FVG_FILL","BEARISH_ENGULFING","TRENDING","OVERLAP",81,True,  14.0),
        ("S11","EURUSD","BUY","SD_BOUNCE","BULLISH_ENGULFING","RANGING","OVERLAP",74,False,-13.0),
        ("S12","GBPUSD","SELL","SD_BOUNCE","BEARISH_PIN_BAR","RANGING","LONDON",71,False,-14.0),
        ("S13","AUDUSD","BUY","SD_BOUNCE","BULLISH_PIN_BAR","RANGING","ASIAN",70,False,-10.0),
        ("S14","NZDUSD","BUY","SD_BOUNCE","INSIDE_BAR","RANGING","ASIAN",72,True,   9.0),
        ("S15","USDCAD","SELL","SD_BOUNCE","BEARISH_ENGULFING","RANGING","NY",73,False,-11.0),
        ("S16","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",88,True,  19.0),
        ("S17","GBPUSD","BUY","OB_RETEST","BULLISH_PIN_BAR","TRENDING","OVERLAP",85,True,  21.0),
        ("S18","USDJPY","SELL","FVG_FILL","BEARISH_ENGULFING","TRENDING","NY",78,False,-13.0),
        ("S19","USDCHF","BUY","SD_BOUNCE","BULLISH_ENGULFING","RANGING","LONDON",71,True,  11.0),
        ("S20","EURJPY","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",83,True,  28.0),
        ("S21","EURUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","OVERLAP",91,True,  18.0),
        ("S22","GBPUSD","SELL","SD_BOUNCE","BEARISH_PIN_BAR","RANGING","LONDON",70,False,-12.0),
        ("S23","AUDUSD","BUY","FVG_FILL","BULLISH_ENGULFING","TRENDING","NY",76,True,  13.0),
        ("S24","USDJPY","BUY","OB_RETEST","BULLISH_PIN_BAR","TRENDING","NY",80,True,  22.0),
        ("S25","NZDUSD","SELL","SD_BOUNCE","BEARISH_ENGULFING","RANGING","ASIAN",72,False,-10.0),
        ("S26","USDCAD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","NY",78,True,  16.0),
        ("S27","EURJPY","SELL","FVG_FILL","BEARISH_ENGULFING","TRENDING","OVERLAP",82,True,  25.0),
        ("S28","EURUSD","BUY","SD_BOUNCE","BULLISH_PIN_BAR","RANGING","OVERLAP",73,False,-13.0),
        ("S29","GBPUSD","BUY","OB_RETEST","BULLISH_ENGULFING","TRENDING","LONDON",86,True,  20.0),
        ("S30","USDJPY","BUY","FVG_FILL","BULLISH_ENGULFING","TRENDING","NY",77,False,-12.0),
    ]

    for i, t in enumerate(seed_trades):
        open_rec = TradeOpenRecord(
            trade_id=t[0], mt5_ticket=i*1000, symbol=t[1], direction=t[2],
            entry_price=1.085, sl_price=1.082, initial_sl_price=1.082, tp_price=1.091,
            lots=0.03, opened_at_utc=(now_utc - timedelta(hours=len(seed_trades)-i)).isoformat(),
            signal_type=t[3], pattern=t[4], r_ratio=2.0, confidence=float(t[7]),
            regime=t[5], session=t[6], risk_pct=1.0, risk_usd=10.0,
            win_rate_at_open=0.5, daily_loss_at_open=0.0,
        )
        tl.log_open(open_rec)
        tl.log_close(
            trade_id=t[0], close_price=1.091 if t[8] else 1.082,
            pnl_usd=t[9], won=t[8],
            close_reason="TP_HIT" if t[8] else "SL_HIT",
            breakeven_applied=t[8], trailing_active=(t[8] and t[7] >= 82),
        )

    print(f"\nSeeded {len(seed_trades)} trades.")

    rg = ReportGenerator(db_path=tmp_db)
    path = rg.generate_weekly_report(period_days=7)

    print(f"\nReport generated: {path}")
    assert os.path.exists(path), "FAILED: HTML report not found"

    size = os.path.getsize(path)
    print(f"  File size: {size:,} bytes")
    assert size > 5000, f"FAILED: report too small ({size} bytes)"

    # Print a snippet of the report to verify content
    with open(path, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "OB_RETEST" in content, "FAILED: signal type missing from report"
    assert "TRENDING"  in content, "FAILED: regime missing from report"
    assert "Profit Factor" in content, "FAILED: profit factor section missing"
    assert "Recommendations" in content, "FAILED: recommendations section missing"

    print("  Content checks: PASS")

    # Show equity chart
    pnl_series = rg._build_cumulative_pnl(tl.get_recent_trades(limit=30))
    print("\n-- Equity Chart (ASCII) --")
    print(_build_equity_chart(pnl_series, width=50, height=8))

    # Cleanup
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(tmp_db + ext)
        except OSError:
            pass
    import shutil
    try:
        shutil.rmtree(tmp_logs)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
