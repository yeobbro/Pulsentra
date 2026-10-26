"""Research data eligibility and audit provenance (rules version `eligibility-v1`).

A data AUDIT (python -m app.audit) describes problems in a data set. ELIGIBILITY is a separate judgement about one
research run: may the rows that run used be treated as audited real data? It needs evidence that the run's rows are
the rows that were audited (fingerprint and window), that the audit outputs are intact, that excluded data was not
used, and that the data source is recorded and, ideally, independently checked.

Statuses reuse the Robustness Gate vocabulary so both gates can read the record directly:
    PASS              USABLE                 every check passed
    WATCH             REVIEW_REQUIRED        usable only after the listed items are reviewed
    FAIL              UNUSABLE               contradicting evidence (synthetic data, fingerprint mismatch, excluded rows used, damaged audit)
    INSUFFICIENT_DATA INSUFFICIENT_EVIDENCE  the evidence needed to decide is missing
The overall status is the worst check. A non-demo source label alone never gives PASS: a source record and an
independent cross-source check are separate checks. `real_data_verified` is true only for an overall PASS on REAL data.

The record is computed when a run is made and stored with that run; it is never recomputed into an old run.
Thresholds live in `EligibilityPolicy` and docs/DATA_ELIGIBILITY.md and are fixed before results are seen.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from app.data.audit import bars_fingerprint
from app.data.schema import is_demo_source
from app.research.manifest import file_sha256

RULES_VERSION = "eligibility-v3"
# v3 (2026-10-26): unverified price-adjustment rows in the price evidence (price-evidence-v2, or the adjustment rows
#   of a v1 file) keep source_verification at WATCH at best, even when a full-period flag is set; reports show their
#   count instead of "no mismatch". FAIL rules of v2 are unchanged.
# v2 (2026-10-26): source_verification also reads saved cross-source price evidence (app.research.price_evidence):
#   evidence damaged or made from files that changed since -> FAIL; unresolved material price disagreements on dates
#   inside the run -> FAIL; evidence only for some dates (or none inside the run) -> WATCH. v1 inputs give v1 results.
PASS, WATCH, FAIL, INSUFFICIENT = "PASS", "WATCH", "FAIL", "INSUFFICIENT_DATA"
SEVERITY = {PASS: 0, WATCH: 1, INSUFFICIENT: 2, FAIL: 3}
LABELS = {PASS: "USABLE", WATCH: "REVIEW_REQUIRED", FAIL: "UNUSABLE", INSUFFICIENT: "INSUFFICIENT_EVIDENCE"}
INDEPENDENT_CHECKS = ("cross_source_price_check",)        # verifications that confirm prices from a second source


@dataclass(frozen=True)
class EligibilityPolicy:
    max_audit_age_days: int = 30          # older audits are WATCH (data or rules may have moved on)


@dataclass
class RunDataFacts:
    """What a run knows about the rows it used. Unknown values stay None; nothing is inferred."""
    sources: list[str]
    data_fingerprint: str | None = None            # the run's own fingerprint (e.g. study._fingerprint)
    audit_compatible_fingerprint: str | None = None  # app.data.audit.bars_fingerprint of the run's loaded rows
    requested_start: str | None = None
    requested_end: str | None = None
    first_date: str | None = None
    last_date: str | None = None
    symbols: list[str] = field(default_factory=list)
    source_record: dict | None = None              # collection record (loader provenance), None when unknown
    verifications: dict = field(default_factory=dict)   # e.g. crosscheck_status.json "verified" flags
    audit_applied: bool = False                    # the run dropped `exclude` symbols and masked `exclude_until_verified` bars
    settings_hash: str | None = None
    price_evidence: dict | None = None             # price_evidence.summary_for_eligibility(...) or None


@dataclass
class AuditEvidence:
    dir: str
    ok: bool
    problems: list[str]
    summary: dict | None = None
    manifest: dict | None = None
    statuses: dict[str, str] = field(default_factory=dict)       # symbol -> ok / review / exclude
    last_dates: dict[str, str] = field(default_factory=dict)
    error_symbols: set[str] = field(default_factory=set)         # symbols with exclude_until_verified findings


def facts_from_bars(bars: pd.DataFrame, sources=None, **kw) -> RunDataFacts:
    srcs = sorted(map(str, sources if sources is not None else (bars["source"].dropna().unique() if "source" in bars else [])))
    d = pd.to_datetime(bars["date"]) if len(bars) else pd.Series(dtype="datetime64[ns]")
    return RunDataFacts(sources=srcs, audit_compatible_fingerprint=bars_fingerprint(bars) if len(bars) else None,
                        first_date=d.min().strftime("%Y-%m-%d") if len(d) else None,
                        last_date=d.max().strftime("%Y-%m-%d") if len(d) else None,
                        symbols=sorted(map(str, bars["symbol"].unique())) if len(bars) else [], **kw)


def load_audit_evidence(audit_dir: str | Path | None) -> AuditEvidence | None:
    """Read an audit folder and check its outputs against the hashes in audit_manifest.json. Never raises on bad input."""
    if not audit_dir:
        return None
    d = Path(audit_dir)
    ev = AuditEvidence(dir=str(d), ok=False, problems=[])
    if not d.is_dir():
        ev.problems.append("감사 폴더가 없음")
        return ev
    try:
        ev.manifest = json.loads((d / "audit_manifest.json").read_text(encoding="utf-8"))
        ev.summary = json.loads((d / "audit_summary.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        ev.problems.append(f"감사 파일 누락: {Path(exc.filename).name}")
        return ev
    except ValueError:
        ev.problems.append("감사 JSON을 읽을 수 없음(손상)")
        return ev
    for o in ev.manifest.get("outputs") or []:
        p = d / o.get("path", "")
        if not p.is_file():
            ev.problems.append(f"감사 산출물 누락: {o.get('path')}")
        elif o.get("sha256") and file_sha256(p) != o["sha256"]:
            ev.problems.append(f"감사 산출물이 감사 후 바뀜: {o.get('path')}")
    if not ev.manifest.get("outputs"):
        ev.problems.append("감사 manifest에 산출물 해시가 없음")
    try:
        sym = pd.read_csv(d / "symbol_summary.csv", dtype={"symbol": str})
        ev.statuses = dict(zip(sym["symbol"], sym["audit_status"]))
        ld = pd.to_datetime(sym["last_date"], errors="coerce")        # symbols without usable bars have no last date
        ev.last_dates = {k: v.strftime("%Y-%m-%d") for k, v in zip(sym["symbol"], ld) if pd.notna(v)}
        ev.error_symbols = set(sym.loc[sym.get("exclude_until_verified", pd.Series(0, index=sym.index)).fillna(0).astype(float) > 0, "symbol"])
    except (FileNotFoundError, KeyError, ValueError, pd.errors.ParserError) as exc:
        ev.problems.append(f"symbol_summary.csv를 읽을 수 없음: {type(exc).__name__}")
    ev.ok = not ev.problems
    return ev


def _c(name: str, status: str, detail: str, **values) -> dict:
    return {"check": name, "status": status, "detail": detail, "values": values}


def data_scope(sources: list[str]) -> str:
    if not sources:
        return "UNKNOWN"
    demo = [s for s in sources if is_demo_source(s)]
    if demo and len(demo) == len(sources):
        return "SYNTHETIC_DEMO"
    return "MIXED" if demo else "REAL"


def assess(facts: RunDataFacts, audit: AuditEvidence | None, policy: EligibilityPolicy | None = None,
           now: pd.Timestamp | None = None) -> dict:
    policy = policy or EligibilityPolicy()
    now = now or pd.Timestamp.now(tz="UTC")
    checks: list[dict] = []
    scope = data_scope(facts.sources)
    # 1. scope
    checks.append(_c("data_scope", {"REAL": PASS, "UNKNOWN": INSUFFICIENT}.get(scope, FAIL),
                     {"REAL": "출처 표시가 실제 데이터(출처 표시만으로는 검증되지 않음)", "UNKNOWN": "데이터 출처가 기록되지 않음",
                      "SYNTHETIC_DEMO": "합성(DEMO) 데이터: 실제 데이터 연구에 쓸 수 없음",
                      "MIXED": "DEMO와 실제 데이터가 섞임"}[scope], sources=facts.sources, scope=scope))
    # 2. source record (how the data was obtained)
    rec = facts.source_record
    if not rec:
        checks.append(_c("source_record", INSUFFICIENT, "수집 기록(출처 URL·수집 시각·파일 해시)이 없음"))
    else:
        gaps = {k: rec.get(k) for k in ("files_missing", "fetch_failures") if rec.get(k)}
        checks.append(_c("source_record", WATCH if gaps else PASS,
                         "수집 기록 있음, 누락·실패 항목 있음" if gaps else "수집 기록 있음(출처·수집 시각·파일 해시)",
                         price_source=rec.get("price_source"), fetched_at_range=rec.get("fetched_at_range"),
                         files_recorded=len(rec.get("manifest_sha256") or {}), gaps={k: len(v) for k, v in gaps.items()}))
    # 3. independent source verification
    ver = {k: bool(facts.verifications.get(k)) for k in INDEPENDENT_CHECKS}
    pe = facts.price_evidence
    if all(ver.values()) and not (pe and (not pe.get("ok") or pe.get("unresolved_price_in_run") or pe.get("unverified_adjustments_in_run"))):
        checks.append(_c("source_verification", PASS, "독립 출처 가격 대조가 실행되어 통과", verified=ver))
    elif pe is not None:
        vals = dict(verified=ver, evidence=pe.get("path"), evidence_rules=pe.get("rules_version"), dates_in_run=pe.get("dates_in_run"),
                    dates_outside_run=pe.get("dates_outside_run"), unresolved_price_in_run=pe.get("unresolved_price_in_run"),
                    comparable_rows_in_run=pe.get("comparable_rows_in_run"), scope=(pe.get("scope") or {}).get("kind"),
                    evidence_sha256=pe.get("evidence_sha256"), evidence_range=pe.get("evidence_range"), run_range=pe.get("run_range"),
                    dates_compared_in_run=pe.get("dates_compared_in_run"), dates_passed_in_run=pe.get("dates_passed_in_run"),
                    dates_with_unverified_adjustments_in_run=pe.get("dates_with_unverified_adjustments_in_run"),
                    unverified_adjustments_in_run=pe.get("unverified_adjustments_in_run"),
                    near_one_unverified_adjustments_in_run=pe.get("near_one_unverified_adjustments_in_run"),
                    not_comparable_in_run=pe.get("not_comparable_in_run"))
        uv, n1 = pe.get("unverified_adjustments_in_run") or 0, pe.get("near_one_unverified_adjustments_in_run")
        uv_txt = f"확인되지 않은 조정 가능 {uv}행" + (f"(근접 1배율 {n1}행)" if n1 is not None else "(근접 1배율 집계 없음: v1 증거)")
        if not pe.get("ok"):
            checks.append(_c("source_verification", FAIL, "가격 대조 증거가 손상됐거나 만든 뒤 원본이 바뀜: " + "; ".join((pe.get("problems") or [])[:3]), **vals))
        elif pe.get("unresolved_price_in_run"):
            checks.append(_c("source_verification", FAIL,
                             f"실행 기간 안의 대조 날짜 {pe.get('dates_failed_in_run')}에서 독립 출처와 설명되지 않은 가격 불일치 "
                             f"{pe.get('unresolved_price_in_run')}행", **vals))
        elif not pe.get("dates_in_run"):
            checks.append(_c("source_verification", WATCH, "가격 대조 증거의 날짜가 모두 실행 기간 밖 — 이 실행의 데이터는 대조되지 않음", **vals))
        else:
            checks.append(_c("source_verification", WATCH,
                             f"{pe.get('dates_compared_in_run')}개 거래일 대조: 통과 {pe.get('dates_passed_in_run')}일, 미검증 조정 포함 "
                             f"{pe.get('dates_with_unverified_adjustments_in_run')}일"
                             + (f", 표본 부족 {len(pe.get('dates_failed_in_run'))}일" if pe.get('dates_failed_in_run') else "")
                             + f" · 미해결 가격 불일치 0행 · {uv_txt} · 비교 불가 {pe.get('not_comparable_in_run')}행 "
                             "— 조정은 기업행동 자료로 확인되지 않았고, 대조한 날짜 외 기간은 검증되지 않음", **vals))
    else:
        checks.append(_c("source_verification", INSUFFICIENT if not rec else WATCH,
                         "독립 출처 가격 대조(cross_source_price_check) 결과 없음: 출처의 정확성은 확인되지 않음", verified=ver))
    # 4. audit present and intact
    a = audit
    if a is None:
        checks.append(_c("audit_present", INSUFFICIENT, "이 실행에 연결된 데이터 감사 결과가 없음"))
    else:
        checks.append(_c("audit_present", PASS if a.ok else FAIL,
                         "감사 결과 있음, 산출물 해시 일치" if a.ok else "감사 결과가 없거나 손상됨: " + "; ".join(a.problems[:5]),
                         dir=a.dir, problems=a.problems))
    usable = a is not None and a.ok
    m, s = (a.manifest or {}) if a else {}, (a.summary or {}) if a else {}
    # 5. same window and same rows
    if usable:
        same_window = (str(m.get("start") or "") == str(facts.requested_start or "")) and (str(m.get("end") or "") == str(facts.requested_end or ""))
        afp, rfp = s.get("usable_fingerprint"), facts.audit_compatible_fingerprint
        if not afp or not rfp:
            checks.append(_c("audit_fingerprint", INSUFFICIENT, "감사 또는 실행의 지문이 없음", audit=afp, run=rfp))
        elif afp == rfp:
            checks.append(_c("audit_fingerprint", PASS, "실행 데이터가 감사한 사용 가능 행과 같음(같은 지문)", audit=afp, run=rfp))
        elif not same_window:
            checks.append(_c("audit_fingerprint", INSUFFICIENT, "지문이 다르고 실행 기간도 감사 기간과 달라 같은 데이터인지 확인할 수 없음",
                             audit=afp, run=rfp, audit_window=[m.get("start"), m.get("end")],
                             run_window=[facts.requested_start, facts.requested_end]))
        else:
            checks.append(_c("audit_fingerprint", FAIL, "같은 기간인데 실행 데이터가 감사한 데이터와 다름(데이터 변경 또는 다른 정리 규칙)",
                             audit=afp, run=rfp))
        # 6. coverage
        unknown = [x for x in facts.symbols if x not in a.statuses]
        a_last = max(a.last_dates.values()) if a.last_dates else None
        late = bool(facts.last_date and a_last and facts.last_date > a_last)
        early = bool(m.get("start") and facts.first_date and facts.first_date < str(m.get("start")))
        checks.append(_c("audit_coverage", FAIL if unknown or late or early else PASS,
                         (f"감사에 없는 종목 {len(unknown)}개" if unknown else "") + (" · 감사 이후 날짜 포함" if late else "")
                         + (" · 감사 시작일 이전 날짜 포함" if early else "") if unknown or late or early else "실행 종목·기간이 감사 범위 안에 있음",
                         unaudited_symbols=unknown[:20], run_last=facts.last_date, audit_last=a_last))
        # 7. freshness and reproducibility of the audit itself
        started = pd.Timestamp(m["started_utc"]) if m.get("started_utc") else None
        age = (now - started).days if started is not None else None
        git = m.get("git") or {}
        stale = age is None or age > policy.max_audit_age_days
        checks.append(_c("audit_freshness", WATCH if stale or not git.get("reproducible_from_commit") else PASS,
                         ("감사 시각 미기록" if age is None else f"감사 후 {age}일 경과") + (" (기준 초과)" if stale and age is not None else "")
                         + ("" if git.get("reproducible_from_commit") else " · 감사 코드가 커밋 상태가 아님"),
                         audit_started=m.get("started_utc"), age_days=age, max_age_days=policy.max_audit_age_days, audit_commit=git.get("commit")))
        # 8. exclude / exclude_until_verified
        excl = [x for x in facts.symbols if a.statuses.get(x) == "exclude"]
        errs = [x for x in facts.symbols if x in a.error_symbols]
        bad = bool(excl) or (bool(errs) and not facts.audit_applied)
        checks.append(_c("excluded_data", FAIL if bad else PASS,
                         (f"exclude 종목 {len(excl)}개 포함" if excl else "") + (" · 검증 전 제외 대상 봉이 있는 종목을 감사 적용 없이 사용" if errs and not facts.audit_applied else "")
                         if bad else ("감사 제외 적용됨" if facts.audit_applied else "제외 대상 없음"),
                         exclude_symbols=excl[:20], symbols_with_unverified_bars=len(errs), audit_applied=facts.audit_applied))
        # 9. review
        rev = [x for x in facts.symbols if a.statuses.get(x) == "review"]
        share = len(rev) / len(facts.symbols) if facts.symbols else None
        checks.append(_c("review_symbols", WATCH if rev else PASS,
                         f"검토 대상(review) 종목 {len(rev)}개({share:.0%}) 포함 — 검토 전에는 사용 가능으로 보지 않음" if rev else "review 종목 없음",
                         review_symbols=len(rev), share=share))
        # 10. known limitations recorded by the audit
        cands = sum((s.get("corporate_action_candidates") or {}).values())
        checks.append(_c("known_limitations", WATCH if cands else PASS,
                         f"공시로 확인되지 않은 기업행동 후보 {cands}건, 배당 미반영, 거래량 미수정(출처 특성)" if cands else "감사가 기록한 미해결 기업행동 후보 없음",
                         corporate_action_candidates=s.get("corporate_action_candidates"), rows_removed=s.get("rows_removed"),
                         rows_raw=s.get("rows_raw")))
    overall = max((c["status"] for c in checks), key=SEVERITY.get)
    return {"rules_version": RULES_VERSION, "assessed_utc": now.isoformat(), "status": overall, "label": LABELS[overall],
            "data_scope": scope, "real_data_verified": bool(overall == PASS and scope == "REAL"),
            "provenance": {"sources": facts.sources, "data_fingerprint": facts.data_fingerprint,
                           "audit_compatible_fingerprint": facts.audit_compatible_fingerprint,
                           "requested_window": [facts.requested_start, facts.requested_end], "data_range": [facts.first_date, facts.last_date],
                           "symbols": len(facts.symbols), "settings_hash": facts.settings_hash,
                           "audit": None if a is None else {"dir": a.dir, "intact": a.ok, "started_utc": m.get("started_utc"),
                                                            "window": [m.get("start"), m.get("end")],
                                                            "usable_fingerprint": s.get("usable_fingerprint"),
                                                            "input_fingerprint": s.get("input_fingerprint"),
                                                            "audit_status": s.get("audit_status"), "by_severity": s.get("by_severity")}},
            "checks": checks, "policy": asdict(policy)}


def gate_check(record: dict | None, run_fingerprint: str | None = None) -> dict:
    """The same Robustness Gate check for Factor Lab and Strategy Library runs: the eligibility recorded at run time.

    `run_fingerprint`: the data fingerprint the run recorded elsewhere; a record made for other data is a FAIL.
    """
    if not record or record.get("rules_version") is None:
        return _c("data_eligibility", INSUFFICIENT, "실행 시점의 데이터 적격성 기록이 없음(이전 버전 실행) — 소급해서 검증 완료로 보지 않음")
    st = record.get("status")
    if st not in SEVERITY:
        return _c("data_eligibility", INSUFFICIENT, f"알 수 없는 적격성 상태 {st!r}")
    rec_fp = (record.get("provenance") or {}).get("data_fingerprint")
    if run_fingerprint is not None and rec_fp != run_fingerprint:
        return _c("data_eligibility", FAIL, "적격성 기록의 데이터 지문이 실행 지문과 다름(다른 데이터에 대한 기록)",
                  record_fingerprint=rec_fp, run_fingerprint=run_fingerprint)
    bad = [c["check"] for c in record.get("checks") or [] if c.get("status") != PASS]
    return _c("data_eligibility", st, f"{LABELS[st]} ({record.get('rules_version')})" + (f": {', '.join(bad)}" if bad else ""),
              rules_version=record.get("rules_version"), assessed_utc=record.get("assessed_utc"), data_scope=record.get("data_scope"),
              real_data_verified=record.get("real_data_verified"), not_passed=bad)
