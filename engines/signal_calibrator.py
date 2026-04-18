"""
ARCS-FX -- engines/signal_calibrator.py
Train a small logistic model to turn the confidence score + DNA
features into a CALIBRATED P(win).

WHY THIS EXISTS:
Our hand-tuned 7-component confidence score is a useful heuristic,
but it is not calibrated: a "score=80" trade isn't empirically an
80% winner. Lopez de Prado's meta-labelling approach (AFML, 2018)
trains a SECONDARY model that learns *when our primary model is
right* based on observed outcomes. Hedge-fund-grade version of
"just trust your rules."

HOW IT WORKS:
  1. Read trades.db for all CLOSED trades.
  2. For each trade, build a feature vector:
       - confidence score
       - regime  (one-hot)
       - session (one-hot)
       - signal_type (one-hot)
       - spread_at_entry
       - news_score_at_entry
       - day_of_week
       - r_ratio
  3. Target = won (1/0).
  4. Train a logistic regression with L2 regularisation.
  5. Persist model + scaler to data/calibrator.pkl
  6. predict_win_probability(features) returns P(win) in [0,1].

SAFETY:
  * Requires MIN_TRADES_FOR_TRAINING to fit (default 50).
  * Under that we SKIP training and return None -- the meta_filter
    falls back to raw confidence.
  * Won't retrain more than once a day.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
MIN_TRADES_FOR_TRAINING = 50
MODEL_PATH   = "data/calibrator.pkl"
META_PATH    = "data/calibrator_meta.json"

# Categorical vocabularies (stable ordering; unknown values get zero vector)
REGIMES      = ["TRENDING_CLEAN", "TRENDING_EXTENDED", "RANGING_CLEAN",
                "QUIET", "CHAOTIC"]
SESSIONS     = ["ASIAN", "LONDON", "NY", "OVERLAP"]
SIGNAL_TYPES = ["OB_RETEST", "SD_BOUNCE", "FVG_FILL",
                "RANGE_FADE", "TREND_PULLBACK", "LIQ_SWEEP"]


@dataclass
class CalibratorPrediction:
    p_win: float            # in [0,1]
    edge_r: float           # expected R-multiple using current p_win (at 1:2 R:R default)
    feature_snapshot: dict


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def _one_hot(value: str, vocab: list[str]) -> list[float]:
    vec = [0.0] * len(vocab)
    if value in vocab:
        vec[vocab.index(value)] = 1.0
    return vec


def build_features(
    confidence: float,
    regime: str,
    session: str,
    signal_type: str,
    spread_pips: float,
    news_score: float,
    day_of_week: int,
    r_ratio: float,
) -> np.ndarray:
    """Build the feature vector used by the calibrator. Order is stable."""
    num = [
        float(confidence) / 100.0,
        float(spread_pips),
        float(news_score),
        float(day_of_week) / 6.0,
        float(r_ratio),
    ]
    cat = _one_hot(regime, REGIMES) \
        + _one_hot(session, SESSIONS) \
        + _one_hot(signal_type, SIGNAL_TYPES)
    return np.array(num + cat, dtype=float)


FEATURE_NAMES = (
    ["confidence", "spread_pips", "news_score", "day_of_week_norm", "r_ratio"]
    + [f"regime_{r}" for r in REGIMES]
    + [f"session_{s}" for s in SESSIONS]
    + [f"signal_{t}" for t in SIGNAL_TYPES]
)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def _load_training_data(db_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Pull closed trades from the DB and build (X, y)."""
    if not os.path.exists(db_path):
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0, dtype=int)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT confidence, regime, session, signal_type,
                   spread_at_entry, news_score_at_entry,
                   day_of_week, r_ratio, won
              FROM trades
             WHERE won IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return np.empty((0, len(FEATURE_NAMES))), np.empty(0, dtype=int)

    X_list, y_list = [], []
    for r in rows:
        try:
            vec = build_features(
                confidence  = r["confidence"]         or 0.0,
                regime      = r["regime"]             or "",
                session     = r["session"]            or "",
                signal_type = r["signal_type"]        or "",
                spread_pips = r["spread_at_entry"]    or 0.0,
                news_score  = r["news_score_at_entry"] or 0.0,
                day_of_week = r["day_of_week"]        or 0,
                r_ratio     = r["r_ratio"]            or 0.0,
            )
            X_list.append(vec)
            y_list.append(int(r["won"]))
        except Exception as exc:
            logger.debug("[CAL] skip row: %s", exc)

    return np.vstack(X_list), np.array(y_list, dtype=int)


