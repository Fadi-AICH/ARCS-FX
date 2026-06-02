<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/MetaTrader-5-00c853?logo=metatrader&logoColor=white" />
  <img src="https://img.shields.io/badge/Platform-Windows-0078d6?logo=windows&logoColor=white" />
  <img src="https://img.shields.io/badge/Status-Paper%20Trading-orange" />
  <img src="https://img.shields.io/badge/License-MIT-lightgrey" />
</p>

<h1 align="center">ARCS-FX</h1>
<p align="center"><strong>Adaptive Regime-Conditioned Scalping for Forex</strong></p>
<p align="center">
  A self-learning, news-aware, multi-engine Forex trading bot built on MetaTrader 5.<br/>
  Capital preservation first. Quality over quantity. No martingale, no grid, no blowup risk.
</p>

---

## What is ARCS-FX?

ARCS-FX is a fully automated Forex algorithmic trading system that:

- **Classifies the market** into 5 regimes (`TRENDING_CLEAN`, `TRENDING_EXTENDED`, `RANGING_CLEAN`, `QUIET`, `CHAOTIC`) before doing anything
- **Routes signals** through purpose-built strategy engines — scalp, swing, trend continuation, and mean reversion
- **Stays completely silent** during chaotic markets and news blackout windows — this is a feature, not a bug
- **Tags every trade** with full DNA context (regime, session, spread, news score, pattern, confidence) to learn from its own history
- **Adjusts its own confidence weights** weekly based on real trade outcomes

It runs on a live MT5 demo account alongside two specialized sub-bots:

| Bot | Purpose | Entry |
|-----|---------|-------|
| **ARCS-FX** | Main Forex scalp + swing portfolio bot | `main.py` |
| **ARCS-PROP** | Prop challenge bot (FundedNext $10k) | `prop_main.py` |
| **Crypto Weekend** | BTC/ETH/SOL weekend session bot | `crypto_weekend/main.py` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        ARCS-FX CORE                         │
│                                                             │
│   main.py  ──►  Regime Detector  ──►  Signal Router         │
│                      │                      │               │
│                 5-state map           ┌─────┴──────┐        │
│            TRENDING_CLEAN             │            │        │
│            TRENDING_EXTENDED     Scalp Engine  Swing Engine │
│            RANGING_CLEAN              │            │        │
│            QUIET                      └─────┬──────┘        │
│            CHAOTIC                          │               │
│                                       Price Action          │
│                                       (OB / FVG / S&D)      │
│                                             │               │
│  ┌──────────────────────────────────────────▼────────────┐  │
│  │                EDGE STACK (9 Engines)                  │  │
│  │  COT · Vol Regime · Macro Anchor · Cross Momentum     │  │
│  │  Event Flow · Carry Basket · Execution Cost            │  │
│  │  Signal Calibrator · Meta Filter                       │  │
│  └───────────────────────────────────────────────────────┘  │
│                             │                               │
│                    Confidence Score (0–100)                  │
│                             │                               │
│          ┌──────────────────▼──────────────────┐           │
│          │          Risk Manager                │           │
│          │  Kelly sizing · Circuit breakers     │           │
│          │  Correlation guard · 3-trade max      │           │
│          └──────────────────┬──────────────────┘           │
│                             │                               │
│                    Order Manager (MT5)                       │
│                  Asymmetric trade management                 │
│                             │                               │
│          ┌──────────────────▼──────────────────┐           │
│          │         Learning System              │           │
│          │  Session Heatmap · Pattern Analyzer  │           │
│          │  Strategy Adjuster · Report Gen      │           │
│          └─────────────────────────────────────┘           │
│                                                             │
│   Dashboard (Flask :5000)  ──────── SQLite Trade DNA       │
└─────────────────────────────────────────────────────────────┘
```

---

## Core Philosophy

| Principle | Implementation |
|-----------|----------------|
| Capital preservation first | Hard circuit breakers — 3% daily / 6% weekly drawdown |
| Quality over quantity | Composite confidence gate (0–100 score) blocks low-conviction setups |
| Context, not just signals | Every trade tagged with 9 DNA fields; bot learns context, not just outcomes |
| No blowup risk | No martingale, no grid, max 3 open trades, correlation guard active |
| Adaptive sizing | Kelly-inspired sizing on rolling 20-trade window (0.5% → 1.5% → 2% cap) |

---

## Strategy Overview

### 5-State Regime Model

```
ATR Percentile ─────────────────────────────────────────────────►
        Low             Mid              High          Extreme
         │               │                │               │
    ┌────▼────┐     ┌────▼──────┐   ┌────▼────┐    ┌────▼──────┐
    │  QUIET  │     │ RANGING   │   │TRENDING │    │  CHAOTIC  │
    │  skip   │     │  CLEAN    │   │  CLEAN  │    │   skip    │
    └─────────┘     └───────────┘   └─────────┘    └───────────┘
                                         │
                               high ADX + extended
                                         │
                                ┌────────▼────────┐
                                │ TRENDING        │
                                │ EXTENDED        │
                                └─────────────────┘
