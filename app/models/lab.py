"""ML Model Lab: time-ordered, purged walk-forward comparison of simple models against baselines.

Nothing here assumes a model is useful. A model is ADOPTED for predictions only when the
adoption gate passes on out-of-sample folds; otherwise the run is stored as REJECTED with the
reasons, and the prediction API refuses to serve it.
"""
from __future__ import annotations

import hashlib
import json
import math
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, f1_score, log_loss, precision_score, recall_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.backtesting.metrics import bootstrap_mean_ci, max_consecutive
from app.data.schema import is_demo_source
from app.indicators import ML_FEATURES, compute_indicators
from app.models.labels import forward_direction, triple_barrier
from app.risk import CostModel
from app.strategies import get_strategy
from app.validation import regime_series, time_folds

SHORT_FEATURES = ["ret_1", "ret_5", "volume_spike_1d", "clv_20", "rsi_14", "atr_pct_14", "bb_width_20", "close_to_ma20",
                  "volume_ratio_20", "up_down_volume_ratio_20"]
LONG_FEATURES = ["ret_20", "ret_60", "ret_120", "ret_252", "ma20_over_ma60", "close_to_ma60", "dist_high60", "rel_ret_60",
                 "volatility_20", "log_avg_turnover_20", "volume_ratio_20"]
FEATURE_SETS = {"short": SHORT_FEATURES, "full": ML_FEATURES, "long": LONG_FEATURES}
HORIZON_FEATURE_SET = {1: "short", 5: "short", 20: "full", 60: "full", 252: "long"}
MODEL_NAMES = ("logistic", "random_forest", "hist_gb")


@dataclass
class MLConfig:
    task: str = "triple_barrier"         # triple_barrier | direction
    horizon: int = 10                    # trading days (direction: 1, 5, 20, 60, 252 suggested)
    atr_mult: float = 1.5
    target_r: float = 2.0
    universe: str = "all"                # all | signals (rows where `base_strategy` fires)
    base_strategy: str = "volume_quiet"
    feature_set: str | None = None       # short | full | long; default by horizon
    n_folds: int = 4
    models: tuple[str, ...] = MODEL_NAMES
    allow_synthetic: bool = False        # True only for tests/demo; such runs are never adopted
    min_rows: int = 300
    seed: int = 42

    @classmethod
    def from_dict(cls, d: dict | None) -> "MLConfig":
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in (d or {}).items() if k in known})
        if cfg.task not in {"triple_barrier", "direction"}:
            raise ValueError("task must be triple_barrier or direction")
        if not (1 <= cfg.horizon <= 252):
            raise ValueError("horizon must be 1..252")
        if cfg.universe not in {"all", "signals"}:
            raise ValueError("universe must be all or signals")
        bad = set(cfg.models) - set(MODEL_NAMES)
        if bad:
            raise ValueError(f"unknown models {sorted(bad)}")
        cfg.models = tuple(cfg.models)
        return cfg

    def features(self) -> list[str]:
        name = self.feature_set or (HORIZON_FEATURE_SET.get(self.horizon, "full") if self.task == "direction" else "full")
        return FEATURE_SETS[name]


# ---------------------------------------------------------------- dataset

