"""Research data eligibility (eligibility-v1): audit provenance, fingerprint identity, ok/review/exclude handling, and
the record each Factor Lab / Strategy Library run keeps. Data here are TEST fixtures (tests/audit_fixtures.py) and the
DEMO generator — never market data."""
import json
import shutil

import pandas as pd
import pytest

from app.research.eligibility import (FAIL, INSUFFICIENT, PASS, RULES_VERSION, WATCH, AuditEvidence, EligibilityPolicy,
                                      RunDataFacts, assess, data_scope, gate_check, load_audit_evidence)
from tests.audit_fixtures import write_naver_raw

NOW = pd.Timestamp("2026-10-26T00:00:00+00:00")


def by(res):
    return {c["check"]: c for c in res["checks"]}


@pytest.fixture(scope="module")
def audited(tmp_path_factory):
    """A naver_raw TEST folder, its audit (python -m app.audit) and a Factor Lab run that uses the audit."""
    from app.audit.__main__ import main as audit_main
    from app.factorlab.__main__ import main as fl_main
    root = tmp_path_factory.mktemp("elig")
    data = write_naver_raw(root / "data")
    before = {p.name: p.read_bytes() for p in sorted((data / "raw").glob("*.csv"))}
    assert audit_main(["--data", str(data), "--out", str(root / "audit")]) == 0
    args = ["run", "--data", str(data), "--out", str(root / "fl"), "--factors", "mom_20", "--horizon", "5", "--quantiles", "2",
            "--min-names", "3", "--min-turnover", "0", "--audit", str(root / "audit")]
    assert fl_main(args) == 0
    return {"root": root, "data": data, "audit": root / "audit", "fl": root / "fl", "raw_before": before}


def good_audit(**over):
    ev = AuditEvidence(dir="a", ok=True, problems=[],
                       summary={"usable_fingerprint": "u" * 16, "corporate_action_candidates": {}, "audit_status": {"ok": 2}},
                       manifest={"start": "2016-01-01", "end": None, "started_utc": "2026-10-20T00:00:00+00:00",
                                 "git": {"commit": "c", "reproducible_from_commit": True}},
                       statuses={"A": "ok", "B": "ok"}, last_dates={"A": "2026-10-23", "B": "2026-10-23"})
    for k, v in over.items():
        setattr(ev, k, v)
    return ev


def good_facts(**over):
    f = RunDataFacts(sources=["naver_fchart"], data_fingerprint="d" * 16, audit_compatible_fingerprint="u" * 16,
                     requested_start="2016-01-01", first_date="2016-01-04", last_date="2026-10-23", symbols=["A", "B"],
                     source_record={"price_source": "x", "manifest_sha256": {"A": "h", "B": "h"}},
                     verifications={"cross_source_price_check": True}, audit_applied=True)
    for k, v in over.items():
        setattr(f, k, v)
    return f


# ------------------------------------------------------------------ rules

def test_pass_requires_every_piece_of_evidence():
    r = assess(good_facts(), good_audit(), now=NOW)
    assert r["status"] == PASS and r["label"] == "USABLE" and r["real_data_verified"] is True and r["rules_version"] == RULES_VERSION
    assert r["provenance"]["audit"]["usable_fingerprint"] == "u" * 16 and r["provenance"]["data_range"] == ["2016-01-04", "2026-10-23"]


def test_fingerprint_mismatch_and_window():
    c = by(assess(good_facts(audit_compatible_fingerprint="x" * 16), good_audit(), now=NOW))["audit_fingerprint"]
    assert c["status"] == FAIL
    c = by(assess(good_facts(audit_compatible_fingerprint="x" * 16, requested_start="2023-01-01"), good_audit(), now=NOW))["audit_fingerprint"]
    assert c["status"] == INSUFFICIENT                                  # different window: identity cannot be shown
    assert by(assess(good_facts(audit_compatible_fingerprint=None), good_audit(), now=NOW))["audit_fingerprint"]["status"] == INSUFFICIENT


