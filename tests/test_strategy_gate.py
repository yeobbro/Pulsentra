"""Robustness Gate for Strategy Library runs (explicit adapter, not a Factor Lab disguise).

Backtests here use the DEMO (synthetic) data set, so every gate result must say so and none may claim real-data
verification. Hand-built evidence objects test the rules themselves.
"""
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.research.robustness import FAIL, INSUFFICIENT, PASS, WATCH
from app.research.robustness_strategy import StrategyGateConfig, StrategyRunEvidence, data_scope, evaluate_strategy

PERIODS = {"dev": ["2016-01-01", "2021-12-31"], "validation": ["2022-01-01", "2023-12-31"], "test": ["2024-01-01", "2099-12-31"]}


def by_name(res):
    return {c["check"]: c for c in res["checks"]}


def _period(name, excess=0.05, trades=60, er=0.01, ci=(0.002, 0.02), crossing=0):
    return {"period": name, "total_return": 0.1, "benchmark_total_return": 0.1 - excess, "excess_net": excess, "trades": trades,
            "trades_crossing_period_end": crossing, "expectancy_return": er, "expectancy_return_ci95": list(ci)}


def good_library(**over):
    lib = {"evidence_version": 1, "periods": PERIODS, "data_sources": ["csv_import"], "synthetic": False, "mixed_demo_and_real": False,
           "by_period": [_period("dev"), _period("validation"), _period("test")],
           "yearly": [{"year": y, "excess_net": 0.02} for y in range(2016, 2024)],
           "cost_stress": {m: {"by_period": [_period("validation", excess=0.01)]} for m in ("2.0", "3.0")},
           "benchmark": {"kind": "index", "symbol": "KOSPI", "label": "시장지수 매수 후 보유"},
           "validity_status": "VERIFIED", "audit": {"applied": True, "review_share": 0.1},
           "data_eligibility": {"rules_version": "eligibility-v1", "status": "PASS", "checks": [], "provenance": {"data_fingerprint": "f" * 16}}}
    lib.update(over)
    return lib


def good_evidence(**over):
    req = {"entry_id": "trend_ma_crossover", "params": {"fast": 20, "slow": 60}, "costs": {"commission_bps": 1.5, "sell_tax_bps": 20.0, "slippage_bps": 10.0},
           "settings_hash": "h" * 64, "ledger": {"experiment_id": "LIB-x", "spec_hash": "s1", "run_id": "r1"}}
    state = {"registered_at": "2026-01-01T00:00:00+00:00", "registered_seq": 1, "amendments": [], "spec_hash": "s1", "spec": {},
             "runs": {"r1": {"started_seq": 2, "status": "success"}}}
    ev = StrategyRunEvidence(run_id=1, entry_id="trend_ma_crossover", created_at="2026-01-02T00:00:00+00:00", data_fingerprint="f" * 16,
                             request=req, library=good_library(), settings_hash_recomputed="h" * 64,
                             manifest={"git": {"reproducible_from_commit": True}, "config": {"costs": req["costs"], "params": req["params"]}},
                             ledger_spec={"data_fingerprint": "f" * 16, "cost_assumptions": req["costs"]}, ledger_state=state,
                             family_settings_tried=1, neighbours=[{"run_id": 2, "params": {"fast": 10, "slow": 60}, "validation_excess": 0.01}])
    return replace(ev, **over)


# ------------------------------------------------------------------ rules (evaluator only)

def test_pass_is_reachable_only_with_full_evidence():
    res = evaluate_strategy(good_evidence())
    assert res["status"] == PASS, {k: c["status"] for k, c in by_name(res).items() if c["status"] != PASS}
    assert res["real_data_verified"] is True and res["data_scope"] == "REAL"
    assert "PBO (probability of backtest overfitting)" in res["not_implemented"]


def test_demo_data_never_counts_as_real_verification():
    lib = good_library(synthetic=True, data_sources=["synthetic_demo"])
    res = evaluate_strategy(good_evidence(library=lib))
    assert by_name(res)["data_source"]["status"] == FAIL and res["status"] == FAIL
    assert res["data_scope"] == "SYNTHETIC_DEMO" and res["real_data_verified"] is False
    mixed = evaluate_strategy(good_evidence(library=good_library(mixed_demo_and_real=True)))
    assert by_name(mixed)["data_source"]["status"] == FAIL and mixed["real_data_verified"] is False


