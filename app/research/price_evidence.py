"""Cross-source price evidence (rules `price-evidence-v2`): KRX daily trading answers saved on disk vs the local
naver_raw bars, turned into a record that `app.research.eligibility` can read.

Rules fixed before the comparison was re-run (they reuse existing project rules, not new numbers):
  * row classes come from `app.krxcheck.fullmarket.compare_market` (statuses, comparable = match + volume_mismatch_only
    + close_mismatch + ohl_mismatch_close_eq) and its per-row `classifier_status` from
    `app.research.krx_crosscheck.classify_pair` (match / adjusted_price_basis / volume_mismatch / price_mismatch);
  * exact equality: |local - KRX| < 0.5 (integer KRW and share counts), as in compare_market;
  * tolerance equality: relative price difference <= CLOSE_TOLERANCE (0.1 %) and volume <= VOLUME_TOLERANCE (1 %),
    the tolerances app.research.crosscheck already uses for the KIS check;
  * any price disagreement larger than CLOSE_TOLERANCE that is not a constant same-direction factor across all four
    prices is an UNRESOLVED material disagreement. Volume is reported but, as in the existing rule, does not decide
    the price verdict;
  * (v2) a row whose four prices differ by one factor in one direction is an UNVERIFIED ADJUSTMENT: possibly a
    different adjustment basis, not confirmed by any corporate-action source. It is counted on its own
    (`unverified_adjustments`, split-like / near-one / other) and is never treated as consistent;
  * a date needs at least MIN_COMPARABLE comparable rows to count;
  * date status: FAILED (unresolved price disagreement, too few comparable rows, or no saved answer),
    PASSED_WITH_UNVERIFIED_ADJUSTMENTS (no unresolved disagreement, but unverified adjustment rows), PASSED (neither).
    `passed` (per date and `price_check.passed`) is true only for PASSED;
  * `verified.cross_source_price_check` is true only when every compared date is PASSED AND the evidence covers the
    whole period of the research data (`scope.kind == "full_period"`). Single dates never verify a multi-year set.

v1 (price-evidence-v1) counted unverified adjustment rows as passing; v1 files stay as they were and are read with
their adjustment rows counted as unverified (see summary_for_eligibility).

Classification of disagreements never names a cause without evidence: an adjustment basis is "possible" only because
all four prices move by one factor; a corporate action is not asserted here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from app.research.crosscheck import CLOSE_TOLERANCE, VOLUME_TOLERANCE
from app.research.krx_crosscheck import classify_pair

RULES_VERSION = "price-evidence-v2"
EVIDENCE_VERSION = 2
PASSED, PASSED_UNVERIFIED, FAILED = "PASSED", "PASSED_WITH_UNVERIFIED_ADJUSTMENTS", "FAILED"
NEAR_ONE = 0.03                  # |factor - 1| within 3 % — the same log(1.03) band classify_pair uses for split-likeness
MIN_COMPARABLE = 100
FIELDS = ("open", "high", "low", "close", "volume")
PRICE = ("open", "high", "low", "close")
COMPARABLE = ("match", "volume_mismatch_only", "close_mismatch", "ohl_mismatch_close_eq")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def classify_disagreement(r: pd.Series) -> str:
    """One comparable row -> a disagreement class. Causes are named only when the data themselves show them."""
    cs = r.get("classifier_status")
    price_rel = max(abs(r[f"local_{f}"] - r[f"krx_{f}"]) / r[f"krx_{f}"] for f in PRICE)
    vol_eq = abs(r["local_volume"] - r["krx_volume"]) < 0.5
    vol_tol = abs(r["local_volume"] / r["krx_volume"] - 1) <= VOLUME_TOLERANCE if r["krx_volume"] else False
    if cs == "match":
        return "exact_match"
    if cs == "adjusted_price_basis":
        return "unverified_adjustment_possible"           # every OHLC field off by one factor, same direction — not confirmed
    if cs == "volume_mismatch":
        return "volume_within_tolerance" if vol_tol else "volume_difference_unresolved"
    if price_rel <= CLOSE_TOLERANCE:
        return "price_within_tolerance" + ("" if vol_eq or vol_tol else "_volume_difference")
    return "price_difference_unresolved"


def evaluate_date(detail: pd.DataFrame, info: dict, krx_meta: dict, local_only: list[str], day: str) -> dict:
    n = len(detail)
    by = detail["status"].value_counts().to_dict() if n else {}
    comp = detail[detail["status"].isin(COMPARABLE)].copy() if n else detail
    m = len(comp)
    out = {"date": day, "krx_rows": int(n), "krx_raw_file": krx_meta.get("krx_raw_file"), "krx_raw_sha256": krx_meta.get("krx_raw_sha256"),
           "krx_received_at_utc": krx_meta.get("received_at_utc"), "local_files_read": info.get("local_files_read"),
           "local_fingerprint": info.get("local_fingerprint"), "by_status": {k: int(v) for k, v in by.items()},
           "mapping": {"krx_codes_valid": int(n - by.get("krx_row_invalid", 0)), "krx_codes_invalid": int(by.get("krx_row_invalid", 0)),
                       "local_file_found": int(detail["local_file"].sum()) if n else 0,
                       "local_file_missing_in_universe": int(((detail["status"] == "missing_local_file") & detail["in_universe"]).sum()) if n else 0,
                       "local_file_missing_out_of_universe": int(((detail["status"] == "missing_local_file") & ~detail["in_universe"]).sum()) if n else 0},
           "only_in_krx": int(by.get("missing_local_file", 0) + by.get("missing_local_date", 0)),
           "only_in_local": len(local_only), "only_in_local_symbols": local_only[:50],
           "not_comparable": int(by.get("not_comparable", 0) + by.get("local_row_invalid", 0)),
           "comparable": int(m), "denominator_note": "rates use comparable rows only; rows not compared are counted above, never dropped silently"}
    if m:
        for f in FIELDS:
            comp[f"{f}_equal"] = comp[f"{f}_equal"].astype(bool)
        rel = {f: (comp[f"local_{f}"] - comp[f"krx_{f}"]).abs() / comp[f"krx_{f}"] for f in FIELDS}
        out["exact_equal"] = {f: int(comp[f"{f}_equal"].sum()) for f in FIELDS}
        out["exact_equal_rate"] = {f: round(out["exact_equal"][f] / m, 6) for f in FIELDS}
        tol = {f: (CLOSE_TOLERANCE if f in PRICE else VOLUME_TOLERANCE) for f in FIELDS}
        out["within_tolerance"] = {f: int((rel[f] <= tol[f]).sum()) for f in FIELDS}
        out["within_tolerance_rate"] = {f: round(out["within_tolerance"][f] / m, 6) for f in FIELDS}
        out["tolerances"] = {"price_rel": CLOSE_TOLERANCE, "volume_rel": VOLUME_TOLERANCE, "exact": "abs diff < 0.5"}
        comp["disagreement_class"] = comp.apply(classify_disagreement, axis=1)
        out["classes"] = {k: int(v) for k, v in comp["disagreement_class"].value_counts().items()}
        adj = comp[comp["disagreement_class"] == "unverified_adjustment_possible"]
        sub = {"split_like": 0, "near_one": 0, "other": 0}
        for _, r in adj.iterrows():
            c = classify_pair({f: r[f"local_{f}"] for f in FIELDS}, {f: r[f"krx_{f}"] for f in FIELDS})
            if c.get("split_like_ratio") is not None:
                sub["split_like"] += 1
            elif c.get("factor") is not None and abs(c["factor"] - 1) <= NEAR_ONE:
                sub["near_one"] += 1
            else:
                sub["other"] += 1
        out["unverified_adjustments"] = int(len(adj))
        out["split_like_unverified_adjustments"] = sub["split_like"]
        out["near_one_unverified_adjustments"] = sub["near_one"]
        out["other_unverified_adjustments"] = sub["other"]
        unresolved = comp[comp["disagreement_class"] == "price_difference_unresolved"]
        out["unresolved_price"] = int(len(unresolved))
        out["unresolved_volume"] = int((comp["disagreement_class"].isin(["volume_difference_unresolved", "price_within_tolerance_volume_difference"])).sum())
        lr = (unresolved["local_close"] / unresolved["krx_close"] - 1) if len(unresolved) else pd.Series(dtype=float)
        out["unresolved_price_close_rel"] = ({"n": int(len(lr)), "median": float(lr.median()), "min": float(lr.min()), "max": float(lr.max()),
                                              "share_local_below_krx": round(float((lr < 0).mean()), 4)} if len(lr) else {"n": 0})
        vr = comp["local_volume"] / comp["krx_volume"]
        out["volume_ratio_local_to_krx"] = {"median": float(vr.median()), "share_below_1": round(float((vr < 1 - 1e-9).mean()), 4)}
        out["unresolved_examples"] = unresolved.assign(a=(unresolved["local_close"] / unresolved["krx_close"] - 1).abs()) \
            .sort_values("a", ascending=False)[["symbol", "name", "krx_open", "local_open", "krx_high", "local_high", "krx_low", "local_low",
                                                "krx_close", "local_close", "krx_volume", "local_volume"]].head(10).to_dict("records") if len(unresolved) else []
    else:
        out.update(exact_equal={}, within_tolerance={}, classes={}, unresolved_price=0, unresolved_volume=0, unverified_adjustments=0,
                   split_like_unverified_adjustments=0, near_one_unverified_adjustments=0, other_unverified_adjustments=0)
    out["enough_rows"] = m >= MIN_COMPARABLE
    if not out["enough_rows"] or out["unresolved_price"]:
        out["status"] = FAILED
    elif out["unverified_adjustments"]:
        out["status"] = PASSED_UNVERIFIED
    else:
        out["status"] = PASSED
    out["passed"] = out["status"] == PASSED
    out["verdict_reason"] = ("비교 가능 행 부족" if not out["enough_rows"] else
                             f"설명되지 않은 가격 불일치 {out['unresolved_price']}행" if out["unresolved_price"] else
                             f"미해결 가격 불일치 0행, 확인되지 않은 조정 가능 행 {out['unverified_adjustments']}행"
                             f"(근접 1배율 {out['near_one_unverified_adjustments']}행) — 조정으로 확인되지 않음" if out["unverified_adjustments"] else
                             "비교 가능 행에서 허용 오차를 넘는 가격 차이 없음")
    out["rules_version"] = RULES_VERSION
    return out


def build_status(dates: list[dict], data_root: str, research_range: list[str | None], tool: dict) -> dict:
    """crosscheck_status.json-compatible record. `verified` follows the existing contract (bool per verification)."""
    compared = [d["date"] for d in dates]
    passed_dates = all(d.get("status") == PASSED for d in dates) if dates else False
    scope = {"kind": "sample_dates", "dates": compared, "market": "KOSPI",
             "date_range": [min(compared), max(compared)] if compared else None, "research_data_range": research_range,
             "note": "개별 거래일의 KOSPI 종목만 비교함. 다른 날짜·KOSDAQ·2016년 이후 전체 기간을 검증하지 않음"}
    unresolved = sum(d["unresolved_price"] for d in dates)
    return {"evidence_version": EVIDENCE_VERSION, "rules_version": RULES_VERSION, "created_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "tool": tool, "data_root": data_root,
            "price_check": {"ran": bool(dates), "source": "KRX Open API 유가증권 일별매매정보(저장된 응답, 새 요청 없음)",
                            "dates_compared": len(dates), "dates_passed": sum(d.get("status") == PASSED for d in dates),
                            "dates_passed_with_unverified_adjustments": sum(d.get("status") == PASSED_UNVERIFIED for d in dates),
                            "dates_failed": sum(d.get("status") == FAILED for d in dates),
                            "passed": passed_dates, "unresolved_price_rows": unresolved,
                            "unverified_adjustment_rows": sum(d.get("unverified_adjustments", 0) for d in dates),
                            "near_one_unverified_adjustment_rows": sum(d.get("near_one_unverified_adjustments", 0) for d in dates),
                            "not_comparable_rows": sum(d.get("not_comparable", 0) for d in dates),
                            "comparable_rows": sum(d["comparable"] for d in dates), "scope": scope,
                            "rules": {"rules_version": RULES_VERSION, "min_comparable": MIN_COMPARABLE, "price_rel_tolerance": CLOSE_TOLERANCE,
                                      "near_one_band": NEAR_ONE, "unverified_adjustments_pass": False,
                                      "volume_rel_tolerance": VOLUME_TOLERANCE, "volume_decides_verdict": False},
                            "by_date": dates},
            "filing_check": {"ran": False, "reason": "이 도구는 공시 대조를 하지 않음"},
            # existing contract: true only for checks that ran, passed and cover the research data period
            "verified": {"cross_source_price_check": bool(passed_dates and scope["kind"] == "full_period"),
                         "corporate_action_disclosure_match": False}}


def load_evidence(path: str | Path | None, verify_sources: bool = True) -> dict | None:
    """Read a price-evidence crosscheck_status.json and re-check the files it was made from.
    Returns {"ok", "problems", "status"}; None when no path is given. Never raises on bad input."""
    if not path:
        return None
    p = Path(path)
    res = {"ok": False, "problems": [], "status": None, "path": str(p)}
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        res["problems"].append("증거 파일 없음")
        return res
    except ValueError:
        res["problems"].append("증거 파일을 읽을 수 없음(손상)")
        return res
    res["status"] = st
    pc = st.get("price_check") if isinstance(st, dict) else None
    if not isinstance(pc, dict) or st.get("evidence_version") is None or not isinstance(pc.get("by_date"), list):
        res["problems"].append("가격 증거 형식이 아님(evidence_version·price_check.by_date 없음)")
        return res
    if verify_sources:
        man = p.parent / "source_files.json"
        try:
            files = json.loads(man.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            res["problems"].append("원본 파일 해시 목록(source_files.json) 없음 또는 손상")
            files = {}
        changed = [f for f, h in files.items() if not Path(f).is_file() or sha256_file(Path(f)) != h]
        if changed:
            res["problems"].append(f"증거를 만든 뒤 원본 파일 {len(changed)}개가 바뀌었거나 없어짐: {changed[:5]}")
    res["ok"] = not res["problems"]
    return res


def _unverified(d: dict) -> int:
    """v2 days carry the count; v1 days only have the class count (they were counted as passing then)."""
    if "unverified_adjustments" in d:
        return int(d["unverified_adjustments"])
    return int((d.get("classes") or {}).get("price_adjustment_basis_possible", 0))


def _day_status(d: dict) -> str:
    if d.get("status"):
        return d["status"]
    if not d.get("enough_rows", True) or d.get("unresolved_price") or not d.get("comparable"):
        return FAILED
    return PASSED_UNVERIFIED if _unverified(d) else PASSED


def summary_for_eligibility(ev: dict | None, run_first: str | None, run_last: str | None) -> dict | None:
    """The part of the evidence the eligibility rule reads: validity, dates inside the run, unresolved disagreements,
    unverified adjustments and how the evidence period relates to the run period."""
    if ev is None:
        return None
    st = ev.get("status") or {}
    pc = st.get("price_check") or {}
    all_days = pc.get("by_date") or []
    days = [d for d in all_days if (not run_first or d["date"] >= run_first) and (not run_last or d["date"] <= run_last)]
    stat = [_day_status(d) for d in days]
    p = Path(ev["path"]) if ev.get("path") else None
    ev_dates = [d["date"] for d in all_days]
    return {"ok": ev.get("ok"), "problems": ev.get("problems"), "path": ev.get("path"),
            "evidence_sha256": sha256_file(p) if p and p.is_file() else None,
            "rules_version": st.get("rules_version"), "scope": pc.get("scope"),
            "evidence_range": [min(ev_dates), max(ev_dates)] if ev_dates else None, "run_range": [run_first, run_last],
            "dates_in_run": [d["date"] for d in days], "dates_outside_run": [d for d in ev_dates if d not in {x["date"] for x in days}],
            "dates_compared_in_run": len(days), "dates_passed_in_run": stat.count(PASSED),
            "dates_with_unverified_adjustments_in_run": stat.count(PASSED_UNVERIFIED),
            "dates_failed_in_run": [d["date"] for d, s in zip(days, stat) if s == FAILED],
            "unresolved_price_in_run": int(sum(d.get("unresolved_price", 0) for d in days)),
            "unverified_adjustments_in_run": int(sum(_unverified(d) for d in days)),
            "near_one_unverified_adjustments_in_run": (int(sum(d["near_one_unverified_adjustments"] for d in days))
                                                       if all("near_one_unverified_adjustments" in d for d in days) else None),
            "not_comparable_in_run": int(sum(d.get("not_comparable", 0) for d in days)),
            "comparable_rows_in_run": int(sum(d.get("comparable", 0) for d in days))}
