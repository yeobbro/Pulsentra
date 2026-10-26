"""Cross-source price evidence (price-evidence-v2) and its use by eligibility-v3. Offline: mock KRX answers written to
disk and TEST bars — never market data."""
import json

import pandas as pd
import pytest

from app.krxcheck.evidence import main as evidence_main
from app.research.eligibility import FAIL, INSUFFICIENT, PASS, WATCH, RunDataFacts, assess
from app.research.price_evidence import (FAILED, MIN_COMPARABLE, PASSED, PASSED_UNVERIFIED, RULES_VERSION, classify_disagreement,
                                         load_evidence, summary_for_eligibility)
from tests.test_krx import stock_row

DAYS = ("2026-01-05", "2026-01-06")


def build(tmp_path, n=MIN_COMPARABLE + 10, local=None, krx_extra=(), local_only=(), days=DAYS, skip_krx=()):
    """n symbols with identical bars on every day; `local[(code, day)] = dict(...)` overrides the local bar."""
    data, krx_root = tmp_path / "data", tmp_path / "krx"
    (data / "raw").mkdir(parents=True)
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    uni = []
    for c in [*codes, *local_only]:
        rows = []
        for d in days:
            b = {"date": d, "open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5000}
            b.update((local or {}).get((c, d), {}))
            rows.append(b)
        pd.DataFrame(rows).to_csv(data / "raw" / f"{c}.csv", index=False)
        uni.append({"symbol": c, "name": c, "market": "KOSPI", "status": "listed"})
    pd.DataFrame(uni).to_csv(data / "universe.csv", index=False)
    (data / "manifest.jsonl").write_text("\n".join(json.dumps({"symbol": c, "first": days[0], "last": days[-1]}) for c in codes) + "\n")
    for d in days:
        if d in skip_krx:
            continue
        bas = pd.Timestamp(d).strftime("%Y%m%d")
        k = krx_root / f"full_market_{bas}"
        k.mkdir(parents=True)
        rows = [stock_row(c, bas, o="1,000", h="1,100", l="990", c="1,050", v="5,000") for c in codes]
        rows += [stock_row(c, bas) for c in krx_extra]
        pd.DataFrame(rows).to_csv(k / f"krx_raw_kospi_stock_{bas}.csv", index=False)
        (k / "fetch_meta.json").write_text(json.dumps({"status": "ok", "received_at_utc": "2026-01-07T00:00:00+00:00"}))
    return data, krx_root


def run(tmp_path, data, krx_root, *extra):
    out = tmp_path / "evidence"
    assert evidence_main(["--krx-root", str(krx_root), "--data", str(data), "--out", str(out), *extra]) == 0
    return out, json.loads((out / "crosscheck_status.json").read_text())


def day(st, d):
    return next(x for x in st["price_check"]["by_date"] if x["date"] == d)


def test_exact_match(tmp_path):
    data, krx = build(tmp_path)
    out, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["comparable"] == MIN_COMPARABLE + 10 and r["exact_equal_rate"]["close"] == 1.0 and r["passed"]
    assert r["status"] == PASSED and r["unverified_adjustments"] == 0
    assert st["price_check"]["passed"] is True and st["price_check"]["dates_passed"] == 2
    assert st["rules_version"] == RULES_VERSION == "price-evidence-v2" and r["rules_version"] == RULES_VERSION
    assert st["verified"]["cross_source_price_check"] is False          # two dates are not the whole period
    assert st["price_check"]["scope"]["kind"] == "sample_dates"


def test_within_tolerance_and_beyond(tmp_path):
    local = {("000001", DAYS[0]): {"close": 1051}, ("000002", DAYS[0]): {"close": 1080}}     # 0.095 % and 2.9 %
    data, krx = build(tmp_path, local=local)
    _, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["exact_equal"]["close"] == r["comparable"] - 2 and r["within_tolerance"]["close"] == r["comparable"] - 1
    assert r["classes"]["price_within_tolerance"] == 1 and r["unresolved_price"] == 1 and r["passed"] is False
    assert day(st, DAYS[1])["passed"] is True and st["price_check"]["passed"] is False


def test_unverified_adjustment_is_counted_and_not_passed(tmp_path):
    """v1 counted this row as passing (`passed=True`); v2 keeps it visible: not unresolved, not confirmed, not PASSED."""
    half = {"open": 500, "high": 550, "low": 495, "close": 525}                     # every price x 0.5: split-like
    data, krx = build(tmp_path, local={("000003", DAYS[0]): half})
    out, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["classes"]["unverified_adjustment_possible"] == 1 and r["unresolved_price"] == 0
    assert r["unverified_adjustments"] == 1 and r["split_like_unverified_adjustments"] == 1 and r["near_one_unverified_adjustments"] == 0
    assert r["status"] == PASSED_UNVERIFIED and r["passed"] is False and "확인되지 않음" in r["verdict_reason"]
    pc = st["price_check"]
    assert pc["passed"] is False and pc["unverified_adjustment_rows"] == 1 and pc["dates_passed_with_unverified_adjustments"] == 1
    assert pc["dates_passed"] == 1 and pc["dates_failed"] == 0
    assert "confirmed" not in json.dumps(st) and "기업행동 자료로 확인되지 않았" in (out / "price_evidence.md").read_text()


def test_near_one_factor_is_its_own_subcount(tmp_path):
    near = {"open": 980, "high": 1078, "low": 970, "close": 1029}                  # every price x 0.98: no split ratio
    data, krx = build(tmp_path, local={("000006", DAYS[0]): near})
    _, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["unverified_adjustments"] == 1 and r["near_one_unverified_adjustments"] == 1 and r["split_like_unverified_adjustments"] == 0
    assert r["status"] == PASSED_UNVERIFIED and st["price_check"]["near_one_unverified_adjustment_rows"] == 1


def test_unresolved_still_fails_with_adjustments_present(tmp_path):
    local = {("000003", DAYS[1]): {"open": 500, "high": 550, "low": 495, "close": 525}, ("000002", DAYS[1]): {"close": 1080}}
    data, krx = build(tmp_path, local=local)
    _, st = run(tmp_path, data, krx)
    r = day(st, DAYS[1])
    assert r["status"] == FAILED and r["unresolved_price"] == 1 and r["unverified_adjustments"] == 1


def test_volume_difference_reported_but_not_price_verdict(tmp_path):
    data, krx = build(tmp_path, local={("000004", DAYS[0]): {"volume": 4000}, ("000005", DAYS[0]): {"volume": 4990}})
    _, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["exact_equal"]["volume"] == r["comparable"] - 2 and r["within_tolerance"]["volume"] == r["comparable"] - 1
    assert r["classes"]["volume_difference_unresolved"] == 1 and r["classes"]["volume_within_tolerance"] == 1
    assert r["unresolved_volume"] == 1 and r["unresolved_price"] == 0 and r["passed"]


def test_mapping_failures_and_one_sided_symbols(tmp_path):
    data, krx = build(tmp_path, krx_extra=("900001",), local_only=("800001",))
    # 800001 has local bars and is in the universe but not in the KRX answer; 900001 is only in KRX; a bad code too
    bas = pd.Timestamp(DAYS[0]).strftime("%Y%m%d")
    p = krx / f"full_market_{bas}" / f"krx_raw_kospi_stock_{bas}.csv"
    df = pd.read_csv(p, dtype=str)
    pd.concat([df, pd.DataFrame([stock_row("12AB", bas)])]).to_csv(p, index=False)
    _, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["mapping"]["krx_codes_invalid"] == 1 and r["mapping"]["local_file_missing_out_of_universe"] == 1
    assert r["only_in_krx"] == 1 and r["only_in_local"] == 1 and r["only_in_local_symbols"] == ["800001"]
    assert r["krx_rows"] == r["comparable"] + r["only_in_krx"] + r["not_comparable"] + r["mapping"]["krx_codes_invalid"]


def test_too_few_comparable_rows(tmp_path):
    data, krx = build(tmp_path, n=MIN_COMPARABLE - 1)
    _, st = run(tmp_path, data, krx)
    r = day(st, DAYS[0])
    assert r["enough_rows"] is False and r["passed"] is False and r["unresolved_price"] == 0
    assert st["price_check"]["passed"] is False


def test_missing_krx_answer_for_a_requested_date(tmp_path):
    data, krx = build(tmp_path, skip_krx=(DAYS[1],))
    _, st = run(tmp_path, data, krx, "--dates", *DAYS)
    assert day(st, DAYS[1])["passed"] is False and day(st, DAYS[1])["verdict_reason"] == "저장된 KRX 응답 없음"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert evidence_main(["--krx-root", str(empty), "--data", str(data), "--out", str(tmp_path / "e2")]) == 3


def test_source_hash_change_invalidates_evidence(tmp_path):
    data, krx = build(tmp_path)
    out, _ = run(tmp_path, data, krx)
    assert load_evidence(out / "crosscheck_status.json")["ok"] is True
    f = data / "raw" / "000001.csv"
    f.write_text(f.read_text().replace("1050", "1049", 1))
    ev = load_evidence(out / "crosscheck_status.json")
    assert ev["ok"] is False and any("바뀌었" in p for p in ev["problems"])


def test_missing_or_damaged_evidence(tmp_path):
    assert load_evidence(None) is None
    assert load_evidence(tmp_path / "nope.json")["problems"] == ["증거 파일 없음"]
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    assert "손상" in load_evidence(bad)["problems"][0]
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"verified": {"cross_source_price_check": True}}))
    assert "형식" in load_evidence(legacy)["problems"][0]
    data, krx = build(tmp_path / "x")
    out, _ = run(tmp_path / "x", data, krx)
    (out / "source_files.json").unlink()
    assert any("source_files" in p for p in load_evidence(out / "crosscheck_status.json")["problems"])


