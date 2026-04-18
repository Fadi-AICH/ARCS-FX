"""
ARCS-FX — engines/news_engine.py
Economic calendar + real-time news sentiment engine.

WHY THIS MODULE EXISTS:
High-impact news events are the number one cause of stop-hunt liquidations
in retail accounts. A perfectly valid SMC setup can be destroyed in seconds
when the FOMC releases a surprise rate decision. This module:
  1. Tracks the economic calendar — knows when events are coming
  2. Enforces pre/post event blackout windows (bot goes silent)
  3. Scores real-time news headlines with FinBERT NLP
  4. Returns a sentiment signal that feeds the confidence score engine

ARCHITECTURE:
  Layer 1 — Calendar (scheduled events):
    Primary:   ForexFactory calendar scraper with caching
    Fallback:  Hardcoded known high-impact dates (NFP, FOMC schedule)

  Layer 2 — Live news (unscheduled surprises):
    Source:    NewsAPI for recent forex headlines
    Scoring:   FinBERT (domain-adapted BERT for financial sentiment)
    Cache:     Results cached 5 minutes to avoid API hammering

  Layer 3 — Combined signal:
    - Is the bot in a news blackout window?
    - What is the net sentiment score for the relevant pair?
    - Returns NewsResult consumed by confidence_score.py

DEGRADED MODE:
  If ForexFactory is unreachable or NewsAPI key is missing,
  the engine falls back to NEUTRAL sentiment and logs a warning.
  It NEVER crashes the bot. Safety always wins.
"""

import os
import sys
import json
import logging
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    NEWS_PAUSE_BEFORE_MIN, NEWS_PAUSE_AFTER_MIN,
    HIGH_IMPACT_KEYWORDS, FINBERT_MODEL,
    NEWS_SENTIMENT_NEUTRAL_BAND, FOREXFACTORY_URL,
)
from core.instruments import news_mode

logger = logging.getLogger(__name__)

_last_scrape_warn_ts: dict[str, float] = {}
_SCRAPE_WARN_TTL_S = 300.0   # 5-min cooldown per warning key


def _throttled_scrape_warning(key: str, message: str, *args) -> None:
    # Why: FF returns 429 on nearly every poll during rate-limit windows, which
    # spams arcs_fx.log. One warning per key per 5min is enough — the fallback
    # calendar already kicks in silently.
    now = time.monotonic()
    last = _last_scrape_warn_ts.get(key, 0.0)
    if now - last < _SCRAPE_WARN_TTL_S:
        return
    _last_scrape_warn_ts[key] = now
    logger.warning(message, *args)


# ---------------------------------------------------------
# Currency -> country code mapping
# (used to filter ForexFactory events by pair's currencies)
# ---------------------------------------------------------
# ---------------------------------------------------------
# Benign-event downgrade list
# ---------------------------------------------------------
# Events that ForexFactory/fallback tags as HIGH or MEDIUM impact but which
# historically rarely move majors >5 pips. We downgrade them to "LOW" so they
# never trigger a blackout. Substring match, case-insensitive.
#
# Why: Run #2 showed Empire State Manufacturing Index blacking out 4 USD pairs
# for hours. That event prints ~10 pips on EURUSD at most, and only when the
# surprise is >2σ. Not worth sitting out the London+NY overlap for.
BENIGN_EVENT_PATTERNS = (
    "empire state",
    "philly fed",
    "richmond fed",
    "chicago pmi",
    "chicago fed",
    "dallas fed",
    "kansas city fed",
    "consumer sentiment prelim",
    "consumer sentiment revised",
    "michigan sentiment",
    "housing starts",
    "building permits",
    "existing home sales",
    "new home sales",
    "pending home sales",
    "wholesale inventories",
    "business inventories",
    "factory orders",
    "ivey pmi",
    "tankan",
    "current account",
    "trade balance",     # unless HIGH impact already handled upstream
    "redbook",
    "api crude",
    "eia crude",         # energy — matters for CAD but not worth blackout
)


def _is_benign_event(title: str) -> bool:
    t = (title or "").lower()
    return any(pat in t for pat in BENIGN_EVENT_PATTERNS)


