"""Robustness Gate for Strategy Library runs: an explicit adapter, separate from the Factor Lab gate.

The Factor Lab gate (`app.research.robustness.evaluate`) reads per-date rank IC and quantile returns; a portfolio
backtest has none of those, so nothing here converts a backtest into a Factor Lab folder. This module evaluates a
`StrategyRunEvidence` — the facts a Strategy Library run recorded about itself (per-period net/gross/benchmark
returns, trade expectancy and its bootstrap CI, cost-stress re-runs, yearly results, benchmark kind, data sources,
fingerprint, settings, ledger link, manifest) — with its own fixed checks. Statuses, their order and the "worst check
wins" rule are shared with the Factor Lab gate (imported, not copied), and the Factor Lab gate is not changed.

A check whose evidence the run did not record returns INSUFFICIENT_DATA with the missing field named; no metric is
estimated. Synthetic (DEMO) or mixed data FAILs the data-source check by policy: such a run can show the pipeline
works but cannot validate anything about a real market.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

import numpy as np
import pandas as pd

from app.research.eligibility import gate_check as eligibility_check
from app.research.robustness import FAIL, INSUFFICIENT, PASS, SEVERITY, WATCH, _check

NOT_IMPLEMENTED_STRATEGY = [
    "PBO (probability of backtest overfitting)", "Deflated Sharpe Ratio", "White Reality Check / Hansen SPA",
    "walk-forward re-optimisation of parameters (the separate /api/backtests/walk-forward is not linked to the gate)",
    "regime-conditional performance", "audit-based exclusion of bad bars for app-DB data",
    "total-return benchmark and dividends", "benchmark with trading costs",
]
DEMO_SCOPES = ("SYNTHETIC_DEMO", "MIXED")


@dataclass
class StrategyGateConfig:
    min_trades_dev: int = 30
    min_trades_validation: int = 15
    min_trades_test: int = 15
    min_years: int = 3                    # years in dev + validation for the yearly consistency check
    yearly_min_hit: float = 0.6
    yearly_fail_below: float = 0.4
    cost_stress_multipliers: tuple[float, ...] = (2.0, 3.0)

    @classmethod
    def from_dict(cls, d: dict | None) -> "StrategyGateConfig":
        d = d or {}
        unknown = sorted(set(d) - {f.name for f in fields(cls)})
        if unknown:
            raise ValueError(f"unknown gate thresholds {unknown}")
        vals = {k: (tuple(float(x) for x in v) if k == "cost_stress_multipliers" else v) for k, v in d.items()}
        cfg = cls(**vals)
        for k in ("min_trades_dev", "min_trades_validation", "min_trades_test", "min_years"):
            v = getattr(cfg, k)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise ValueError(f"{k} must be an integer >= 1")
        if not (0 <= cfg.yearly_fail_below <= cfg.yearly_min_hit <= 1):
            raise ValueError("0 <= yearly_fail_below <= yearly_min_hit <= 1 required")
        if not cfg.cost_stress_multipliers or any(m <= 1 for m in cfg.cost_stress_multipliers):
            raise ValueError("cost_stress_multipliers must be > 1")
        return cfg


@dataclass
class StrategyRunEvidence:
    run_id: int
    entry_id: str | None
    created_at: str | None
    data_fingerprint: str | None
    request: dict
    library: dict                         # the run's stored "library" summary (may lack fields in old runs)
    settings_hash_recomputed: str | None = None
    manifest: dict | None = None
    manifest_output_mismatches: list[str] = field(default_factory=list)
    ledger_spec: dict | None = None       # current spec of the linked experiment
    ledger_state: dict | None = None      # replayed state of the linked experiment
    family_settings_tried: int | None = None   # distinct settings of the same entry seen in the ledger or the DB
    neighbours: list[dict] = field(default_factory=list)   # same entry, same conditions, other parameters


def _per(lib: dict, name: str) -> dict | None:
    return next((r for r in lib.get("by_period") or [] if r.get("period") == name), None)


def _ci_low(r: dict) -> float | None:
    ci = r.get("expectancy_return_ci95")
    return float(ci[0]) if ci else None


def data_scope(lib: dict) -> str:
    if lib.get("synthetic"):
        return "SYNTHETIC_DEMO"
    if lib.get("mixed_demo_and_real"):
        return "MIXED"
    return "REAL" if lib.get("data_sources") else "UNKNOWN"


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def evaluate_strategy(ev: StrategyRunEvidence, gate: StrategyGateConfig | None = None, registered_gate: dict | None = None) -> dict:
    lib, req = ev.library or {}, ev.request or {}
    gate = gate or StrategyGateConfig.from_dict(registered_gate)
    checks: list[dict] = []
    has = lib.get("evidence_version") is not None

    def missing(name: str, what: str) -> None:
        checks.append(_check(name, INSUFFICIENT, f"이 실행에 기록되지 않음: {what}. 현재 버전으로 다시 실행해야 평가할 수 있음"))

    # 1. evidence recorded at all
    checks.append(_check("evidence_recorded", PASS if has else INSUFFICIENT,
                         "게이트용 실행 기록(evidence_version) 있음" if has else
                         "이전 버전 실행: 기간별 벤치마크·비용 스트레스·데이터 출처가 기록되지 않음",
                         evidence_version=lib.get("evidence_version")))
    # 2. thresholds fixed in advance
    reg = StrategyGateConfig.from_dict(registered_gate) if registered_gate is not None else StrategyGateConfig()
    diff = {k: {"registered_or_default": v, "used": asdict(gate)[k]} for k, v in asdict(reg).items() if asdict(gate)[k] != v}
    checks.append(_check("thresholds", WATCH if diff else PASS,
                         "등록값·기본값과 다른 임계값(결과를 본 뒤 정한 기준과 구별할 수 없음)" if diff else
                         ("사전 등록된 임계값" if registered_gate is not None else "기본 임계값(docs/ROBUSTNESS_GATE.md)"), changed=diff))
    # 3. data source, validity, audit
    scope = data_scope(lib)
    if not has:
        missing("data_source", "데이터 출처(data_sources/synthetic)")
    else:
        checks.append(_check("data_source", {"REAL": PASS, "UNKNOWN": INSUFFICIENT}.get(scope, FAIL),
                             {"SYNTHETIC_DEMO": "합성(DEMO) 데이터: 실제 시장 검증으로 쓸 수 없음",
                              "MIXED": "DEMO와 실제 데이터가 섞임: 결과를 해석할 수 없음",
                              "UNKNOWN": "데이터 출처가 기록되지 않음",
                              "REAL": "데이터 출처가 실제 데이터로 표시됨(출처 자체의 검증은 별도)"}[scope],
                             sources=lib.get("data_sources"), scope=scope))
    vs = lib.get("validity_status")            # recorded only when a run was assessed with app.research.validity
    checks.append(_check("data_validity", {"VERIFIED": PASS, "PROVISIONAL": WATCH, "INELIGIBLE": FAIL}.get(vs, WATCH),
                         f"결과 상태 {vs}" if vs else "결과 상태 판정(validity.assess)이 이 실행에 기록되지 않음 — 앱 DB 실행에는 아직 연결되지 않음",
                         validity_status=vs))
    au = lib.get("audit")                      # recorded only when a run excluded bars with an app.audit folder
    if not au:
        checks.append(_check("audit_applied", WATCH, "데이터 감사(app.audit) 결과가 이 실행에 연결되지 않아 문제 봉을 제외하지 못함 — 앱 DB 실행에는 아직 연결되지 않음"))
    else:
        rs = au.get("review_share")
        checks.append(_check("audit_applied", WATCH if rs is None or rs > 0.5 else PASS,
                             f"감사 적용, review 비율 {rs}" if rs is not None else "감사 적용, review 비율 미기록", **au))
    checks.append(eligibility_check(lib.get("data_eligibility"), ev.data_fingerprint if lib.get("data_eligibility") else None))
    # 4. fingerprint and settings integrity
    fp = ev.data_fingerprint
    if not fp:
        checks.append(_check("data_fingerprint", INSUFFICIENT, "데이터 지문이 기록되지 않음"))
    else:
        spec_fp = (ev.ledger_spec or {}).get("data_fingerprint")
        bad = spec_fp is not None and spec_fp != fp
        checks.append(_check("data_fingerprint", FAIL if bad else PASS,
                             "등록된 지문과 실행 데이터 지문이 다름" if bad else "실행 데이터 지문 기록됨", run=fp, registered=spec_fp))
    problems = []
    if not req.get("settings_hash"):
        pass
    elif ev.settings_hash_recomputed is None:
        problems.append("저장된 설정으로 설정 해시를 다시 계산할 수 없음")
    elif ev.settings_hash_recomputed != req["settings_hash"]:
        problems.append("저장된 설정으로 다시 계산한 설정 해시가 기록과 다름")
    rc = req.get("costs") or {}
    spec_costs = (ev.ledger_spec or {}).get("cost_assumptions") or {}
    man_costs = ((ev.manifest or {}).get("config") or {}).get("costs") or {}
    for k in ("commission_bps", "sell_tax_bps", "slippage_bps"):
        if k in spec_costs and _num(spec_costs[k]) != _num(rc.get(k)):
            problems.append(f"원장에 등록된 비용 {k}와 실행 비용이 다름")
        if k in man_costs and _num(man_costs[k]) != _num(rc.get(k)):
            problems.append(f"manifest 비용 {k}와 실행 비용이 다름")
    if ev.manifest and (ev.manifest.get("config") or {}).get("params") != req.get("params"):
        problems.append("manifest 파라미터와 실행 파라미터가 다름")
    checks.append(_check("settings_integrity", FAIL if problems else (PASS if req.get("settings_hash") else INSUFFICIENT),
                         "; ".join(problems) if problems else
                         ("설정 해시·비용·파라미터가 실행 기록과 일치" if req.get("settings_hash") else "설정 해시가 기록되지 않음"),
                         costs=rc, settings_hash=req.get("settings_hash")))
    # 5. periods
    per_cfg = lib.get("periods")
    if not per_cfg:
        missing("period_separation", "기간 설정(periods)")
    else:
        spans = sorted((pd.Timestamp(a), pd.Timestamp(b), k) for k, (a, b) in per_cfg.items())
        overlap = [f"{k1}/{k2}" for (a1, b1, k1), (a2, _, k2) in zip(spans, spans[1:]) if a2 <= b1]
        crossing = int(sum(r.get("trades_crossing_period_end") or 0 for r in lib.get("by_period") or []))
        need = {"dev", "validation", "test"} - set(per_cfg)
        status = FAIL if overlap or need else (WATCH if crossing else PASS)
        checks.append(_check("period_separation", status,
                             f"기간 겹침 {overlap}" if overlap else f"기간 누락 {sorted(need)}" if need else
                             f"기간 끝을 넘는 거래 {crossing}건(다음 기간 가격으로 청산)" if crossing else
                             "dev/validation/test가 겹치지 않고 기간을 넘는 거래 없음",
                             periods=per_cfg, overlapping=overlap, trades_crossing=crossing))
    # 6. pre-registration and selection
    link = req.get("ledger")
    st = ev.ledger_state
    if not link or st is None:
        checks.append(_check("preregistration", WATCH, "Experiment Ledger에 사전 등록되지 않은 실행: 탐색 결과와 구별할 수 없음"))
    else:
        # order in the append-only ledger decides (the DB timestamp has only second resolution)
        lrun = (st.get("runs") or {}).get(link.get("run_id"))
        after = lrun is None or lrun.get("started_seq", 0) < (st.get("registered_seq") or 0)
        amended = [a for a in st["amendments"] if a["after_results"]]
        stale = link.get("spec_hash") != st["spec_hash"]
        checks.append(_check("preregistration", FAIL if after else (WATCH if amended or stale else PASS),
                             "원장에 이 실행의 시작 기록이 없거나 등록보다 먼저 시작됨" if after else "결과 후 명세 수정" if amended else
                             "실행 이후 명세가 바뀜" if stale else "실행 전에 등록, 명세 변경 없음",
                             experiment_id=link.get("experiment_id"), registered_at=st["registered_at"], run_created=ev.created_at))
    tried = ev.family_settings_tried
    if tried is None:
        checks.append(_check("selection_count", INSUFFICIENT, "같은 전략에서 시도한 설정 수를 알 수 없음"))
    else:
        checks.append(_check("selection_count", WATCH if tried > 1 else PASS,
                             f"같은 전략에서 {tried}개 설정을 실행함: 각 설정마다 test 구간도 이미 보았으므로 test 결과도 선택 편향을 받음"
                             if tried > 1 else "이 전략에서 실행한 설정은 이것 하나", settings_tried=tried))
    # 7. sample size
    mins = {"dev": gate.min_trades_dev, "validation": gate.min_trades_validation, "test": gate.min_trades_test}
    if not lib.get("by_period"):
        missing("sample_size", "기간별 결과(by_period)")
    else:
        counts = {p: int((_per(lib, p) or {}).get("trades") or 0) for p in mins}
        short = {p: c for p, c in counts.items() if c < mins[p]}
        checks.append(_check("sample_size", INSUFFICIENT if short else PASS, f"거래 수 부족 {short}" if short else "기간별 거래 수 충분",
                             trades=counts, minimum=mins))

    # 8. dev / validation / test with fixed parameters; test is evaluated last and only confirms
    def oos(p: str, is_dev: bool = False) -> dict:
        r = _per(lib, p)
        name = "dev_effect" if is_dev else f"{p}_out_of_sample"
        if not r or r.get("excess_net") is None or "expectancy_return" not in r:
            return _check(name, INSUFFICIENT, f"{p}: 비용 후 벤치마크 대비 초과수익 또는 거래 기대수익이 없음(기간에 데이터 없음 또는 이전 버전 실행)")
        if int(r.get("trades") or 0) < mins[p]:
            return _check(name, INSUFFICIENT, f"{p}: 거래 {r.get('trades')}건 < {mins[p]}", trades=r.get("trades"))
        ex, er, lo = float(r["excess_net"]), r.get("expectancy_return"), _ci_low(r)
        vals = dict(excess_net=ex, total_return=r.get("total_return"), benchmark_total_return=r.get("benchmark_total_return"),
                    expectancy_return=er, expectancy_return_ci95=r.get("expectancy_return_ci95"), trades=r.get("trades"))
        if ex <= 0 or er is None or er <= 0:
            return _check(name, FAIL, f"{p}: 비용 후 벤치마크 대비 초과수익 ≤ 0 또는 거래 기대수익 ≤ 0", **vals)
        if lo is None or lo <= 0:
            return _check(name, FAIL if is_dev else WATCH,
                          f"{p}: 거래 기대수익 95% 구간이 0을 포함" + (" — dev에서 효과가 확인되지 않음" if is_dev else ""), **vals)
        return _check(name, PASS, f"{p}: 비용 후 초과수익 > 0, 거래 기대수익 95% 구간 > 0", **vals)
    checks.append(oos("dev", True))
    checks.append(oos("validation"))
    # 9. yearly consistency inside dev + validation (fixed parameters; not a re-optimising walk-forward)
    yrs = lib.get("yearly")
    if yrs is None or not per_cfg:
        missing("yearly_consistency", "연도별 결과(yearly)")
    else:
        end_val = pd.Timestamp(per_cfg["validation"][1]) if "validation" in per_cfg else None
        use = [y for y in yrs if y.get("excess_net") is not None and (end_val is None or pd.Timestamp(f"{y['year']}-01-01") <= end_val)]
        if len(use) < gate.min_years:
            checks.append(_check("yearly_consistency", INSUFFICIENT, f"dev+validation 연도 {len(use)}개 < {gate.min_years}", years=use))
        else:
            hit = float(np.mean([y["excess_net"] > 0 for y in use]))
            checks.append(_check("yearly_consistency", PASS if hit >= gate.yearly_min_hit else (FAIL if hit < gate.yearly_fail_below else WATCH),
                                 f"고정 파라미터로 연도별 비용 후 초과수익 > 0인 비율 {hit:.0%}(재최적화 워크포워드 아님)",
                                 hit_share=hit, years=use))
    # 10. cost stress (validation)
    cs = lib.get("cost_stress")
    val_trades = int((_per(lib, "validation") or {}).get("trades") or 0)
    if cs is not None and val_trades < gate.min_trades_validation:
        checks.append(_check("cost_stress", INSUFFICIENT, f"validation 거래 {val_trades}건 < {gate.min_trades_validation}: 비용 스트레스를 판단할 표본이 없음"))
    elif cs is None:
        missing("cost_stress", "비용 스트레스 재실행(cost_stress)")
    else:
        res, absent = {}, []
        for m in gate.cost_stress_multipliers:
            r = _per(cs.get(str(m)) or {}, "validation")
            if r is None or r.get("excess_net") is None:
                absent.append(m)
            else:
                res[str(m)] = r["excess_net"]
        if absent:
            checks.append(_check("cost_stress", INSUFFICIENT, f"비용 배수 {absent}의 validation 재실행 결과가 없음", excess_by_multiplier=res))
        else:
            bad = [k for k, x in res.items() if not x > 0]
            first = str(gate.cost_stress_multipliers[0])
            checks.append(_check("cost_stress", PASS if not bad else (FAIL if first in bad else WATCH),
                                 "비용을 올려도 validation 초과수익 > 0" if not bad else f"비용 x{', x'.join(bad)}에서 validation 초과수익 ≤ 0",
                                 excess_by_multiplier=res, note="수수료·세금·슬리피지를 모두 배수로 올려 엔진을 다시 실행한 결과"))
    # 11. parameter neighbourhood
    if not ev.neighbours:
        checks.append(_check("parameter_neighbourhood", WATCH, "같은 조건에서 다른 파라미터로 실행한 기록이 없어 안정성을 확인하지 못함"))
    else:
        flips = [n for n in ev.neighbours if not (n.get("validation_excess") is not None and n["validation_excess"] > 0)]
        checks.append(_check("parameter_neighbourhood", PASS if not flips else WATCH,
                             "같은 조건의 다른 파라미터도 validation 초과수익 > 0" if not flips else
                             f"다른 파라미터 {len(flips)}개는 validation 초과수익 ≤ 0 또는 기록 없음", neighbours=ev.neighbours))
    # 12. benchmark kind
    b = lib.get("benchmark")
    if not b:
        missing("benchmark", "벤치마크 종류(benchmark)")
    else:
        checks.append(_check("benchmark", PASS if b.get("kind") == "index" else WATCH,
                             f"실제 시장지수({b.get('symbol')}) 매수 후 보유, 비용 미반영" if b.get("kind") == "index"
                             else "지수가 없어 동일가중 대용 벤치마크 사용: 실제 시장지수가 아님", **b))
    # 13. reproducibility
    if not ev.manifest:
        checks.append(_check("reproducibility", WATCH, "실행 manifest 없음(Experiment Ledger 없이 실행): 코드 커밋·산출물 해시를 확인할 수 없음"))
    else:
        git = ev.manifest.get("git") or {}
        mism = ev.manifest_output_mismatches
        checks.append(_check("reproducibility", FAIL if mism else (PASS if git.get("reproducible_from_commit") else WATCH),
                             f"산출물이 실행 후 바뀜: {mism}" if mism else
                             "커밋된 코드, 산출물 해시 일치" if git.get("reproducible_from_commit") else "실행 시 커밋되지 않은 코드",
                             commit=git.get("commit"), changed_outputs=mism))
    checks.append(oos("test"))
    overall = max((c["status"] for c in checks), key=SEVERITY.get)
    return {"subject": {"kind": "strategy_library_run", "run_id": ev.run_id, "entry_id": ev.entry_id, "params": req.get("params")},
            "status": overall, "data_scope": scope, "real_data_verified": bool(overall == PASS and scope == "REAL"),
            "checks": checks, "gate_config": asdict(gate), "registered_gate": registered_gate,
            "not_implemented": NOT_IMPLEMENTED_STRATEGY, "data_fingerprint": fp,
            "ledger_experiment": (link or {}).get("experiment_id"),
            "note": ("PASS는 구현된 검사를 이 데이터에서 통과했다는 뜻일 뿐 지속적인 우위의 증거가 아닙니다. "
                     "Factor Lab 게이트와 검사 항목이 다르므로 두 판정을 같은 척도로 비교하지 마세요.")}