```

### Signal Families

| Family | Regime | Session | Strategy |
|--------|--------|---------|----------|
| `OB_RETEST` | TRENDING_CLEAN | Any | SMC Order Block retest with BOS confirmation |
| `SCALP_PULLBACK` | TRENDING_CLEAN/EXT | London / NY / Overlap | Momentum pullback to structure |
| `SCALP_SWEEP_REVERSAL` | Any trending | London / NY | Liquidity sweep → counter-move |
| `SWING_BREAKOUT` | TRENDING_CLEAN | Any | Structure breakout + retest |
| `SWING_REVERSION` | TRENDING_EXTENDED | Any | Extended-move mean reversion |
| `SD_BOUNCE` | RANGING_CLEAN | Any | Supply & Demand zone boundary play |

---

## Edge Stack Engines

Nine complementary engines score each setup independently. The **Meta Filter** synthesizes them into a final edge multiplier applied to the confidence score.

| Engine | Signal Source | Purpose |
|--------|--------------|---------|
| `cot_positioning.py` | CFTC COT reports | Institutional positioning bias |
| `vol_regime.py` | ATR percentile + VIX proxy | Volatility regime classification |
| `macro_anchor.py` | Interest rate differentials | Macro currency bias |
| `cross_momentum.py` | Relative pair momentum | Cross-pair direction alignment |
| `event_flow.py` | ForexFactory calendar | Pre/post event positioning |
| `carry_basket.py` | Interest rate differentials | Carry-trade alignment |
| `execution_cost.py` | Live spread + session | Spread-adjusted value assessment |
| `signal_calibrator.py` | Historical signal outcomes | Per-signal-type win rate calibration |
| `meta_filter.py` | All 8 above | Composite edge score → confidence multiplier |

---

## 8 Core Features

### 1. Asymmetric Trade Management (by strategy family)

```
Scalp trades            Classic/Trend trades       Swing trades
─────────────           ────────────────────        ────────────
BE at +0.8R             BE at +1.0R                BE at +1.2R
Trail at +1.5R          Trail at +2.0R             Trail at +2.5R
Trail ratio: 65%        Trail ratio: 50%           Trail ratio: 35%
```

### 2. Trade DNA Tagging

Every trade is tagged at entry with:

```
{regime, session, spread_pips, news_score, day_of_week,
 mtf_confluence, vol_percentile, pa_pattern, key_level_proximity,
 confidence_breakdown, signal_family, strategy_type}
