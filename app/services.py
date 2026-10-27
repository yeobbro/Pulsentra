"""Glue between storage and the analysis modules (used by the API and by scripts)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from app import db
from app.backtesting.engine import ExitConfig
from app.backtesting.report import data_info
from app.data.freshness import all_series_status
from app.data.ingest import ingest_frame
from app.demo import DEMO_SOURCE, demo_instruments, make_demo_data
from app.models.lab import MLConfig, report_json, run_lab
from app.risk import CostModel, RiskConfig
from app.settings import DB_PATH, MODEL_DIR


def benchmark_symbols(market: str, db_path=None) -> list[str]:
    return [r["symbol"] for r in db.fetch_all("SELECT symbol FROM instruments WHERE market=? AND is_benchmark=1 ORDER BY symbol",
                                               (market.upper(),), db_path)]


def universe(market: str, symbols: list[str] | None = None, db_path=None) -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DataFrame]:
    """(instrument bars without benchmark rows, benchmark bars or None, instrument metadata)."""
    market = market.upper()
    frame = db.load_candles(market=market, db_path=db_path)
    bench_syms = benchmark_symbols(market, db_path)
    bench = None
    if bench_syms and not frame.empty:
        b = frame[frame["symbol"] == bench_syms[0]]
        bench = b[["date", "close"]].copy() if not b.empty else None
        frame = frame[~frame["symbol"].isin(bench_syms)]
    if symbols:
        wanted = {s.upper() for s in symbols}
        frame = frame[frame["symbol"].isin(wanted)]
    return frame, bench, db.load_instruments(db_path)


def lookups(market: str | None = None, db_path=None) -> tuple[dict, dict, dict]:
    statuses = {(s["market"], s["symbol"]): {"status": s["status"], "reason": s["reason"], "last_date": s.get("last_date"),
                                              "age_business_days": s.get("age_business_days")} for s in all_series_status(db_path)}
    names = {(r["market"], r["symbol"]): r["name"] for r in db.fetch_all("SELECT market, symbol, name FROM instruments", (), db_path) if r["name"]}
    disc: dict = {}
    for d in db.fetch_all("SELECT * FROM disclosures ORDER BY rcept_dt DESC", (), db_path):
        disc.setdefault((d["market"] or "KR", d["symbol"]), []).append(d)
    return statuses, names, disc


def load_demo(db_path=None, days: int = 800, n_symbols: int = 12) -> dict:
    db.clear_source(DEMO_SOURCE, db_path)
    db.execute("DELETE FROM quality_issues WHERE market='DEMO'", (), db_path)
    frame = make_demo_data(days=days, n_symbols=n_symbols, include_index=True)
    db.upsert_instruments(demo_instruments(n_symbols, include_index=True), db_path)
    res = ingest_frame(frame, DEMO_SOURCE, adjusted=True, market="DEMO", db_path=db_path)
    return {**res, "market": "DEMO",
            "message": "합성 데모 데이터입니다. 실시간 시세가 아니며 전략·모델 성능 평가나 실제 매매 근거로 사용할 수 없습니다."}


def demo_autoload_marker(db_path=None) -> Path:
    """File next to the database recording that the startup DEMO load already ran (so it runs at most once)."""
    path = Path(db_path or DB_PATH)
    return path.with_name(path.name + ".demo-autoloaded")


def autoload_demo_if_empty(db_path=None) -> dict:
    """Startup hook for PULSENTRA_AUTOLOAD_DEMO=true: load the synthetic DEMO set once, only into an empty database.

    Skips (and never deletes or overwrites anything) when the database already has any instrument or bar, or when an
    earlier startup already loaded DEMO for this database file (so DEMO deleted on purpose is not reloaded)."""
    marker = demo_autoload_marker(db_path)
    if marker.exists():
        return {"loaded": False, "reason": "already_autoloaded"}
    has_data = db.fetch_all("SELECT EXISTS(SELECT 1 FROM candles) AS c, EXISTS(SELECT 1 FROM instruments) AS i", (), db_path)[0]
    if has_data["c"] or has_data["i"]:
        return {"loaded": False, "reason": "database_not_empty"}
    res = load_demo(db_path)
    marker.write_text(db.utc_now() + "\n", encoding="utf-8")
    return {"loaded": True, "rows_saved": res.get("rows_saved"), "market": "DEMO", "message": res["message"]}


def fingerprint(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    cols = [c for c in ["market", "symbol", "date", "close", "volume"] if c in frame]
    return hashlib.sha256(pd.util.hash_pandas_object(frame[cols], index=False).values.tobytes()).hexdigest()[:16]


def save_backtest(kind: str, request: dict, frame: pd.DataFrame, report: dict, db_path=None) -> int:
    info = report.get("data") or data_info(frame, None)
    summary = {k: report.get(k) for k in ("metrics", "benchmark", "cost_impact", "out_of_sample", "rows", "variants", "period", "library") if k in report}
    return db.execute("""INSERT INTO backtest_runs (created_at, kind, request, data_fingerprint, sources, quality_warnings, summary, synthetic)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (db.utc_now(), kind, db.dumps(request), fingerprint(frame), db.dumps(info.get("sources")),
                       db.dumps({"flagged_bars_in_input": info.get("flagged_bars_in_input")}), db.dumps(summary),
                       int(bool(info.get("synthetic")))), db_path)