def build_dataset(frame: pd.DataFrame, cfg: MLConfig, benchmark: pd.DataFrame | None = None,
                  costs: CostModel | None = None) -> pd.DataFrame:
    costs = costs or CostModel()
    feats = cfg.features()
    strat = get_strategy(cfg.base_strategy)
    parts = []
    for (market, symbol), raw in frame.groupby(["market", "symbol"], sort=True):
        x = compute_indicators(raw, benchmark).reset_index(drop=True)
        if len(x) < 60:
            continue
        sig = strat.signals(x)
        if cfg.task == "triple_barrier":
            lab = triple_barrier(x, cfg.atr_mult, cfg.target_r, cfg.horizon, costs)
            lab["y"] = (lab["tb_outcome"] == "TARGET").astype(float).where(lab["tb_outcome"].notna())
            lab["outcome_value"] = lab["tb_r"]  # economic value of acting on the row, in R
        else:
            lab = forward_direction(x, cfg.horizon, costs)
            lab["outcome_value"] = lab["fwd_net"]
        d = pd.concat([x[["date"] + [f for f in feats if f in x]], lab], axis=1)
        for f in feats:
            if f not in d:
                d[f] = np.nan
        d["market"], d["symbol"], d["rule_signal"] = market, symbol, sig.to_numpy()
        d["source"] = ",".join(sorted(raw["source"].dropna().unique())) if "source" in raw else ""
        d = d[d["y"].notna()]
        # rows with most features unknown carry no information
        d = d[d[feats].notna().sum(axis=1) >= math.ceil(len(feats) * 0.6)]
        if cfg.universe == "signals":
            d = d[d["rule_signal"]]
        parts.append(d)
    if not parts:
        return pd.DataFrame()
    ds = pd.concat(parts, ignore_index=True).sort_values(["date", "market", "symbol"]).reset_index(drop=True)
    ds["label_end"] = pd.to_datetime(ds["label_end"])
    return ds


def check_feature_causality(raw: pd.DataFrame, features: list[str], samples: int = 8,
                            benchmark: pd.DataFrame | None = None) -> dict:
    """Leakage test: features of row i must be identical whether or not bars after i exist."""
    x_full = compute_indicators(raw, benchmark).reset_index(drop=True)
    n = len(x_full)
    if n < 80:
        return {"checked": 0, "passed": None, "note": "이력이 짧아 검사하지 않음"}
    idxs = np.linspace(60, n - 2, num=min(samples, n - 61)).astype(int)
    mismatches = []
    for i in idxs:
        cut_date = x_full.at[i, "date"]
        truncated = raw[pd.to_datetime(raw["date"]) <= cut_date]
        x_cut = compute_indicators(truncated, benchmark).reset_index(drop=True)
        a, b = x_full.loc[i, features].astype(float), x_cut.iloc[-1][features].astype(float)
        diff = [f for f in features if not (np.isclose(a[f], b[f], equal_nan=True, rtol=1e-9, atol=1e-12))]
        if diff:
            mismatches.append({"date": pd.Timestamp(cut_date).strftime("%Y-%m-%d"), "features": diff})
    return {"checked": int(len(idxs)), "passed": not mismatches, "mismatches": mismatches[:5]}


# ---------------------------------------------------------------- models

def _make_model(name: str, seed: int):
    if name == "logistic":
        return Pipeline([("imp", SimpleImputer(strategy="median", add_indicator=True)), ("sc", StandardScaler()),
                         ("m", LogisticRegression(max_iter=2000, C=0.5))])
    if name == "random_forest":
        return Pipeline([("imp", SimpleImputer(strategy="median", add_indicator=True)),
                         ("m", RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=40, n_jobs=-1, random_state=seed))])
    if name == "hist_gb":
        return Pipeline([("imp", SimpleImputer(strategy="median", add_indicator=True)),
                         ("m", HistGradientBoostingClassifier(max_iter=150, learning_rate=0.05, max_leaf_nodes=15,
                                                              l2_regularization=1.0, random_state=seed))])
    raise ValueError(name)


