"""Robustness Gate: each check and the overall status, on hand-built Factor Lab outputs (test fixtures only)."""
import json

import pandas as pd
import pytest

from app.research.ledger import Ledger
from app.research.manifest import output_inventory
from app.research.robustness import FAIL, INSUFFICIENT, PASS, WATCH, GateConfig, evaluate

PERIODS = {"dev": ["2016-01-01", "2021-12-31"], "validation": ["2022-01-01", "2023-12-31"], "test": ["2024-01-01", "2099-12-31"]}
FACTORS = {"mom_20": {"kind": "momentum", "lookback": 20, "skip": 0}, "mom_60": {"kind": "momentum", "lookback": 60, "skip": 0},
           "mom_120": {"kind": "momentum", "lookback": 120, "skip": 0}}


def _rows(factor, period, start, end, ric, edge, turnover=0.3, n=200):
    dates = pd.date_range(start, end, freq="28D")
    out = []
    for i, d in enumerate(dates):
        noise = 0.03 if i % 2 else -0.03
        ew = 0.004 + (0.01 if i % 3 == 0 else -0.005)
        out.append({"factor": factor, "date": d, "entry_date": d + pd.Timedelta(1, unit="D"), "exit_date": d + pd.Timedelta(27, unit="D"),
                    "period": period, "year": d.year, "n": n, "n_eligible": n + 20, "ic": ric + noise, "rank_ic": ric + noise,
                    "q1": ew - edge, "q2": ew, "q3": ew, "q4": ew, "q5": ew + edge, "top": ew + edge, "bottom": ew - edge,
                    "ls_gross": 2 * edge, "turnover_top": turnover, "turnover_bottom": turnover, "cost_top": 0.0, "cost_bottom": 0.0,
                    "ew_universe": ew, "kospi_cc": ew - 0.001, "first_in_period": i == 0, "last_in_period": i == len(dates) - 1})
    return out


def make_run(tmp_path, name="fl", ric=(0.05, 0.05, 0.05), edge=(0.01, 0.01, 0.01), turnover=0.3, neighbours=0.05,
             periods=None, factors=None, ends=("2021-11-15", "2023-11-15", "2025-11-15"), validity="VERIFIED",
             reproducible=True, audit=None, started=None, ledger=None, total=3, embargo=35, eligibility="PASS",
             neighbour_edge=0.5):
    d = tmp_path / name
    d.mkdir()
    rows = []
    for fac in (factors or FACTORS):
        r = ric if fac == "mom_60" else tuple(neighbours if x > 0 else -neighbours for x in ric) if isinstance(neighbours, float) else neighbours
        for (per, start), end, rr, ee in zip((("dev", "2016-01-04"), ("validation", "2022-01-03"), ("test", "2024-01-02")), ends, r, edge):
            # neighbours carry half the edge, so mom_60 is the in-sample winner of the run (selection checks)
            rows += _rows(fac, per, start, end, rr, ee if fac == "mom_60" else ee * neighbour_edge, turnover)
    pd.DataFrame(rows).to_csv(d / "factor_ic_by_date.csv", index=False)
    m = {"manifest_version": 2, "kind": "factorlab", "started_utc": started or pd.Timestamp.now(tz="UTC").isoformat(),
         "git": {"commit": "c" * 40, "reproducible_from_commit": reproducible, "uncommitted_code_files": [] if reproducible else ["app/x.py"]},
         "config": {"factors": factors or FACTORS, "periods": periods or PERIODS, "horizon": 20, "embargo_days": embargo},
         "cost_assumptions": {"buy_side_bps": 11.5, "sell_side_bps": 31.5}, "data_fingerprint": "feedfacefeedface",
         "validity": {"status": validity}, "audit": {"review_share": 0.1, "symbols_dropped": 2} if audit is None else (audit or None),
         "experiments_total": total, "ledger_experiment": ledger,
         # eligibility-v1 record as Factor Lab writes it at run time (None = a run made before the record existed)
         "data_eligibility": None if eligibility is None else {"rules_version": "eligibility-v1", "status": eligibility, "checks": [],
                                                               "provenance": {"data_fingerprint": "feedfacefeedface"}}}
    m["outputs"] = output_inventory(d)
    (d / "run_manifest.json").write_text(json.dumps(m), encoding="utf-8")
    return d


def by_name(res):
    return {c["check"]: c for c in res["checks"]}


@pytest.fixture
def registered(tmp_path):
    from tests.test_ledger import spec
    L = Ledger(tmp_path / "l.jsonl")
    ev = L.register(spec(eid="EXP-gate", periods=PERIODS))
    return L, {"id": "EXP-gate", "spec_hash": ev["spec_hash"], "version": 1, "run_id": "r1"}


def test_pass_only_when_every_check_passes(tmp_path, registered):
    L, link = registered
    d = make_run(tmp_path, ledger=link)
    res = evaluate(d, "mom_60", ledger_state=L.state(), ledger_id="EXP-gate")
    bad = {k: c["status"] for k, c in by_name(res).items() if c["status"] != PASS}
    assert res["status"] == PASS, bad
    assert res["direction_from_dev"] == 1
    # PBO and DSR are implemented since 2026-10-30 and must be among the passing checks, not listed as missing
    assert {"selection_overfitting", "deflated_sharpe"} <= set(by_name(res))
    assert not any("PBO" in x or "Deflated" in x for x in res["not_implemented"])