def run_ml(market: str, cfg: MLConfig, costs: CostModel, compare_backtest: bool = True, db_path=None,
           model_dir: Path | None = None) -> dict:
    frame, bench, inst = universe(market, db_path=db_path)
    if frame.empty:
        raise ValueError("학습할 데이터가 없습니다.")
    report = run_lab(frame, cfg, bench, costs, model_dir or MODEL_DIR)
    oos = report.pop("_oos_predictions", {})
    gate = report["adoption"]
    db.execute("""INSERT INTO model_registry (version, created_at, task, horizon, selected_model, adopted, adoption_reason,
                  data_start, data_end, sources, data_fingerprint, report, model_path, synthetic)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (report["version"], db.utc_now(), cfg.task, cfg.horizon, gate["selected_model"], int(gate["adopted"]),
                db.dumps(gate["reasons"]), report["data"]["start"], report["data"]["end"], db.dumps(report["data"]["sources"]),
                report["data"]["fingerprint"], report_json(report), report.get("model_path"), int(report["data"]["synthetic"])), db_path)
    if compare_backtest and cfg.task == "triple_barrier" and oos:
        best = max(oos, key=lambda m: report["walk_forward"]["summary"].get(m, {}).get("roc_auc") or 0)
        preds = oos[best]
        base_rate = report["data"]["positive_rate"]
        start = preds["date"].min().strftime("%Y-%m-%d")
        lookup = {(m, s): g.set_index("date")["p"] for (m, s), g in preds.groupby(["market", "symbol"])}

        def ml_filter(key, x):
            p = lookup.get(key)
            if p is None:
                return pd.Series(False, index=x.index)
            return (x["date"].map(p) >= base_rate + 0.05).fillna(False)

        # the comparison must cover only dates that have out-of-sample predictions
        comp_window = compare_window(frame, bench, inst, cfg, costs, ml_filter, start)
        report["ml_filter_comparison"] = {**comp_window, "model": best,
                                          "note": "ML 필터는 워크포워드 표본 외 예측만 사용. 같은 기간·비용·체결 규칙으로 기준 전략과 비교."}
    return report


def compare_window(frame, bench, inst, cfg: MLConfig, costs: CostModel, ml_filter, start: str) -> dict:
    from app.backtesting.engine import prepare, run_portfolio
    from app.backtesting.metrics import equity_metrics, trade_metrics
    from app.strategies import get_strategy
    prepared = prepare(frame, bench)
    exit_cfg = ExitConfig(atr_mult=cfg.atr_mult, target_r=cfg.target_r, max_hold_days=cfg.horizon)
    risk = RiskConfig()
    rows = []
    for label, filt in (("규칙만", None), ("규칙 + ML 필터", ml_filter)):
        r = run_portfolio(prepared, get_strategy(cfg.base_strategy), exit_cfg, risk, costs, filt, inst, start, None)
        em, tm = equity_metrics(r["equity"], risk.initial_cash), trade_metrics(r["trades"])
        rows.append({"label": label, "trades": tm["trades"], "win_rate": tm["win_rate"], "expectancy_r": tm["expectancy_r"],
                     "expectancy_r_ci95": tm["expectancy_r_ci95"], "total_return": em["total_return"],
                     "max_drawdown": em["max_drawdown"], "sample_warning": tm["sample_warning"]})
    return {"start": start, "rows": rows}


def adopted_model(db_path=None) -> dict | None:
    rows = db.fetch_all("SELECT * FROM model_registry WHERE adopted=1 AND synthetic=0 ORDER BY id DESC LIMIT 1", (), db_path)
    return rows[0] if rows else None


def model_list(db_path=None) -> list[dict]:
    rows = db.fetch_all("""SELECT id, version, created_at, task, horizon, selected_model, adopted, adoption_reason, data_start,
                           data_end, sources, synthetic FROM model_registry ORDER BY id DESC LIMIT 50""", (), db_path)
    for r in rows:
        r["adoption_reason"] = json.loads(r["adoption_reason"] or "{}")
        r["sources"] = json.loads(r["sources"] or "[]")
    return rows


def json_safe(value):
    if isinstance(value, dict):
        return {str(k) if not isinstance(k, tuple) else ":".join(map(str, k)): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d") if value == value.normalize() else value.isoformat()
    if isinstance(value, pd.DataFrame):
        return json_safe(value.to_dict("records"))
    if value is None or isinstance(value, (str, bool, int)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value