```

And at close: `{exit_price, pnl_usd, r_multiple, close_reason}`

### 3. Confidence Score (0–100)

| Component | Weight | What it measures |
|-----------|--------|-----------------|
| Regime clarity | 25% | How cleanly defined is the current regime? |
| Price action signal | 20% | OB / S&D signal quality + pattern strength |
| MTF confluence | 15% | H1 and M15 direction agreement |
| News sentiment | 15% | NLP score + no blackout present |
| Key level proximity | 10% | Signal at a significant price level? |
| Volatility percentile | 10% | ATR in the tradeable sweet spot? |
| Spread + session | 5% | Spread acceptable and session active? |

### 4. Learning System

```
Every trade ──► SQLite Trade DNA DB
                      │
          ┌───────────┴────────────┐
          │                        │
   Session Heatmap          Pattern Analyzer
   (per-pair per-hour        (multi-axis breakdown:
    win rate table)           regime × signal × session)
          │                        │
          └───────────┬────────────┘
                      │
               Strategy Adjuster
               (auto-shifts weights every Sunday
                30% dampening, bounds 5–35)
```

### 5. News Awareness

- **ForexFactory** calendar — HIGH-impact events trigger 30-min pre/post blackout
- **NewsAPI** — real-time headline monitoring
- **FinBERT NLP** — sentiment scoring (optional, keyword fallback if not installed)
- Mid-trade blackout: open positions get TP tightened to `entry + news_lock_fraction × R`

### 6. Circuit Breakers (hardcoded, non-negotiable)

```
Daily loss    ─── 3%  ──► STOP TRADING until midnight UTC
Weekly loss   ─── 6%  ──► STOP TRADING until Monday 00:00 UTC
Consec losses ─── 3   ──► 2-hour mandatory cooldown
Max exposure  ─── 3   ──► max open trades (correlated pairs blocked)
Max risk/trade─── 2%  ──► hard ceiling regardless of win rate
```

### 7. Spread Trap Filter

Live spread is checked before every order. Exceeds threshold → trade skipped and logged.

| Pair | Max spread |
|------|-----------|
| EURUSD | 1.0 pip |
| Major pairs | 1.5 pips |
| Minor pairs | 2.0 pips |

### 8. Backtest Diagnostics

```bash
# Full 90-day signal pipeline validation
py -3.11 backtest/backtest_runner.py --days 90 --step 1