def test_no_preregistration_is_watch(tmp_path):
    res = evaluate(make_run(tmp_path), "mom_60")
    assert by_name(res)["preregistration"]["status"] == WATCH and res["status"] == WATCH


def test_registered_after_run_fails(tmp_path, registered):
    L, link = registered
    d = make_run(tmp_path, ledger=link, started="2020-01-01T00:00:00+00:00")
    assert by_name(evaluate(d, "mom_60", ledger_state=L.state(), ledger_id="EXP-gate"))["preregistration"]["status"] == FAIL


def test_amended_after_results_is_watch(tmp_path, registered):
    L, link = registered
    L.start_run("EXP-gate", "r1"); L.finish_run("EXP-gate", "r1", "success")
    L.amend("EXP-gate", {"planned_experiments": 5}, "added windows after seeing dev")
    d = make_run(tmp_path, ledger=link)
    c = by_name(evaluate(d, "mom_60", ledger_state=L.state(), ledger_id="EXP-gate"))["preregistration"]
    assert c["status"] == WATCH and c["values"]["amendments_after_results"] == 1


def test_more_experiments_than_planned_is_watch(tmp_path, registered):
    L, link = registered
    d = make_run(tmp_path, ledger=link, total=8)
    assert by_name(evaluate(d, "mom_60", ledger_state=L.state(), ledger_id="EXP-gate"))["experiment_count"]["status"] == WATCH


def test_insufficient_data(tmp_path):
    d = make_run(tmp_path, ends=("2016-06-01", "2023-11-15", "2025-11-15"))
    res = evaluate(d, "mom_60")
    assert by_name(res)["sample_size"]["status"] == INSUFFICIENT and res["status"] == INSUFFICIENT


def test_overlapping_periods_fail(tmp_path):
    p = {**PERIODS, "validation": ["2021-06-01", "2023-12-31"]}
    res = evaluate(make_run(tmp_path, periods=p), "mom_60")
    assert by_name(res)["period_separation"]["status"] == FAIL and res["status"] == FAIL


def test_short_embargo_fails(tmp_path):
    assert by_name(evaluate(make_run(tmp_path, embargo=10), "mom_60"))["period_separation"]["status"] == FAIL


def test_holding_window_crossing_period_end_fails(tmp_path):
    d = make_run(tmp_path, ends=("2021-12-30", "2023-11-15", "2025-11-15"))       # last dev exit lands in 2022
    c = by_name(evaluate(d, "mom_60"))["period_separation"]
    assert c["status"] == FAIL and c["values"]["rows_crossing"] >= 1


def test_no_dev_effect_fails(tmp_path):
    res = evaluate(make_run(tmp_path, ric=(0.001, 0.05, 0.05)), "mom_60")
    assert by_name(res)["dev_effect"]["status"] == FAIL and res["status"] == FAIL


def test_validation_reversal_fails(tmp_path):
    res = evaluate(make_run(tmp_path, ric=(0.05, -0.05, 0.05)), "mom_60")
    assert by_name(res)["validation_out_of_sample"]["status"] == FAIL


def test_direction_fixed_on_dev_not_test(tmp_path):
    res = evaluate(make_run(tmp_path, ric=(0.05, 0.05, -0.08), edge=(0.01, 0.01, -0.01)), "mom_60")
    assert res["direction_from_dev"] == 1 and by_name(res)["test_out_of_sample"]["status"] == FAIL


def test_negative_direction_uses_bottom_leg(tmp_path):
    res = evaluate(make_run(tmp_path, ric=(-0.05, -0.05, -0.05), edge=(-0.01, -0.01, -0.01)), "mom_60")
    assert res["direction_from_dev"] == -1 and by_name(res)["validation_out_of_sample"]["status"] == PASS


def test_higher_costs_break_a_thin_edge(tmp_path):
    # edge 0.25%/window, turnover 100%: base cost 0.43% > edge -> fails at x1 already; turnover 20% -> fails only under stress
    res = evaluate(make_run(tmp_path, edge=(0.0025, 0.0025, 0.0025), turnover=1.0), "mom_60")
    assert by_name(res)["validation_out_of_sample"]["status"] == FAIL
    res = evaluate(make_run(tmp_path, name="fl2", edge=(0.0012, 0.0012, 0.0012), turnover=0.2), "mom_60")
    c = by_name(res)["cost_stress"]
    assert c["status"] in (WATCH, FAIL) and c["values"]["excess_by_multiplier"]["3.0"] < c["values"]["excess_by_multiplier"]["2.0"]


def test_fragile_parameter_neighbourhood_is_watch(tmp_path):
    res = evaluate(make_run(tmp_path, neighbours=-0.05), "mom_60")
    assert by_name(res)["parameter_neighbourhood"]["status"] == WATCH
    only = {"mom_60": FACTORS["mom_60"]}
    assert by_name(evaluate(make_run(tmp_path, name="fl2", factors=only), "mom_60"))["parameter_neighbourhood"]["status"] == WATCH