class _Calibrated:
    """Model fitted on the early part of a training window, calibrated on its later part."""

    def __init__(self, model, method: str):
        self.model, self.method, self.cal = model, method, None

    def fit(self, X, y, Xc, yc):
        self.model.fit(X, y)
        raw = self.model.predict_proba(Xc)[:, 1]
        if len(np.unique(yc)) < 2:
            self.method = "none"
            return self
        if self.method == "isotonic":
            self.cal = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, yc)
        else:
            self.cal = LogisticRegression().fit(_logit(raw).reshape(-1, 1), yc)
        return self

    def predict_proba(self, X) -> np.ndarray:
        raw = self.model.predict_proba(X)[:, 1]
        if self.cal is None:
            return raw
        if self.method == "isotonic":
            return self.cal.predict(raw)
        return self.cal.predict_proba(_logit(raw).reshape(-1, 1))[:, 1]


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _clf_metrics(y: np.ndarray, p: np.ndarray, base_rate: float) -> dict:
    out: dict[str, Any] = {"n": int(len(y)), "positive_rate": float(y.mean()) if len(y) else None}
    if len(y) == 0:
        return out
    pred = (p >= 0.5).astype(int)
    two = len(np.unique(y)) == 2
    clim = np.full(len(y), base_rate)
    brier = float(brier_score_loss(y, p))
    brier_ref = float(brier_score_loss(y, clim))
    out.update({"roc_auc": float(roc_auc_score(y, p)) if two else None,
                "precision": float(precision_score(y, pred, zero_division=0)), "recall": float(recall_score(y, pred, zero_division=0)),
                "f1": float(f1_score(y, pred, zero_division=0)), "brier": brier, "brier_climatology": brier_ref,
                "brier_skill": (1 - brier / brier_ref) if brier_ref > 0 else None,
                "log_loss": float(log_loss(y, np.clip(p, 1e-6, 1 - 1e-6), labels=[0, 1]))})
    return out


def _reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (p >= a) & ((p < b) if b < 1 else (p <= b))
        if m.sum():
            rows.append({"bin": f"{a:.1f}-{b:.1f}", "n": int(m.sum()), "mean_predicted": float(p[m].mean()), "observed_rate": float(y[m].mean())})
    return rows


def _economic(values: np.ndarray, dates: np.ndarray) -> dict:
    v = values[np.isfinite(values)]
    if not len(v):
        return {"n": 0, "mean": None, "mean_ci95": None, "max_drawdown_cum": None, "max_consecutive_losses": 0}
    order = np.argsort(dates[np.isfinite(values)], kind="stable")
    seq = v[order]
    cum = np.cumsum(seq)
    dd = float((cum - np.maximum.accumulate(np.r_[0, cum])[1:]).min())
    return {"n": int(len(v)), "mean": float(v.mean()), "mean_ci95": bootstrap_mean_ci(v),
            "max_drawdown_cum": dd, "max_consecutive_losses": max_consecutive([x <= 0 for x in seq])}