def test_missing_or_damaged_audit(audited, tmp_path):
    r = assess(good_facts(), None, now=NOW)
    assert by(r)["audit_present"]["status"] == INSUFFICIENT and r["status"] == INSUFFICIENT and r["real_data_verified"] is False
    assert load_audit_evidence(tmp_path / "nope").ok is False
    bad = tmp_path / "audit_changed"
    shutil.copytree(audited["audit"], bad)
    with (bad / "findings.csv").open("a") as fh:
        fh.write("tampered\n")
    ev = load_audit_evidence(bad)
    assert not ev.ok and any("findings.csv" in p for p in ev.problems)
    assert by(assess(good_facts(), ev, now=NOW))["audit_present"]["status"] == FAIL
    broken = tmp_path / "audit_broken"
    shutil.copytree(audited["audit"], broken)
    (broken / "audit_summary.json").write_text("{not json")
    assert by(assess(good_facts(), load_audit_evidence(broken), now=NOW))["audit_present"]["status"] == FAIL
    missing = tmp_path / "audit_missing"
    shutil.copytree(audited["audit"], missing)
    (missing / "audit_manifest.json").rename(missing / "moved.json")
    ev = load_audit_evidence(missing)
    assert not ev.ok and any("누락" in p for p in ev.problems)


def test_source_label_alone_is_not_verification():
    r = assess(good_facts(source_record=None, verifications={}), good_audit(), now=NOW)
    c = by(r)
    assert c["data_scope"]["status"] == PASS                           # the label says real data ...
    assert c["source_record"]["status"] == INSUFFICIENT and c["source_verification"]["status"] == INSUFFICIENT
    assert r["status"] == INSUFFICIENT and r["real_data_verified"] is False
    r = assess(good_facts(verifications={}), good_audit(), now=NOW)
    assert by(r)["source_verification"]["status"] == WATCH and r["status"] == WATCH
    gap = good_facts(source_record={"price_source": "x", "files_missing": ["A"], "manifest_sha256": {}})
    assert by(assess(gap, good_audit(), now=NOW))["source_record"]["status"] == WATCH


def test_demo_and_mixed_data_never_verified():
    for sources, scope in ((["synthetic_demo"], "SYNTHETIC_DEMO"), (["synthetic_demo", "csv_import"], "MIXED")):
        r = assess(good_facts(sources=sources), good_audit(), now=NOW)
        assert r["data_scope"] == scope and by(r)["data_scope"]["status"] == FAIL and r["status"] == FAIL
        assert r["real_data_verified"] is False
    assert data_scope([]) == "UNKNOWN"
    assert assess(good_facts(sources=[]), good_audit(), now=NOW)["status"] == INSUFFICIENT


def test_ok_review_exclude_handling():
    ev = good_audit(statuses={"A": "ok", "B": "review", "C": "exclude"}, last_dates={"A": "2026-10-23", "B": "2026-10-23", "C": "2026-10-23"})
    c = by(assess(good_facts(symbols=["A", "B", "C"]), ev, now=NOW))
    assert c["excluded_data"]["status"] == FAIL                        # an exclude symbol is in the run
    assert c["review_symbols"]["status"] == WATCH and c["review_symbols"]["values"]["review_symbols"] == 1
    c = by(assess(good_facts(symbols=["A", "B"]), ev, now=NOW))
    assert c["excluded_data"]["status"] == PASS and c["review_symbols"]["status"] == WATCH
    assert by(assess(good_facts(symbols=["A"]), ev, now=NOW))["review_symbols"]["status"] == PASS
    ev2 = good_audit(error_symbols={"A"})
    assert by(assess(good_facts(audit_applied=False), ev2, now=NOW))["excluded_data"]["status"] == FAIL
    assert by(assess(good_facts(audit_applied=True), ev2, now=NOW))["excluded_data"]["status"] == PASS


def test_run_outside_the_audited_range():
    assert by(assess(good_facts(last_date="2026-10-24"), good_audit(), now=NOW))["audit_coverage"]["status"] == FAIL
    assert by(assess(good_facts(first_date="2015-12-30"), good_audit(), now=NOW))["audit_coverage"]["status"] == FAIL
    c = by(assess(good_facts(symbols=["A", "B", "Z"]), good_audit(), now=NOW))["audit_coverage"]
    assert c["status"] == FAIL and c["values"]["unaudited_symbols"] == ["Z"]