def _facts(pe, verified=None):
    return RunDataFacts(sources=["naver_fchart"], source_record={"price_source": "x", "manifest_sha256": {}},
                        verifications=verified or {}, price_evidence=pe, first_date=DAYS[0], last_date=DAYS[1])


def _sv(res):
    return next(c for c in res["checks"] if c["check"] == "source_verification")


def test_evidence_feeds_eligibility(tmp_path):
    data, krx = build(tmp_path, local={("000002", DAYS[1]): {"close": 1080}})
    out, st = run(tmp_path, data, krx)
    pe = load_evidence(out / "crosscheck_status.json")
    in_run = summary_for_eligibility(pe, DAYS[0], DAYS[1])
    assert _sv(assess(_facts(in_run), None))["status"] == FAIL                     # unresolved disagreement inside the run
    first_only = summary_for_eligibility(pe, DAYS[0], DAYS[0])
    sv = _sv(assess(_facts(first_only), None))
    assert sv["status"] == WATCH and sv["values"]["dates_in_run"] == [DAYS[0]]       # clean date, but only a sample
    outside = summary_for_eligibility(pe, "2020-01-01", "2020-12-31")
    sv = _sv(assess(_facts(outside), None))
    assert sv["status"] == WATCH and sv["values"]["dates_in_run"] == []              # evidence does not cover the run
    damaged = dict(in_run, ok=False, problems=["x"])
    assert _sv(assess(_facts(damaged), None))["status"] == FAIL
    # a full-period flag cannot hide a disagreement the evidence shows
    assert _sv(assess(_facts(in_run, {"cross_source_price_check": True}), None))["status"] == FAIL