def walk_forward_ml(ds: pd.DataFrame, cfg: MLConfig, regimes: pd.Series | None = None) -> dict:
    # A feature that is unknown for every row (e.g. relative strength without a benchmark) carries no information.
    feats = [f for f in cfg.features() if ds[f].notna().any()]
    dropped = [f for f in cfg.features() if f not in feats]
    folds = time_folds(ds["date"], n_folds=cfg.n_folds, gap=0)
    if not folds:
        raise ValueError("시간순 분할에 필요한 날짜가 부족합니다.")
    value_unit = "R (손절폭 대비 순손익)" if cfg.task == "triple_barrier" else "순수익률"
    per_fold, oos = [], {m: [] for m in cfg.models}
    oos_base = []
    for f in folds:
        test = ds[(ds["date"] >= f.test_start) & (ds["date"] <= f.test_end)]
        # PURGE: a training row is usable only if its label ended before the test window starts.
        train_all = ds[(ds["date"] >= f.train_start) & (ds["date"] <= f.train_end) & (ds["label_end"] < f.test_start)]
        if len(train_all) < 100 or len(test) < 30 or train_all["y"].nunique() < 2:
            per_fold.append({**f.to_dict(), "skipped": "학습 또는 검증 표본 부족"})
            continue
        # chronological calibration split inside the training window (also purged)
        cal_dates = sorted(train_all["date"].unique())
        cut = cal_dates[int(len(cal_dates) * 0.8)]
        fit_part = train_all[(train_all["date"] < cut) & (train_all["label_end"] < cut)]
        cal_part = train_all[train_all["date"] >= cut]
        if len(fit_part) < 80 or fit_part["y"].nunique() < 2:
            fit_part, cal_part = train_all, train_all.iloc[0:0]
        base_rate = float(train_all["y"].mean())
        row: dict[str, Any] = {**f.to_dict(), "train_rows": int(len(train_all)), "purged_rows": int(
            ((ds["date"] >= f.train_start) & (ds["date"] <= f.train_end) & (ds["label_end"] >= f.test_start)).sum()),
            "test_rows": int(len(test)), "train_positive_rate": base_rate,
            "train_label_end_max": train_all["label_end"].max().strftime("%Y-%m-%d"), "models": {}}
        y_test = test["y"].to_numpy(int)
        # Baseline 1: climatology (train positive rate). Baseline 2: the rule signal itself.
        rule_rate_on = float(train_all.loc[train_all["rule_signal"], "y"].mean()) if train_all["rule_signal"].any() else base_rate
        rule_rate_off = float(train_all.loc[~train_all["rule_signal"], "y"].mean()) if (~train_all["rule_signal"]).any() else base_rate
        p_rule = np.where(test["rule_signal"].to_numpy(), rule_rate_on, rule_rate_off)
        row["models"]["climatology"] = _clf_metrics(y_test, np.full(len(y_test), base_rate), base_rate)
        row["models"]["rule"] = _clf_metrics(y_test, p_rule, base_rate)
        oos_base.append(test.assign(p_rule=p_rule))
        for name in cfg.models:
            method = "isotonic" if len(cal_part) >= 500 else "sigmoid"
            model = _Calibrated(_make_model(name, cfg.seed), method if len(cal_part) else "none")
            with warnings.catch_warnings():
                # early folds can have a column that is entirely unknown (e.g. ret_252 before 252 bars exist);
                # the imputer then skips it consistently in fit and predict.
                warnings.simplefilter("ignore", UserWarning)
                if len(cal_part):
                    model.fit(fit_part[feats], fit_part["y"].astype(int), cal_part[feats], cal_part["y"].astype(int))
                else:
                    model.model.fit(fit_part[feats], fit_part["y"].astype(int))
                p = model.predict_proba(test[feats])
            row["models"][name] = {**_clf_metrics(y_test, p, base_rate), "calibration": model.method}
            oos[name].append(test.assign(p=p, base_rate=base_rate))
        per_fold.append(row)

    summary: dict[str, Any] = {}
    base_df = pd.concat(oos_base) if oos_base else pd.DataFrame()
    if not base_df.empty:
        all_vals = base_df["outcome_value"].to_numpy(float)
        rule_mask = base_df["rule_signal"].to_numpy(bool)
        summary["all_rows"] = _economic(all_vals, base_df["date"].to_numpy())
        summary["rule_rows"] = _economic(all_vals[rule_mask], base_df["date"].to_numpy()[rule_mask])
    for name, parts in oos.items():
        if not parts:
            continue
        d = pd.concat(parts)
        y, p = d["y"].to_numpy(int), d["p"].to_numpy(float)
        folds_auc = [r["models"][name]["roc_auc"] for r in per_fold if "models" in r and name in r["models"]]
        # act only when the model is more confident than the base rate by a margin
        sel = p >= (d["base_rate"].to_numpy() + 0.05)
        m = _clf_metrics(y, p, float(d["base_rate"].mean()))
        m["auc_by_fold"] = folds_auc
        m["reliability"] = _reliability(y, p)
        m["selected_rows"] = _economic(d["outcome_value"].to_numpy(float)[sel], d["date"].to_numpy()[sel])
        if regimes is not None and not regimes.empty:
            reg = d["date"].map(lambda t: regimes.get(t, "unknown"))
            m["by_regime"] = {}
            for rname in sorted(reg.unique()):
                mask = (reg == rname).to_numpy()
                if mask.sum() >= 30:
                    m["by_regime"][rname] = _clf_metrics(y[mask], p[mask], float(d["base_rate"].mean()))
        summary[name] = m
    return {"folds": per_fold, "summary": summary, "value_unit": value_unit, "features": feats, "dropped_features_all_unknown": dropped,
            "oos_predictions": {name: pd.concat(parts)[["market", "symbol", "date", "p"]] for name, parts in oos.items() if parts}}