# PA gate diagnostic — understand where setups die
py -3.11 backtest/backtest_runner.py --diagnose --days 14 --pair EURUSD
```

**Latest validated result (90d × 8 pairs × step=1):**

| Metric | Value |
|--------|-------|
| Signals generated | 170 |
| Per week | ~13.2 |
| Avg confidence | 75.6 |
| Avg R:R | 3.67 |
| OFF-session signals | 0 |
| Session distribution | ASIAN 45 · LONDON 42 · NY 43 · OVERLAP 40 |

---

## Project Structure

```
ARCS-FX/
│
├── main.py                          # Main orchestrator — ARCS-FX bot
├── config.py                        # All parameters in one place
├── prop_main.py                     # ARCS-PROP challenge bot
├── crypto_main.py                   # Crypto weekend bot launcher
├── .env                             # MT5 credentials (never commit)
│
├── core/
│   ├── mt5_connection.py            # MT5 connection lifecycle
│   ├── data_fetcher.py              # OHLCV + tick data from MT5
│   ├── regime_detector.py           # 5-state regime classifier
│   └── instruments.py               # Pair metadata
│
├── engines/
│   ├── price_action.py              # SMC engine (OB, FVG, S&D, patterns)
│   ├── confidence_score.py          # 7-component confidence gate
│   ├── news_engine.py               # ForexFactory + NewsAPI + FinBERT
│   ├── scalp_engine.py              # Scalp session policy
│   ├── swing_engine.py              # Swing regime policy
│   │── ── Edge Stack ───────────────────────────────────────
│   ├── cot_positioning.py           # CFTC COT institutional positioning
│   ├── vol_regime.py                # Volatility regime engine
│   ├── macro_anchor.py              # Macro / rate differential bias
│   ├── cross_momentum.py            # Cross-pair momentum
│   ├── event_flow.py                # Event pre/post positioning
│   ├── carry_basket.py              # Carry trade alignment
│   ├── execution_cost.py            # Spread-adjusted value
│   ├── signal_calibrator.py         # Per-signal-type win rate
│   └── meta_filter.py               # Composite edge score
│
├── risk/
│   └── risk_manager.py              # Kelly sizing + circuit breakers
│
├── execution/
│   └── order_manager.py             # MT5 order placement + asymmetric mgmt
│
├── learning/
│   ├── trade_logger.py              # SQLite trade DNA database
│   ├── session_heatmap.py           # Per-pair per-hour win rate heatmap
│   ├── pattern_analyzer.py          # Multi-axis performance breakdown
│   ├── strategy_adjuster.py         # Auto-adjusts confidence weights weekly
│   └── report_generator.py          # Weekly HTML report (dark theme)
│
├── backtest/
│   └── backtest_runner.py           # Bar-by-bar signal pipeline simulator
│
├── dashboard/
│   ├── app.py                       # Flask backend (port 5000)
│   └── templates/index.html         # Dark-theme live dashboard
│
├── prop/
│   ├── prop_config.py               # FundedNext challenge parameters
│   ├── challenge_state.py           # Challenge phase state machine
│   ├── dynamic_risk.py              # Phase-aware position sizing
│   └── equity_watchdog.py           # Hard stop-loss enforcement
│
├── crypto_weekend/
│   ├── config.py                    # Crypto bot parameters
│   ├── main.py                      # Weekend crypto session orchestrator
│   ├── strategy.py                  # BTC/ETH/SOL signal logic
│   ├── mt5_bridge.py                # MT5 connection for crypto CFDs
│   └── storage.py                   # Trade state persistence
│
├── data/                            # Runtime state (auto-created, gitignored)
│   ├── trades.db                    # SQLite trade DNA
│   ├── risk_state.json              # Circuit breaker counters
│   ├── bot_status.json              # Live bot snapshot
│   └── weights.json                 # Current confidence weights
│
└── logs/                            # Log files (gitignored)
    ├── arcs_fx.log                  # Rotating bot log (10MB × 5)
    ├── trading_events.log           # High-signal trading lifecycle log
    └── archive/                     # Previous run logs (timestamped)
```

---

## Installation

### Prerequisites

- Windows 10/11
- MetaTrader 5 desktop app installed and running
- **Python 3.11** — the MetaTrader5 library is only compatible with 3.11

> Use `py -3.11` for all commands. Do NOT use `python` or `py` alone.

### 1. Clone the repo

```bash
git clone https://github.com/Fadi-AICH/ARCS-FX.git
cd ARCS-FX
```

### 2. Install dependencies

```bash
py -3.11 -m pip install MetaTrader5 pandas numpy ta requests python-dotenv feedparser flask
```

### 3. Optional packages

```bash
# PDF weekly reports
py -3.11 -m pip install reportlab

# FinBERT NLP sentiment (~1.5 GB download — keyword fallback works without it)
py -3.11 -m pip install transformers torch
```

### 4. Create `.env`

```ini
MT5_LOGIN=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=XMGlobal-MT5 2
NEWSAPI_KEY=your_newsapi_key   # optional — get free key at newsapi.org
```

### 5. Create runtime directories

```bash
mkdir data logs
```

These are auto-created on first run but pre-creating avoids a startup error if MT5 is not connected.

---

## How to Run

### One-click (Windows)

Double-click `start_arcs_fx.bat` — starts the bot and opens the dashboard at `http://localhost:5000`.

### Command line

```bash
# Bot only
py -3.11 main.py

# Bot + dashboard (auto-opens browser)
py -3.11 main.py --dashboard

# Bot + dashboard on a different port
py -3.11 main.py --dashboard --port 8080

# Dashboard only (review historical data without the bot)
py -3.11 dashboard/app.py
```