def test_symbols_without_dates_do_not_hide_late_data(audited, tmp_path):
    """A symbol with no usable bars has an empty last_date; it must not become the audit's 'latest date' ('nan' > '2026')."""
    d = tmp_path / "audit_nan"
    shutil.copytree(audited["audit"], d)
    sym = pd.read_csv(d / "symbol_summary.csv", dtype={"symbol": str})
    sym = pd.concat([sym, pd.DataFrame([{"symbol": "ZZZ", "audit_status": "exclude", "last_date": None}])], ignore_index=True)
    sym.to_csv(d / "symbol_summary.csv", index=False)
    ev = load_audit_evidence(d)
    assert "ZZZ" not in ev.last_dates and all(v[:2] in ("19", "20") for v in ev.last_dates.values())
    ev.ok, ev.problems = True, []                          # isolate the coverage rule from the (expected) hash mismatch
    f = good_facts(symbols=[k for k, v in ev.statuses.items() if v == "ok"][:1], last_date="2099-01-01", first_date=None)
    assert by(assess(f, ev, now=NOW))["audit_coverage"]["status"] == FAIL


def test_stale_or_unreproducible_audit():
    old = NOW + pd.Timedelta(31, unit="D")
    c = by(assess(good_facts(), good_audit(), now=old))["audit_freshness"]
    assert c["status"] == WATCH and c["values"]["age_days"] > EligibilityPolicy().max_audit_age_days
    ev = good_audit(manifest={"start": "2016-01-01", "end": None, "started_utc": "2026-10-20T00:00:00+00:00", "git": {"reproducible_from_commit": False}})
    assert by(assess(good_facts(), ev, now=NOW))["audit_freshness"]["status"] == WATCH
    ev = good_audit(manifest={"start": "2016-01-01", "end": None, "git": {"reproducible_from_commit": True}})
    assert by(assess(good_facts(), ev, now=NOW))["audit_freshness"]["status"] == WATCH          # audit time unknown


# ------------------------------------------------------------------ runs

def test_factorlab_run_records_eligibility(audited):
    m = json.loads((audited["fl"] / "run_manifest.json").read_text())
    e = m["data_eligibility"]
    assert e["rules_version"] == RULES_VERSION and e["provenance"]["data_fingerprint"] == m["data_fingerprint"]
    c = by(e)
    assert c["audit_present"]["status"] == PASS and c["audit_fingerprint"]["status"] == PASS   # same rows as audited
    assert c["excluded_data"]["status"] == PASS and c["review_symbols"]["status"] == WATCH
    assert c["source_verification"]["status"] == WATCH and e["real_data_verified"] is False
    assert e["status"] in (WATCH, FAIL, INSUFFICIENT) and "데이터 적격성" in (audited["fl"] / "REPORT.md").read_text()
    assert {p.name: p.read_bytes() for p in sorted((audited["data"] / "raw").glob("*.csv"))} == audited["raw_before"]


def test_factorlab_without_audit_is_insufficient(audited, tmp_path):
    from app.factorlab.__main__ import main as fl_main
    out = tmp_path / "fl_noaudit"
    assert fl_main(["run", "--data", str(audited["data"]), "--out", str(out), "--factors", "mom_20", "--horizon", "5",
                    "--quantiles", "2", "--min-names", "3", "--min-turnover", "0"]) == 0
    e = json.loads((out / "run_manifest.json").read_text())["data_eligibility"]
    assert by(e)["audit_present"]["status"] == INSUFFICIENT and e["status"] == INSUFFICIENT


