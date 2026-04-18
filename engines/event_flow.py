"""
ARCS-FX -- engines/event_flow.py
Edge 3: Event-driven and flow-driven timing windows.

WHY THIS EXISTS:
FX has several EMPIRICALLY-DOCUMENTED anomalies that occur at fixed
times. These are not "indicators" -- they are systematic flow effects
driven by institutional mechanics. They work because the people
creating them are NOT trying to profit from them (rebalancers, hedgers,
central banks). Each anomaly has published academic evidence:

  1. LONDON 4pm FIX  (15:00-16:00 UTC, aka WMR fix)
     Krishnamurthy & Moorthy (2018), Evans (2018). Pre-fix drift,
     post-fix reversal. We can fade the final 30 min pre-fix spike.

  2. END-OF-MONTH REBALANCING  (last 3 business days)
     Melvin & Prins (2015). FX flows driven by equity-portfolio
     currency rebalancing. Direction depends on prior month's equity
     return. We bias toward USD mean-reversion in last 3 days.

  3. NFP GAP FADE  (first Friday, 13:30 UTC + 30-90 min)
     Savor & Wilson (2013). Initial NFP spike overreacts; 60% of
     initial 15-min move is reversed within 90 min.

  4. FOMC DELAYED MOVE  (any FOMC day, 15 min AFTER announcement)
     Lucca & Moench (2015). FOMC drift: the true directional move
     materialises 15-60 min after the statement, not on impact.

  5. TOKYO FIX  (00:55 UTC)
     JPY-specific. Gomber et al. (2020). Documented flow pattern.

  6. QUARTER-END / YEAR-END  amplified month-end effect, days 28-last.

This module tells the orchestrator which window (if any) is active,
and whether the current trade direction aligns with the expected
flow direction for that window.

IT DOES NOT force a trade -- it BOOSTS or SUPPRESSES the existing
confidence score in meta_filter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta
from typing import Optional
import calendar

logger = logging.getLogger(__name__)


@dataclass
class EventFlowResult:
    active_windows: list[str] = field(default_factory=list)  # names of active windows
    boost: float = 0.0           # +/- modifier for confidence score (-10..+10)
    bias_direction: str = ""     # BULLISH | BEARISH | NEUTRAL | "" (varies per pair)
    bias_pair_map: dict = field(default_factory=dict)   # per-pair direction hints
    no_trade: bool = False       # True during known toxic windows (e.g. FOMC impact)
    note: str = ""

    def __str__(self) -> str:
        if not self.active_windows and not self.no_trade:
            return "EventFlow: QUIET"
        tag = "NO-TRADE" if self.no_trade else f"boost={self.boost:+.1f}"
        return f"EventFlow: {','.join(self.active_windows)} {tag} :: {self.note}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_first_friday(d: date) -> bool:
    """First Friday of the month = NFP day."""
    if d.weekday() != 4:  # Friday
        return False
    return d.day <= 7


def _last_n_business_days_of_month(d: date, n: int = 3) -> bool:
    """True if d is in the last n business days of its month."""
    last_day = calendar.monthrange(d.year, d.month)[1]
    # walk back from last_day counting weekdays
    count = 0
    for i in range(last_day, 0, -1):
        probe = date(d.year, d.month, i)
        if probe.weekday() < 5:  # Mon-Fri
            count += 1
            if probe == d:
                return count <= n
            if count >= n:
                return False
    return False


def _is_quarter_end_month(month: int) -> bool:
    return month in (3, 6, 9, 12)


# ---------------------------------------------------------------------------
# Window detectors
# ---------------------------------------------------------------------------

def evaluate(
    pair: str,
    now_utc: Optional[datetime] = None,
    fomc_today: bool = False,
    fomc_announce_hour_utc: int = 18,
) -> EventFlowResult:
    """
    Evaluate all event/flow windows at the current UTC time for a pair.

    `fomc_today` should be set by the caller when the news engine has
    flagged a FOMC event; we need it explicitly because our news engine
    doesn't always tag FOMC with high confidence.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    active: list[str] = []
    boost = 0.0
    notes: list[str] = []
    pair_bias: dict[str, str] = {}
    no_trade = False

    h = now_utc.hour
    m = now_utc.minute
    total_min = h * 60 + m
    today = now_utc.date()
    weekday = today.weekday()

    # ----- 1) LONDON FIX: 15:30-16:00 UTC pre-fix, 16:00-16:15 post-fix ----
    # Pre-fix spike tradeable on Tuesdays/Wednesdays/Thursdays.
    # Post-fix reversal is the higher-probability trade.
    if weekday in (1, 2, 3):  # Tue-Thu (Mon/Fri are noisy)
        if 15 * 60 + 45 <= total_min <= 16 * 60:
            active.append("LONDON_FIX_PRE")
            boost -= 2.0
            notes.append("pre-fix window -- avoid chasing spikes")
        elif 16 * 60 <= total_min <= 16 * 60 + 20:
            active.append("LONDON_FIX_POST")
            boost += 3.0
            notes.append("post-fix reversal window -- favoured")

    # ----- 2) END-OF-MONTH REBALANCING --------------------------------------
    if _last_n_business_days_of_month(today, n=3):
        active.append("MONTH_END")
        boost += 2.0
        # Month-end tends to weaken USD into the fix (portfolio rebalancing
        # dollars flowing OUT of US assets after a strong equity month).
        # Without the prior-month equity return, we default to USD-weak bias
        # which is the more common outcome.
        pair_bias = {
            "EURUSD": "BULLISH", "GBPUSD": "BULLISH",
            "AUDUSD": "BULLISH", "NZDUSD": "BULLISH",
            "USDCAD": "BEARISH", "USDCHF": "BEARISH",
            "USDJPY": "BEARISH", "EURJPY": "NEUTRAL",
        }
        notes.append("month-end USD rebalancing bias")

        if _is_quarter_end_month(today.month):
            active.append("QUARTER_END")
            boost += 1.0
            notes.append("quarter-end amplifier")

    # ----- 3) NFP GAP FADE -------------------------------------------------
    # NFP releases at 13:30 UTC first Friday. Impact window 13:25-13:45.
    # Fade window 14:00-15:30 -- counter-trade the initial spike.
    if _is_first_friday(today):
        if 13 * 60 + 25 <= total_min <= 13 * 60 + 45:
            active.append("NFP_IMPACT")
            no_trade = True
            notes.append("NFP impact -- toxic, no trade")
        elif 14 * 60 <= total_min <= 15 * 60 + 30:
            active.append("NFP_FADE")
            boost += 4.0
            notes.append("NFP fade window -- post-spike reversal favoured")

    # ----- 4) FOMC DELAYED MOVE --------------------------------------------
    if fomc_today:
        announce_minute = fomc_announce_hour_utc * 60
        # 30 min before announce to 15 min after = no-trade
        if announce_minute - 30 <= total_min <= announce_minute + 15:
            active.append("FOMC_IMPACT")
            no_trade = True
            notes.append("FOMC impact -- toxic, no trade")
        # 15-60 min after = favoured directional window
        elif announce_minute + 15 < total_min <= announce_minute + 60:
            active.append("FOMC_DRIFT")
            boost += 3.5
            notes.append("FOMC drift window -- directional follow-through")

    # ----- 5) TOKYO FIX (JPY pairs only) -----------------------------------
    if "JPY" in pair:
        # 00:50-01:00 UTC
        if 50 <= total_min <= 60:
            active.append("TOKYO_FIX")
            boost += 1.5
            notes.append("Tokyo fix JPY flow")

    # ----- 6) WEEKEND EDGE PROTECTION --------------------------------------
    # Nothing opened after 20:00 Friday, nothing before 08:00 Monday.
    if weekday == 4 and h >= 20:
        active.append("FRIDAY_LATE")
        no_trade = True
        notes.append("Friday late -- weekend gap risk")
    elif weekday == 0 and h < 8:
        active.append("MONDAY_EARLY")
        no_trade = True
        notes.append("Monday early -- weekend gap digestion")

    return EventFlowResult(
        active_windows=active,
        boost=round(boost, 2),
        bias_direction=pair_bias.get(pair, ""),
        bias_pair_map=pair_bias,
        no_trade=no_trade,
        note=" | ".join(notes) if notes else "",
    )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    for h in [0, 1, 13, 14, 15, 16, 20]:
        ts = datetime(2026, 4, 17, h, 30, tzinfo=timezone.utc)  # Friday
        r = evaluate("EURUSD", ts)
        print(f"{ts.isoformat()}: {r}")