def adoption_gate(result: dict, cfg: MLConfig, synthetic: bool, causality: list[dict]) -> dict:
    s = result["summary"]
    candidates = [m for m in cfg.models if m in s]
    reasons_by_model: dict[str, list[str]] = {}
    best, best_mean = None, -np.inf
    rule = s.get("rule_rows", {}) if cfg.universe == "all" else s.get("all_rows", {})
    rule_mean = rule.get("mean")
    for name in candidates:
        m = s[name]
        r: list[str] = []
        if synthetic:
            r.append("합성 데모 데이터로 학습한 모델은 채택하지 않음")
        if any(c.get("passed") is False for c in causality):
            r.append("특징 계산 누수 검사 실패")
        if m["n"] < cfg.min_rows:
            r.append(f"표본 외 행 {m['n']}개 < 최소 {cfg.min_rows}개")
        aucs = [a for a in m.get("auc_by_fold", []) if a is not None]
        if not aucs or np.mean(aucs) < 0.55 or np.mean([a > 0.5 for a in aucs]) < 0.75:
            r.append(f"폴드별 ROC-AUC 부족 (평균 {np.mean(aucs) if aucs else float('nan'):.3f}, 기준 ≥0.55 그리고 75% 이상 폴드에서 >0.5)")
        if m.get("brier_skill") is None or m["brier_skill"] <= 0:
            r.append("Brier 점수가 기준 확률(학습 구간 양성 비율)보다 나쁘거나 같음 — 확률 보정 실패")
        sel = m.get("selected_rows", {})
        ci = sel.get("mean_ci95")
        if not sel.get("n") or sel["n"] < 30:
            r.append("모델이 선택한 표본 외 거래가 30건 미만")
        elif ci is None or ci[0] <= 0:
            r.append("모델 선택 거래의 비용 차감 후 평균 성과 95% 신뢰구간 하한이 0 이하")
        if rule_mean is not None and sel.get("mean") is not None and sel["mean"] <= rule_mean:
            r.append("모델 선택 거래 평균 성과가 단순 규칙(또는 전체 표본) 평균보다 낫지 않음")
        reasons_by_model[name] = r
        if not r and sel.get("mean", -np.inf) > best_mean:
            best, best_mean = name, sel["mean"]
    return {"adopted": best is not None, "selected_model": best, "reasons": reasons_by_model,
            "criteria": ["실데이터(합성 제외)", "누수 검사 통과", f"표본 외 행 ≥ {cfg.min_rows}",
                         "평균 ROC-AUC ≥ 0.55, 75% 이상 폴드에서 > 0.5", "Brier skill > 0",
                         "선택 거래 ≥ 30건, 비용 차감 후 평균의 95% CI 하한 > 0", "선택 거래 평균 > 단순 규칙 평균"]}


def _fingerprint(frame: pd.DataFrame) -> str:
    h = hashlib.sha256()
    cols = [c for c in ["market", "symbol", "date", "close", "volume"] if c in frame]
    h.update(pd.util.hash_pandas_object(frame[cols], index=False).values.tobytes())
    return h.hexdigest()[:16]