def test_strategy_library_run_records_eligibility():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/demo/load").status_code == 200
        r = c.post("/api/library/strategies/trend_ma_crossover/backtest", json={"market": "DEMO", "random_runs": 0}).json()
        e = r["library"]["data_eligibility"]
        assert e["data_scope"] == "SYNTHETIC_DEMO" and e["status"] == FAIL and e["real_data_verified"] is False
        assert by(e)["audit_present"]["status"] == INSUFFICIENT
        assert e["provenance"]["data_fingerprint"] == r["library"]["data_fingerprint"]
        g = c.post(f"/api/library/runs/{r['run_id']}/robustness", json={}).json()
        assert by(g)["data_eligibility"]["status"] == FAIL and g["status"] == FAIL


def test_both_gates_read_the_record_the_same_way(audited, tmp_path):
    from app.research.robustness import evaluate
    from app.research.robustness_strategy import evaluate_strategy
    from tests.test_robustness import make_run
    from tests.test_strategy_gate import good_evidence, good_library
    for st in (PASS, WATCH, FAIL, INSUFFICIENT):
        d = make_run(tmp_path, name=f"fl_{st}", eligibility=st)
        f = by(evaluate(d, "mom_60"))["data_eligibility"]
        rec = {"rules_version": RULES_VERSION, "status": st, "checks": [], "provenance": {"data_fingerprint": "f" * 16}}
        s = by(evaluate_strategy(good_evidence(library=good_library(data_eligibility=rec))))["data_eligibility"]
        assert f["status"] == s["status"] == st == gate_check(rec)["status"]
    wrong = {"rules_version": RULES_VERSION, "status": PASS, "checks": [], "provenance": {"data_fingerprint": "0" * 16}}
    assert by(evaluate_strategy(good_evidence(library=good_library(data_eligibility=wrong))))["data_eligibility"]["status"] == FAIL
    assert gate_check(wrong, "f" * 16)["status"] == FAIL


def test_runs_without_a_record_are_not_verified_retroactively(tmp_path):
    from app.research.robustness import evaluate
    from app.research.robustness_strategy import evaluate_strategy
    from tests.test_robustness import make_run
    from tests.test_strategy_gate import good_evidence, good_library
    res = evaluate(make_run(tmp_path, eligibility=None), "mom_60")
    assert by(res)["data_eligibility"]["status"] == INSUFFICIENT and res["status"] != PASS
    lib = good_library()
    del lib["data_eligibility"]
    res = evaluate_strategy(good_evidence(library=lib))
    assert by(res)["data_eligibility"]["status"] == INSUFFICIENT and res["status"] != PASS
    assert gate_check({"status": PASS})["status"] == INSUFFICIENT       # no rules version: not a record


def test_ledger_and_manifest_preserved(audited, tmp_path):
    """The record is fixed at run time: later assessments or audit changes do not touch the run's manifest or ledger."""
    from app.factorlab.__main__ import main as fl_main
    from app.research.ledger import Ledger, verify
    from tests.test_ledger import spec
    L = tmp_path / "l.jsonl"
    Ledger(L).register(spec(eid="EXP-elig"))
    out = tmp_path / "fl_led"
    assert fl_main(["run", "--data", str(audited["data"]), "--out", str(out), "--factors", "mom_20", "--horizon", "5", "--quantiles", "2",
                    "--min-names", "3", "--min-turnover", "0", "--audit", str(audited["audit"]), "--ledger", str(L), "--ledger-id", "EXP-elig"]) == 0
    st = Ledger(L).experiment("EXP-elig")
    facts = st["manifests"][0]["facts"]["data_eligibility"]
    m_bytes = (out / "run_manifest.json").read_bytes()
    assert facts["rules_version"] == RULES_VERSION and facts["status"] == json.loads(m_bytes)["data_eligibility"]["status"]
    assess(good_facts(), load_audit_evidence(audited["audit"]), now=NOW + pd.Timedelta(365, unit="D"))   # a later assessment
    assert (out / "run_manifest.json").read_bytes() == m_bytes and verify(L).ok


def test_git_rules_still_protect_env_and_data():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        pytest.skip("not a git checkout")
    for p in (".env", "data/real/raw/005930.csv", "data/audit/x/findings.csv", "data/factorlab/x/run_manifest.json", "data/library/x/report.json"):
        assert subprocess.run(["git", "check-ignore", "--no-index", p], cwd=root, capture_output=True, text=True).stdout.strip() == p