def test_sample_dates_never_verify_the_whole_period(tmp_path):
    data, krx = build(tmp_path)
    out, st = run(tmp_path, data, krx)
    pe = load_evidence(out / "crosscheck_status.json")
    assert st["price_check"]["passed"] is True and st["verified"]["cross_source_price_check"] is False
    res = assess(_facts(summary_for_eligibility(pe, DAYS[0], DAYS[1]), st["verified"]), None)
    assert _sv(res)["status"] == WATCH and res["status"] != PASS and res["real_data_verified"] is False
    assert _sv(assess(_facts(None), None))["status"] in (WATCH, INSUFFICIENT)       # v1 behaviour without evidence


def test_factorlab_reads_price_evidence(tmp_path):
    from app.factorlab.__main__ import main as fl_main
    from tests.audit_fixtures import write_naver_raw
    d = write_naver_raw(tmp_path / "nav")
    # evidence made from a different data folder: its dates fall outside this data, so it must not verify anything
    data, krx = build(tmp_path / "ev")
    out, _ = run(tmp_path / "ev", data, krx)
    fl = tmp_path / "fl"
    assert fl_main(["run", "--data", str(d), "--out", str(fl), "--factors", "mom_20", "--horizon", "5", "--quantiles", "2", "--min-names", "3",
                    "--min-turnover", "0", "--crosscheck", str(out / "crosscheck_status.json")]) == 0
    e = json.loads((fl / "run_manifest.json").read_text())["data_eligibility"]
    sv = _sv(e)
    assert e["rules_version"] == "eligibility-v3" and sv["status"] == WATCH and sv["values"]["dates_in_run"] == []


