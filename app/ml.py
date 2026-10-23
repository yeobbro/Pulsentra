"""MVP ML experiment (kept for reference only; the API no longer uses it).

Replaced by app.models.lab: purged walk-forward, calibration and an adoption gate. This module
trained a single 80/20 holdout despite the name `train_walk_forward`.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score, brier_score_loss, confusion_matrix
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
import joblib
from app.indicators import compute_indicators, FEATURE_COLUMNS
from app.settings import MODEL_PATH, MODEL_META_PATH

TARGET_NAME = "target_hit_before_stop"


def _triple_barrier_label(x: pd.DataFrame, i: int, atr_mult: float, reward_risk: float, horizon: int) -> int | None:
    """Label 1 if upper target is reached before stop; 0 otherwise.
    Signal uses close[i], entry uses open[i+1]. If stop and target hit in one bar,
    count it as a loss. Gaps through stop count as losses.
    """
    if i + 1 >= len(x):
        return None
    atr = x.iloc[i].get("atr_14", np.nan)
    entry = x.iloc[i + 1].get("open", np.nan)
    if pd.isna(atr) or pd.isna(entry) or atr <= 0 or entry <= 0:
        return None
    risk = float(atr_mult) * float(atr)
    stop = float(entry) - risk
    target = float(entry) + float(reward_risk) * risk
    end = min(i + 1 + int(horizon), len(x))
    for j in range(i + 1, end):
        row = x.iloc[j]
        o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
        if o <= stop:
            return 0
        if o >= target:
            return 1
        hit_s, hit_t = l <= stop, h >= target
        if hit_s and hit_t:
            return 0
        if hit_s:
            return 0
        if hit_t:
            return 1
    # Binary target is deliberately strict: only a target touch before stop is positive.
    # A timeout without target touch is labelled 0, even if close is slightly above entry.
    return 0


def build_training_dataset(frame: pd.DataFrame, horizon: int = 10, atr_mult: float = 1.5, reward_risk: float = 2.0) -> pd.DataFrame:
    rows = []
    if frame.empty:
        return pd.DataFrame()
    for (market, symbol), raw in frame.groupby(["market", "symbol"], sort=False):
        x = compute_indicators(raw).reset_index(drop=True)
        for i in range(len(x) - horizon - 1):
            row = x.iloc[i]
            label = _triple_barrier_label(x, i, atr_mult, reward_risk, horizon)
            if label is None:
                continue
            features = {col: row.get(col, np.nan) for col in FEATURE_COLUMNS}
            if sum(pd.isna(v) for v in features.values()) > len(FEATURE_COLUMNS) // 2:
                continue
            features.update({"date": pd.Timestamp(row["date"]), "market": market, "symbol": symbol, TARGET_NAME: label})
            rows.append(features)
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True) if rows else pd.DataFrame()


def train_walk_forward(frame: pd.DataFrame, model_path: Path | str | None = None, meta_path: Path | str | None = None,
                       horizon: int = 10, atr_mult: float = 1.5, reward_risk: float = 2.0) -> dict:
    dataset = build_training_dataset(frame, horizon=horizon, atr_mult=atr_mult, reward_risk=reward_risk)
    if len(dataset) < 500:
        raise ValueError(f"학습 데이터가 부족합니다: {len(dataset)}개. 권장 최소 500개 라벨이며 다양한 종목과 긴 이력이 필요합니다.")
    if dataset["date"].nunique() < 30:
        raise ValueError("시간 순서 검증에 필요한 날짜가 부족합니다.")
    # Time-ordered holdout, not random split. Date cut prevents same-day leakage across instruments.
    unique_dates = sorted(dataset["date"].dropna().unique())
    cut_index = int(len(unique_dates) * 0.8)
    cut_date = unique_dates[cut_index]
    # Purge training rows whose forward-label window could overlap the test period.
    purge_index = max(0, cut_index - int(horizon) - 1)
    purge_before = unique_dates[purge_index]
    train = dataset[dataset["date"] < purge_before].copy()
    test = dataset[dataset["date"] >= cut_date].copy()
    if train[TARGET_NAME].nunique() < 2 or test[TARGET_NAME].nunique() < 2:
        raise ValueError("시간순 학습/검증 구간 중 하나에 라벨 클래스가 하나뿐입니다. 데이터 기간·종목을 늘리세요.")
    x_train, y_train = train[FEATURE_COLUMNS], train[TARGET_NAME].astype(int)
    x_test, y_test = test[FEATURE_COLUMNS], test[TARGET_NAME].astype(int)

    # Baseline: train-set majority-class probability. Helps determine whether ML has real value.
    majority = int(y_train.mean() >= 0.5)
    baseline_pred = np.full(len(y_test), majority, dtype=int)
    baseline_acc = accuracy_score(y_test, baseline_pred)

    pipeline = Pipeline([
        ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
        ("model", HistGradientBoostingClassifier(max_iter=180, learning_rate=0.06, max_leaf_nodes=15,
                                                  l2_regularization=1.0, random_state=42)),
    ])
    pipeline.fit(x_train, y_train)
    probs = pipeline.predict_proba(x_test)[:, 1]
    pred = (probs >= 0.5).astype(int)
    metrics = {
        "rows_total": int(len(dataset)), "rows_train": int(len(train)), "rows_test": int(len(test)),
        "train_start": train["date"].min().strftime("%Y-%m-%d"), "train_end": train["date"].max().strftime("%Y-%m-%d"),
        "test_start": test["date"].min().strftime("%Y-%m-%d"), "test_end": test["date"].max().strftime("%Y-%m-%d"),
        "positive_label_rate_test": float(y_test.mean()),
        "baseline_accuracy": float(baseline_acc), "accuracy": float(accuracy_score(y_test, pred)),
        "precision": float(precision_score(y_test, pred, zero_division=0)),
        "recall": float(recall_score(y_test, pred, zero_division=0)),
        "f1": float(f1_score(y_test, pred, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_test, probs)),
        "brier_score": float(brier_score_loss(y_test, probs)),
        "confusion_matrix_labels_0_1": confusion_matrix(y_test, pred, labels=[0, 1]).tolist(),
        "feature_columns": FEATURE_COLUMNS,
        "target": "target_hit_before_stop (a timeout without target touch is negative)",
        "horizon_trading_days": int(horizon), "atr_stop_multiple": float(atr_mult), "reward_risk": float(reward_risk),
        "validation": "chronological ~80/20 holdout with a training-label purge gap before test; not yet walk-forward cross-validation; do not treat as a live return forecast",
        "model": "HistGradientBoostingClassifier", "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_warning": "Must exclude demo/synthetic rows and include delisted symbols/corporate-action adjustments for credible research.",
    }
    mpath = Path(model_path or MODEL_PATH)
    metapath = Path(meta_path or MODEL_META_PATH)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    metapath.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, mpath)
    metapath.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics
