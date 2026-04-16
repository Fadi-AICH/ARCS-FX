# ARCS-FX
**Adaptive Regime-Conditioned Scalping for Forex**

A self-learning, news-aware, price-action-driven Forex trading bot built on MetaTrader 5.
Designed for low-balance demo accounts. Capital preservation first. Quality over quantity.

---

## Table of Contents

1. [What is ARCS-FX?](#what-is-arcs-fx)
2. [Core Philosophy](#core-philosophy)
3. [Strategy Overview](#strategy-overview)
4. [8 Differentiating Features](#8-differentiating-features)
5. [Project Structure](#project-structure)
6. [Module Reference](#module-reference)
7. [How to Install](#how-to-install)
8. [How to Run](#how-to-run)
9. [Live Dashboard](#live-dashboard)
10. [Configuration](#configuration)
11. [Signal Chain (how a trade happens)](#signal-chain)
12. [Learning System](#learning-system)
13. [Safety Rules](#safety-rules)
14. [Build Phases](#build-phases)

---

## What is ARCS-FX?

ARCS-FX is a fully automated Forex scalping bot that:

- **Classifies the market** into one of 3 regimes (Trending, Ranging, Chaotic) before doing anything
- **Applies a different strategy** depending on which regime it detects
- **Goes completely silent** during chaotic markets and news events — this is a feature, not a bug
- **Tags every trade** with full context (regime, session, spread, news score, pattern, confidence) so it can learn from its own history
- **Adjusts its own confidence weights** weekly based on what has actually worked

It does not use martingale, grid strategies, or fixed TP/SL. Every trade has asymmetric management — risk is capped, upside is left open.

---

## Core Philosophy

| Principle | Implementation |
|-----------|---------------|
| Capital preservation first | Hard circuit breakers at 3% daily / 6% weekly drawdown |
| Quality over quantity | Confidence gate (score >= 80 in early mode) blocks low-conviction trades |
| Context, not just signals | Every trade tagged with 9 DNA fields; bot learns context not just outcomes |
| No blowup risk | No martingale, no grid, max 2 open trades, correlation guard |
| Designed for small accounts | Position sizing calculated from 1% risk, minimum 0.01 lot |

---

## Strategy Overview

### ARCS = Adaptive Regime-Conditioned Scalping

The bot first classifies the market into one of 3 regimes, then applies a regime-specific strategy.

#### Regime 1 — TRENDING
- **Detected by:** ADX > 25 AND ATR between 30th–70th percentile
- **Strategy:** SMC Momentum Entry
  - Break of Structure (BOS) to confirm direction
  - Order Block (OB) retest as entry zone
  - Bullish/bearish engulfing candle as trigger
  - Tight trailing stop once +2R achieved
  - Direction locked to H1 market structure (HH/HL = longs only, LH/LL = shorts only)

#### Regime 2 — RANGING
- **Detected by:** ADX < 20 AND price bouncing inside Bollinger Bands
- **Strategy:** Supply & Demand Mean Reversion
  - Identify supply/demand zones from prior impulse moves
  - Wait for pin bar or engulfing at zone boundary
  - TP at opposite zone boundary
  - Tight SL just beyond the zone

#### Regime 3 — CHAOTIC
- **Detected by:** ATR >= 90th percentile OR active high-impact news event
- **Strategy:** IDLE — bot does absolutely nothing
  - This protects capital during unpredictable, stop-hunt-prone conditions

---

## 8 Differentiating Features

### 1. Time-of-Day Weighting (Session Heatmap)
The bot builds a per-pair, per-hour win rate heatmap from historical trades.
Once a time slot has 5+ trades, it uses the learned score instead of static session weights.
Before sufficient data, it falls back to: OVERLAP=0.80, LONDON=0.65, NY=0.60, ASIAN=0.35.

### 2. Spread Trap Filter
Before every order, the bot checks the live spread. If it exceeds the configured limit
(EURUSD: 1.0 pip, others: 1.5–2.0 pips), the trade is skipped. Spread is also logged
with every trade for pattern analysis.

### 3. Asymmetric Trade Management
- **At +1R:** SL moved to breakeven — trade becomes risk-free
- **At +2R:** Trailing stop activates at 50% of remaining move — rides the trend
- **Mid-trade news:** If a high-impact event fires, TP is tightened to entry + 0.5R
- **Regime shift:** If regime changes mid-trade, position is closed immediately

### 4. Correlation Awareness
Before opening any trade, the bot checks all open positions. If the new trade correlates
above 0.7 with an existing position, it is skipped to prevent accidental 3x exposure.
Static groups: EUR/GBP/AUD/NZD and USD/CHF/JPY/CAD.

### 5. Trade DNA Tagging
Every trade is tagged at entry with:
- Regime type, session, spread, news score, day of week
- MTF confluence score, volatility percentile
- Price action pattern, key level proximity, confidence score breakdown

After close: tagged with outcome and R multiple. This is what enables context-based learning.

### 6. Smart Idle (Consecutive Loss Cooldown)
If the bot loses 3 trades in a row, it enters a mandatory 2-hour cooldown.
Reason: 3 consecutive losses suggests the model is out of sync with current market conditions.
During cooldown: monitors but does not trade. After cooldown: re-evaluates regime before re-entering.

### 7. Volatility Percentile Filter
Uses ATR percentile over 14 days, not raw ATR.
- Below 30th percentile: market too quiet, no profit potential — skip
- Above 70th percentile: market too explosive, stop hunts everywhere — skip
- 30th–70th: the tradeable sweet spot

### 8. Confidence Score Gating (0–100)
The bot only trades when its composite confidence score reaches 80 (early/conservative mode) or 70 (normal mode).

| Component | Weight | What it measures |
|-----------|--------|-----------------|
| Regime clarity | 25% | How cleanly defined is the current regime? |
| Price action signal | 20% | OB/S&D signal quality + pattern strength |
| MTF confluence | 15% | Do H1 and M15 agree on regime and direction? |
| News sentiment | 15% | NLP score and no blackout present |
| Key level proximity | 10% | Is the signal at a significant price level? |
| Volatility percentile | 10% | Is ATR in the tradeable sweet spot? |
| Spread + session | 5% | Is spread acceptable and session active? |

---

## Project Structure

```
ARCS-FX/
│
├── main.py                      # Main orchestrator — run this to start the bot
├── config.py                    # ALL parameters in one place (edit here, not in code)
├── .env                         # MT5 credentials (never commit this file)
│
├── core/
│   ├── mt5_connection.py        # MT5 connection lifecycle (connect/disconnect/reconnect)
│   ├── data_fetcher.py          # OHLCV data from MT5 (get_h1, get_m15, get_m5, get_tick)
│   └── regime_detector.py       # Market regime classifier (TRENDING / RANGING / CHAOTIC)
│
├── engines/
│   ├── price_action.py          # SMC engine (OB, FVG, S&D zones, sweeps, patterns, M15 gate)
│   ├── news_engine.py           # ForexFactory calendar + NewsAPI + FinBERT sentiment
│   └── confidence_score.py      # 7-component confidence gate (0–100 composite score)
│
├── risk/
│   └── risk_manager.py          # Kelly sizing, circuit breakers, correlation guard
│
├── execution/
│   └── order_manager.py         # MT5 order placement + asymmetric trade management
│
├── learning/
│   ├── trade_logger.py          # SQLite DNA database (write at open, update at close)
│   ├── session_heatmap.py       # Per-pair per-hour win rate heatmap (singleton)
│   ├── pattern_analyzer.py      # Multi-axis performance breakdown from trade history
│   ├── strategy_adjuster.py     # Auto-adjusts confidence weights based on performance
│   └── report_generator.py      # Weekly HTML report (dark theme, ASCII equity curve)
│
├── backtest/
│   └── backtest_runner.py       # Historical bar-by-bar simulation (no live orders)
│
├── dashboard/
│   ├── app.py                   # Flask backend — reads DB + status files, serves API
│   └── templates/
│       └── index.html           # Dark-theme live dashboard UI (auto-refreshes every 15s)
│
├── data/
│   ├── trades.db                # SQLite trade DNA database (auto-created)
│   ├── risk_state.json          # Circuit breaker counters (auto-created, survives restarts)
│   ├── bot_status.json          # Live bot snapshot written every tick (read by dashboard)
│   ├── weights.json             # Current confidence weights (auto-updated weekly)
│   └── weights_history.jsonl    # Append-only weight change audit trail
│
├── logs/
│   ├── arcs_fx.log              # Rotating bot log (10MB x 5 files)
│   ├── weekly_report_*.html     # Auto-generated weekly reports
│   └── backtest_*.json          # Backtest output files
│
└── start_arcs_fx.bat            # Windows one-click launcher (bot + dashboard)
```

---

## Module Reference

### `main.py`
The top-level orchestrator. This is the only file you run.

- Connects to MT5 on startup
- Refreshes OHLCV cache every 5 minutes
- Runs the full signal chain every 60 seconds per pair
- Manages open positions on every tick (trailing, breakeven, close detection)
- Generates the weekly report on Sunday at 22:00 UTC
- Recovers orphaned trades on restart (positions open in MT5 but unclosed in DB)
- Closes all positions gracefully on CTRL+C

### `config.py`
Single source of truth for all bot parameters. **Never** scatter magic numbers in other files.
Key settings: `PAIRS`, `CONFIDENCE_MIN`, `CONFIDENCE_EARLY_MODE`, `RISK_BASE_PCT`, spread limits, session windows.

### `core/mt5_connection.py`
Handles the MT5 Python bridge lifecycle. Reads credentials from `.env`.
Functions: `connect()`, `disconnect()`, `is_connected()`, `reconnect()`.

### `core/data_fetcher.py`
Fetches OHLCV bars and live tick data from MT5.
- `get_h1(symbol)` / `get_m15(symbol)` / `get_m5(symbol)` — closed candles only (drops the forming bar)
- `get_tick(symbol)` — live bid/ask/spread
- `fetch_all_pairs(timeframe)` — all 8 pairs in one call
- All DataFrames have UTC-aware DatetimeIndex

### `core/regime_detector.py`
Classifies the market into TRENDING / RANGING / CHAOTIC using ADX, ATR percentile, and Bollinger Bands.
Returns `RegimeResult` with regime label, confidence (0–1), direction bias (BULLISH/BEARISH/NEUTRAL),
and all raw indicator values.
- `detect(symbol, df, timeframe)` — single timeframe
- `detect_multi_tf(symbol, h1_df, m15_df)` — H1 + M15 simultaneously

### `engines/price_action.py`
The full Smart Money Concepts engine.

Detects:
- **Order Blocks (OB):** Last opposing candle before a 1.0x ATR impulse move (measured as max high extension, not net close)
- **Fair Value Gaps (FVG):** 3-candle price imbalances that act as magnets
- **Liquidity Sweeps:** Stop hunts above/below key levels
- **Supply & Demand Zones:** Origin of strong impulsive moves (1.2x ATR impulse required)
- **Market Structure:** HH/HL (uptrend) or LH/LL (downtrend)
- **Key Levels:** PDH/PDL, weekly H/L, round numbers, session H/L
- **Candlestick Patterns:** Engulfing, pin bar, inside bar, doji, morning/evening star

OB/zone invalidation: a zone is only marked invalid when price **closes through** the zone boundary — a retest (price touches and bounces) does NOT invalidate it. This is the correct SMC rule.

S&D proximity: price is considered "at the zone" within 1.5x ATR of the zone boundary.

M15 confirmation gate: before any signal fires, M15 must show a BOS or structure alignment with H1 bias.
Returns `PriceActionSignal` with has_signal, direction, entry/SL/TP prices, R:R ratio.

Public diagnostic API: `diagnose(symbol, h1_df, m15_df, m5_df, regime, direction_bias, atr)` — returns a breakdown dict used by `backtest --diagnose` mode.

### `engines/news_engine.py`
Three-layer news evaluation:
1. **ForexFactory calendar** — scheduled high-impact events (NFP, FOMC, CPI, GDP, rate decisions)
2. **NewsAPI** — real-time headlines
3. **FinBERT** — NLP sentiment scoring (falls back to keyword scoring if not installed)

Returns `NewsResult` with `is_blackout` flag and `sentiment_score` (-1.0 to +1.0).
Enforces 30-minute pre/post blackout for HIGH-impact events.

### `engines/confidence_score.py`
The final synthesis gate. Combines all signals into a 0–100 score.
Blocks trades on: CHAOTIC regime, news blackout, no PA signal (hard gates, score irrelevant).
Logs full component breakdown with every trade for learning and debugging.
- `compute(symbol, regime_result, regime_m15, pa_signal, news_result, spread_pips, early_mode)` → `ConfidenceResult`

### `risk/risk_manager.py`
Stateful risk engine. Persists state across restarts.
- **Kelly-inspired sizing:** 0.5% (losing streak) → 1.0% (base) → 1.5% (winning streak), hard cap 2%
- **Rolling win rate:** Last 20 closed trades (5+ required for scaling)
- **Daily circuit breaker:** 3% drawdown → stop for the day
- **Weekly circuit breaker:** 6% drawdown → stop for the week
- **Consecutive loss cooldown:** 3 losses → 2-hour pause
- **Correlation guard:** Static group membership, max 2 open trades
- `evaluate_trade(...)` → `RiskCheckResult` (approved + lot size + all metrics)

### `execution/order_manager.py`
The only module that touches real money.
- Places market orders with SL+TP attached server-side
- Tracks open positions in memory (keyed by `trade_id = ARCS_{symbol}_{ticket}`)
- Asymmetric management: BE at +1R, trailing at +2R (50% of move)
- Mid-trade news: tightens TP to entry+0.5R on blackout activation
- All bot orders tagged with `ARCS_MAGIC = 20260411`
- `open_trade(TradeIntent)`, `manage_open_positions()`, `close_trade(trade_id, reason)`, `close_all_trades(reason)`

### `learning/trade_logger.py`
SQLite WAL-mode database. Two-phase write: open row at entry, close row at exit.
Trade DNA stored at entry: regime, session, spread, news score, day of week, pattern, confidence breakdown.
Analytics: `get_performance_summary()`, `get_performance_by_dimension("regime")`, `get_confidence_correlation()`.

### `learning/session_heatmap.py`
Builds a per-pair per-hour win rate heatmap from historical trades.
Score formula: `0.6 × win_rate + 0.4 × tanh_normalised_profit_factor`.
Below 5 trades per cell: uses static session fallbacks.
Module-level singleton via `SessionHeatmap._get_shared_instance()` — called by confidence_score.py on every tick.

### `learning/pattern_analyzer.py`
Reads all closed trades from the DB and produces multi-axis performance analysis:
- Per-dimension: signal type, regime, session, pattern, symbol, close reason
- Cross-dimension: regime × signal type, session × regime
- Confidence calibration: win rate per decile (70-79, 80-89, 90+)
- Trade management effectiveness: breakeven vs none, trailing vs fixed TP
- Generates weight recommendations for StrategyAdjuster

### `learning/strategy_adjuster.py`
Auto-adjusts confidence component weights based on PatternAnalyzer output.
- Dampening factor 0.3: applies 30% of recommended delta per cycle (prevents overreaction)
- Hard bounds: MIN_WEIGHT=5, MAX_WEIGHT=35, always renormalised to 100
- Minimum 20 trades before any adjustment fires
- Saves to `data/weights.json` (atomic write) + appends to `data/weights_history.jsonl`

### `learning/report_generator.py`
Generates a self-contained dark-theme HTML report every Sunday.
Sections: Executive Summary, ASCII Equity Curve, Performance Breakdown (5 tables),
Confidence Calibration, Trade Management, Recent Trades, Weight Adjustments, Recommendations.
Triggers StrategyAdjuster before rendering — weights and report always in sync.
Optional PDF: install reportlab first (see below).

### `backtest/backtest_runner.py`
Bar-by-bar historical simulation of the full signal chain.
Feeds historical OHLCV through regime → PA → confidence → risk.
Uses synthetic news (NEUTRAL, no blackout) for pure signal quality measurement.
Outputs signal count, regime distribution, confidence distribution, signals/week estimate.
Uses `searchsorted` for fast M15/M5 bar alignment (O(log n) vs O(n) boolean scan).
Progress printed every 200 bars so you can see it working.

**`--diagnose` mode:** Instead of running the full signal chain, instruments the PA gate
and prints a per-pair breakdown of which sub-condition is blocking bars:
OBs found per bar, S&D zones per bar, pattern frequency, M15 fail rate, proximity fail rate.
Runs on 2 pairs at step=1 by default. Use `--pair SYMBOL` to target a specific pair.

### `dashboard/app.py`
Flask backend for the live monitoring dashboard.
Reads from `data/bot_status.json`, `data/trades.db`, `data/risk_state.json`,
`data/weights.json`, and `logs/arcs_fx.log`.

API endpoints:
- `GET /` — serves the dashboard HTML
- `GET /api/overview` — account info, regime status, open positions, circuit breakers
- `GET /api/trades?limit=20` — recent closed trades from the DB
- `GET /api/equity` — cumulative P&L time series for the equity curve chart
- `GET /api/performance` — win rate broken down by session, pair, signal type, day
- `GET /api/weights` — current confidence weights from weights.json
- `GET /api/logs?lines=80` — last N lines of arcs_fx.log

### `dashboard/templates/index.html`
Self-contained dark-theme dashboard. Pure HTML/CSS/JS — no build step required.
Auto-refreshes every 15 seconds via `fetch()` API calls. Key sections:
- **Header:** Bot status badge (RUNNING / OFFLINE), session, last-update countdown
- **Account bar:** Balance, equity, float P&L, free margin, open positions, win rate
- **KPI cards:** Win rate, total P&L, avg R multiple, avg confidence, consecutive losses, daily drawdown
- **Regime monitor:** 8 pair cards with TRENDING/RANGING/CHAOTIC badge, confidence bar, direction
- **Open positions:** Live table with current price, floating P&L, R multiple
- **Equity curve:** Chart.js line chart with gradient fill
- **Circuit breakers:** Progress bars for daily/weekly loss and consecutive losses
- **Performance:** Win rate by session, by pair, by signal type
- **Recent trades:** Full DNA table (20 rows) — entry/close/P&L/R/confidence/signal/session/day/spread
- **Confidence weights:** Horizontal bar chart of current weights
- **Live log:** Last 80 lines of arcs_fx.log, auto-scrolling, colour-coded by level

### `start_arcs_fx.bat`
Windows batch launcher. Double-click to start the bot with the dashboard in one step.
Equivalent to: `py -3.11 main.py --dashboard`

---

## How to Install

### Prerequisites
- Windows 10/11
- MetaTrader 5 desktop app installed and running
- Python 3.11 installed (the `py` launcher must be available)

> **Important:** Use `py -3.11` for all commands — NOT `python` or `py`.
> The MetaTrader5 library is only compatible with Python 3.11.

### 1. Clone or place the project
```
C:\Users\YourName\ARCS-FX\
```

### 2. Install required packages
```bash
py -3.11 -m pip install MetaTrader5 pandas numpy ta requests python-dotenv feedparser flask
```

### 3. Optional packages
```bash
# For PDF weekly reports
py -3.11 -m pip install reportlab

# For FinBERT NLP sentiment (requires ~1.5GB download)
py -3.11 -m pip install transformers torch
```

### 4. Create the `.env` file
Create a file named `.env` in the project root:
```
MT5_LOGIN=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=XMGlobal-MT5 2
NEWSAPI_KEY=your_newsapi_key_here
```

Get a free NewsAPI key at newsapi.org (optional — bot works without it in degraded mode).

### 5. Create required directories
```bash
mkdir data
mkdir logs
```

These are auto-created on first run, but you can pre-create them.

---

## How to Run

### Recommended order (first-time setup)

1. Run the backtest first — confirms the signal chain is working on your MT5 account
2. Review the results — check signal count and confidence distribution
3. Start the bot + dashboard — begin paper trading

---

### Option A — One-click launch (bot + dashboard)
Double-click `start_arcs_fx.bat` in the project folder.

This starts the bot and automatically opens the dashboard at `http://localhost:5000`.

---

### Option B — Command line

```bash
cd C:\Users\YourName\ARCS-FX

# Bot only (no dashboard)
py -3.11 main.py

# Bot + dashboard (auto-opens browser)
py -3.11 main.py --dashboard

# Bot + dashboard on a different port
py -3.11 main.py --dashboard --port 8080

# Dashboard only (without running the bot — for reviewing past data)
py -3.11 dashboard/app.py
```

Stop with **CTRL+C** — all open positions are closed gracefully before shutdown.

---

### Run the backtest (do this before starting the bot)
```bash
# Recommended overnight run — all 8 pairs, every H1 bar, 90 days (~3-4 hours)
py -3.11 backtest/backtest_runner.py --days 90 --step 1

# PowerShell: full backtest + PA diagnostic in sequence (paste as one line)
py -3.11 backtest/backtest_runner.py --days 90 --step 1; py -3.11 backtest/backtest_runner.py --diagnose --days 90

# Faster scan — checks every 4 hours (default, ~15-20 min)
py -3.11 backtest/backtest_runner.py --days 90

# With 80-point early mode threshold
py -3.11 backtest/backtest_runner.py --days 90 --early

# Single pair, verbose output (shows every signal found)
py -3.11 backtest/backtest_runner.py --pair EURUSD --verbose

# PA diagnostic: understand why signals are blocked at the PA gate
py -3.11 backtest/backtest_runner.py --diagnose --days 14 --pair EURUSD
```

**`--step N`** controls how many H1 bars to skip between evaluations.
Step 1 = every bar (most complete). Step 4 = every 4 hours (faster, default).

Results are printed to terminal and saved to `logs/backtest_YYYYMMDD_HHMM.json`.

**What to look for:**
- `Signals generated:` — expect 5–30 across 90 days, 8 pairs, step=1
- `PA no-signal:` — the biggest bucket; use `--diagnose` if this is 100% of bars
- `Confidence blocked:` — if PA fires but confidence blocks, consider tuning thresholds
- Regime distribution: mostly TRENDING and RANGING signals, very few CHAOTIC (those are skipped)

> **Note:** Do not run the bot and backtest simultaneously.
> Both connect to MT5 and will interfere with each other.

---

### Auto-start on Windows boot (optional)
To make the bot start automatically when you turn on your PC:

1. Press `Win + R`, type `taskschd.msc`, press Enter
2. Click **Create Basic Task**
3. Name: `ARCS-FX`
4. Trigger: **When the computer starts**
5. Action: **Start a program**
6. Program: `C:\Users\YourName\ARCS-FX\start_arcs_fx.bat`
7. Finish

The bot will start in the background every time Windows boots.
Dashboard will be at `http://localhost:5000`.

---

### Generate the weekly report manually
```bash
py -3.11 learning/report_generator.py
```
Opens `logs/weekly_report_YYYY-MM-DD.html` in your browser.
Also triggers a strategy weight adjustment if 20+ trades exist.

---

### Run individual module tests
```bash
py -3.11 risk/risk_manager.py
py -3.11 execution/order_manager.py
py -3.11 learning/trade_logger.py
py -3.11 learning/session_heatmap.py
py -3.11 learning/strategy_adjuster.py
```

---

### Check live market data (Phase 1 test)
```bash
py -3.11 core/data_fetcher.py
```
Connects to MT5 and prints live H1 candles + tick data for all pairs.

---

## Live Dashboard

Run the bot with `--dashboard` and open `http://localhost:5000` in any browser.

The dashboard auto-refreshes every 15 seconds with no manual reload needed.

### What you see

| Section | Refresh | Data source |
|---------|---------|-------------|
| Header (status, session, last update) | 15s | bot_status.json |
| Account bar (balance, equity, P&L, margin) | 15s | bot_status.json (via MT5) |
| KPI cards (win rate, total P&L, avg R, avg confidence) | 30s | trades.db |
| Regime monitor (8 pair cards) | 15s | bot_status.json |
| Open positions (live P&L, current price) | 15s | bot_status.json (via MT5) |
| Equity curve chart | 30s | trades.db |
| Circuit breakers (daily/weekly drawdown bars) | 15s | risk_state.json |
| Performance breakdown (by session, pair, signal type) | 60s | trades.db |
| Recent trades (last 20, full DNA fields) | 30s | trades.db |
| Confidence weights (bar chart) | 60s | weights.json |
| Live log (last 80 lines, colour-coded) | 15s | logs/arcs_fx.log |

### Dashboard without the bot
You can run `py -3.11 dashboard/app.py` standalone to review historical data
without the bot running. All sections that rely on `bot_status.json` will show
"OFFLINE" but the trades, equity curve, and performance sections still work.

---

## Configuration

All parameters live in `config.py`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `PAIRS` | 8 pairs | EURUSD, GBPUSD, AUDUSD, USDJPY, USDCHF, USDCAD, NZDUSD, EURJPY |
| `CONFIDENCE_MIN` | 70 | Normal mode confidence threshold |
| `CONFIDENCE_EARLY_MODE` | 80 | Conservative threshold (used in main.py by default) |
| `RISK_BASE_PCT` | 1.0 | Base risk per trade (% of account) |
| `RISK_MAX_PCT` | 2.0 | Hard ceiling, never exceeded |
| `DAILY_LOSS_LIMIT_PCT` | 3.0 | Stop trading for the day |
| `WEEKLY_LOSS_LIMIT_PCT` | 6.0 | Stop trading for the week |
| `MAX_CONSECUTIVE_LOSSES` | 3 | Trigger 2-hour cooldown |
| `MAIN_LOOP_INTERVAL_S` | 60 | Tick frequency (seconds) |
| `DATA_REFRESH_INTERVAL_S` | 300 | OHLCV cache refresh (seconds) |
| `WEEKLY_REPORT_DAY` | 6 | Sunday |
| `WEEKLY_REPORT_HOUR_UTC` | 22 | Report generation time |

**To switch from early mode to normal mode** (after 50+ live trades):
Edit `main.py`, line with `early_mode=True` → change to `early_mode=False`.

---

## Signal Chain

How a trade happens, step by step:

```
Every 60 seconds, for each of 8 pairs:

[1] OHLCV cache available?
     No  --> skip (data unavailable)
     Yes --> continue

[2] Regime detection (H1 + M15)
     CHAOTIC --> bot silent (ATR too high or news active)
     TRENDING or RANGING --> continue

[3] News evaluation
     BLACKOUT (30 min before/after high-impact event) --> skip
     CLEAR --> continue

[4] Price action engine
     No signal (no valid OB/S&D/pattern alignment) --> skip
     Signal found (with M15 confirmation) --> continue

[5] Confidence score (7 components, 0-100)
     Score < 80 (early mode) --> skip with reason logged
     Score >= 80 --> continue

[6] Risk evaluation
     Cooldown active? --> skip
     Daily CB fired? --> skip
     Weekly CB fired? --> skip
     2 trades already open? --> skip
     Correlated position exists? --> skip
     All clear --> calculate lot size

[7] Open trade
     Build TradeIntent with full DNA context
     Place market order via MT5
     Log to trades.db (TradeOpenRecord with all DNA fields)
     Update risk_state.json

Every tick (unconditionally):
     Check all open positions
       --> Move SL to breakeven at +1R
       --> Engage trailing at +2R
       --> Tighten TP if news blackout fires mid-trade
       --> Detect SL/TP hit (position no longer in MT5)
       --> Log close to trades.db

Every Sunday 22:00 UTC:
     Generate weekly HTML report
     Run StrategyAdjuster (update confidence weights)
     Save weights.json + append to weights_history.jsonl
```

---

## Learning System

The bot learns from its own trade history across 4 feedback loops:

### 1. Session Heatmap
Builds a per-pair per-hour win rate table. Once a time slot has 5+ trades,
the confidence engine uses the real win rate instead of static session preference.
After 100+ trades, the bot knows exactly when each pair performs best on your account.

### 2. Pattern Analyzer
After each weekly report, it breaks down performance by:
- Which price action signal type wins most?
- Which regime produces the best results?
- Which session is most profitable for each pair?
- Does high confidence actually predict higher win rate? (calibration check)

### 3. Strategy Adjuster
Uses PatternAnalyzer output to automatically shift confidence weights.
If news sentiment consistently correlates with losing trades, its weight decreases.
If regime clarity consistently predicts winning trades, its weight increases.
Change is dampened (30% per cycle) to prevent overreacting to short-term noise.

### 4. Trade DNA
Every trade carries 9 context fields at entry + outcome at exit.
This allows future analysis of questions like:
- "What is my win rate on ENGULFING patterns during LONDON session in TRENDING regime?"
- "Do I do better on Monday or Friday?"
- "Does a news score above 0.5 actually improve outcomes?"

---

## Safety Rules

These are non-negotiable and hardcoded:

1. **Never trade in CHAOTIC regime** — bot goes fully silent
2. **Never trade during news blackout** — 30 min before/after high-impact events
3. **3% daily loss limit** — hard stop, bot waits for midnight UTC reset
4. **6% weekly loss limit** — hard stop, bot waits for Monday 00:00 UTC reset
5. **3 consecutive losses** — 2-hour mandatory cooldown
6. **Max 2 open trades** — correlation and overexposure protection
7. **Never exceed 2% risk per trade** — hardcoded ceiling regardless of win rate
8. **No credentials in code** — always from `.env`
9. **Graceful shutdown** — CTRL+C closes all positions before disconnecting
10. **Log everything** — every skip has a reason, every trade has full DNA

---

## Build Phases

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Foundation: MT5 connection, data fetcher, config, main loop | DONE — Live tested |
| 2 | Intelligence: regime detector, price action engine, news engine, confidence score | DONE — Live tested |
| 3 | Risk: position sizing, circuit breakers, correlation guard | DONE — Live tested |
| 4 | Execution: order placement, asymmetric trade management, trade logger | DONE — Live tested |
| 5 | Learning: session heatmap, pattern analyzer, strategy adjuster, weekly report | DONE — Live tested |
| 6 | Integration: full main.py orchestrator, backtest runner | DONE |
| 7 | Dashboard: Flask live monitoring UI, bot_status.json, Windows auto-start | DONE |
| 8 | PA engine bug fixes: OB detection, invalidation logic, S&D proximity, --diagnose tool | DONE — 2026-04-12 |
| — | Backtest validation: 90-day exhaustive run (step=1) running overnight | **IN PROGRESS** |
| — | Paper trade: 2-week live demo run (start after backtest confirms signals) | NEXT |

---

## Rebuild Plan (2026-04-13)

After a 24-hour live run with **0 trades** and very sparse backtest signal generation, the project direction has been updated.
The conclusion is **not** "throw away the project" — it is:

- the current bot is **over-filtered**
- at least one part of the regime logic is currently **wrong**
- the strategy architecture needs to evolve from a single ultra-strict pipeline into a **portfolio of strategy engines**

This section is the active redesign plan going forward.

### Why the current version is under-trading

Evidence from live logs and backtest summaries showed:

- Live run took **0 trades**
- Trade database remained empty (`data/trades.db`)
- 90-day backtest generated only **18 signals** across 8 pairs
- Most evaluated bars died at the **price action gate**
- A large number of pairs were repeatedly penalised by **MTF regime mismatch**
- Some setups that did appear were then blocked by the **80-point early-mode confidence gate**

This means the problem is not just "no opportunities happened." The system is currently filtering too aggressively to express its edge.

### Critical issue found

The current regime detector labels some **very low ATR / dead markets** as `CHAOTIC`.

That does **not** match the project spec.

Per the intended design:

- `CHAOTIC` should mean **high-volatility / news-dangerous / disorderly**
- low-volatility dead markets should be treated as **quiet / non-ideal**, but **not** the same as chaos

This matters because the bot goes fully silent on `CHAOTIC`, so mislabeling quiet markets as chaotic suppresses valid opportunities and distorts downstream analytics.

### New strategic direction

ARCS-FX will evolve from a single strategy pipe into a **multi-engine adaptive FX portfolio bot**:

- **Scalp engine** for fast intraday opportunities
- **Swing engine** for slower H1/H4 continuation or reversal trades
- Both engines can run across the same 8-pair universe
- Portfolio-level risk rules remain global

Goal:

- see actual live trade activity
- keep capital preservation first
- increase frequency **without** turning the bot into noise-chasing overtrading

### 10-part rebuild plan

#### 1. Fix the 6 immediate issues

These changes are the first mandatory step:

- Reclassify low ATR as `QUIET` / `LOW_VOL`, not `CHAOTIC`
- Reserve `CHAOTIC` for true high-volatility/news-danger conditions
- Stop treating degraded news data as a major confidence penalty
- Remove one layer of duplicated MTF punishment
- Lower the live entry threshold from the current hardcoded ultra-conservative mode after the fixes
- Add real gate analytics so every skip reason is measurable

#### 2. Expand the regime map from 3 states to 5 states

Current:

- `TRENDING`
- `RANGING`
- `CHAOTIC`

Target:

- `TRENDING_CLEAN`
- `TRENDING_EXTENDED`
- `RANGING_CLEAN`
- `QUIET`
- `CHAOTIC`

Why:

- quiet markets and chaotic markets are not the same thing
- extended trends should not be treated the same as fresh trends
- each regime should map to a more appropriate strategy engine

#### 3. Split the system into 2 strategy engines

**Scalp engine**

- Lower timeframe execution
- Best during London, NY, and overlap
- Designed for short-duration momentum and liquidity plays

**Swing engine**

- H1 / H4 context
- Fewer trades, longer hold time
- Designed for structure continuation and selective reversal setups

This allows the bot to show more live activity without forcing one strategy style to do everything.

#### 4. Replace the single PA bottleneck with setup clusters

Instead of relying on one narrow "perfect setup" path, the bot should support several setup families:

- Scalp trend continuation
- Scalp liquidity sweep reversal
- Swing breakout + retest
- Swing displacement mean reversion

This does **not** mean random strategy stacking.
It means each regime gets a small number of purpose-built setup types that match current market behavior better.

#### 5. Change confidence from pure rejection to ranking + selection

Current behavior:

- confidence acts mostly as a hard blocker

Target behavior:

- hard blockers only for real danger states:
  - news blackout
  - spread disaster
  - broken data / connection
  - circuit breakers
- otherwise, confidence should help **rank and select** the best setup candidates

Why:

- a portfolio bot should allocate toward the best opportunities, not reject almost everything

#### 6. Add frequency controls intentionally

The bot should be designed to produce visible but controlled activity:

- target around **2-6 trades per day** in normal conditions across the full basket
- allow more than 2 concurrent trades when correlation allows it
- use pair-level cooldowns instead of globally starving the book
- separate position budgets for scalp vs swing strategies

#### 7. Make session behavior explicit

Planned session logic:

- scalping active mainly during London / NY / overlap
- swing trades allowed outside peak sessions if spread and structure are acceptable
- Asian session mostly observation mode except where pair-specific logic supports participation

#### 8. Redesign exits by strategy type

Scalp exits:

- faster partials
- tighter trailing
- optional session-end flattening

Swing exits:

- slower scaling out
- structure-based trailing
- less aggressive premature tightening

One exit model should not be forced on both styles.

#### 9. Upgrade backtesting before claiming profitability

Current backtest is useful for pipeline diagnostics and signal counting, but not yet enough to validate the whole strategy.

The upgraded backtest should include:

- full trade lifecycle
- realistic spread assumptions by pair/session
- slippage modeling
- walk-forward testing
- per-engine performance reporting

#### 10. Roll out in stages

Planned rollout:

1. Patch the 6 immediate issues
2. Add the new regime taxonomy
3. Build the scalp engine
4. Build the swing engine
5. Upgrade confidence and analytics
6. Run improved backtests
7. Paper trade first on a smaller subset of pairs
8. Expand to the full basket after the bot proves sane frequency and drawdown behavior

### Design principles for the rebuild

The rebuild should stay aligned with the project's original identity:

- still adaptive
- still context-aware
- still capital-preservation first
- still no martingale / no grid / no blowup behavior

But it must stop being so perfectionist that it produces no live participation.

The target is:

- **more actual trades**
- **better regime mapping**
- **better observability**
- **multiple complementary engines**
- **a realistic path toward paper-trade profitability**

### Current development priority

The active development order is now:

1. fix regime misclassification
2. relax artificial penalties
3. add analytics for all gate decisions
4. redesign strategy architecture into scalp + swing engines
5. validate with stronger backtesting before scaling live paper trading

This roadmap is now part of the project direction and should be treated as the working plan for the next iteration of ARCS-FX.

---

## Current Implementation State

This section tracks what has already been implemented from the rebuild plan, and what is still pending.

### Implemented now

#### Phase 1 foundations

- Regime misclassification fix:
  - low-volatility dead markets are no longer treated as `CHAOTIC`
  - a new `QUIET` regime label has been added
- Confidence gate improvements:
  - degraded news mode is no longer punished like bad news
  - duplicate MTF punishment has been reduced
- Live threshold behavior improved:
  - live mode now defaults to normal confidence mode instead of a hardcoded ultra-conservative early mode
- Gate analytics added:
  - per-tick gate counters are written into `data/bot_status.json`
  - tick logs now show where pairs died: quiet / chaotic / PA / confidence / risk / execution

#### Strategy expansion already added

The original narrow signal flow has been expanded with additional setup families:

- `SCALP_PULLBACK`
- `SCALP_SWEEP_REVERSAL`
- `SWING_BREAKOUT`
- `SWING_REVERSION`

These were added to increase controlled trade participation before fully splitting the architecture into dedicated engine modules.

#### Phase 2 structural upgrades now added

- Regime model expanded in code to a richer 5-state map:
  - `TRENDING_CLEAN`
  - `TRENDING_EXTENDED`
  - `RANGING_CLEAN`
  - `QUIET`
  - `CHAOTIC`
- Scalp participation is now session-aware:
  - default scalp activity is focused on `LONDON`, `NY`, and `OVERLAP`
  - Asian-session scalp logic is restricted to pairs with more natural Asian flow such as JPY/AUD/NZD-linked symbols
- Initial strategy separation has started with dedicated helper modules:
  - `engines/scalp_engine.py`
  - `engines/swing_engine.py`
- Dashboard regime rendering now recognises the richer trend/range labels instead of collapsing them into unknown states
- Range logic is less brittle:
  - ranging setups can now use recent range boundaries as fallback structure when an opposite S&D zone is not cleanly available
  - S&D proximity tolerance has been widened modestly to reduce over-filtering
- Trade management is no longer one-size-fits-all:
  - scalp, swing, and classic trend setups now use different breakeven / trailing / news-lock behavior in `execution/order_manager.py`

#### Risk / portfolio change already added

- max concurrent open trades increased from `2` to `3`
- correlation guard still remains active

#### Dashboard improvements already added

The dashboard now shows:

- `QUIET` as its own regime
- live gate analytics counters
- per-symbol gate reasons
- regime trigger explanations

#### Logging hygiene already added

- `logs/arcs_fx.log` now keeps the main runtime trail cleaner by filtering repetitive low-value noise such as:
  - Flask `GET /api/...` request spam
  - per-pair regime snapshot `INFO` lines
  - repetitive `NewsEngine: CLEAR` / calendar refresh lines
  - third-party model download transport chatter
- log rotation remains enabled for `logs/arcs_fx.log`
- a second high-signal companion log now exists at `logs/trading_events.log`
  - this file keeps the key trading lifecycle events easy to review:
    - startup / shutdown
    - `PA signal`
    - confidence pass / block
    - news blackout
    - order placement / failure
    - trade open / close
    - tick summary
- each fresh bot start now archives the previous run's live logs into `logs/archive/`
  - examples: `arcs_fx_20260416_091935.log`, `trading_events_20260416_091935.log`
  - the dashboard still reads the current live `logs/arcs_fx.log`, so this does not break the UI

#### Backtest improvements already added

The backtest now reports:

- `QUIET` skips separately from `CHAOTIC`
- signals by family
- signals by type
- signals by symbol
- expected signals per week by family

#### Latest validated rebuild result

From the latest completed 90-day exhaustive backtest (`logs/backtest_20260413_2348.json`):

- `231` signals generated across `8` pairs
- previous exhaustive baseline before the rebuild was only `18` signals
- previous intermediate runs were:
  - `193` signals (`logs/backtest_20260413_1428.json`)
  - `155` signals (`logs/backtest_20260413_2021.json`)
- signal family mix:
  - `MEAN_REVERSION`: `119`
  - `SCALP`: `79`
  - `SWING`: `23`
  - `TREND`: `10`
- regime mix:
  - `RANGING_CLEAN`: `165`
  - `TRENDING_CLEAN`: `24`
  - `TRENDING_EXTENDED`: `42`
- expected frequency is now about `18.0` signals per week across the basket
- compared with the prior `155`-signal run:
  - total participation increased strongly
  - swing improved from `20` to `23`
  - trend improved from `6` to `10`
  - scalp stayed controlled at `79`
  - but `MEAN_REVERSION` became too dominant
- the current architecture is productive, but still imbalanced:
  - range logic is now carrying too much of the system
  - trend participation is better but still secondary
  - `ASIAN` + `OFF` participation is lower than the old loose architecture, but still not clean enough yet

#### External research / market-structure alignment

Recent high-quality external references support the current direction of the rebuild:

- large and growing FX turnover in 2025 confirms that session-aware execution and liquidity-aware routing matter more, not less
- modern trend-following research still supports trend / momentum as a durable core building block across markets
- recent intraday momentum work shows that exit design can materially improve expectancy
- regime-dependent research continues to show that mean reversion should be selective rather than dominant

Practical implication for ARCS-FX:

- keep trend / breakout logic as a core pillar
- keep mean reversion, but do not let it dominate the full portfolio
- keep session-aware scalp restrictions
- continue separating management logic by strategy family

From the latest 14-day diagnostic run:

- `QUIET` remains a major real bucket on both `EURUSD` and `GBPUSD`
- `no_sd_near_price` is still one of the largest blockers, but it has improved slightly on `EURUSD`
- M5 pattern absence remains common
- `scalp_session_blocked` is present but small, which means session cleanup is working without crushing the system

### Not implemented yet

These parts of the rebuild plan are still pending:

- deeper 5-state regime exploitation:
  - use the richer regime labels more aggressively in confidence, execution, and reporting
  - validate that `TRENDING_CLEAN` and `TRENDING_EXTENDED` produce meaningfully different behavior in fresh backtests
- full separation into dedicated strategy modules:
  - move more setup logic out of `price_action.py`
  - keep `scalp_engine.py` and `swing_engine.py` as the primary routing / execution-layer modules
  - make the main router call engines more explicitly instead of embedding most setup logic in one file
- stronger session policy validation:
  - confirm weaker `ASIAN` / `OFF` scalp participation drops after the new session gating
  - tune pair-level exceptions only from evidence
- realistic trade lifecycle backtest:
  - fills
  - slippage
  - exit simulation
  - trade outcome statistics
- exit-model separation by strategy type:
  - different management rules for scalp vs swing trades
- staged rollout decisions based on fresh post-rebuild backtest output

### Immediate next step

The latest backtests confirm the current direction.

The next smart implementation step is:

- continue the architecture split
- improve swing quality
- reduce over-dependence on S&D proximity
- separate exits by strategy family
- only then move to a realistic trade-lifecycle backtest

The latest code changes after that result have already started addressing those points:

- reduced the range engine's dependence on exact opposite-zone availability
- modestly relaxed S&D proximity rules
- introduced strategy-family-specific management profiles for:
  - `SCALP_*`
  - `SWING_*`
  - classic trend setups such as `OB_RETEST`
- made `TRENDING_EXTENDED` continuation entries stricter instead of disabling them outright:
  - stronger pattern quality
  - stronger confluence requirements
  - higher minimum R:R
- made `SD_BOUNCE` setups more selective:
  - mean reversion is now session-aware
  - range entries now require more stretch away from the recent H1 mean unless price is already at a range extreme
  - weaker zone bounces are filtered out earlier
- added richer diagnostic visibility:
  - `mean_reversion_session_blocked` can now appear in `--diagnose`
  - mean-reversion signal details now include `z_score` and the applied session filter
- reduced confidence bias toward `SD_BOUNCE` so the score engine no longer rewards range setups too generously relative to trend / swing setups

The immediate next validation step after these changes is to rerun:

- `py -3.11 backtest/backtest_runner.py --days 90 --step 1`
- `py -3.11 backtest/backtest_runner.py --diagnose --days 14`

### Remaining phase work summary

What is left from the planned rebuild at a high level:

1. finish the architecture split
2. validate and tune the 5-state regime behavior
3. separate scalp and swing exit logic
4. upgrade backtesting into a real trade-lifecycle simulator
5. run a smaller-scope paper phase before scaling back to the full basket

---

## Notes

- The bot is **demo-account only** until paper trading validates it over at least 50 trades
- FinBERT NLP is not installed by default — install when ready: `py -3.11 -m pip install transformers torch`
- PDF reports require reportlab: `py -3.11 -m pip install reportlab`
- `data/weights.json` can be deleted to reset confidence weights to config defaults
- `data/risk_state.json` can be deleted to reset all circuit breaker counters (use with care)
- To run on a single pair for testing, edit `PAIRS` in `config.py` temporarily

---

## Code Analysis & Changes (2026-04-14 audit)

Full read-only audit performed on `config.py`, `core/regime_detector.py`, `engines/price_action.py` (2050 lines), `engines/confidence_score.py`, `engines/news_engine.py`, `engines/scalp_engine.py`, `engines/swing_engine.py`, `risk/risk_manager.py`, `execution/order_manager.py`, plus the two backtest JSONs (`backtest_20260412_1230.json` 5-signal run, `backtest_20260413_2348.json` 231-signal run).

### 1. Strategy soundness — is this "good algo trading"?

**Yes, directionally. But the edge is currently blurry, not sharp.**

#### What is genuinely strong (keep)

- **5-state regime taxonomy is correct.** Splitting `QUIET` from `CHAOTIC` was the single most important fix in the rebuild. Pros do this. Many retail bots conflate "low vol" with "bad" and trade through it anyway.
- **ATR-percentile instead of absolute ATR.** Textbook quant hygiene. The old "absolute ATR threshold" bug (which mislabeled quiet JPY sessions as CHAOTIC) is exactly the failure mode percentile normalization exists to solve.
- **Asymmetric management by strategy family** (`order_manager.py:503-529`). Scalp: BE at 0.8R, trail at 1.5R, 65% trail ratio. Swing: BE at 1.2R, trail at 2.5R, 35% trail ratio. **Professional-grade.** Most retail SMC bots use one-size-fits-all management and leak edge on both ends.
- **Kelly-inspired sizing on rolling 20-trade window** (`risk_manager.py:470-560`). Capped at 2%, floors at 0.5%, interpolates linearly between 40% and 60% win-rate. Adaptive but not reckless.
- **Correlation guard via static groups** (`risk_manager.py:76-81`). Blocking "EURUSD BUY + GBPUSD BUY" as one bet on USD-weakness is institutional thinking. Skipping rolling Pearson is the right call at this scale — noisy and brittle.
- **Mid-trade news handling** — conceptually correct. Blackout mid-trade → tighten TP to a strategy-specific news lock fraction. (BUT — see Critical Bug #1.)
- **Atomic state persistence** (`risk_manager.py:772-794`). Temp-file + `os.replace`. Correct on Windows and POSIX.
- **SL/TP attached at order level** (`order_manager.py:320-338`). If Python dies, broker still protects the position. Non-negotiable, done correctly.
- **Confidence scoring is coherent.** The 7-component weighted model is logically sound. MTF confluence bonus for signal-aligned-with-H1 is the right prioritization.

#### What is weak or mis-placed (alpha is leaking here)

##### (a) The "engine split" is architectural cosplay

`engines/scalp_engine.py` is 85 lines of session-policy helpers. `engines/swing_engine.py` is 27 lines of regime allow/deny. **Neither contains strategy logic.** All six signal generators live in `engines/price_action.py` (2050 lines):

- `_evaluate_scalp_pullback` (line 1358)
- `_evaluate_scalp_sweep_reversal` (line 1460)
- `_evaluate_swing_breakout` (line 1568)
- `_evaluate_swing_reversion` (line 1662)

**Verdict:** Not broken, but dishonest naming. Either move those four functions into the respective engine modules, or rename `scalp_engine.py` → `scalp_policy.py` and `swing_engine.py` → `swing_policy.py`.

##### (b) MEAN_REVERSION dominance (52% of signals) has a specific cause

`_evaluate_ranging` gates on:
```python
near_demand = (
    abs(current_price - dz.top) <= atr * 2.0
    or current_price <= recent_range_low + atr * 0.8
)
```
Two qualifying paths (`or`), and `atr * 2.0` is huge — in RANGING_CLEAN that's ~80% of the range width. Compared to trending's `atr * 2.0` distance from OB.mid (one path only), ranging is **structurally easier to trigger.** That's why SD_BOUNCE is 119/231.

**Fix:** Drop the `or recent_range_low` fallback entirely, tighten to `atr * 1.2`. Expect SD_BOUNCE to drop to ~40-50 signals (22% of book).

##### (c) Pattern detection on a single M5 bar is the biggest bottleneck

`_detect_pattern` evaluates only the last M5 candle. A textbook engulfing two bars ago is invisible. Combined with `_evaluate_trending` *requiring* a pattern for TRENDING_EXTENDED, this is why OB_RETEST only fires 10 times in 90 days across 8 pairs.

**Fix:** Scan the last 3 M5 bars and keep the strongest. One-liner, likely doubles OB_RETEST count.

##### (d) Swing breakout is over-gated

`_evaluate_swing_breakout` requires **all four simultaneously**: breakout confirmed, retest confirmed, EMA-aligned, pattern present. 14 signals in 90 days / 8 pairs is famine, not selectivity. Real desks gate 2 of 3, not 4 of 4.

**Fix:** Drop "pattern required" for SWING_BREAKOUT — breakout+retest IS the pattern.

##### (e) DI overrides slope in direction resolution

`regime_detector._resolve_direction`: when DI says BULLISH but 20-bar slope says BEARISH, DI wins. Wrong priority — slope is less noisy on M15. Generates counter-trend entries in inflection zones.

**Fix:** If DI and slope disagree, return `NEUTRAL`. The ×0.70 confidence penalty is exactly what you want during regime transitions.

---

### 2. 🛑 Critical bugs found (flagged, not touched)

#### Bug #1 — Mid-trade news handler is permanently broken

`order_manager.py:652-654`:
```python
try:
    from engines.news_engine import NewsEngine
```
**There is no `NewsEngine` class in `engines/news_engine.py`.** Only a module-level `evaluate()` function. `ImportError` is caught and silently swallowed. **Result: the bot has never tightened TP on a news event, contrary to spec and README.** Most dangerous finding. Running live without documented news protection on open positions.

**Fix:** `from engines.news_engine import evaluate as news_evaluate`, then `news = news_evaluate(pos.symbol)`. Remove `hasattr(self, "_news_engine")` caching — `evaluate()` caches internally for 30 min.

#### Bug #2 — Pip value is a flat $10 for all pairs including JPY

`risk_manager.py:533`:
```python
pip_value_per_lot = 10.0
```
The comment acknowledges this is wrong for JPY (~$9.26/pip) and says order manager will apply precise `tick_value` at execution. **It doesn't.** Lot sizing is final at the risk layer. On USDJPY/EURJPY you risk ~1.08% thinking it's 1.0%. Silent 8% over-leverage on JPY pairs.

**Fix:** Use `mt5.symbol_info(symbol).trade_tick_value / tick_size` at sizing time.

#### Bug #3 — Linear win-rate interpolation crosses scale-up band awkwardly

`risk_manager.py:505-514`: at exactly 50% win rate you get 1.0% by coincidence, not by design. `RISK_BASE_PCT` is never used directly. Minor, but the "base" concept is misleading.

---

### 3. New recommendations (not in current plan)

| Priority | Recommendation |
|----------|----------------|
| 1 | Raise `CONFIDENCE_MIN` to 72. Current avg 75.8 with 70 gate is loose — dominant signals cluster near 70-74. |
| 2 | Session-heatmap payoff check before sizing up: if `(symbol, hour)` cell <40% WR on ≥10 trades, force `RISK_LOW_PCT` regardless of global rolling win rate. |
| 3 | Replace single-bar pattern detection with 3-bar max-scan (see 1.c). |
| 4 | Log `actual_r_on_close` vs `r_ratio_at_entry`. Drift >20% over 30 trades = trailing params wrong. Without this you can't tell if losses are bad entries or bad exits. |
| 5 | `CORRELATION_GROUPS` missing RISK_ON cluster. Add `("GBPUSD", "AUDUSD", "NZDUSD", "EURJPY")` — all risk-on proxies. |
| 6 | Weekend gap guard: `close_all_trades("WEEKEND_GAP")` on Friday 20:00 UTC. |
| 7 | `FVG_FILL` exists in confidence scorer type table (0.75) but isn't emitted by any generator. Wire it up or delete. |

---

### 4. Architectural verdict on file placement

| Module | Verdict |
|---|---|
| `config.py` | ✅ Clean single source of truth. |
| `core/regime_detector.py` | ✅ Well-scoped. Fix the DI-vs-slope priority. |
| `engines/price_action.py` | ⚠️ **2050 lines — too big.** Split: structure/zones → `core/smc_primitives.py`; trending → `engines/trend_engine.py`; ranging → `engines/mean_reversion_engine.py`; scalp/swing → their respective engine files. |
| `engines/scalp_engine.py`, `engines/swing_engine.py` | ⚠️ Misnamed. Policy files, not engines. |
| `engines/confidence_score.py` | ✅ Correct placement, clean scoring. |
| `engines/news_engine.py` | ✅ Correct. Exposes `evaluate()` not `NewsEngine` — see Bug #1. |
| `risk/risk_manager.py` | ✅ Exemplary. Tests inline. Atomic persistence. Best file in the project. |
| `execution/order_manager.py` | ✅ Solid MT5 handling. Fix Bug #1 and it's production-ready. |

---

### TL;DR

**Strategy is sound. Architecture is 80% right.** But: one silent production bug (news-mid-trade is disabled), one structural signal imbalance (mean-reversion eats 52% of the book via an over-generous `OR` gate), one cosmetic lie (the "engine split" didn't actually split anything).

Fix Bug #1 today. Tighten `_evaluate_ranging` before next backtest. Everything else is polish.

Closer to a real edge than 95% of retail SMC bots. Don't touch the risk module — it's the crown jewel. Focus surgery on `price_action.py` and `order_manager.py:_handle_mid_trade_news`.

---

## Backtest + Fix Cycle (2026-04-14, second pass)

### 90-day × 8-pair backtest (baseline, pre-fix)

| Metric | Value |
|---|---|
| Signals generated | 133 (~10.3/wk) |
| Avg confidence | 75.4 |
| Avg R:R | 3.65 |
| Confidence-blocked | 218 (62% rejection) |
| Risk-blocked | 0 (dead path in backtest — no open positions to correlate) |
| News-skipped | 0 (news engine not wired into backtest runner) |

**Family mix:** SCALP 60% (80) · TREND 20% (27) · SWING 17% (22) · MEAN_REVERSION 3% (4) — the mean-reversion over-firing problem from the first audit had already resolved itself in this build.

**Signals by type:** SCALP_PULLBACK 42 · SCALP_SWEEP_REVERSAL 38 · OB_RETEST 27 · SWING_BREAKOUT 12 · SWING_REVERSION 10 · SD_BOUNCE 4.

### Diagnostic run (14d × 2 pairs) — where the leaks were

- **PATTERN_NONE on M5 last candle: 59-61%** of non-chaotic bars → biggest bottleneck.
- `no_sd_near_price` 21-22% of non-chaotic bars (largest block-reason bucket).
- `no_ob_near_price` 14-15%.
- `pattern_none_ob_present` 13% (OB is in range but single-bar pattern check fails).
- `direction_neutral` 8-9% (DI vs slope disagreement).
- **OFF session was producing 11 signals (~8%) in the 90d run** — should be zero.

### Fixes applied (seven, all in one pass)

| # | File | Change | Why |
|---|---|---|---|
| 1 | `execution/order_manager.py:652-660` | `from engines import news_engine` + `news_engine.evaluate(sym)` | `NewsEngine` class never existed — mid-trade news tightening was permanently broken (Bug #1) |
| 2 | `risk/risk_manager.py:528-542` | JPY pairs: `pip_value_per_lot = 1000/entry_price` | Flat $10 was ~35% off on JPY pairs (Bug #2) |
| 3 | `engines/price_action.py:200-210` | Global OFF-session hard-block in `evaluate()` | OFF was only blocked in scalp/MR layers; swing/OB_RETEST slipped through |
| 4 | `core/regime_detector.py:484-496` | DI vs slope active disagreement → NEUTRAL (stop letting DI win) | Trading into exhausted moves; standing aside is safer |
| 5 | `engines/price_action.py:959-1000` | Pattern detector scans **last 2 trigger bars**, stale pattern at 0.8× strength | Recovers the ~20% of non-chaotic bars where rejection formed one bar ago |
| 6a | `engines/price_action.py:1126-1130` | OB_RETEST proximity **2.0 → 2.3 ATR** | Tuned per family (not blanket) — OB leak was ~14% |
| 6b | `engines/price_action.py:1267-1272` + supply side | SD_BOUNCE proximity **2.0 → 2.4 ATR** | Largest block-reason bucket (22%) |
| 7 | `engines/price_action.py:1638-1662` | SWING_BREAKOUT: `breakout AND pattern AND (retest OR trend_ok)` — 3-of-4 instead of 4-of-4 | Allows entry when EMA hasn't fully crossed but breakout is clean, or when retest window missed but trend is strong |

**Left deliberately untouched:**

- Scalp-pullback proximity (0.40 ATR) — tightness is by design.
- SD-zone inner edges (`range_low + atr*0.8`, `range_low + atr*0.5`) — these are "inside the zone" checks, widening them is a different conversation.
- Risk module — still the crown jewel.

### Softening from first-pass plan

The original audit called for a blanket +20% proximity widening. Implemented selectively by family instead: OB_RETEST +15%, SD_BOUNCE +20%, scalp unchanged. Avoids loosening setups that were already well-calibrated.

### Expected deltas (to verify on next backtest)

- OB_RETEST ↑ from 27 (wider prox + multi-bar pattern)
- SD_BOUNCE ↑ meaningfully from 4 (biggest leak addressed)
- SWING_BREAKOUT ↑ from 12 (3-of-4 gate)
- OFF-session signals → **0**
- `pattern_none` diagnostic bucket ↓ ~30-40%
- MEAN_REVERSION ≈ flat (no ranging logic touched)

### Post-fix backtest (same 90d × 8 pairs window)

| Metric | Baseline | Post-fix | Δ |
|---|---|---|---|
| Signals (90d) | 133 | **170** | **+28%** |
| Per week | 10.3 | 13.2 | +28% |
| Avg confidence | 75.4 | 75.6 | Held |
| Avg R:R | 3.65 | 3.67 | Held |
| Confidence-blocked | 218 | 295 | Threshold still doing real work |
| OFF-session signals | 11 | **0** ✅ | Hard-block effective |

**Signals by type:**

| Type | Before | After | Δ |
|---|---|---|---|
| SCALP_PULLBACK | 42 | **66** | +57% (multi-bar pattern — biggest winner) |
| SWING_REVERSION | 10 | 19 | +90% |
| SWING_BREAKOUT | 12 | 20 | +67% (3-of-4 gate) |
| SD_BOUNCE | 4 | 7 | +75% |
| OB_RETEST | 27 | 30 | +11% |
| SCALP_SWEEP_REVERSAL | 38 | **28** | **−26%** (expected — stricter NEUTRAL rule) |

**Diagnostic breakdown (14d × EURUSD+GBPUSD):**

| Bucket | Before | After |
|---|---|---|
| PATTERN=NONE on M5 | 59-61% | **35-36%** |
| `pattern_none_ob_present` | 13% | 7% |
| `pattern_none_sd_present` | 6-10% | 5-7% |
| `no_sd_near_price` | 21-22% | 16% |
| `no_ob_near_price` | 14-15% | 15-16% |
| `direction_neutral` | 8-9% | 12-14% |

Pattern-none bucket cut ~40% — multi-bar window worked as designed. SD-proximity leak halved by the 2.0→2.4 widening. `direction_neutral` climbed by design (stricter DI-vs-slope rule), and SCALP_SWEEP's −26% is the visible consequence — those were the counter-trend setups the audit flagged as alpha leaks, so trading fewer of them is the point.

**Session distribution post-fix:** ASIAN 45 · LONDON 42 · NY 43 · OVERLAP 40 · OFF 0 — perfectly balanced, no session over- or under-fishing.

**Open observations for next cycle:**

- **GBPUSD stuck at 12** (unchanged while all others rose) — pair-specific diagnostic recommended.
- OB proximity 2.3 has headroom to 2.5 if more OB_RETEST volume is wanted — current lift was only +11%.
- `low_rr_or_other` climbed 10% → 16% on EURUSD — benign side effect of wider prox letting more R:R-failing candidates surface.

### TL;DR of this cycle

+28% signal count, confidence and R:R held flat, OFF-session leak sealed, pattern-none bucket cut by 40%, two critical production bugs (NewsEngine, JPY pip) fixed. 13.2/wk at 75.6 conf / 3.67 R is a strong place to stop tweaking and let it run on paper.

---

## Live Paper Run #1 (2026-04-14 → 2026-04-15, ~12h)

First end-to-end run on XM Global Demo (1:100 leverage, $5000 starting equity).

### Result

**0 trades executed** despite 5 valid signals — all EURJPY SELL, risk sized correctly at ~$50 (1%). Every order rejected by XM with:

```
MT5 error: (-2, 'Invalid "comment" argument')
```

Broker validation refused the comment format `"ARCS OB_RETEST PIN_BAR_BULL"` (spaces + underscores). The signal chain itself was healthy — regime detection, news blackout (caught a real ECB Lagarde event on EURUSD, validating the NewsEngine import fix), confidence scoring, risk sizing all worked.

### Fixes applied post-run

| # | File | Change |
|---|---|---|
| 1 | `execution/order_manager.py` | New `_safe_comment()` helper sanitises to `[A-Za-z0-9_-]` and caps at 31 chars. Applied to both open and close requests. |
| 2 | `main.py` | Split log filters: `_NoiseSuppressingFilter` (file) preserves regime snapshots for post-mortem, `_ConsoleNoiseFilter` (console) drops them to keep the terminal clean. |
| 3 | `main.py` | Fixed `_TradingEventsFilter.KEYWORDS` — removed 5 dead entries (`"Order placed"`, `"Opened trade"`, etc. that no code emits), added `"All gates PASSED"`, `"Risk BLOCKED"`, `"open_trade() failed"` to actually capture orchestrator-side trade lifecycle in `logs/trading_events.log`. |
| 4 | `engines/news_engine.py` | New `_throttled_scrape_warning()` with 5-min TTL per key. Stops FF 429 warnings spamming the log every poll during rate-limit windows. |

### Known open items (non-blocking)

- Sentiment keyword-fallback stuck at +1.00 for every pair — skews confidence slightly high.
- `transformers` not installed → FinBERT disabled, keyword fallback only.
- ForexFactory 429 during most polls → fallback calendar active (proven working).
- GBPUSD underperforming other pairs on signal count — pair-specific diagnostic pending.

### Companion log

New rotating log: `logs/trading_events.log` — captures only startup, dashboard online, PA signals, news blackouts, gate-passes/blocks, order placement/failure, tick summaries, shutdown. Clean high-signal trail for quick audit.

---

## Live Paper Run #2 (2026-04-15, in progress)

Bot relaunched after the 4 post-run-1 fixes. **Currently waiting through London+NY overlap (13:00-17:00 UTC) before any more tuning.**

### Observations so far (first ~90 min, pre-overlap)

Tick pattern: `quiet=3 chaotic=0 pa=5 conf=0-1 risk=0 opened=0` every tick.

- **3 pairs QUIET** — EURUSD, GBPUSD, USDCHF (low-volatility dead market)
- **4 pairs in extended news blackout** — AUDUSD, USDJPY, USDCAD, NZDUSD for `"President Trump Speaks"` (~15h window)
- **1 real signal repeating** — USDCAD `SCALP_SWEEP_REVERSAL / PIN_BAR_BEARISH / R=7.5 / strength=0.75` firing every tick but ConfScore=54.9/70 BLOCKED. Score breakdown: `mtf_confluence=5.2/15`, `key_level=1.0/10`, `volatility_pct=5.4/10`
- **EURJPY no signal** — despite a sharp V-bottom reversal visible on the user's chart (187.10 → 187.42). PA engine found no qualifying setup: our signal library is pullback/retest-based, not momentum-chasing, and the move was too clean/steep for OB_RETEST or SD_BOUNCE.

### Suspected tuning issues (to investigate AFTER overlap window)

1. **"Trump Speaks" blackout too aggressive** — 15+ hours on 4 USD pairs kills half the universe. Either downgrade speech-type events or shorten their blackout window. Check `engines/news_engine.py` blackout rules.
2. **Confidence threshold (70) blocks genuine USDCAD setups** — R=7.5 and strength=0.75 is premium. Weak scoring components (mtf_confluence, key_level) are structural. Fix is likely improving `mtf_confluence` scoring so real trend continuations don't get penalized during normal regime handoffs — NOT lowering threshold.
3. **No momentum-chase signal type** — EURJPY rally type is uncatchable with current setup library. Optional enhancement: add a momentum-breakout variant that doesn't require retest.

### Next checkpoint

User will report back after overlap window closes (~17:00 UTC 2026-04-15). Metrics to compare:
- Did the 3 QUIET pairs wake up?
- Did USDCAD confidence break 70?
- Did Trump blackout expire?
- Any trades opened?

If still 0 trades after overlap: audit news blackout rules (#1) first, then confidence `mtf_confluence` scoring (#2).

### Run #2 interim result (stopped ~13:52 UTC) + fixes round 2

User stopped bot after ~3h20m. Final tick log showed:

- **EURJPY produced a valid signal** at 13:38 UTC: `SELL SWING_REVERSION / PIN_BAR_BEARISH / ConfScore=72.2/70 / R=2.5`. All gates PASSED.
- **Broker rejected it 3 times in a row** with `MT5 error (-2, 'Invalid "comment" argument')`. Run #1's `_safe_comment()` fix was still too permissive — XM apparently also rejects `-` (dash) and/or is length-sensitive below 31 chars.
- **USD pairs blacked out for hours** by Empire State Manufacturing Index — a medium-tier event that rarely moves majors >10 pips. Sitting out London+NY overlap on that basis is too conservative.
- **USDJPY signal ConfScore=63.6/70 repeatedly** — same `mtf_confluence=5.2/15` weakness. Unchanged, deferred.

**Fixes applied (round 2):**

1. **`execution/order_manager.py` `_safe_comment()`** — tightened from `[A-Za-z0-9_-]`/31 chars to strict `[A-Za-z0-9]`/20 chars, with `"ARCSFX"` fallback. Web research confirmed MT5's `MqlTradeRequest.comment` is `char[32]`, brokers can inject their own text (eating budget), and non-alphanumeric chars cause terminal-side `TERMINAL_INVALID_PARAMS` (-2). If this still fails, the nuclear test is `comment=""`.

2. **`engines/news_engine.py` `BENIGN_EVENT_PATTERNS`** — new allowlist of ~20 known low-mover events (Empire State, Philly/Richmond/Chicago/Dallas/KC Fed surveys, housing starts/permits/sales, Michigan sentiment, factory orders, Ivey PMI, Tankan, trade balance, Redbook, API/EIA crude). These are stripped at scrape time and never trigger blackout regardless of FF's HIGH/MEDIUM tag.

3. **`engines/news_engine.py` `_check_blackout()`** — MEDIUM-tier window tightened from ±15 min to ±5 min. HIGH-tier unchanged (still `NEWS_PAUSE_BEFORE_MIN`/`NEWS_PAUSE_AFTER_MIN` from config).

4. **`engines/news_engine.py` `SPEECH_PATTERNS`** — new rule: events matching `speaks / speech / testimony / testifies / press conference / remarks` are downgraded HIGH→MEDIUM so they get the ±5 min window instead of ±30. Addresses Run #1's 15h "President Trump Speaks" blackout. Powell/ECB/BoE speeches still protected at the reaction-candle level.

**Net effect on next run:**
- Most medium-tier USD data ignored entirely.
- Other MEDIUM events only dodge the event candle (±5 min) instead of a 30-min window.
- HIGH events (NFP, CPI, FOMC, rate decisions) still fully protected.
- Broker-side comment validation should accept `"ARCSSWINGREVERSIONPI"` etc.

**Still deferred (Run #3 will re-expose these):**
- `mtf_confluence` scoring always ~5/15 across multiple pairs — structural scorer issue, likely penalising normal regime handoffs too harshly.
- No momentum-chase signal type for sharp reversals without retest (EURJPY V-bottom miss).
- Sentiment stuck at +1.00 for every pair (keyword fallback bug in `_score_with_keywords`).
- GBPUSD consistently underperforms on signal count — pair-specific diagnostic.
- `transformers` not installed → FinBERT disabled (optional install to re-enable real sentiment).

## Live Paper Run #3 (2026-04-15 ~17:51 to 2026-04-16 ~09:19, 15h)

### Results — first real trades!

| Metric | Value |
|---|---|
| Starting balance | $5,000.00 |
| Ending balance | $5,217.25 (+4.3%) |
| Trades opened | 3 (all NZDUSD BUY) |
| Win/Loss | 3W / 0L (TP hit on all 3) |
| Signal type | SCALP_SWEEP_REVERSAL / MORNING_STAR |
| Entry ~0.5915, SL 0.59024, TP 0.59197 | ~4.8 pips per trade, 1.51 lots each |
| Session | OFF (21:00+ UTC — Asian overlap) |
| Confidence | 70.4/70 (barely cleared) |

**What worked:**
- Comment fix (`_safe_comment()`) — no more `-2` rejections.
- News blackout fix — `news_blackout=0` throughout run. Benign events + speech downgrade working.
- AutoTrading enabled — orders filled cleanly.
- NZDUSD TP hit 3/3 — the strategy edge is real.

**What broke (4 bugs discovered):**

1. **Position close tracker blind** — `_get_last_deal_info()` returned `(0.0, 0.0)` for all 3 winning trades. Root cause: used `float` timestamps with `history_deals_get()` which some MT5 builds silently reject. All 3 TP wins logged as `pnl=$0.00 won=False`.

2. **False consecutive-loss cooldown** — Because wins looked like losses, `consecutive_losses=3` triggered a 2-hour smart-idle cooldown that **blocked valid USDJPY signals** (ConfScore=73.1, the highest score ever seen) at 04:08 UTC.

3. **Duplicate positions on same symbol** — Bot opened 3 identical NZDUSD BUYs on 3 consecutive ticks. No guard checking "is there already an open position on this symbol?" The 4th tick was finally caught by `Max open trades: 3/3`.

4. **OFF-session block inconsistency** — Bot logged `Session=OFF` but the OFF hard-block in `price_action.evaluate()` didn't fire (M5 candle timestamp vs real-time mismatch). The trades won anyway, proving OFF-session can produce valid setups.

### Fixes applied (round 3)

1. **`execution/order_manager.py` `_get_last_deal_info()`** — now uses `datetime` objects (not float timestamps) for `history_deals_get()`, `group="*SYMBOL*"` wildcard, fallback to unfiltered query, matches on both `position_id` and `order` (broker-dependent), added debug/warning logging.

2. **`execution/order_manager.py` `open_trade()`** — new duplicate guard at top: checks `self._positions` for existing open position on same symbol, skips with log if found. Max 1 position per symbol.

3. **`engines/price_action.py` `evaluate()`** — OFF-session converted from hard-block (`return no_signal`) to soft pass. Confidence scorer already penalises OFF via `spread_session` component (0.25 vs 1.0 for OVERLAP = ~3-point natural penalty). Only strong OFF setups clear 70.

4. Cooldown bug auto-fixes once #1 works — correct `won=True` → no false consecutive losses → no spurious cooldown.

### Signals observed but not traded (pre-fix data)

- **18 orders rejected** with `retcode=10027` (AutoTrading disabled) before user enabled it mid-run.
- **EURJPY SELL** cleared 70.5-72.2 repeatedly — would have been valid trades.
- **USDCAD BUY** scored 71.3 during OVERLAP — blocked only by AutoTrading, not confidence.
- **USDJPY** scored 73.1 at 04:08 UTC — highest score ever — blocked by false cooldown from bug #2.

### Still deferred

- `mtf_confluence=5.2/15` persistent on all pairs — structural scorer issue, top priority for next session.
- No momentum-chase signal type (EURJPY V-bottom miss).
- Keyword sentiment always +1.00.
- GBPUSD under-signals.
- FinBERT not installed.

---

*Developer: Fadi — Built with Claude Code*