def test_audit_warnings(tmp_path):
    assert by_name(evaluate(make_run(tmp_path, audit=False), "mom_60"))["audit_applied"]["status"] == WATCH
    c = by_name(evaluate(make_run(tmp_path, name="fl2", audit={"review_share": 0.79}), "mom_60"))["audit_applied"]
    assert c["status"] == WATCH


def test_data_validity_status(tmp_path):
    assert by_name(evaluate(make_run(tmp_path, validity="PROVISIONAL"), "mom_60"))["data_validity"]["status"] == WATCH
    assert evaluate(make_run(tmp_path, name="fl2", validity="INELIGIBLE"), "mom_60")["status"] == FAIL


def test_reproducibility(tmp_path, registered):
    assert by_name(evaluate(make_run(tmp_path, reproducible=False), "mom_60"))["reproducibility"]["status"] == WATCH
    d = make_run(tmp_path, name="fl2")
    with (d / "factor_ic_by_date.csv").open("a") as fh:
        fh.write("\n")
    assert by_name(evaluate(d, "mom_60"))["reproducibility"]["status"] == FAIL
    L, link = registered
    from tests.test_ledger import spec
    L.register(spec(eid="EXP-gate-fp", periods=PERIODS, data_fingerprint="0000000000000000"))
    d3 = make_run(tmp_path, name="fl3")
    assert by_name(evaluate(d3, "mom_60", ledger_state=L.state(), ledger_id="EXP-gate-fp"))["data_fingerprint_matches_spec"]["status"] == FAIL
    L.register(spec(eid="EXP-gate-per", periods={**PERIODS, "test": ["2024-06-01", "2099-12-31"]}))
    assert by_name(evaluate(d3, "mom_60", ledger_state=L.state(), ledger_id="EXP-gate-per"))["periods_match_spec"]["status"] == FAIL


def test_changed_thresholds_are_watch(tmp_path):
    res = evaluate(make_run(tmp_path), "mom_60", gate=GateConfig(dev_rank_ic_t_min=1.0))
    c = by_name(res)["thresholds"]
    assert c["status"] == WATCH and "dev_rank_ic_t_min" in c["values"]["changed"]
    with pytest.raises(ValueError):
        GateConfig.from_dict({"made_up": 1})


def test_unknown_factor(tmp_path):
    with pytest.raises(ValueError):
        evaluate(make_run(tmp_path), "nope")


def test_cli_with_factorlab_output_and_ledger(tmp_path):
    from app.factorlab.__main__ import main as fl_main
    from app.robustness.__main__ import main as rb_main
    from app.research.ledger import verify
    from tests.test_factorlab import _standard_dir
    from tests.test_ledger import spec
    data = tmp_path / "data"; data.mkdir()
    _standard_dir(data)
    L = tmp_path / "l.jsonl"
    Ledger(L).register(spec(eid="EXP-cli", gate={"min_dates_dev": 24}))
    fl = tmp_path / "fl"
    assert fl_main(["run", "--data", str(data), "--format", "standard", "--out", str(fl), "--factors", "mom_20,mom_60", "--horizon", "5",
                    "--quantiles", "2", "--min-names", "4", "--min-turnover", "0", "--ledger", str(L), "--ledger-id", "EXP-cli"]) == 0
    out = tmp_path / "gate"
    assert rb_main(["evaluate", "--factorlab", str(fl), "--factor", "mom_20", "--out", str(out), "--ledger", str(L), "--ledger-id", "EXP-cli"]) == 0
    g = json.loads((out / "gate.json").read_text())
    assert g["status"] != PASS                                    # 5 test-fixture symbols, no dev period data: never a PASS
    assert by_name(g)["sample_size"]["status"] == INSUFFICIENT and (out / "GATE.md").exists()
    assert by_name(g)["benchmark"]["status"] == INSUFFICIENT                # no dev direction -> nothing to compare, not a FAIL
    st = Ledger(L).experiment("EXP-cli")
    assert any(a["kind"] == "robustness_gate" for a in st["artifacts"]) and verify(L).ok
    assert rb_main(["evaluate", "--factorlab", str(fl), "--factor", "mom_20", "--out", str(out)]) == 2       # non-empty out
    assert rb_main(["evaluate", "--factorlab", str(tmp_path), "--factor", "mom_20", "--out", str(tmp_path / "g2")]) == 2
    assert rb_main(["evaluate", "--factorlab", str(fl), "--factor", "mom_20", "--out", str(tmp_path / "g3"), "--threshold", "bogus=1"]) == 2
    assert rb_main(["evaluate", "--factorlab", str(fl), "--factor", "mom_20", "--out", str(tmp_path / "g4"), "--threshold", "min_dates_dev=2"]) == 0
    assert by_name(json.loads((tmp_path / "g4" / "gate.json").read_text()))["thresholds"]["status"] == WATCH
    assert rb_main(["thresholds"]) == 0