def test_unrecorded_evidence_is_insufficient_not_invented():
    res = evaluate_strategy(good_evidence(library={}, manifest=None, neighbours=[], family_settings_tried=None))
    c = by_name(res)
    for name in ("evidence_recorded", "data_source", "period_separation", "sample_size", "dev_effect", "validation_out_of_sample",
                 "test_out_of_sample", "yearly_consistency", "cost_stress", "benchmark", "selection_count"):
        assert c[name]["status"] == INSUFFICIENT, name
    assert c["cost_stress"]["values"] == {} and "excess_net" not in c["validation_out_of_sample"]["values"]
    assert res["status"] == INSUFFICIENT
    partial = good_library()
    partial["cost_stress"] = {"2.0": partial["cost_stress"]["2.0"]}               # 3x never run
    assert by_name(evaluate_strategy(good_evidence(library=partial)))["cost_stress"]["status"] == INSUFFICIENT


def test_missing_source_or_fingerprint_is_limited():
    res = evaluate_strategy(good_evidence(data_fingerprint=None, library=good_library(data_sources=[])))
    c = by_name(res)
    assert c["data_fingerprint"]["status"] == INSUFFICIENT and c["data_source"]["status"] == INSUFFICIENT
    assert res["status"] != PASS and res["real_data_verified"] is False
    assert data_scope({}) == "UNKNOWN"


def test_period_overlap_missing_and_crossing():
    bad = dict(PERIODS, validation=["2021-06-01", "2023-12-31"])
    assert by_name(evaluate_strategy(good_evidence(library=good_library(periods=bad))))["period_separation"]["status"] == FAIL
    no_test = {k: v for k, v in PERIODS.items() if k != "test"}
    assert by_name(evaluate_strategy(good_evidence(library=good_library(periods=no_test))))["period_separation"]["status"] == FAIL
    lib = good_library(by_period=[_period("dev", crossing=2), _period("validation"), _period("test")])
    c = by_name(evaluate_strategy(good_evidence(library=lib)))["period_separation"]
    assert c["status"] == WATCH and c["values"]["trades_crossing"] == 2


def test_dev_validation_test_rules():
    weak_dev = good_library(by_period=[_period("dev", ci=(-0.01, 0.02)), _period("validation"), _period("test")])
    assert by_name(evaluate_strategy(good_evidence(library=weak_dev)))["dev_effect"]["status"] == FAIL
    neg_val = good_library(by_period=[_period("dev"), _period("validation", excess=-0.01), _period("test")])
    assert by_name(evaluate_strategy(good_evidence(library=neg_val)))["validation_out_of_sample"]["status"] == FAIL
    weak_val = good_library(by_period=[_period("dev"), _period("validation", ci=(-0.001, 0.02)), _period("test")])
    assert by_name(evaluate_strategy(good_evidence(library=weak_val)))["validation_out_of_sample"]["status"] == WATCH
    few = good_library(by_period=[_period("dev"), _period("validation", trades=3), _period("test")])
    assert by_name(evaluate_strategy(good_evidence(library=few)))["sample_size"]["status"] == INSUFFICIENT
    bad_test = good_library(by_period=[_period("dev"), _period("validation"), _period("test", excess=-0.2)])
    res = evaluate_strategy(good_evidence(library=bad_test))
    assert by_name(res)["test_out_of_sample"]["status"] == FAIL and res["checks"][-1]["check"] == "test_out_of_sample"


def test_cost_stress_yearly_neighbours_benchmark():
    lib = good_library(cost_stress={"2.0": {"by_period": [_period("validation", excess=-0.01)]}, "3.0": {"by_period": [_period("validation", excess=-0.02)]}})
    assert by_name(evaluate_strategy(good_evidence(library=lib)))["cost_stress"]["status"] == FAIL
    lib = good_library(cost_stress={"2.0": {"by_period": [_period("validation", excess=0.01)]}, "3.0": {"by_period": [_period("validation", excess=-0.02)]}})
    assert by_name(evaluate_strategy(good_evidence(library=lib)))["cost_stress"]["status"] == WATCH
    few = good_library(by_period=[_period("dev"), _period("validation", trades=2), _period("test")])
    assert by_name(evaluate_strategy(good_evidence(library=few)))["cost_stress"]["status"] == INSUFFICIENT
    lib = good_library(yearly=[{"year": y, "excess_net": -0.01} for y in range(2016, 2024)])
    assert by_name(evaluate_strategy(good_evidence(library=lib)))["yearly_consistency"]["status"] == FAIL
    assert by_name(evaluate_strategy(good_evidence(neighbours=[])))["parameter_neighbourhood"]["status"] == WATCH
    flip = [{"run_id": 3, "params": {}, "validation_excess": -0.02}]
    assert by_name(evaluate_strategy(good_evidence(neighbours=flip)))["parameter_neighbourhood"]["status"] == WATCH
    proxy = good_library(benchmark={"kind": "equal_weight_proxy", "symbol": None, "label": "대용"})
    assert by_name(evaluate_strategy(good_evidence(library=proxy)))["benchmark"]["status"] == WATCH


def test_preregistration_selection_and_integrity():
    assert by_name(evaluate_strategy(good_evidence(ledger_state=None)))["preregistration"]["status"] == WATCH
    late = {"registered_at": "2026-02-01T00:00:00+00:00", "registered_seq": 5, "amendments": [], "spec_hash": "s1", "spec": {},
            "runs": {"r1": {"started_seq": 2}}}
    assert by_name(evaluate_strategy(good_evidence(ledger_state=late)))["preregistration"]["status"] == FAIL
    amended = {"registered_at": "2026-01-01T00:00:00+00:00", "registered_seq": 1, "amendments": [{"after_results": True}], "spec_hash": "s1",
               "spec": {}, "runs": {"r1": {"started_seq": 2}}}
    unrecorded = {**amended, "amendments": [], "runs": {}}
    assert by_name(evaluate_strategy(good_evidence(ledger_state=unrecorded)))["preregistration"]["status"] == FAIL
    assert by_name(evaluate_strategy(good_evidence(ledger_state=amended)))["preregistration"]["status"] == WATCH
    assert by_name(evaluate_strategy(good_evidence(family_settings_tried=4)))["selection_count"]["status"] == WATCH
    c = by_name(evaluate_strategy(good_evidence(settings_hash_recomputed="x" * 64)))["settings_integrity"]
    assert c["status"] == FAIL and "설정 해시" in c["detail"]
    spec = {"data_fingerprint": "f" * 16, "cost_assumptions": {"commission_bps": 5.0, "sell_tax_bps": 20.0, "slippage_bps": 10.0}}
    assert by_name(evaluate_strategy(good_evidence(ledger_spec=spec)))["settings_integrity"]["status"] == FAIL
    spec = {"data_fingerprint": "0" * 16}
    assert by_name(evaluate_strategy(good_evidence(ledger_spec=spec)))["data_fingerprint"]["status"] == FAIL
    assert by_name(evaluate_strategy(good_evidence(manifest_output_mismatches=["report.json"])))["reproducibility"]["status"] == FAIL
    assert by_name(evaluate_strategy(good_evidence(manifest=None)))["reproducibility"]["status"] == WATCH


def test_validity_and_audit_evidence():
    for vs, st in (("PROVISIONAL", WATCH), ("INELIGIBLE", FAIL), (None, WATCH)):
        assert by_name(evaluate_strategy(good_evidence(library=good_library(validity_status=vs))))["data_validity"]["status"] == st
    assert by_name(evaluate_strategy(good_evidence(library=good_library(audit=None))))["audit_applied"]["status"] == WATCH
    assert by_name(evaluate_strategy(good_evidence(library=good_library(audit={"review_share": 0.79}))))["audit_applied"]["status"] == WATCH


def test_thresholds_changed_after_registration_are_watch():
    res = evaluate_strategy(good_evidence(), StrategyGateConfig(min_trades_dev=5), registered_gate={"min_trades_dev": 30})
    assert by_name(res)["thresholds"]["status"] == WATCH
    for bad in ({"made_up": 1}, {"min_trades_dev": 0}, {"cost_stress_multipliers": [0.5]}, {"yearly_min_hit": 0.3, "yearly_fail_below": 0.5}):
        with pytest.raises(ValueError):
            StrategyGateConfig.from_dict(bad)


# ------------------------------------------------------------------ adapter on real saved runs (DEMO data) and API

@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/demo/load").status_code == 200
        yield c


def _run(client, entry="trend_ma_crossover", **body):
    r = client.post(f"/api/library/strategies/{entry}/backtest", json={"market": "DEMO", "random_runs": 0, **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_library_run_records_gate_evidence(client):
    r = _run(client)
    L = r["library"]
    assert L["evidence_version"] == 1 and L["synthetic"] is True and L["periods"] == PERIODS
    assert L["benchmark"]["kind"] == "index" and L["benchmark"]["symbol"] and L["benchmark"]["cost_applied"] is False
    assert set(L["cost_stress"]) == {"2.0", "3.0"}
    assert L["cost_stress"]["2.0"]["costs"] == {"commission_bps": 3.0, "sell_tax_bps": 40.0, "slippage_bps": 20.0}
    v = next(x for x in L["by_period"] if x["period"] == "test")
    assert v["excess_net"] == pytest.approx(v["total_return"] - v["benchmark_total_return"]) and "expectancy_return_ci95" in v
    assert all(y["year"] for y in L["yearly"])


def test_saved_demo_run_through_the_gate(client):
    r = _run(client, ledger=True, params={"fast": 15, "slow": 50})
    g = client.post(f"/api/library/runs/{r['run_id']}/robustness", json={}).json()
    c = by_name(g)
    assert g["subject"] == {"kind": "strategy_library_run", "run_id": r["run_id"], "entry_id": "trend_ma_crossover", "params": {"fast": 15, "slow": 50}}
    assert g["data_scope"] == "SYNTHETIC_DEMO" and g["real_data_verified"] is False and g["status"] == FAIL
    assert c["data_source"]["status"] == FAIL
    assert c["settings_integrity"]["status"] == PASS and c["preregistration"]["status"] == PASS and c["data_fingerprint"]["status"] == PASS
    assert c["settings_integrity"]["values"]["costs"] == r["costs"]                     # gate reads the run's own costs
    assert c["period_separation"]["status"] in (PASS, WATCH) and c["benchmark"]["status"] == PASS
    assert c["reproducibility"]["status"] in (PASS, WATCH) and g["ledger_artifact_linked"] is True
    from app.research.ledger import Ledger
    from app.strategy_library import default_ledger_path
    st = Ledger(default_ledger_path()).experiment(r["library"]["ledger"]["experiment_id"])
    assert any(a["kind"] == "robustness_gate" for a in st["artifacts"])
    assert st["spec"]["gate"]["min_trades_dev"] == StrategyGateConfig().min_trades_dev   # thresholds declared at registration
    g2 = client.post(f"/api/library/runs/{r['run_id']}/robustness", json={}).json()
    hist = client.get(f"/api/library/runs/{r['run_id']}/robustness").json()
    assert len(hist) == 2 and g2["saved_to"] != g["saved_to"]                         # earlier results kept, never overwritten


def test_gate_detects_tampered_settings(client):
    from app import db
    r = _run(client, entry="volume_breakout")
    row = db.fetch_all("SELECT request FROM backtest_runs WHERE id=?", (r["run_id"],))[0]
    req = json.loads(row["request"])
    req["costs"]["commission_bps"] = 0.0                                               # rewrite the record after the run
    db.execute("UPDATE backtest_runs SET request=? WHERE id=?", (json.dumps(req), r["run_id"]))
    c = by_name(client.post(f"/api/library/runs/{r['run_id']}/robustness", json={}).json())
    assert c["settings_integrity"]["status"] == FAIL


def test_selection_count_and_neighbours_from_saved_runs(client):
    a = _run(client, entry="donchian_breakout", params={"entry_n": 20, "exit_n": 10})
    _run(client, entry="donchian_breakout", params={"entry_n": 30, "exit_n": 10})
    c = by_name(client.post(f"/api/library/runs/{a['run_id']}/robustness", json={}).json())
    assert c["selection_count"]["status"] == WATCH and c["selection_count"]["values"]["settings_tried"] >= 2
    assert c["parameter_neighbourhood"]["values"].get("neighbours")


def test_gate_api_errors_and_threshold_overrides(client, monkeypatch, tmp_path):
    assert client.post("/api/library/runs/999999/robustness", json={}).status_code == 404
    assert client.get("/api/library/runs/999999/robustness").status_code == 404
    r = _run(client, entry="rsi_mean_reversion")
    rid = r["run_id"]
    assert client.post(f"/api/library/runs/{rid}/robustness", json={"thresholds": {"bogus": 1}}).status_code == 400
    assert client.post(f"/api/library/runs/{rid}/robustness", json={"thresholds": {"min_trades_dev": -1}}).status_code == 400
    assert client.post(f"/api/library/runs/{rid}/robustness", json={"thresholds": "x"}).status_code == 422
    g = client.post(f"/api/library/runs/{rid}/robustness", json={"thresholds": {"min_trades_dev": 1}}).json()
    assert by_name(g)["thresholds"]["status"] == WATCH
    led = _run(client, entry="rsi_mean_reversion", ledger=True, params={"oversold": 25.0})
    import app.strategy_library as sl
    bad = tmp_path / "ledger.jsonl"
    bad.write_text("not json\n")
    monkeypatch.setattr(sl, "default_ledger_path", lambda: bad)
    assert client.post(f"/api/library/runs/{led['run_id']}/robustness", json={}).status_code == 409


def test_compare_saved_runs_with_different_benchmarks_is_blocked(client):
    from app import db
    a = _run(client, entry="volume_breakout", exit={"max_hold_days": 7})["run_id"]
    b = _run(client, entry="rsi_mean_reversion", exit={"max_hold_days": 7})["run_id"]
    assert client.post("/api/library/runs/compare", json={"run_ids": [a, b]}).json()["comparable"] is True
    row = db.fetch_all("SELECT summary FROM backtest_runs WHERE id=?", (b,))[0]
    s = json.loads(row["summary"])
    s["library"]["benchmark"] = {"kind": "equal_weight_proxy", "symbol": None, "label": "대용"}
    db.execute("UPDATE backtest_runs SET summary=? WHERE id=?", (json.dumps(s), b))
    res = client.post("/api/library/runs/compare", json={"run_ids": [a, b]}).json()
    assert res["comparable"] is False and any("벤치마크가 다름" in x for x in res["reasons"])
    del s["library"]["benchmark"]
    db.execute("UPDATE backtest_runs SET summary=? WHERE id=?", (json.dumps(s), b))
    res = client.post("/api/library/runs/compare", json={"run_ids": [a, b]}).json()
    assert res["comparable"] is False and any("기록되지 않은" in x for x in res["reasons"])


def test_proxy_benchmark_is_never_labelled_as_index():
    from app.strategy_library import benchmark_info
    b = benchmark_info("NOPE", None)
    assert b["kind"] == "equal_weight_proxy" and "대용" in b["label"] and "실제 시장지수가 아님" in b["method"]


def test_compare_entries_report_benchmark_source(client):
    r = client.post("/api/library/compare", json={"market": "DEMO", "items": [{"id": "trend_ma_crossover"}, {"id": "volume_breakout"}]}).json()
    assert r["conditions"]["benchmark_info"]["kind"] == "index"
    row = r["rows"][0]["by_period"][1]
    assert "benchmark_total_return" in row and "excess_net" in row


def test_ui_wires_gate_and_handles_errors(client):
    html = client.get("/").text
    for needle in ("runLibGate", "/robustness", "게이트 평가 실패", "real_data_verified", "libGateRun"):
        assert needle in html, needle
