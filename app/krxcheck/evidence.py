"""CLI: python -m app.krxcheck.evidence --krx-root data/krxcheck --data data/real --out data/crosscheck/price_evidence_<ts>
                                       [--dates YYYY-MM-DD ...] [--audit data/audit/<run>]

Offline only: compares SAVED KRX whole-market answers (data/krxcheck/full_market_<YYYYMMDD>/, from
`python -m app.krxcheck.fullmarket fetch`) with the local naver_raw files and writes price evidence for the eligibility
rules (app.research.price_evidence). No network, no keys. Inputs are only read; --out must be a new folder.

Outputs: crosscheck_status.json (existing contract + price_check.by_date), price_evidence.md, source_files.json
(SHA-256 of every file the evidence was made from, re-checked when the evidence is loaded), <date>/comparison_detail.csv.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from app.krxcheck.fullmarket import compare_market, load_krx_answer
from app.research.manifest import environment_info, git_info, guard_output_dir
from app.research.price_evidence import RULES_VERSION, build_status, evaluate_date, sha256_file


def discover_dates(krx_root: Path) -> list[str]:
    out = []
    for d in sorted(krx_root.glob("full_market_*")):
        meta = d / "fetch_meta.json"
        if meta.is_file() and json.loads(meta.read_text(encoding="utf-8")).get("status") == "ok":
            out.append(pd.Timestamp(d.name.split("_")[-1]).strftime("%Y-%m-%d"))
    return out


def local_only(data_root: Path, uni: pd.DataFrame | None, krx_codes: set[str], day: str, market: str) -> list[str]:
    """Universe symbols of the market with a local bar on the day but no row in the KRX answer."""
    if uni is None:
        return []
    out = []
    for s in uni.loc[uni["market"] == market, "symbol"].astype(str).str.zfill(6):
        if s in krx_codes:
            continue
        p = data_root / "raw" / f"{s}.csv"
        if p.is_file():
            df = pd.read_csv(p, dtype=str, usecols=lambda c: c.lower() == "date")
            if (pd.to_datetime(df.iloc[:, 0], errors="coerce").dt.strftime("%Y-%m-%d") == day).any():
                out.append(s)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.krxcheck.evidence")
    ap.add_argument("--krx-root", default="data/krxcheck")
    ap.add_argument("--data", default="data/real")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dates", nargs="*", help="YYYY-MM-DD; default: every saved full_market_* answer with status ok")
    ap.add_argument("--market", default="KOSPI", choices=["KOSPI"])
    ap.add_argument("--audit", help="audit folder to link (fingerprints are recorded, nothing is changed)")
    a = ap.parse_args(argv)
    root, data, out = Path(a.krx_root), Path(a.data), Path(a.out)
    try:
        guard_output_dir(out, data, overwrite=False)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    dates = a.dates or discover_dates(root)
    if not dates:
        print("저장된 KRX 응답이 없습니다. 교차 검증을 하지 않았습니다.", file=sys.stderr)
        return 3
    uni_path = data / "universe.csv"
    uni = pd.read_csv(uni_path, dtype=str) if uni_path.exists() else None
    files: dict[str, str] = {}
    for p in (uni_path, data / "manifest.jsonl"):
        if p.is_file():
            files[str(p)] = sha256_file(p)
    results = []
    out.mkdir(parents=True, exist_ok=False)
    for day in dates:
        bas = pd.Timestamp(day).strftime("%Y%m%d")
        kdir = root / f"full_market_{bas}"
        if not (kdir / f"krx_raw_kospi_stock_{bas}.csv").is_file():
            print(f"{day}: 저장된 KRX 응답 없음 — 건너뜀(비교하지 않은 날짜로 기록)", file=sys.stderr)
            results.append({"date": day, "krx_rows": 0, "comparable": 0, "unresolved_price": 0, "unverified_adjustments": 0,
                            "near_one_unverified_adjustments": 0, "enough_rows": False, "status": "FAILED", "passed": False,
                            "verdict_reason": "저장된 KRX 응답 없음", "rules_version": RULES_VERSION})
            continue
        krx, meta = load_krx_answer(kdir, bas, a.market)
        files[meta["krx_raw_file"]] = meta["krx_raw_sha256"]
        detail, info = compare_market(krx, data, day, uni)
        lo = local_only(data, uni, set(detail["symbol"].dropna()), day, a.market)
        for s in set(detail.loc[detail["local_file"], "symbol"]) | set(lo):
            p = data / "raw" / f"{s}.csv"
            files[str(p)] = sha256_file(p)
        r = evaluate_date(detail, info, meta, lo, day)
        results.append(r)
        (out / day).mkdir()
        if len(detail):
            from app.research.price_evidence import COMPARABLE, classify_disagreement
            comp = detail["status"].isin(COMPARABLE)
            detail.loc[comp, "disagreement_class"] = detail[comp].apply(classify_disagreement, axis=1)
        detail.to_csv(out / day / "comparison_detail.csv", index=False)
        print(f"{day}: KRX {r['krx_rows']} rows, comparable {r['comparable']}, unresolved price {r['unresolved_price']}, "
              f"unverified adjustments {r['unverified_adjustments']} (near-one {r['near_one_unverified_adjustments']}), {r['status']}", flush=True)
    rng = None
    man = data / "manifest.jsonl"
    if man.is_file():
        recs = [json.loads(x) for x in man.read_text(encoding="utf-8").splitlines() if x.strip()]
        firsts = [r["first"] for r in recs if r.get("first")]
        lasts = [r["last"] for r in recs if r.get("last")]
        rng = [min(firsts), max(lasts)] if firsts and lasts else None
    status = build_status(results, str(data), rng, {"command": "python -m app.krxcheck.evidence " + " ".join(argv or sys.argv[1:]),
                                                    "git": git_info(), "environment": environment_info(), "rules_version": RULES_VERSION})
    if a.audit:
        s = json.loads((Path(a.audit) / "audit_summary.json").read_text(encoding="utf-8"))
        status["audit_link"] = {"dir": a.audit, "usable_fingerprint": s.get("usable_fingerprint"), "input_fingerprint": s.get("input_fingerprint")}
    (out / "source_files.json").write_text(json.dumps(files, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "crosscheck_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    pc = status["price_check"]
    lines = [f"# 가격 교차 검증 증거 ({RULES_VERSION})", "", f"- 출처: {pc['source']}", f"- 비교 날짜: {pc['scope']['dates']} ({pc['scope']['market']})",
             f"- 연구 데이터 기간(수집 기록): {rng}",
             f"- 날짜 상태: PASSED {pc['dates_passed']} · PASSED_WITH_UNVERIFIED_ADJUSTMENTS {pc['dates_passed_with_unverified_adjustments']} · "
             f"FAILED {pc['dates_failed']} (총 {pc['dates_compared']}) · price_check.passed = {pc['passed']}",
             f"- 미해결 가격 불일치 {pc['unresolved_price_rows']}행 · 확인되지 않은 조정 가능 {pc['unverified_adjustment_rows']}행"
             f"(근접 1배율 {pc['near_one_unverified_adjustment_rows']}행) · 비교 불가 {pc['not_comparable_rows']}행",
             f"- verified.cross_source_price_check = **{status['verified']['cross_source_price_check']}** (일부 날짜만 비교하면 항상 false)", "",
             "| 날짜 | KRX 행 | 비교 가능 | 종가 일치(정확) | 종가 일치(0.1%) | 거래량 일치(정확) | 미검증 조정 가능(분할형/근접1/기타) | 미해결 가격 | 미해결 거래량 | 로컬에만 | 상태 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in results:
        if not r.get("comparable"):
            lines.append(f"| {r['date']} | {r.get('krx_rows', 0)} | 0 | — | — | — | — | — | — | — | {r['status']} |")
            continue
        lines.append(f"| {r['date']} | {r['krx_rows']} | {r['comparable']} | {r['exact_equal']['close']} ({r['exact_equal_rate']['close']:.1%}) | "
                     f"{r['within_tolerance']['close']} ({r['within_tolerance_rate']['close']:.1%}) | {r['exact_equal']['volume']} ({r['exact_equal_rate']['volume']:.1%}) | "
                     f"{r['unverified_adjustments']} ({r['split_like_unverified_adjustments']}/{r['near_one_unverified_adjustments']}/{r['other_unverified_adjustments']}) | "
                     f"{r['unresolved_price']} | {r['unresolved_volume']} | {r['only_in_local']} | {r['status']} |")
    lines += ["", "비율의 분모는 각 날짜의 '비교 가능' 행입니다. 비교하지 않은 행은 by_status에 따로 셉니다. 차이의 원인은 이 도구가 확정하지 않습니다.",
              "'미검증 조정 가능'은 네 가격이 같은 배율로 다른 행이며, 기업행동 자료로 확인되지 않았으므로 통과로 세지 않습니다."]
    (out / "price_evidence.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"done: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