def train(db_path: str = "data/trades.db",
          model_path: str = MODEL_PATH,
          meta_path: str = META_PATH) -> Optional[dict]:
    """
    Train (or refit) the calibration model. Returns a meta dict on success.
    Safe to call often -- short-circuits if trade count is below threshold.
    """
    X, y = _load_training_data(db_path)
    n = len(y)
    if n < MIN_TRADES_FOR_TRAINING:
        logger.info("[CAL] Only %d trades -- need %d before calibration kicks in.",
                    n, MIN_TRADES_FOR_TRAINING)
        return None

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        logger.warning("[CAL] sklearn not installed -- calibrator disabled.")
        return None

    # Stratify check: need both classes
    if len(np.unique(y)) < 2:
        logger.warning("[CAL] Only one class represented (all wins or all losses).")
        return None

    scaler = StandardScaler(with_mean=True, with_std=True)
    X_scaled = scaler.fit_transform(X)

    model = LogisticRegression(
        C=1.0, penalty="l2", solver="lbfgs", max_iter=500,
        class_weight="balanced",
    )
    model.fit(X_scaled, y)

    # In-sample accuracy and base rate
    preds = model.predict(X_scaled)
    acc = float((preds == y).mean())
    base_rate = float(y.mean())

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    with open(model_path, "wb") as f:
        pickle.dump({"scaler": scaler, "model": model,
                     "feature_names": FEATURE_NAMES}, f)

    meta = {
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_trades":       n,
        "accuracy":       round(acc, 3),
        "base_rate":      round(base_rate, 3),
        "feature_names":  FEATURE_NAMES,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    logger.info("[CAL] Trained on %d trades | acc=%.2f | base=%.2f",
                n, acc, base_rate)
    return meta


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

_model_cache: dict = {}


def _load_model(model_path: str = MODEL_PATH) -> Optional[dict]:
    if "bundle" in _model_cache:
        return _model_cache["bundle"]
    if not os.path.exists(model_path):
        return None
    try:
        with open(model_path, "rb") as f:
            bundle = pickle.load(f)
        _model_cache["bundle"] = bundle
        return bundle
    except Exception as exc:
        logger.warning("[CAL] Failed to load model: %s", exc)
        return None


def predict_win_probability(
    confidence: float,
    regime: str,
    session: str,
    signal_type: str,
    spread_pips: float,
    news_score: float,
    day_of_week: int,
    r_ratio: float,
) -> Optional[CalibratorPrediction]:
    """
    Return calibrated P(win). None if model is not yet trained.
    """
    bundle = _load_model()
    if not bundle:
        return None

    vec = build_features(confidence, regime, session, signal_type,
                         spread_pips, news_score, day_of_week, r_ratio)
    X = bundle["scaler"].transform(vec.reshape(1, -1))
    try:
        p = float(bundle["model"].predict_proba(X)[0][1])
    except Exception as exc:
        logger.warning("[CAL] predict_proba failed: %s", exc)
        return None

    # Edge in R-multiples assuming the trade's own r_ratio as reward:risk
    edge_r = p * max(r_ratio, 1.0) - (1.0 - p) * 1.0

    return CalibratorPrediction(
        p_win=round(p, 4),
        edge_r=round(edge_r, 3),
        feature_snapshot={
            "confidence": confidence, "regime": regime, "session": session,
            "signal_type": signal_type, "spread_pips": spread_pips,
            "news_score": news_score, "day_of_week": day_of_week,
            "r_ratio": r_ratio,
        },
    )


def invalidate_cache() -> None:
    """Call after training so the next predict() reloads the new model."""
    _model_cache.pop("bundle", None)


# ---------------------------------------------------------------------------
# Standalone train
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s -- %(message)s")
    meta = train()
    print("Training result:", meta)