Stop with **CTRL+C** — all open positions are closed gracefully before shutdown.

### Backtest first (recommended before going live)

```bash
# Full 90-day exhaustive run — takes 3–4 hours
py -3.11 backtest/backtest_runner.py --days 90 --step 1

# Faster scan (every 4 hours, ~15 min)
py -3.11 backtest/backtest_runner.py --days 90

# Diagnose why the PA gate is blocking setups
py -3.11 backtest/backtest_runner.py --diagnose --days 14 --pair EURUSD

# Single pair verbose
py -3.11 backtest/backtest_runner.py --pair EURUSD --verbose
```

### ARCS-PROP (FundedNext challenge)

```bash
py -3.11 prop_main.py --dashboard  # Dashboard on port 5001
```

### Crypto Weekend bot

```bash
py -3.11 crypto_weekend/main.py
```

---

## Live Dashboard

Open `http://localhost:5000` after starting the bot with `--dashboard`.

Auto-refreshes every 15 seconds — no manual reload needed.

| Panel | Data Source | Refresh |
|-------|------------|---------|
| Bot status + session | `bot_status.json` | 15s |
| Account bar (balance, equity, float P&L) | `bot_status.json` via MT5 | 15s |
| Regime monitor (8 pair cards) | `bot_status.json` | 15s |
| Open positions + live P&L | `bot_status.json` via MT5 | 15s |
| Gate analytics (quiet / chaotic / PA / conf / risk) | `bot_status.json` | 15s |
| KPI cards (win rate, total P&L, avg R, avg confidence) | `trades.db` | 30s |
| Equity curve chart | `trades.db` | 30s |
| Recent trades — full DNA table (20 rows) | `trades.db` | 30s |
| Circuit breakers (daily/weekly progress bars) | `risk_state.json` | 15s |
| Performance by session / pair / signal type | `trades.db` | 60s |
| Confidence weights bar chart | `weights.json` | 60s |
| Live log (80 lines, colour-coded) | `logs/arcs_fx.log` | 15s |

---

## Configuration

All parameters live in `config.py`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `PAIRS` | 8 pairs | EURUSD, GBPUSD, AUDUSD, USDJPY, USDCHF, USDCAD, NZDUSD, EURJPY |
| `CONFIDENCE_MIN` | 70 | Normal mode threshold |
| `RISK_BASE_PCT` | 1.0 | Base risk per trade (% of account) |
| `RISK_MAX_PCT` | 2.0 | Hard ceiling |
| `DAILY_LOSS_LIMIT_PCT` | 3.0 | Stop for the day |
| `WEEKLY_LOSS_LIMIT_PCT` | 6.0 | Stop for the week |
| `MAX_CONSECUTIVE_LOSSES` | 3 | Trigger 2-hour cooldown |
| `MAX_OPEN_TRADES` | 3 | Max concurrent positions |
| `MAIN_LOOP_INTERVAL_S` | 60 | Tick frequency |
| `DATA_REFRESH_INTERVAL_S` | 300 | OHLCV cache refresh |

---

## Signal Chain

```
Every 60 seconds, for each of 8 pairs:

[1] OHLCV cache ready?                      No  ──► skip
[2] Regime detect (H1 + M15)                CHAOTIC / QUIET ──► skip
[3] News evaluation                         BLACKOUT ──► skip
[4] Session filter                          OFF session ──► skip
[5] Price action engine                     No signal ──► skip
[6] Edge Stack (9 engines → meta_filter)    Low edge score ──► penalise
[7] Confidence score (7 components)         < 70 ──► skip with reason logged
[8] Risk evaluation                         CB fired / correlated / at max ──► skip
[9] Open trade                              Place order, write DNA to DB

Every tick (unconditionally):
    ├─ BE at +0.8–1.2R (strategy-family-specific)
    ├─ Trail at +1.5–2.5R
    ├─ Mid-trade news blackout ──► tighten TP
    ├─ Regime shift ──► close position
    └─ SL/TP hit ──► log close record to DB

Every Sunday 22:00 UTC:
    ├─ Generate weekly HTML report
    ├─ Run StrategyAdjuster (rebalance weights)
    └─ Save weights.json + append to weights_history.jsonl
```