def run_lab(frame: pd.DataFrame, cfg: MLConfig, benchmark: pd.DataFrame | None = None, costs: CostModel | None = None,
            model_dir: Path | None = None) -> dict:
    costs = costs or CostModel()
    sources = sorted(frame["source"].dropna().unique()) if "source" in frame else []
    synthetic = bool(sources) and all(is_demo_source(s) for s in sources)
    if any(is_demo_source(s) for s in sources) and not synthetic:
        raise ValueError("데모 데이터와 실제 데이터가 섞여 있습니다. 데모를 제외하고 학습하세요.")
    if synthetic and not cfg.allow_synthetic:
        raise ValueError("합성 데모 데이터만 있습니다. 실제 데이터를 넣거나, 테스트 목적이면 allow_synthetic=true 로 실행하세요(채택되지 않음).")
    ds = build_dataset(frame, cfg, benchmark, costs)
    if len(ds) < 200:
        raise ValueError(f"학습 데이터가 부족합니다: 라벨 {len(ds)}개 (최소 200개, 권장 수천 개 이상).")
    feats = cfg.features()
    causality = []
    for _, raw in list(frame.groupby(["market", "symbol"]))[:3]:
        causality.append(check_feature_causality(raw, feats, benchmark=benchmark))
    idx_close = None
    if benchmark is not None and not benchmark.empty:
        idx_close = benchmark.assign(date=pd.to_datetime(benchmark["date"])).set_index("date")["close"].sort_index()
    regimes = regime_series(idx_close) if idx_close is not None else None
    result = walk_forward_ml(ds, cfg, regimes)
    gate = adoption_gate(result, cfg, synthetic, causality)
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + _fingerprint(frame)[:6]
    report = {
        "version": version, "config": {**asdict(cfg), "models": list(cfg.models)}, "features": feats,
        "label_definition": (f"삼중 장벽: 다음 시가 진입, 손절 {cfg.atr_mult}×ATR14, 익절 {cfg.target_r}R, {cfg.horizon}거래일 시간 청산. "
                             "양성 = 손절보다 익절 먼저 도달. 같은 봉에서 둘 다 닿으면 손절." if cfg.task == "triple_barrier" else
                             f"방향: 다음 시가 진입 → {cfg.horizon}거래일 뒤 종가, 왕복 비용 차감 순수익 > 0 이면 양성."),
        "data": {"rows": int(len(ds)), "positive_rate": float(ds["y"].mean()), "start": ds["date"].min().strftime("%Y-%m-%d"),
                 "end": ds["date"].max().strftime("%Y-%m-%d"), "instruments": int(ds.groupby(["market", "symbol"]).ngroups),
                 "sources": sources, "synthetic": synthetic, "fingerprint": _fingerprint(frame)},
        "leakage_checks": {"feature_causality": causality,
                           "purge": "학습 행은 라벨 종료일이 검증 시작일 이전인 것만 사용",
                           "calibration_split": "학습 구간의 마지막 20% 날짜로 확률 보정(테스트 구간 미사용)"},
        "walk_forward": {k: v for k, v in result.items() if k != "oos_predictions"},
        "adoption": gate,
        "warning": ("합성 데이터 결과 — 모델 성능에 대한 어떤 결론도 내릴 수 없음" if synthetic else
                    "표본 외 검증 결과도 미래 성과를 보장하지 않음. 확률은 보정된 과거 빈도 추정치이며 실제 상승 확률이 아님."),
    }
    model_path = None
    if gate["adopted"] and model_dir is not None:
        name = gate["selected_model"]
        dates = sorted(ds["date"].unique())
        cut = dates[int(len(dates) * 0.8)]
        fit = ds[(ds["date"] < cut) & (ds["label_end"] < cut)]
        cal = ds[ds["date"] >= cut]
        feats = result["features"]
        final = _Calibrated(_make_model(name, cfg.seed), "isotonic" if len(cal) >= 500 else "sigmoid")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            final.fit(fit[feats], fit["y"].astype(int), cal[feats], cal["y"].astype(int))
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / f"model-{version}.joblib"
        joblib.dump({"model": final, "features": feats, "config": report["config"]}, model_path)
    report["model_path"] = str(model_path) if model_path else None
    report["_oos_predictions"] = result["oos_predictions"]
    return report


def report_json(report: dict) -> str:
    return json.dumps({k: v for k, v in report.items() if not k.startswith("_")}, ensure_ascii=False, default=str)

