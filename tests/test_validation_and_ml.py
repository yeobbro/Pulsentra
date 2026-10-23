"""Required tests 10-12: leakage prevention, chronological splits, model metrics."""
import numpy as np
import pandas as pd
import pytest

from app.demo import make_demo_data
from app.indicators import ML_FEATURES, compute_indicators
from app.models.lab import MLConfig, _clf_metrics, build_dataset, check_feature_causality, run_lab, walk_forward_ml
from app.models.labels import forward_direction, triple_barrier
from app.strategies import get_strategy, list_strategies
from app.validation import time_folds


@pytest.fixture(scope="module")
def demo():
    return make_demo_data(days=600, n_symbols=6, end="2026-09-30", seed=5)


# 10. leakage: features and signals at t must not change when the future changes
def test_features_do_not_use_future_bars(demo):
    raw = demo[demo["symbol"] == "DEMO001"].reset_index(drop=True)
    res = check_feature_causality(raw, ML_FEATURES, samples=10)
    assert res["passed"] is True and res["checked"] == 10


def test_every_indicator_is_causal(demo):
    raw = demo[demo["symbol"] == "DEMO003"].reset_index(drop=True)
    full = compute_indicators(raw)
    numeric = [c for c in full.columns if c not in ("market", "symbol", "date", "source", "quality_flags", "adjusted")]
    for cut in (130, 260, 400):
        part = compute_indicators(raw.iloc[: cut + 1])
        a = full.loc[cut, numeric].astype(float).to_numpy()
        b = part.iloc[-1][numeric].astype(float).to_numpy()
        bad = [numeric[i] for i in range(len(numeric)) if not np.isclose(a[i], b[i], equal_nan=True)]
        assert not bad, f"row {cut}: {bad}"


def test_signals_unchanged_by_future_shock(demo):
    raw = demo[demo["symbol"] == "DEMO002"].reset_index(drop=True)
    shocked = raw.copy()
    cut = 400
    shocked.loc[cut + 1:, ["open", "high", "low", "close"]] *= 3.0
    shocked.loc[cut + 1:, "volume"] *= 50
    for key in ["volume_quiet", "volume_quiet_breakout", "breakout_volume", "pullback_uptrend", "ma_trend", "rsi_rebound", "bb_squeeze"]:
        s = get_strategy(key)
        a = s.signals(compute_indicators(raw)).iloc[: cut + 1]
        b = s.signals(compute_indicators(shocked)).iloc[: cut + 1]
        assert a.equals(b), key


def test_labels_look_forward_and_record_end(bars):
    close = np.full(40, 100.0)
    x = compute_indicators(bars(close, highs=np.full(40, 102.0), lows=np.full(40, 98.0)))
    tb = triple_barrier(x, horizon=5)
    assert tb.loc[20, "tb_outcome"] == "TIMEOUT"
    assert pd.Timestamp(tb.loc[20, "label_end"]) == x.loc[25, "date"]
    assert tb.loc[38, "tb_outcome"] is None  # not enough future bars: no label, never filled
    fd = forward_direction(x, 5)
    assert pd.Timestamp(fd.loc[20, "label_end"]) == x.loc[25, "date"] and np.isnan(fd.loc[36, "y"])


# 11. chronological split with a purge gap
def test_time_folds_are_ordered_and_gapped():
    dates = pd.bdate_range("2023-01-02", periods=500)
    folds = time_folds(dates, n_folds=4, gap=11)
    assert len(folds) == 4
    for f in folds:
        assert f.train_end < f.test_start
        assert len(pd.bdate_range(f.train_end, f.test_start)) - 2 >= 11
    for a, b in zip(folds, folds[1:]):
        assert a.test_end < b.test_start


def test_walk_forward_purges_overlapping_labels(demo):
    cfg = MLConfig(allow_synthetic=True, n_folds=3, models=("logistic",))
    ds = build_dataset(demo, cfg)
    res = walk_forward_ml(ds, cfg)
    folds = [f for f in res["folds"] if "models" in f]
    assert folds
    for f in folds:
        assert f["purged_rows"] > 0  # rows whose label window reaches into the test window were dropped
        assert pd.Timestamp(f["train_label_end_max"]) < pd.Timestamp(f["test_start"])
    # every OOS prediction date lies inside a test window
    preds = res["oos_predictions"]["logistic"]
    starts = min(pd.Timestamp(f["test_start"]) for f in folds)
    assert preds["date"].min() >= starts


# 12. model metrics
def test_classification_metrics_against_manual():
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.4, 0.35, 0.8])
    m = _clf_metrics(y, p, base_rate=0.5)
    assert m["roc_auc"] == pytest.approx(0.75)
    assert m["brier"] == pytest.approx(np.mean((p - y) ** 2))
    assert m["brier_climatology"] == pytest.approx(0.25)
    assert m["brier_skill"] == pytest.approx(1 - m["brier"] / 0.25)
    assert m["precision"] == 1.0 and m["recall"] == 0.5


def test_ml_lab_refuses_synthetic_by_default_and_never_adopts(demo):
    with pytest.raises(ValueError):
        run_lab(demo, MLConfig())
    rep = run_lab(demo, MLConfig(allow_synthetic=True, n_folds=3, models=("logistic", "hist_gb")), model_dir=None)
    assert rep["adoption"]["adopted"] is False
    assert any("합성" in r for r in rep["adoption"]["reasons"]["logistic"])
    assert rep["leakage_checks"]["feature_causality"][0]["passed"] is True


def test_strategy_registry_lists_unavailable_honestly():
    items = {s["key"]: s for s in list_strategies()}
    assert items["disclosure_event"]["available"] is False
    assert {"volume_quiet", "volume_quiet_breakout", "breakout_volume", "pullback_uptrend", "ma_trend",
            "rsi_rebound", "bb_squeeze", "momentum_rs", "random_entry"} <= set(items)