---

## Safety Rules

These are hardcoded and cannot be disabled:

1. **Never trade in CHAOTIC or QUIET regime** — bot goes fully silent
2. **Never trade during news blackout** — 30 min before/after HIGH-impact events
3. **3% daily loss limit** — waits for midnight UTC reset
4. **6% weekly loss limit** — waits for Monday 00:00 UTC reset
5. **3 consecutive losses** — 2-hour mandatory cooldown
6. **Max 3 open trades** — correlation guard still active
7. **Never exceed 2% risk per trade** — hardcoded, ignores win streak
8. **No credentials in code** — always loaded from `.env`
9. **Graceful shutdown** — CTRL+C closes all positions before disconnecting
10. **SL/TP attached server-side** — broker protects positions if Python dies

---

## ARCS-PROP

A separate challenge-mode bot for passing a FundedNext $10k evaluation.

```
prop/
├── prop_config.py      # Challenge parameters (phase targets, DDs)
├── challenge_state.py  # Phase 1 → Phase 2 → Funded state machine
├── dynamic_risk.py     # Phase-aware sizing (tighter as target approaches)
└── equity_watchdog.py  # Hard equity floor enforcement
```

Dashboard on port 5001: `py -3.11 prop_main.py --dashboard`

---

## Crypto Weekend Bot

Trades BTC, ETH, and SOL via MT5 crypto CFDs during weekend sessions when the main Forex bot is idle.

```
crypto_weekend/
├── config.py      # Crypto-specific parameters
├── strategy.py    # Signal logic for crypto instruments
├── main.py        # Weekend session orchestrator
└── mt5_bridge.py  # MT5 bridge for crypto CFDs
```

---

## Auto-start on Windows Boot (optional)

1. Press `Win + R` → type `taskschd.msc`
2. Create Basic Task → Name: `ARCS-FX`
3. Trigger: **When the computer starts**
4. Action: Start a program → `C:\path\to\ARCS-FX\start_arcs_fx.bat`

Dashboard available at `http://localhost:5000` on every boot.

---

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Foundation: MT5, data fetcher, config, main loop | Done |
| 2 | Intelligence: regime, price action, news, confidence | Done |
| 3 | Risk: sizing, circuit breakers, correlation guard | Done |
| 4 | Execution: orders, asymmetric management, trade logger | Done |
| 5 | Learning: heatmap, pattern analyzer, strategy adjuster | Done |
| 6 | Integration: orchestrator, backtest runner | Done |
| 7 | Dashboard: Flask live UI, bot_status, Windows launcher | Done |
| 8 | Edge stack: 9 engines + meta_filter + wiring | Done |
| 9 | Rebuild: 5-state regime, engine split, bug fixes | Done |
| — | Paper trade: 2-week live demo run | **In progress** |
| — | Prop challenge: ARCS-PROP FundedNext $10k | **In progress** |

---

## Notes

- Bot is **demo / paper only** until at least 50 live trades validate the edge
- FinBERT is not installed by default — `py -3.11 -m pip install transformers torch`
- PDF reports require reportlab — `py -3.11 -m pip install reportlab`
- Delete `data/weights.json` to reset confidence weights to config defaults
- Delete `data/risk_state.json` to reset all circuit breaker counters (use with care)
- Do **not** run the bot and backtest simultaneously — both connect to MT5

---

<p align="center">Built by <a href="https://github.com/Fadi-AICH">Fadi AICH</a></p>