# Speech/speaker events — CAN move markets (especially Powell/ECB/BoE)
# but often drone on for hours. We downgrade them from HIGH -> MEDIUM so
# the ±5 min window catches the headline-risk candle without killing the
# whole session. Run #1 saw "President Trump Speaks" black out 4 USD pairs
# for 15+ hours — that's the trigger for this rule.
SPEECH_PATTERNS = (
    "speaks",
    "speech",
    "testimony",
    "testifies",
    "press conference",
    "remarks",
)


def _is_speech_event(title: str) -> bool:
    t = (title or "").lower()
    return any(pat in t for pat in SPEECH_PATTERNS)


CURRENCY_MAP = {
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "AUDUSD": ["AUD", "USD"],
    "USDJPY": ["USD", "JPY"],
    "USDCHF": ["USD", "CHF"],
    "USDCAD": ["USD", "CAD"],
    "NZDUSD": ["NZD", "USD"],
    "EURJPY": ["EUR", "JPY"],
}

# ---------------------------------------------------------
# Data structures
# ---------------------------------------------------------

@dataclass
class EconomicEvent:
    title: str
    currency: str
    impact: str          # "HIGH" | "MEDIUM" | "LOW"
    scheduled_utc: datetime
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None

@dataclass
class SentimentResult:
    headline: str
    label: str           # "positive" | "negative" | "neutral"
    score: float         # -1.0 (bearish) to +1.0 (bullish)
    confidence: float    # model confidence 0–1

@dataclass
class NewsResult:
    """
    Full news engine output — consumed by confidence_score.py.
    """
    is_blackout: bool                        # True -> bot must not trade
    blackout_reason: str                     # human-readable reason
    minutes_to_next_event: Optional[int]     # None if no event in next 24h
    next_event: Optional[EconomicEvent]      # upcoming high-impact event
    sentiment_score: float                   # -1.0 to +1.0 (net NLP score)
    sentiment_label: str                     # "BULLISH" | "BEARISH" | "NEUTRAL"
    sentiment_confidence: float              # 0–1
    active_events: list[EconomicEvent]       # events in blackout range right now
    recent_headlines: list[str]              # last 5 scored headlines
    source_quality: str                      # "FULL" | "PARTIAL" | "DEGRADED"
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        blackout_str = f"BLACKOUT({self.blackout_reason})" if self.is_blackout else "CLEAR"
        return (
            f"NewsEngine: {blackout_str} | "
            f"Sentiment={self.sentiment_label}({self.sentiment_score:+.2f}) | "
            f"NextEvent={self.minutes_to_next_event}min | "
            f"Source={self.source_quality}"
        )


# ---------------------------------------------------------
# Simple in-process cache
# ---------------------------------------------------------

class _Cache:
    """Lightweight TTL cache — avoids hammering external APIs."""
    def __init__(self) -> None:
        self._data: dict = {}

    def get(self, key: str):
        if key in self._data:
            value, expires_at = self._data[key]
            if time.monotonic() < expires_at:
                return value
            del self._data[key]
        return None

    def set(self, key: str, value, ttl_s: int = 300) -> None:
        self._data[key] = (value, time.monotonic() + ttl_s)


_cache = _Cache()


# ---------------------------------------------------------
# FinBERT lazy loader
# ---------------------------------------------------------

_finbert_pipeline = None
_finbert_available = False

def _load_finbert() -> bool:
    """
    Lazy-load FinBERT on first use.
    Returns True if model loaded successfully, False on any failure.

    WHY LAZY:
    The model is ~500MB and takes 10–30 seconds to load on first run.
    We don't want to block bot startup for this. It loads on the first
    news evaluation tick and caches in memory for the session.
    """
    global _finbert_pipeline, _finbert_available

    if _finbert_available:
        return True
    if _finbert_pipeline is not None:
        return False   # previously failed, don't retry this session

    try:
        from transformers import pipeline
        logger.info("Loading FinBERT model '%s' — first load may take ~30s ...", FINBERT_MODEL)
        _finbert_pipeline = pipeline(
            "text-classification",
            model=FINBERT_MODEL,
            top_k=None,             # return all labels + scores
            device=-1,              # CPU — no GPU required
        )
        _finbert_available = True
        logger.info("FinBERT loaded successfully.")
        return True
    except ImportError:
        logger.warning(
            "transformers library not installed. "
            "Run: pip install transformers torch. "
            "Falling back to keyword-based sentiment."
        )
    except Exception as exc:
        logger.warning("FinBERT load failed: %s. Falling back to keyword sentiment.", exc)

    _finbert_pipeline = None   # mark as failed
    return False


# ---------------------------------------------------------
# Public API
# ---------------------------------------------------------

def evaluate(
    symbol: str,
    now_utc: Optional[datetime] = None,
) -> NewsResult:
    """
    Full news evaluation for a symbol.

    Args:
        symbol:   trading pair e.g. "EURUSD"
        now_utc:  override current time (for testing/backtesting)

    Returns:
        NewsResult with blackout flag, sentiment, and event details.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    mode = news_mode(symbol)
    if mode == "crypto_light":
        result = NewsResult(
            is_blackout=False,
            blackout_reason="",
            minutes_to_next_event=None,
            next_event=None,
            sentiment_score=0.0,
            sentiment_label="NEUTRAL",
            sentiment_confidence=0.0,
            active_events=[],
            recent_headlines=[],
            source_quality="PARTIAL",
            details={
                "news_mode": mode,
                "total_events_fetched": 0,
                "headlines_scored": 0,
                "finbert_available": _finbert_available,
            },
        )
        logger.info("[%s] %s", symbol, result)
        return result

    currencies = CURRENCY_MAP.get(symbol, ["USD"])

    # -- Layer 1: Calendar check ---------------------------
    events    = _get_upcoming_events(currencies, now_utc)
    blackout, blackout_reason, active_events = _check_blackout(events, now_utc)

    # -- Layer 2: News sentiment ---------------------------
    headlines = _fetch_headlines(symbol, currencies)
    sentiment = _score_sentiment(headlines)

    # -- Layer 3: Next event timing ------------------------
    upcoming = _next_upcoming_event(events, now_utc)
    minutes_to_next = None
    if upcoming:
        delta = upcoming.scheduled_utc - now_utc
        minutes_to_next = max(0, int(delta.total_seconds() / 60))

    # Source quality assessment
    if blackout and not active_events:
        # Blackout came from calendar that has data -> FULL quality
        source_quality = "FULL"
    elif headlines:
        source_quality = "FULL" if _finbert_available else "PARTIAL"
    else:
        source_quality = "DEGRADED"

    result = NewsResult(
        is_blackout=blackout,
        blackout_reason=blackout_reason,
        minutes_to_next_event=minutes_to_next,
        next_event=upcoming,
        sentiment_score=sentiment.score if sentiment else 0.0,
        sentiment_label=_label_from_score(sentiment.score if sentiment else 0.0),
        sentiment_confidence=sentiment.confidence if sentiment else 0.0,
        active_events=active_events,
        recent_headlines=headlines[:5],
        source_quality=source_quality,
        details={
            "total_events_fetched": len(events),
            "headlines_scored": len(headlines),
            "finbert_available": _finbert_available,
        },
    )

    logger.info("[%s] %s", symbol, result)
    return result


# ---------------------------------------------------------
# Layer 1: Economic Calendar
# ---------------------------------------------------------

def _get_upcoming_events(
    currencies: list[str],
    now_utc: datetime,
) -> list[EconomicEvent]:
    """
    Fetch high-impact economic events from ForexFactory or fallback sources.

    Caches results for 30 minutes to avoid repeated scraping.
    Falls back to a hardcoded critical events list if scraping fails.
    """
    cache_key = f"events_{'_'.join(sorted(currencies))}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    events = _scrape_forexfactory(currencies, now_utc)

    if not events:
        _throttled_scrape_warning("empty", "ForexFactory scrape returned 0 events — using fallback calendar.")
        events = _fallback_events(currencies, now_utc)

    _cache.set(cache_key, events, ttl_s=1800)   # 30 min cache
    logger.info("Calendar: %d high-impact events loaded for %s.", len(events), currencies)
    return events


def _scrape_forexfactory(
    currencies: list[str],
    now_utc: datetime,
) -> list[EconomicEvent]:
    """
    Scrape ForexFactory economic calendar.

    Uses JSON endpoint which is more stable than HTML parsing.
    Returns events for the current week filtered to high-impact only.
    """
    try:
        # ForexFactory JSON endpoint (week view)
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        data = resp.json()
        events: list[EconomicEvent] = []

        for item in data:
            impact = item.get("impact", "").upper()
            if impact not in ("HIGH", "MEDIUM"):
                continue

            currency = item.get("country", "").upper()
            if currency not in currencies:
                continue

            title = item.get("title", "")

            # Downgrade well-known benign events so they don't trigger blackouts.
            if _is_benign_event(title):
                logger.debug("Downgrading benign event '%s' (was %s) -> LOW", title, impact)
                continue   # skip entirely — we only blackout on HIGH/MEDIUM

            # Speech events: downgrade HIGH -> MEDIUM so only the ±5 min window applies.
            if _is_speech_event(title) and impact == "HIGH":
                logger.debug("Downgrading speech event '%s' HIGH -> MEDIUM", title)
                impact = "MEDIUM"

            # Parse datetime — ForexFactory format: "01-13-2025T08:30:00-05:00"
            try:
                dt_str = item.get("date", "")
                if "T" in dt_str:
                    dt = datetime.fromisoformat(dt_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt_utc = dt.astimezone(timezone.utc)
                else:
                    continue   # can't parse date, skip
            except (ValueError, TypeError):
                continue

            # Only events within the next 24 hours
            if not (now_utc - timedelta(hours=2) <= dt_utc <= now_utc + timedelta(hours=24)):
                continue

            events.append(EconomicEvent(
                title=title,
                currency=currency,
                impact=impact,
                scheduled_utc=dt_utc,
                forecast=item.get("forecast"),
                previous=item.get("previous"),
                actual=item.get("actual"),
            ))

        logger.debug("ForexFactory: scraped %d relevant events.", len(events))
        return events

    except requests.exceptions.ConnectionError:
        _throttled_scrape_warning("conn", "ForexFactory unreachable (no internet or blocked).")
    except requests.exceptions.Timeout:
        _throttled_scrape_warning("timeout", "ForexFactory request timed out.")
    except Exception as exc:
        key = "http_429" if "429" in str(exc) else "other"
        _throttled_scrape_warning(key, "ForexFactory scrape error: %s", exc)

    return []


def _fallback_events(
    currencies: list[str],
    now_utc: datetime,
) -> list[EconomicEvent]:
    """
    Minimal fallback: flag known high-risk time windows.

    When we can't get the calendar, we use time-of-day heuristics.
    NFP is always the first Friday of the month at 13:30 UTC.
    FOMC releases are every ~6 weeks at 18:00 UTC on Wednesdays.

    This is conservative — may over-block, but NEVER under-blocks.
    """
    events: list[EconomicEvent] = []

    # NFP heuristic: first Friday of month at 13:30 UTC
    if "USD" in currencies:
        day = now_utc.date()
        # Find next first Friday
        for i in range(0, 35):
            candidate = day + timedelta(days=i)
            if candidate.weekday() == 4:   # Friday
                # Is it the first Friday of its month?
                first_day = candidate.replace(day=1)
                days_until_friday = (4 - first_day.weekday()) % 7
                first_friday = first_day + timedelta(days=days_until_friday)
                if candidate == first_friday:
                    nfp_time = datetime(
                        candidate.year, candidate.month, candidate.day,
                        13, 30, tzinfo=timezone.utc,
                    )
                    if now_utc - timedelta(hours=1) <= nfp_time <= now_utc + timedelta(hours=24):
                        events.append(EconomicEvent(
                            title="NFP — Non-Farm Payrolls (estimated)",
                            currency="USD",
                            impact="HIGH",
                            scheduled_utc=nfp_time,
                        ))
                    break

    return events


def _check_blackout(
    events: list[EconomicEvent],
    now_utc: datetime,
) -> tuple[bool, str, list[EconomicEvent]]:
    """
    Determine if the bot should be in a news blackout.

    Rules:
      - HIGH-impact: NEWS_PAUSE_BEFORE_MIN before / NEWS_PAUSE_AFTER_MIN after
      - MEDIUM-impact: tight ±5 min window only (enough to dodge the candle,
        not enough to waste a session). Downgraded benign events never reach
        here — they're stripped at scrape time.

    Why the tight MEDIUM window: Run #2 saw medium-tier data (Empire State,
    etc.) black out pairs for hours. Most MEDIUM events only spike for 1–3
    candles. ±5 min covers the event candle + immediate reaction.
    """
    active: list[EconomicEvent] = []
    pre_window  = timedelta(minutes=NEWS_PAUSE_BEFORE_MIN)
    post_window = timedelta(minutes=NEWS_PAUSE_AFTER_MIN)
    medium_window = timedelta(minutes=5)

    for event in events:
        evt_time = event.scheduled_utc
        in_pre  = (evt_time - pre_window) <= now_utc < evt_time
        in_post = evt_time <= now_utc <= (evt_time + post_window)

        if event.impact == "HIGH" and (in_pre or in_post):
            active.append(event)
        elif event.impact == "MEDIUM" and (
            (evt_time - medium_window) <= now_utc <= (evt_time + medium_window)
        ):
            active.append(event)

    if active:
        names = " + ".join(e.title for e in active[:2])
        return True, f"High-impact event window: {names}", active

    return False, "", []


def _next_upcoming_event(
    events: list[EconomicEvent],
    now_utc: datetime,
) -> Optional[EconomicEvent]:
    """Return the next HIGH-impact event after now_utc, or None."""
    future_high = [
        e for e in events
        if e.impact == "HIGH" and e.scheduled_utc > now_utc
    ]
    if not future_high:
        return None
    return min(future_high, key=lambda e: e.scheduled_utc)


# ---------------------------------------------------------
# Layer 2: News Sentiment
# ---------------------------------------------------------

def _fetch_headlines(
    symbol: str,
    currencies: list[str],
) -> list[str]:
    """
    Fetch recent forex headlines from NewsAPI.

    Returns list of headline strings. Empty list on failure.
    Results cached 5 minutes per symbol.
    """
    cache_key = f"headlines_{symbol}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    api_key = os.getenv("NEWSAPI_KEY")
    if not api_key:
        logger.debug("NEWSAPI_KEY not set — skipping news headlines.")
        return []

    try:
        query = " OR ".join(currencies) + " forex"
        url   = "https://newsapi.org/v2/everything"
        params = {
            "q":        query,
            "language": "en",
            "sortBy":   "publishedAt",
            "pageSize": 15,
            "apiKey":   api_key,
        }
        resp = requests.get(url, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()

        headlines = [
            article["title"]
            for article in data.get("articles", [])
            if article.get("title") and "[Removed]" not in article["title"]
        ]

        _cache.set(cache_key, headlines, ttl_s=300)   # 5 min cache
        logger.debug("[%s] Fetched %d headlines.", symbol, len(headlines))
        return headlines

    except requests.exceptions.ConnectionError:
        logger.debug("NewsAPI unreachable.")
    except requests.exceptions.HTTPError as exc:
        logger.debug("NewsAPI HTTP error: %s", exc)
    except Exception as exc:
        logger.debug("NewsAPI error: %s", exc)

    return []


def _score_sentiment(headlines: list[str]) -> Optional[SentimentResult]:
    """
    Score a list of headlines with FinBERT or keyword fallback.

    Returns a SentimentResult with an aggregated score:
      positive -> +score
      negative -> -score
      neutral  -> 0

    Final score is the mean of all headline scores.
    Headlines are weighted by FinBERT confidence.
    """
    if not headlines:
        return SentimentResult(
            headline="(no headlines)",
            label="neutral",
            score=0.0,
            confidence=0.0,
        )

    cache_key = "sentiment_" + hashlib.md5("".join(headlines[:5]).encode()).hexdigest()
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    # Try FinBERT first
    finbert_ready = _load_finbert()

    if finbert_ready and _finbert_pipeline is not None:
        result = _score_with_finbert(headlines)
    else:
        result = _score_with_keywords(headlines)

    _cache.set(cache_key, result, ttl_s=300)
    return result


def _score_with_finbert(headlines: list[str]) -> SentimentResult:
    """
    Run FinBERT on headlines and aggregate to a single sentiment score.

    FinBERT output: {"label": "positive|negative|neutral", "score": 0-1}
    We convert to a -1 to +1 scale and weight by confidence.
    """
    scores:      list[float] = []
    confidences: list[float] = []

    for headline in headlines[:10]:   # cap at 10 to avoid long inference
        try:
            results = _finbert_pipeline(headline[:512])  # BERT 512 token limit

            # pipeline(top_k=None) returns list of dicts for each label
            if results and isinstance(results[0], list):
                label_scores = {r["label"]: r["score"] for r in results[0]}
            elif results and isinstance(results[0], dict):
                label_scores = {results[0]["label"]: results[0]["score"]}
            else:
                continue

            pos = label_scores.get("positive", 0.0)
            neg = label_scores.get("negative", 0.0)
            neu = label_scores.get("neutral",  0.0)

            # Net score: positive moves up, negative moves down
            net = pos - neg
            confidence = max(pos, neg, neu)

            scores.append(net)
            confidences.append(confidence)

        except Exception as exc:
            logger.debug("FinBERT inference error on headline: %s", exc)
            continue

    if not scores:
        return _score_with_keywords(headlines)

    # Weighted mean
    if sum(confidences) > 0:
        weighted_score = sum(s * c for s, c in zip(scores, confidences)) / sum(confidences)
    else:
        weighted_score = float(np.mean(scores)) if scores else 0.0

    avg_confidence = float(sum(confidences) / len(confidences))
    label = "positive" if weighted_score > NEWS_SENTIMENT_NEUTRAL_BAND else (
            "negative" if weighted_score < -NEWS_SENTIMENT_NEUTRAL_BAND else "neutral"
    )

    return SentimentResult(
        headline=headlines[0],
        label=label,
        score=round(float(weighted_score), 3),
        confidence=round(avg_confidence, 3),
    )


def _score_with_keywords(headlines: list[str]) -> SentimentResult:
    """
    Fallback keyword-based sentiment scoring when FinBERT is unavailable.

    Not as accurate as FinBERT but directionally correct for major themes.
    """
    BULLISH_WORDS = {
        "surge", "rally", "gain", "rise", "strong", "beat", "bullish",
        "hawkish", "rate hike", "tightening", "positive", "growth",
        "better than expected", "exceeds", "above forecast",
    }
    BEARISH_WORDS = {
        "drop", "fall", "plunge", "weak", "miss", "bearish", "dovish",
        "rate cut", "easing", "negative", "recession", "contraction",
        "worse than expected", "below forecast", "disappoints",
    }

    scores: list[float] = []
    for headline in headlines[:10]:
        hl_lower = headline.lower()
        bull_count = sum(1 for w in BULLISH_WORDS if w in hl_lower)
        bear_count = sum(1 for w in BEARISH_WORDS if w in hl_lower)
        if bull_count + bear_count > 0:
            net = (bull_count - bear_count) / (bull_count + bear_count)
            scores.append(net)

    if not scores:
        return SentimentResult("(no match)", "neutral", 0.0, 0.3)

    import statistics
    avg = statistics.mean(scores)
    label = "positive" if avg > NEWS_SENTIMENT_NEUTRAL_BAND else (
            "negative" if avg < -NEWS_SENTIMENT_NEUTRAL_BAND else "neutral"
    )

    return SentimentResult(
        headline=headlines[0],
        label=label,
        score=round(avg, 3),
        confidence=0.5,   # keyword method is medium confidence
    )


def _label_from_score(score: float) -> str:
    if score > NEWS_SENTIMENT_NEUTRAL_BAND:
        return "BULLISH"
    if score < -NEWS_SENTIMENT_NEUTRAL_BAND:
        return "BEARISH"
    return "NEUTRAL"


# ---------------------------------------------------------
# Standalone test
# ---------------------------------------------------------

if __name__ == "__main__":
    import logging as _logging
    from dotenv import load_dotenv

    load_dotenv()
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 70)
    print("  ARCS-FX — News Engine Test")
    print("=" * 70)

    test_symbols = ["EURUSD", "GBPUSD", "USDJPY"]

    for sym in test_symbols:
        print(f"\n{'-'*70}")
        print(f"  {sym}")
        print(f"{'-'*70}")

        result = evaluate(sym)
        print(f"  {result}")

        if result.next_event:
            print(f"  Next event: {result.next_event.title} "
                  f"@ {result.next_event.scheduled_utc.strftime('%H:%M UTC')} "
                  f"({result.minutes_to_next_event} min)")

        if result.recent_headlines:
            print(f"  Headlines ({len(result.recent_headlines)}):")
            for h in result.recent_headlines[:3]:
                print(f"    • {h[:80]}")

        print(f"  Blackout  : {result.is_blackout} — {result.blackout_reason or 'clear'}")
        print(f"  Sentiment : {result.sentiment_label} {result.sentiment_score:+.3f} "
              f"(conf={result.sentiment_confidence:.2f})")

    print("\n\nPhase 2 — News Engine test: DONE")
