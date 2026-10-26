"""CLI: python -m app.factorlab run --data DIR [--format naver_raw|standard] --out DIR [--start --end]
                                 [--factors mom_20,mom_60 ...] [--horizon 20] [--quantiles 5] [--min-names 30]
                                 [--audit DIR] [--ledger FILE --ledger-id ID [--run-id R]] [--overwrite]
       python -m app.factorlab factors        list the factor definitions

Reads the data folder only. Writes factor_ic_by_date.csv, factor_quantiles.csv, factor_summary.csv,
factor_yearly.csv, run_manifest.json and REPORT.md into a new --out folder (default refuses a non-empty folder).
With --ledger-id the run is recorded in the experiment ledger (start, success or failure, manifest, outputs);
the experiment must already be registered, and a failed run is recorded as a failure, not dropped.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

from app.research.data import load_naver_raw, load_standard_csv, sample_description
from app.research.factors import FactorLabConfig, default_factors, run_factorlab
from app.research.ledger import Ledger, LedgerError
from app.research.manifest import environment_info, git_info, guard_output_dir, output_inventory
from app.research.study import _fingerprint
from app.research.validity import assess

OUTPUTS = ("factor_ic_by_date.csv", "factor_quantiles.csv", "factor_summary.csv", "factor_yearly.csv")


def _fmt(x, pct=False, nd=3):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    return f"{x * 100:.2f}%" if pct else f"{x:.{nd}f}"


def write_report(out: Path, res: dict, manifest: dict) -> None:
    s = res["summary"]
    lines = ["# Factor Lab 결과", "", f"- 명령: `{manifest['command']}`",
             f"- 코드: `{manifest['git'].get('commit_short')}` (재현 가능: {manifest['git'].get('reproducible_from_commit')})",
             f"- 데이터 지문: `{manifest['data_fingerprint']}` · 결과 상태: **{manifest['validity']['status']}**",
             f"- 보유 {manifest['config']['horizon']}거래일, 진입 t+1 시가 → 청산 t+1+h 시가, 분위 {manifest['config']['quantiles']}개",
             f"- 비용(편도): 매수 {manifest['cost_assumptions']['buy_side_bps']}bp, 매도 {manifest['cost_assumptions']['sell_side_bps']}bp "
             f"(왕복 {manifest['cost_assumptions']['round_trip_bps']}bp). 공매도 대차비용·제한은 반영하지 않음",
             "", "**이 표는 연구용 진단입니다. 수익을 보장하지 않으며 Alpha 검증 결과가 아닙니다. 판정은 `python -m app.robustness`가 합니다.**", ""]
    if s.empty:
        lines.append("평가 가능한 날짜가 없습니다(표본 부족).")
    else:
        lines += ["| 요인 | 기간 | 날짜 수 | 평균 종목 | Rank IC | t | 상위-하위(총) | 상위-하위(순) | 상위 순-동일가중 | 상위 회전율 |", "|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in s.iterrows():
            lines.append(f"| {r['factor']} | {r['period']} | {r['dates']} | {r['names_mean']:.0f} | {_fmt(r['rank_ic_mean'])} | {_fmt(r['rank_ic_t'], nd=2)} | "
                         f"{_fmt(r['ls_gross_mean'], True)} | {_fmt(r['ls_net_mean'], True)} | {_fmt(r['top_net_excess_ew_mean'], True)} | {_fmt(r['turnover_top_mean'], True)} |")
    lines += ["", "평균은 보유 구간(h거래일) 1회당 값이며 연율화하지 않았습니다. 기간별 날짜 수가 작으면 t값은 신뢰하기 어렵습니다.",
              "", "## 제외 건수", "", "```", json.dumps({"panel": res["panel_counts"], "per_factor": res["exclusions"]}, ensure_ascii=False, indent=1), "```"]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(a, argv) -> int:
    out, data = Path(a.out), Path(a.data)
    try:
        guard_output_dir(out, data, a.overwrite)
    except ValueError as exc:
        print(str(exc), file=sys.stderr); return 2
    cfg = FactorLabConfig(horizon=a.horizon, quantiles=a.quantiles, min_cross_section=a.min_names, audit_dir=a.audit)
    if a.min_turnover is not None:
        cfg.min_avg_turnover = a.min_turnover
    if a.factors:
        names = [x.strip() for x in a.factors.split(",") if x.strip()]
        unknown = [n for n in names if n not in cfg.factors]
        if unknown:
            print(f"unknown factors {unknown}; see `python -m app.factorlab factors`", file=sys.stderr); return 2
        cfg.factors = {n: cfg.factors[n] for n in names}
    try:
        cfg.validate()
    except ValueError as exc:
        print(str(exc), file=sys.stderr); return 2
    led, run_id, spec = None, a.run_id or time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime()), None
    cmd = "python -m app.factorlab " + " ".join(shlex.quote(x) for x in argv)
    if a.ledger_id:
        led = Ledger(a.ledger)
        try:
            st = led.experiment(a.ledger_id)
            led.start_run(a.ledger_id, run_id, cmd)
        except LedgerError as exc:
            print(f"ledger: {exc}", file=sys.stderr); return 2
        spec = {"id": a.ledger_id, "version": st["version"], "spec_hash": st["spec_hash"], "run_id": run_id}
    started = pd.Timestamp.now(tz="UTC").isoformat()
    t0 = time.time()
    try:
        loader = load_naver_raw if a.format == "naver_raw" else load_standard_csv
        ds = loader(data, a.start, a.end)
        if ds.bars.empty:
            raise ValueError("REAL 데이터가 없습니다")
        res = run_factorlab(ds.bars, ds.calendar, cfg, ds.indices.get("KOSPI"))
        out.mkdir(parents=True, exist_ok=True)
        res["by_date"].to_csv(out / OUTPUTS[0], index=False)
        res["quantiles"].to_csv(out / OUTPUTS[1], index=False)
        res["summary"].to_csv(out / OUTPUTS[2], index=False)
        res["yearly"].to_csv(out / OUTPUTS[3], index=False)
        desc = sample_description(ds)
        validity = assess(desc, {"rows_total": int(len(ds.bars) + len(ds.rejected)), "rows_error": int(len(ds.rejected))}, {})
        c = cfg.costs
        manifest = {
            "manifest_version": 2, "kind": "factorlab", "command": cmd, "started_utc": started,
            "finished_utc": pd.Timestamp.now(tz="UTC").isoformat(), "seconds": round(time.time() - t0, 1),
            "git": git_info(), "environment": environment_info(), "config": json.loads(json.dumps(res["config"], default=str)),
            "cost_assumptions": {**c, "buy_side_bps": c["commission_bps"] + c["slippage_bps"],
                                 "sell_side_bps": c["commission_bps"] + c["slippage_bps"] + c["sell_tax_bps"],
                                 "round_trip_bps": 2 * c["commission_bps"] + 2 * c["slippage_bps"] + c["sell_tax_bps"],
                                 "convention": "per side; leg cost = one-way turnover x (buy + sell side); formation buy side, liquidation sell side",
                                 "not_modeled": ["short borrow fee", "short-sale restrictions", "market impact beyond flat slippage", "dividends"]},
            "timing": {"factor": "bars <= t", "entry": "open t+1", "exit": "open t+1+h", "rebalance": "every h calendar trading days (non-overlapping)"},
            "benchmarks": {"ew_universe": "equal-weight mean of names used that date, same timing (primary)",
                           "kospi_cc": "KOSPI close[exit]/close[entry]-1; close-to-close, timing differs from open-to-open (secondary)"},
            "data_fingerprint": _fingerprint(ds.bars), "sample": desc, "provenance": {k: v for k, v in ds.provenance.items() if k != "manifest_sha256"},
            "panel_counts": res["panel_counts"], "exclusions": res["exclusions"],
            "configs_tested_per_strategy": 1, "experiments_total": len(cfg.factors),
            "audit": None if not res["audit"] else {k: res["audit"][k] for k in ("dir", "review_share", "usable_fingerprint", "input_fingerprint")}
            | {"symbols_dropped": res["panel_counts"].get("audit_symbols_dropped", 0)},
            "validity": validity, "ledger_experiment": spec,
            "not_implemented": ["PBO", "deflated Sharpe ratio", "dividend / total return", "short borrow costs"],
        }
        manifest["outputs"] = output_inventory(out, skip=("run_manifest.json", "REPORT.md"))
        (out / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        write_report(out, res, manifest)
    except Exception as exc:  # the failure is recorded in the ledger, then reported
        if led:
            try:
                led.finish_run(a.ledger_id, run_id, "failure", f"{type(exc).__name__}: {exc}")
            except LedgerError as e2:
                print(f"ledger: {e2}", file=sys.stderr)
        traceback.print_exc()
        return 1
    if led:
        led.finish_run(a.ledger_id, run_id, "success")
        led.link_manifest(a.ledger_id, out / "run_manifest.json", run_id)
        for name in (*OUTPUTS, "REPORT.md"):
            led.link_artifact(a.ledger_id, out / name, name.rsplit(".", 1)[0], run_id)
    print(f"done: {out} ({len(res['summary'])} factor-period rows, {time.time() - t0:.1f}s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="python -m app.factorlab", description="Factor Lab MVP (research diagnostics).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("factors", help="list factor definitions")
    r = sub.add_parser("run", help="evaluate factors on a data folder")
    r.add_argument("--data", required=True); r.add_argument("--format", choices=["naver_raw", "standard"], default="naver_raw")
    r.add_argument("--out", required=True); r.add_argument("--start", default="2016-01-01"); r.add_argument("--end")
    r.add_argument("--factors", help="comma-separated subset (default: all)")
    r.add_argument("--horizon", type=int, default=20); r.add_argument("--quantiles", type=int, default=5)
    r.add_argument("--min-names", type=int, default=30, help="minimum names per date")
    r.add_argument("--min-turnover", type=float, help="KRW/day 20-day average (default: StudyConfig 1e9)")
    r.add_argument("--audit", help="audit folder from python -m app.audit")
    r.add_argument("--ledger", default="data/ledger/ledger.jsonl"); r.add_argument("--ledger-id"); r.add_argument("--run-id")
    r.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "factors":
        print(json.dumps(default_factors(), indent=2)); return 0
    return run(a, argv)


if __name__ == "__main__":
    sys.exit(main())
