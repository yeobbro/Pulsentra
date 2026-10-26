"""Strategy Library runs: catalog entry -> existing backtest engine -> optional Experiment Ledger record.

Nothing here simulates trades. Every run goes through app.backtesting.report.run_report (the same function the
Strategy Lab uses), with the same CostModel, ExitConfig and RiskConfig. This module adds:
  * parameter validation against the catalog's ranges (app.strategies.catalog);
  * period breakdown of ONE run (parameters fixed; the periods are reporting slices, not a selection step);
  * turnover and data coverage computed from the run's own trades and data;
  * pre-registration in the Experiment Ledger before the run (when asked): one experiment per distinct setting
    (entry, parameters, exit, risk, costs, universe, period, data fingerprint). Changing a parameter creates a new
    experiment in the same family; the earlier one, its runs and results stay as they were;
  * comparison of several entries under one set of conditions, and of saved runs with a settings consistency check.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict
import json
import secrets
from pathlib import Path

import pandas as pd

from app import db
from app.backtesting.engine import ExitConfig
from app.backtesting.engine import run_portfolio
from app.backtesting.metrics import equity_metrics, trade_metrics
from app.backtesting.report import data_info, prepare, run_report
from app.research.ledger import Ledger, canonical, file_sha256
from app.research.robustness_strategy import StrategyGateConfig, StrategyRunEvidence, evaluate_strategy
from app.research.manifest import environment_info, git_info, output_inventory
from app.research.study import StudyConfig
from app.risk import CostModel, RiskConfig
from app.services import benchmark_symbols, fingerprint, json_safe, save_backtest, universe
from app.strategies.base import STRATEGY_REGISTRY, get_strategy
from app.strategies.catalog import Entry, get_entry, validate_params

REPORT_PERIODS = dict(StudyConfig().periods)          # dev / validation / test, the research split
PRIMARY_METRICS = ["total_return_net", "max_drawdown", "sharpe", "trades", "total_return_gross"]
STRESS_MULTIPLIERS = (2.0, 3.0)        # all three cost terms scaled, as in the Factor Lab gate
EVIDENCE_VERSION = 1                   # library summary fields the Robustness Gate adapter reads
PERIOD_NOTE = ("같은 파라미터로 한 번 실행한 결과를 기간별로 나눈 값입니다. 기간마다 파라미터를 고르지 않았습니다. "
               "거래는 진입일 기준으로 기간에 넣고, 기간 끝을 넘는 거래 수를 따로 셉니다.")


class NotRunnable(ValueError):
    """The catalog entry is PLANNED (no code or data)."""


def _data_dir() -> Path:
    from app import settings
    return Path(settings.DATA_DIR)


def default_ledger_path() -> Path:
    return _data_dir() / "ledger" / "ledger.jsonl"


def settings_hash(entry_id: str, params: dict, exit_cfg: ExitConfig, risk: RiskConfig, costs: CostModel, market: str,
                  symbols: list[str] | None, start: str | None, end: str | None, data_fp: str) -> str:
    return hashlib.sha256(canonical({"entry": entry_id, "params": params, "exit": exit_cfg.to_dict(), "risk": risk.to_dict(),
                                     "costs": costs.to_dict(), "market": market.upper(), "symbols": sorted(symbols or []),
                                     "start": start, "end": end, "data": data_fp}).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ measurements

def _segment(curve: pd.DataFrame | None, a: pd.Timestamp, b: pd.Timestamp, initial_cash: float) -> dict | None:
    """Metrics of an equity curve between a and b, measured from the last value before a (or the initial cash)."""
    if curve is None or curve.empty:
        return None
    c = curve.assign(date=pd.to_datetime(curve["date"]))
    inside = c[(c["date"] >= a) & (c["date"] <= b)]
    if len(inside) < 2:
        return None
    before = c[c["date"] < a]
    base = float(before["equity"].iloc[-1]) if len(before) else float(initial_cash)
    seg = pd.concat([pd.DataFrame({"date": [inside["date"].iloc[0] - pd.Timedelta(1, unit="D")], "equity": [base]}),
                     inside[["date", "equity"]]], ignore_index=True)
    m = equity_metrics(seg, base)
    m["first_date"], m["last_date"] = inside["date"].iloc[0].strftime("%Y-%m-%d"), inside["date"].iloc[-1].strftime("%Y-%m-%d")
    return m


def period_breakdown(eq: pd.DataFrame, gross_eq: pd.DataFrame, trades: list[dict], initial_cash: float,
                     periods: dict | None = None, bench_curve: pd.DataFrame | None = None) -> list[dict]:
    """Per-period results of ONE run. With `bench_curve`, also the benchmark over the same dates and the excess."""
    periods = periods or REPORT_PERIODS
    t = pd.DataFrame(trades)
    rows = []
    for name, (a, b) in periods.items():
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        row = {"period": name, "start": a.strftime("%Y-%m-%d"), "end": b.strftime("%Y-%m-%d")}
        net = _segment(eq, a, b, initial_cash)
        if net:
            row.update({k: net.get(k) for k in ("total_return", "cagr", "max_drawdown", "sharpe", "first_date", "last_date")})
        gross = _segment(gross_eq, a, b, initial_cash)
        if gross:
            row["gross_total_return"] = gross.get("total_return")
        if bench_curve is not None:
            bm = _segment(bench_curve, a, b, initial_cash)
            row["benchmark_total_return"] = bm.get("total_return") if bm else None
            row["excess_net"] = (row["total_return"] - row["benchmark_total_return"]) if net and bm else None
        if not t.empty:
            ed, xd = pd.to_datetime(t["entry_date"]), pd.to_datetime(t["exit_date"])
            inp = (ed >= a) & (ed <= b)
            row["trades"] = int(inp.sum())
            row["trades_crossing_period_end"] = int((inp & (xd > b)).sum())
            if bench_curve is not None:
                tm = trade_metrics(t[inp].to_dict("records"))
                row["expectancy_return"], row["expectancy_return_ci95"] = tm["expectancy_return"], tm["expectancy_return_ci95"]
        else:
            row["trades"], row["trades_crossing_period_end"] = 0, 0
        if "total_return" not in row:
            row["note"] = "이 기간에 데이터가 없거나 2거래일 미만"
        rows.append(row)
    return rows


def yearly_vs_benchmark(eq: pd.DataFrame, bench_curve: pd.DataFrame | None, initial_cash: float) -> list[dict]:
    """Calendar-year net return of the run and of the benchmark over the same dates (parameters fixed)."""
    if eq is None or eq.empty:
        return []
    years = sorted(pd.to_datetime(eq["date"]).dt.year.unique())
    out = []
    for y in years:
        a, b = pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31")
        n, bm = _segment(eq, a, b, initial_cash), _segment(bench_curve, a, b, initial_cash)
        if n:
            out.append({"year": int(y), "total_return": n["total_return"], "benchmark_total_return": bm["total_return"] if bm else None,
                        "excess_net": (n["total_return"] - bm["total_return"]) if bm else None, "first_date": n["first_date"],
                        "last_date": n["last_date"]})
    return out


def benchmark_info(market: str, bench, db_path=None) -> dict:
    """What the run was compared with. The equal-weight proxy is never labelled as an index."""
    if bench is not None and not bench.empty:
        syms = benchmark_symbols(market, db_path)
        return {"kind": "index", "symbol": syms[0] if syms else None, "label": "시장지수 매수 후 보유",
                "method": "지수 종가 매수 후 보유, 비용 미반영", "cost_applied": False}
    return {"kind": "equal_weight_proxy", "symbol": None, "label": "검증 종목 동일가중 매수 후 보유 (지수 미적재 대용)",
            "method": "유니버스 종목 동일가중 매수 후 보유, 비용 미반영. 실제 시장지수가 아님", "cost_applied": False}


def turnover(trades: list[dict], eq: pd.DataFrame) -> dict:
    """One-way traded value / mean equity; annualized when the run spans at least 180 days."""
    if not trades or eq.empty:
        return {"traded_value_one_way": 0.0, "turnover_total": 0.0, "turnover_annual": None}
    t = pd.DataFrame(trades)
    traded = float(((t["qty"] * t["entry"]) + (t["qty"] * t["exit"])).sum() / 2)
    mean_eq = float(eq["equity"].mean())
    days = (pd.Timestamp(eq["date"].iloc[-1]) - pd.Timestamp(eq["date"].iloc[0])).days
    total = traded / mean_eq if mean_eq > 0 else None
    return {"traded_value_one_way": traded, "turnover_total": total,
            "turnover_annual": (total / (days / 365.25)) if total is not None and days >= 180 else None,
            "definition": "(매수 금액 + 매도 금액) / 2 ÷ 평균 평가자산; 연율은 기간 180일 이상일 때만"}


def coverage(frame: pd.DataFrame, trades: list[dict], bench) -> dict:
    per = frame.groupby(["market", "symbol"])["date"].count() if not frame.empty else pd.Series(dtype=int)
    return {"instruments": int(len(per)), "bars": int(len(frame)),
            "first_date": pd.Timestamp(frame["date"].min()).strftime("%Y-%m-%d") if len(frame) else None,
            "last_date": pd.Timestamp(frame["date"].max()).strftime("%Y-%m-%d") if len(frame) else None,
            "bars_per_instrument_min": int(per.min()) if len(per) else 0, "bars_per_instrument_median": float(per.median()) if len(per) else 0.0,
            "instruments_traded": int(len({(x["market"], x["symbol"]) for x in trades})), "benchmark_loaded": bench is not None and not bench.empty}


def _summary_row(label: str, entry: Entry, params: dict, rep: dict, frame: pd.DataFrame, bench, risk: RiskConfig) -> dict:
    m, ci = rep["metrics"], rep["cost_impact"]
    eq, geq, trades = rep["_equity_full"], rep["_gross_equity_full"], rep["_trades_full"]
    return {"label": label, "entry_id": entry.id, "name": entry.name_ko, "status": entry.status,
            "status_reason": entry.status_reason if entry.status != "AVAILABLE" else None, "params": params, "comparable": True,
            "total_return_net": m.get("total_return"), "total_return_gross": ci.get("gross_total_return"),
            "cost_drag": ci.get("difference"), "cagr": m.get("cagr"), "max_drawdown": m.get("max_drawdown"), "sharpe": m.get("sharpe"),
            "trades": m.get("trades"), "gross_trades": ci.get("gross_trades"), "signals": m.get("signals"), "win_rate": m.get("win_rate"),
            "turnover": turnover(trades, eq), "by_period": period_breakdown(eq, geq, trades, risk.initial_cash, None, rep.get("_benchmark_full")),
            "coverage": coverage(frame, trades, bench), "sample_warning": m.get("sample_warning"), "notes": rep.get("notes") or [],
            "period": rep.get("period")}


# ------------------------------------------------------------------------------------------------ single run

def _ledger_spec(entry: Entry, params: dict, exit_cfg, risk, costs, market, symbols, start, end, data_fp, sh) -> dict:
    return {"id": f"LIB-{entry.id}-{sh[:10]}", "title": f"{entry.name_ko} ({entry.name_en})", "hypothesis": entry.hypothesis,
            "rationale": entry.description_ko, "family": entry.family, "universe": {"market": market.upper(), "symbols": sorted(symbols or []) or "app DB 전체"},
            "data": f"app DB market={market.upper()}", "data_fingerprint": data_fp,
            "periods": {"backtest": [start or "first_available", end or "last_available"]},
            "primary_metrics": PRIMARY_METRICS,
            "decision_criteria": "Strategy Library 실행: 자동 판정 없음. 결과 확인 후 사람이 decide로 기록. Robustness Gate는 Factor Lab 출력에만 적용됨",
            "cost_assumptions": {**costs.to_dict(), "convention": "per side; sell tax on sells only; slippage via fill price"},
            "search_space": {"params": params, "exit": exit_cfg.to_dict(), "risk": risk.to_dict()}, "planned_experiments": 1,
            "signal_rule": list(entry.entry_rules), "entry_rule": "signal at close t -> entry at open t+1", "exit_rule": list(entry.exit_rules),
            "benchmark": "시장지수 매수 후 보유(적재 시), 없으면 동일가중 대용 — run_report와 같음",
            "hypothesis_family_id": f"LIB-{entry.id}", "notes": f"strategy_key={entry.strategy_key}; settings_hash={sh}",
            "gate": {**asdict(StrategyGateConfig()), "cost_stress_multipliers": list(StrategyGateConfig().cost_stress_multipliers)}}


def run_entry(entry_id: str, params: dict | None, exit_cfg: ExitConfig | None, risk: RiskConfig, costs: CostModel, market: str = "KR",
              symbols: list[str] | None = None, start: str | None = None, end: str | None = None, random_runs: int = 10,
              ledger: bool = False, db_path=None, ledger_path: Path | None = None, request: dict | None = None) -> dict:
    entry = get_entry(entry_id)
    if not entry.runnable:
        raise NotRunnable(f"{entry.name_ko}: {entry.status} — {entry.status_reason}")
    p = validate_params(entry, params)
    exit_cfg = exit_cfg or ExitConfig.from_dict(entry.default_exit)
    frame, bench, inst = universe(market, symbols, db_path=db_path)
    if frame.empty:
        raise LookupError(f"{market.upper()} 시장 데이터가 없습니다.")
    data_fp = fingerprint(frame)
    sh = settings_hash(entry.id, p, exit_cfg, risk, costs, market, symbols, start, end, data_fp)
    led_info, led, run_id = None, None, f"bt-{pd.Timestamp.now(tz='UTC').strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(2)}"
    if ledger:
        led = Ledger(ledger_path or default_ledger_path())
        spec = _ledger_spec(entry, p, exit_cfg, risk, costs, market, symbols, start, end, data_fp, sh)
        st = led.state()
        new = spec["id"] not in st
        if new:
            led.register(spec)
        led.start_run(spec["id"], run_id, f"strategy_library.run_entry({entry.id})")
        cur = led.experiment(spec["id"])
        led_info = {"experiment_id": spec["id"], "run_id": run_id, "registered_now": new, "spec_version": cur["version"],
                    "spec_hash": cur["spec_hash"], "family_id": spec["hypothesis_family_id"], "runs_before": len(cur["runs"]) - 1}
    started = pd.Timestamp.now(tz="UTC").isoformat()
    try:
        prepared = prepare(frame, bench)
        rep = run_report(frame, entry.strategy_key, p, exit_cfg, risk, costs, bench, inst, start, end, random_runs, prepared=prepared,
                         keep_full=True)
        row = _summary_row(entry.id, entry, p, rep, frame, bench, risk)
        bcurve = rep["_benchmark_full"]
        by_period = period_breakdown(rep["_equity_full"], rep["_gross_equity_full"], rep["_trades_full"], risk.initial_cash,
                                     REPORT_PERIODS, bcurve)
        stress = {}
        strat = get_strategy(entry.strategy_key, **p)
        for mult in STRESS_MULTIPLIERS:
            sc = CostModel(costs.commission_bps * mult, costs.sell_tax_bps * mult, costs.slippage_bps * mult)
            sr = run_portfolio(prepared, strat, exit_cfg, risk, sc, None, inst, start, end)
            stress[str(mult)] = {"costs": sc.to_dict(), "by_period": period_breakdown(sr["equity"], None, sr["trades"], risk.initial_cash,
                                                                                    REPORT_PERIODS, bcurve)}
        rep["library"] = {"entry_id": entry.id, "status": entry.status, "status_reason": entry.status_reason, "params": p,
                          "settings_hash": sh, "data_fingerprint": data_fp, "by_period": by_period, "period_note": PERIOD_NOTE,
                          "periods": {k: list(v) for k, v in REPORT_PERIODS.items()}, "benchmark": benchmark_info(market, bench, db_path),
                          "yearly": yearly_vs_benchmark(rep["_equity_full"], bcurve, risk.initial_cash), "cost_stress": stress,
                          "data_sources": rep["data"].get("sources"), "synthetic": bool(rep["data"].get("synthetic")),
                          "mixed_demo_and_real": bool(rep["data"].get("mixed_demo_and_real")), "evidence_version": EVIDENCE_VERSION,
                          "turnover": row["turnover"], "coverage": row["coverage"], "ledger": led_info,
                          "uses_strategy_exit_rule": bool(exit_cfg.strategy_exit and entry.as_dict()["has_strategy_exit_rule"])}
        for k in ("_equity_full", "_gross_equity_full", "_trades_full", "_gross_trades_full", "_benchmark_full"):
            rep.pop(k, None)
        rep["run_id"] = save_backtest("library", {**(request or {}), "entry_id": entry.id, "params": p, "exit": exit_cfg.to_dict(),
                                                  "risk": risk.to_dict(), "costs": costs.to_dict(), "market": market.upper(),
                                                  "symbols": symbols, "start": start, "end": end, "settings_hash": sh,
                                                  "ledger": led_info}, frame, rep, db_path)
        if led:
            out = _data_dir() / "library" / run_id
            out.mkdir(parents=True, exist_ok=False)
            (out / "report.json").write_text(json.dumps(json_safe(rep), ensure_ascii=False, indent=1), encoding="utf-8")
            c = costs.to_dict()
            manifest = {"manifest_version": 2, "kind": "strategy_library", "command": f"POST /api/library/strategies/{entry.id}/backtest",
                        "started_utc": started, "finished_utc": pd.Timestamp.now(tz="UTC").isoformat(), "git": git_info(),
                        "environment": environment_info(),
                        "config": {"entry_id": entry.id, "strategy_key": entry.strategy_key, "params": p, "exit": exit_cfg.to_dict(),
                                   "risk": risk.to_dict(), "costs": c, "market": market.upper(), "symbols": symbols, "start": start, "end": end},
                        "cost_assumptions": {**c, "round_trip_bps": 2 * c["commission_bps"] + c["sell_tax_bps"] + 2 * c["slippage_bps"],
                                             "convention": "per side; sell tax on sells only"},
                        "data_fingerprint": data_fp, "sample": rep.get("data"), "configs_tested_per_strategy": 1, "experiments_total": 1,
                        "validity": {"status": None, "note": "앱 DB 포트폴리오 백테스트에는 결과 상태 판정(validity.assess)을 적용하지 않음"},
                        "primary_metrics": PRIMARY_METRICS, "ledger_experiment": {**led_info, "id": led_info["experiment_id"]},
                        "backtest_run_id": rep["run_id"]}
            manifest["outputs"] = output_inventory(out, skip=("run_manifest.json",))
            (out / "run_manifest.json").write_text(json.dumps(json_safe(manifest), ensure_ascii=False, indent=1), encoding="utf-8")
            led.finish_run(led_info["experiment_id"], run_id, "success")
            led.link_manifest(led_info["experiment_id"], out / "run_manifest.json", run_id)
            led.link_artifact(led_info["experiment_id"], out / "report.json", "library_report", run_id)
            led_info["artifacts_dir"] = str(out)
    except Exception as exc:
        if led and led_info:
            st = led.experiment(led_info["experiment_id"])
            if st["open_run"] == run_id:
                led.finish_run(led_info["experiment_id"], run_id, "failure", f"{type(exc).__name__}: {exc}")
        raise
    return rep


# ------------------------------------------------------------------------------------------------ comparison

def compare_entries(items: list[dict], exit_cfg: ExitConfig, risk: RiskConfig, costs: CostModel, market: str = "KR",
                    symbols: list[str] | None = None, start: str | None = None, end: str | None = None, db_path=None) -> dict:
    """Several entries, one universe, one period, one exit/risk/cost setting, one benchmark."""
    if not items:
        raise ValueError("비교할 전략이 없습니다.")
    checked = []
    for it in items:
        entry = get_entry(it["id"])
        checked.append((it.get("label") or entry.id, entry, None if not entry.runnable else validate_params(entry, it.get("params"))))
    frame, bench, inst = universe(market, symbols, db_path=db_path)
    if frame.empty:
        raise LookupError(f"{market.upper()} 시장 데이터가 없습니다.")
    prepared = prepare(frame, bench)
    rows, bench_row = [], None
    for label, entry, p in checked:
        if p is None:
            rows.append({"label": label, "entry_id": entry.id, "name": entry.name_ko, "status": entry.status, "comparable": False,
                         "reason": f"{entry.status}: {entry.status_reason}"})
            continue
        rep = run_report(frame, entry.strategy_key, p, exit_cfg, risk, costs, bench, inst, start, end, 0, prepared=prepared, keep_full=True)
        row = _summary_row(label, entry, p, rep, frame, bench, risk)
        if "benchmark" in STRATEGY_REGISTRY[entry.strategy_key].requires and not row["coverage"]["benchmark_loaded"]:
            row["comparable"], row["reason"] = False, "시장지수가 없어 상대강도 조건을 평가할 수 없음(신호 없음)"
        rows.append(row)
        if bench_row is None:
            b = rep["benchmark"]
            bench_row = {"label": b.get("label"), "total_return": b.get("total_return"), "cagr": b.get("cagr"),
                         "max_drawdown": b.get("max_drawdown"), "sharpe": b.get("sharpe"), "note": "비용 미반영"}
    has_own_exit = [r["entry_id"] for r in rows if r.get("comparable") and get_entry(r["entry_id"]).as_dict()["has_strategy_exit_rule"]]
    dw = data_info(frame, inst).get("warning")
    warnings = ([dw] if dw else []) + [f"{len([r for r in rows if r.get('comparable')])}개 설정을 비교했습니다. 많이 비교할수록 우연히 좋아 보이는 결과가 나올 가능성이 커집니다."]
    if exit_cfg.strategy_exit and has_own_exit:
        warnings.append(f"strategy_exit=True: {', '.join(has_own_exit)}는 각자의 청산 규칙을 추가로 사용하므로 청산 규칙이 같지 않습니다.")
    return {"conditions": {"market": market.upper(), "symbols": symbols, "data_fingerprint": fingerprint(frame), "start": start, "end": end,
                           "exit": exit_cfg.to_dict(), "risk": risk.to_dict(), "costs": costs.to_dict(), "initial_cash": risk.initial_cash,
                           "benchmark": bench_row["label"] if bench_row else None, "benchmark_info": benchmark_info(market, bench, db_path),
                           "validation_policy": "전략마다 한 번 실행, 같은 기간 분할(dev/validation/test)로 나눠 보고. 기간별 파라미터 선택 없음",
                           "exit_rules_identical": not (exit_cfg.strategy_exit and has_own_exit)},
            "rows": rows, "benchmark": bench_row, "period_note": PERIOD_NOTE, "warnings": warnings}


CONSISTENCY_FIELDS = ("market", "symbols", "start", "end", "exit", "risk", "costs")


def list_runs(limit: int = 50, db_path=None) -> list[dict]:
    rows = db.fetch_all("SELECT id, created_at, request, data_fingerprint, synthetic, summary FROM backtest_runs WHERE kind='library' "
                        "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),), db_path)
    out = []
    for r in rows:
        req, summ = json.loads(r["request"]), json.loads(r["summary"] or "{}")
        m = summ.get("metrics") or {}
        out.append({"run_id": r["id"], "created_at": r["created_at"], "entry_id": req.get("entry_id"), "params": req.get("params"),
                    "data_fingerprint": r["data_fingerprint"], "synthetic": bool(r["synthetic"]), "request": req,
                    "total_return": m.get("total_return"), "max_drawdown": m.get("max_drawdown"), "sharpe": m.get("sharpe"),
                    "trades": m.get("trades"), "ledger": req.get("ledger"), "library": summ.get("library")})
    return out


def compare_saved(run_ids: list[int], db_path=None) -> dict:
    """Saved library runs side by side, with the settings that differ listed as reasons they are not comparable."""
    if len(run_ids) < 2:
        raise ValueError("비교하려면 실행 기록이 2개 이상 필요합니다.")
    q = ",".join("?" * len(run_ids))
    got = {r["id"]: r for r in db.fetch_all(f"SELECT id, request, data_fingerprint, summary FROM backtest_runs WHERE kind='library' AND id IN ({q})",
                                            tuple(run_ids), db_path)}
    missing = [i for i in run_ids if i not in got]
    if missing:
        raise LookupError(f"Strategy Library 실행 기록이 없습니다: {missing}")
    reqs = {i: json.loads(got[i]["request"]) for i in run_ids}
    reasons = []
    for f in CONSISTENCY_FIELDS:
        vals = {json.dumps(reqs[i].get(f), sort_keys=True) for i in run_ids}
        if len(vals) > 1:
            reasons.append(f"{f} 설정이 다름")
    if len({got[i]["data_fingerprint"] for i in run_ids}) > 1:
        reasons.append("데이터 지문이 다름(데이터가 바뀌었거나 다른 유니버스)")
    benches = {i: (json.loads(got[i]["summary"] or "{}").get("library") or {}).get("benchmark") for i in run_ids}
    if any(b is None for b in benches.values()):
        reasons.append("벤치마크 종류가 기록되지 않은 실행이 있음(이전 버전 실행) — 같은 기준인지 확인할 수 없음")
    elif len({(b.get("kind"), b.get("symbol")) for b in benches.values()}) > 1:
        reasons.append("벤치마크가 다름: " + ", ".join(f"#{i} {b.get('kind')}" + (f"({b['symbol']})" if b.get("symbol") else "") for i, b in benches.items()))
    rows = []
    for i in run_ids:
        s = json.loads(got[i]["summary"] or "{}")
        m, ci, lib = s.get("metrics") or {}, s.get("cost_impact") or {}, s.get("library") or {}
        rows.append({"run_id": i, "entry_id": reqs[i].get("entry_id"), "benchmark": lib.get("benchmark"), "params": reqs[i].get("params"), "total_return_net": m.get("total_return"),
                     "total_return_gross": ci.get("gross_total_return"), "cagr": m.get("cagr"), "max_drawdown": m.get("max_drawdown"),
                     "sharpe": m.get("sharpe"), "trades": m.get("trades"), "turnover": lib.get("turnover"), "by_period": lib.get("by_period"),
                     "coverage": lib.get("coverage"), "sample_warning": m.get("sample_warning"), "ledger": reqs[i].get("ledger")})
    return {"comparable": not reasons, "reasons": reasons, "rows": rows, "period_note": PERIOD_NOTE}


# ------------------------------------------------------------------------------------------------ robustness gate adapter

def _library_row(run_id: int, db_path=None) -> dict:
    rows = db.fetch_all("SELECT id, created_at, request, data_fingerprint, summary FROM backtest_runs WHERE kind='library' AND id=?",
                        (int(run_id),), db_path)
    if not rows:
        raise LookupError(f"Strategy Library 실행 기록이 없습니다: {run_id}")
    r = rows[0]
    return {"id": r["id"], "created_at": r["created_at"], "request": json.loads(r["request"]), "data_fingerprint": r["data_fingerprint"],
            "summary": json.loads(r["summary"] or "{}")}


def _same_conditions(a: dict, b: dict) -> bool:
    return all(json.dumps(a.get(k), sort_keys=True) == json.dumps(b.get(k), sort_keys=True) for k in CONSISTENCY_FIELDS)


def evidence_for_run(run_id: int, db_path=None, ledger_path: Path | None = None) -> tuple[StrategyRunEvidence, dict | None]:
    """Collect what a saved library run recorded (DB row, manifest, ledger) without recomputing any metric."""
    row = _library_row(run_id, db_path)
    req, lib = row["request"], (row["summary"].get("library") or {})
    entry_id = req.get("entry_id")
    recomputed = None
    try:
        recomputed = settings_hash(entry_id, req["params"], ExitConfig.from_dict(req["exit"]), RiskConfig.from_dict(req["risk"]),
                                   CostModel(**req["costs"]), req["market"], req.get("symbols"), req.get("start"), req.get("end"),
                                   row["data_fingerprint"] or "")
    except (KeyError, TypeError, ValueError):
        recomputed = None
    link = req.get("ledger")
    manifest, mism, spec, state, registered_gate, family = None, [], None, None, None, set()
    if link:
        out = _data_dir() / "library" / str(link.get("run_id"))
        mp = out / "run_manifest.json"
        if mp.is_file():
            manifest = json.loads(mp.read_text(encoding="utf-8"))
            for o in manifest.get("outputs") or []:
                f = out / o["path"]
                if not f.is_file() or file_sha256(f) != o["sha256"]:
                    mism.append(o["path"])
        lp = ledger_path or default_ledger_path()
        if lp.exists():
            all_state = Ledger(lp).state()          # raises LedgerCorrupt when the ledger does not verify
            state = all_state.get(link.get("experiment_id"))
            spec = state["spec"] if state else None
            registered_gate = (spec or {}).get("gate")
            fam = link.get("family_id")
            family |= {e for e, s_ in all_state.items() if fam and s_["spec"].get("hypothesis_family_id") == fam and s_["runs"]}
    others = [r for r in list_runs(200, db_path) if r["entry_id"] == entry_id]
    hashes = {(r["request"] or {}).get("settings_hash") for r in others} - {None}
    tried = max(len(hashes), len(family)) if (hashes or family) else None
    neighbours = []
    for r in others:
        rq = r["request"] or {}
        if r["run_id"] == row["id"] or rq.get("params") == req.get("params") or r["data_fingerprint"] != row["data_fingerprint"]:
            continue
        if not _same_conditions(rq, req):
            continue
        v = next((x for x in (r.get("library") or {}).get("by_period") or [] if x.get("period") == "validation"), None)
        neighbours.append({"run_id": r["run_id"], "params": rq.get("params"), "validation_excess": (v or {}).get("excess_net")})
    ev = StrategyRunEvidence(run_id=row["id"], entry_id=entry_id, created_at=row["created_at"], data_fingerprint=row["data_fingerprint"],
                             request=req, library=lib, settings_hash_recomputed=recomputed, manifest=manifest,
                             manifest_output_mismatches=mism, ledger_spec=spec, ledger_state=state, family_settings_tried=tried,
                             neighbours=neighbours)
    return ev, registered_gate


def gate_run(run_id: int, thresholds: dict | None = None, db_path=None, ledger_path: Path | None = None) -> dict:
    """Evaluate a saved library run, keep the result as a new file (never overwritten) and link it to the ledger."""
    ev, registered = evidence_for_run(run_id, db_path, ledger_path)
    base = StrategyGateConfig.from_dict(registered) if registered else StrategyGateConfig()
    gate = StrategyGateConfig.from_dict({**asdict(base), **(thresholds or {})})
    res = evaluate_strategy(ev, gate, registered)
    out = _data_dir() / "robustness" / "library" / f"run-{int(run_id)}"
    out.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%S%fZ")
    res["evaluated_utc"] = stamp
    f = out / f"gate_{stamp}.json"
    f.write_text(json.dumps(json_safe(res), ensure_ascii=False, indent=1), encoding="utf-8")
    res["saved_to"] = str(f)
    link = ev.request.get("ledger")
    if link and ev.ledger_state is not None:
        Ledger(ledger_path or default_ledger_path()).link_artifact(link["experiment_id"], f, "robustness_gate")
        res["ledger_artifact_linked"] = True
    return res


def list_gates(run_id: int, db_path=None) -> list[dict]:
    _library_row(run_id, db_path)
    out = _data_dir() / "robustness" / "library" / f"run-{int(run_id)}"
    rows = []
    for f in sorted(out.glob("gate_*.json")) if out.exists() else []:
        try:
            g = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            rows.append({"file": f.name, "error": "읽을 수 없는 파일"})
            continue
        rows.append({"file": f.name, "evaluated_utc": g.get("evaluated_utc"), "status": g.get("status"), "data_scope": g.get("data_scope")})
    return rows