def test_classifier_names_no_cause_without_evidence():
    base = {"local_open": 1000, "krx_open": 1000, "local_high": 1100, "krx_high": 1100, "local_low": 990, "krx_low": 990,
            "local_close": 1050, "krx_close": 1050, "local_volume": 5000, "krx_volume": 5000}
    assert classify_disagreement(pd.Series({**base, "classifier_status": "match"})) == "exact_match"
    assert classify_disagreement(pd.Series({**base, "local_close": 1100, "classifier_status": "price_mismatch"})) == "price_difference_unresolved"


def test_unverified_adjustments_keep_eligibility_at_watch(tmp_path):
    from app.research.eligibility import AuditEvidence
    data, krx = build(tmp_path, local={("000003", DAYS[0]): {"open": 500, "high": 550, "low": 495, "close": 525}})
    out, st = run(tmp_path, data, krx)
    pe = summary_for_eligibility(load_evidence(out / "crosscheck_status.json"), DAYS[0], DAYS[1])
    assert pe["unverified_adjustments_in_run"] == 1 and pe["dates_with_unverified_adjustments_in_run"] == 1 and pe["dates_passed_in_run"] == 1
    assert pe["evidence_sha256"] and pe["evidence_range"] == list(DAYS) and pe["not_comparable_in_run"] == 0
    sv = _sv(assess(_facts(pe), None))
    assert sv["status"] == WATCH and "확인되지 않은 조정 가능 1행" in sv["detail"] and "불일치 없음" not in sv["detail"]
    # a full-period flag cannot turn unverified adjustments into PASS
    assert _sv(assess(_facts(pe, {"cross_source_price_check": True}), None))["status"] == WATCH
    # another FAIL still decides the overall result
    bad_audit = AuditEvidence(dir="x", ok=False, problems=["damaged"])
    res = assess(_facts(pe, {"cross_source_price_check": True}), bad_audit)
    assert _sv(res)["status"] == WATCH and res["status"] == FAIL


def test_v1_evidence_adjustments_are_read_as_unverified(tmp_path):
    """A price-evidence-v1 file (where adjustment rows counted as passing) is not re-written; its rows count as unverified."""
    data, krx = build(tmp_path, local={("000003", DAYS[0]): {"open": 500, "high": 550, "low": 495, "close": 525}})
    out, st = run(tmp_path, data, krx)
    v1 = json.loads((out / "crosscheck_status.json").read_text())
    v1["rules_version"], v1["evidence_version"] = "price-evidence-v1", 1
    for d in v1["price_check"]["by_date"]:
        for k in ("status", "unverified_adjustments", "split_like_unverified_adjustments", "near_one_unverified_adjustments", "other_unverified_adjustments"):
            d.pop(k, None)
        d["classes"] = {("price_adjustment_basis_possible" if k == "unverified_adjustment_possible" else k): v for k, v in d["classes"].items()}
        d["passed"] = d["unresolved_price"] == 0
    p = out / "v1_status.json"
    p.write_text(json.dumps(v1))
    pe = summary_for_eligibility({"ok": True, "problems": [], "status": v1, "path": str(p)}, DAYS[0], DAYS[1])
    assert pe["unverified_adjustments_in_run"] == 1 and pe["near_one_unverified_adjustments_in_run"] is None
    assert _sv(assess(_facts(pe), None))["status"] == WATCH


def test_recorded_recent_dates_stay_failed(tmp_path):
    """The real 2026-10-22/23 rows, as recorded in the v1 evidence (unresolved 711 / 675), remain FAILED under v2 rules."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "data" / "crosscheck"
    files = sorted(root.glob("price_evidence_*/crosscheck_status.json")) if root.exists() else []
    if not files:
        pytest.skip("no local price evidence (data/ is not in Git)")
    st = json.loads(files[0].read_text())
    pe = summary_for_eligibility({"ok": True, "problems": [], "status": st, "path": str(files[0])}, "2016-01-04", "2026-10-23")
    recent = {d["date"]: d for d in st["price_check"]["by_date"] if d["date"] in ("2026-10-22", "2026-10-23")}
    assert len(recent) == 2 and all(d["unresolved_price"] > 0 for d in recent.values())
    assert set(pe["dates_failed_in_run"]) >= {"2026-10-22", "2026-10-23"} and _sv(assess(_facts(pe), None))["status"] == FAIL
