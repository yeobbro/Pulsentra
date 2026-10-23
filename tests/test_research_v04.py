"""v0.4 reproducibility layer: run manifest, period-boundary guards, result status, determinism."""
import json

import pandas as pd
import pytest

from app.research.manifest import file_sha256
from app.research.study import StudyConfig
from app.research.validity import DATA_TREATMENTS, HANDLED, NOT_HANDLED, PARTIAL, assess
from tests.test_research import _write_standard_dir


def _small(orig=StudyConfig):
    c = orig()
    c.periods = {"dev": ("2022-01-01", "2023-12-31"), "validation": ("2024-01-01", "2024-12-31"), "test": ("2025-01-01", "2099-12-31")}
    c.stops = {"ATR1.5": {"stop_type": "atr", "atr_mult": 1.5}}
    c.targets = {"2R": {"target_r": 2.0}}
    c.holds = (5, 10)
    c.min_avg_turnover = 0
    c.min_dev_trades = 1
    c.random_pool = 2000
    c.random_draws = 50
    c.portfolio_random_seeds = 1
    return c


def _run(data, out, extra=()):
    import app.research.__main__ as cli
    import app.research.study as st
    orig = st.StudyConfig
    st.StudyConfig = cli.StudyConfig = lambda: _small(orig)
    try:
        assert cli.main(["--data", str(data), "--format", "standard", "--out", str(out), "--start", "2022-01-01", *extra]) == 0
    finally:
        st.StudyConfig = cli.StudyConfig = orig
    return json.loads((out / "run_manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def two_runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("v04")
    data = base / "data"
    data.mkdir()
    _write_standard_dir(data)
    m1 = _run(data, base / "out1")
    m2 = _run(data, base / "out2", ["--sell-tax-bps", "18"])
    m3 = _run(data, base / "out3")
    return base, m1, m2, m3


def test_manifest_records_code_environment_seeds_and_outputs(two_runs):
    base, m, _, _ = two_runs
    assert m["manifest_version"] == 2
    assert m["git"]["commit"] is None or len(m["git"]["commit"]) == 40
    assert isinstance(m["git"]["uncommitted_code_files"], list)
    assert m["environment"]["packages"]["numpy"] and m["environment"]["packages"]["pandas"]
    assert set(m["random_seeds"]) == {"random_pool_and_ml", "random_comparison", "portfolio_random_entry", "cluster_bootstrap"}
    assert m["data_range"]["first"] and m["data_range"]["last"]
    assert m["exclusions"]["total_rows"] >= m["exclusions"]["eligible_rows"] > 0
    assert m["experiments_total"] == m["configs_tested_per_strategy"] * 6
    assert isinstance(m["warnings"], list)
    # every output listed with a hash that matches the file on disk
    out = base / "out1"
    listed = {o["path"]: o for o in m["outputs"]}
    assert "strategy_exit_grid.csv" in listed and "run_manifest.json" not in listed
    for path, o in listed.items():
        assert file_sha256(out / path) == o["sha256"]


def test_manifest_holds_no_credentials(two_runs, monkeypatch):
    _, m, _, _ = two_runs
    text = json.dumps(m)
    for word in ("KIS_APP_KEY", "KIS_APP_SECRET", "DART_API_KEY", "appsecret", "crtfc_key"):
        assert word not in text


def test_cost_override_is_recorded_as_a_change(two_runs):
    _, m1, m2, _ = two_runs
    assert m1["cost_assumptions"]["changed_from_defaults"] == {}
    assert m2["cost_assumptions"]["changed_from_defaults"] == {"sell_tax_bps": {"default": 20.0, "used": 18.0}}
    assert m2["cost_assumptions"]["round_trip_bps"] == pytest.approx(2 * 1.5 + 18 + 2 * 10)


def test_same_data_and_settings_reproduce_identical_tables(two_runs):
    base, m1, _, m3 = two_runs
    assert m1["data_fingerprint"] == m3["data_fingerprint"]
    h1 = {o["path"]: o["sha256"] for o in m1["outputs"]}
    h3 = {o["path"]: o["sha256"] for o in m3["outputs"]}
    for f in ("strategy_exit_grid.csv", "vs_random_entry.csv", "walk_forward_by_year.csv", "portfolio_results.csv", "cost_sensitivity.csv"):
        assert h1[f] == h3[f], f


def test_boundary_crossing_flag_matches_exit_dates(two_runs):
    base, m, _, _ = two_runs
    cfg = _small()
    files = list((base / "out1" / "trades").glob("signals_*.csv"))
    assert files
    total = 0
    for f in files:
        df = pd.read_csv(f, parse_dates=["signal_date", "exit_date"])
        assert (df["exit_date"] >= df["signal_date"]).all()
        for per, (_, end) in cfg.periods.items():
            sub = df[df["period"] == per]
            assert (sub["crosses_period_end"] == (sub["exit_date"] > pd.Timestamp(end))).all()
        total += int(df.loc[df["period"].notna(), "crosses_period_end"].sum())
    assert total == sum(v["crossing"] for v in m["boundary_crossing_trades"].values())


def test_synthetic_sample_without_delistings_is_ineligible(two_runs):
    _, m, _, _ = two_runs
    assert m["validity"]["status"] == "INELIGIBLE"
    assert any("생존편향" in r for r in m["validity"]["ineligible_reasons"])


def test_config_rejects_short_embargo_and_overlapping_periods():
    c = StudyConfig()
    c.validate()
    c.embargo_days = 20                      # 20-bar hold needs >= 28 calendar days
    with pytest.raises(ValueError, match="embargo"):
        c.validate()
    c = StudyConfig()
    c.periods = {"dev": ("2016-01-01", "2022-06-30"), "validation": ("2022-01-01", "2023-12-31")}
    with pytest.raises(ValueError, match="overlap"):
        c.validate()


def test_assess_statuses():
    sample = {"bars": 1000, "delisted_with_bars": 3}
    v = assess(sample)
    assert v["status"] == "PROVISIONAL" and v["unverified_items"]
    assert assess({"bars": 1000, "delisted_with_bars": 0})["status"] == "INELIGIBLE"
    assert assess(sample, {"rows_total": 100, "rows_error": 5})["status"] == "INELIGIBLE"
    # verifications only remove their own items; partial/not_handled data treatments keep the run provisional
    all_verified = assess(sample, verified={"cross_source_price_check": True, "corporate_action_disclosure_match": True})
    assert all_verified["status"] == "PROVISIONAL"
    assert not any(i.startswith("cross_source_price_check") for i in all_verified["unverified_items"])


def test_treatment_table_is_well_formed():
    items = [t["item"] for t in DATA_TREATMENTS]
    assert len(items) == len(set(items))
    assert {t["status"] for t in DATA_TREATMENTS} <= {HANDLED, PARTIAL, NOT_HANDLED}
    assert all(t["how"] and t["evidence"] for t in DATA_TREATMENTS)


def test_audit_exclusions_feed_the_study_and_are_recorded(tmp_path):
    """--audit drops nothing by default-off; when given, flagged bars remove nearby signals and the manifest says so."""
    import app.research.__main__ as cli
    import app.research.study as st
    from app.audit.__main__ import main as audit_main
    from tests.audit_fixtures import write_naver_raw
    d = write_naver_raw(tmp_path / "data")
    assert audit_main(["--data", str(d), "--out", str(tmp_path / "audit")]) == 0
    orig = st.StudyConfig

    def cfg():
        c = _small(orig)
        c.periods = {"dev": ("2023-01-01", "2023-08-31"), "validation": ("2023-09-01", "2023-12-31"), "test": ("2024-01-01", "2099-12-31")}
        return c
    st.StudyConfig = cli.StudyConfig = cfg
    try:
        base = ["--data", str(d), "--format", "naver_raw", "--start", "2023-01-01"]
        assert cli.main(base + ["--out", str(tmp_path / "plain")]) == 0
        assert cli.main(base + ["--out", str(tmp_path / "audited"), "--audit", str(tmp_path / "audit")]) == 0
    finally:
        st.StudyConfig = cli.StudyConfig = orig
    plain = json.loads((tmp_path / "plain" / "run_manifest.json").read_text(encoding="utf-8"))
    aud = json.loads((tmp_path / "audited" / "run_manifest.json").read_text(encoding="utf-8"))
    assert plain["audit"] is None
    a = aud["audit"]
    assert a["usable_rows_match_study"] is True                  # the audit described exactly the rows the study used
    assert a["rows_signal_excluded_by_audit"] > 0
    assert aud["exclusions"]["eligible_rows"] < plain["exclusions"]["eligible_rows"]
    assert plain["data_fingerprint"] == aud["data_fingerprint"]  # same data; only eligibility changed


def test_incomplete_trades_are_counted(two_runs):
    _, m, _, _ = two_runs
    inc = m["incomplete_trades_dropped"]
    assert inc and all(v["signals"] >= v["incomplete"] >= v["incomplete_in_delisted"] >= 0 for v in inc.values())


def test_crosscheck_status_only_counts_checks_that_passed(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _write_standard_dir(data)
    st = tmp_path / "crosscheck_status.json"
    st.write_text(json.dumps({"verified": {"cross_source_price_check": True, "corporate_action_disclosure_match": False}}))
    m = _run(data, tmp_path / "out", ["--crosscheck", str(st)])
    items = m["validity"]["unverified_items"]
    assert not any(i.startswith("cross_source_price_check") for i in items)
    assert any(i.startswith("corporate_action_disclosure_match") for i in items)
    assert m["validity"]["verifications_from"] == str(st)
