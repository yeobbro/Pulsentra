"""Instrument-level data audit on synthetic TEST data with one injected anomaly per instrument."""
import json

import numpy as np
import pandas as pd
import pytest

from app.audit import check_output_dir, compare_runs, load_raw_naver
from app.audit.__main__ import main as audit_main
from app.data.audit import EXCLUDE, OK, REVIEW, AuditConfig, audit_frame
from tests.audit_fixtures import build, calendar, write_naver_raw


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    d = write_naver_raw(tmp_path_factory.mktemp("naver") / "data")
    bars, uni, cal = load_raw_naver(d)
    return d, audit_frame(bars, AuditConfig(), cal, uni)


def _f(res, sym, check):
    F = res.findings
    return F[(F["symbol"] == sym) & (F["check"] == check)]


def test_clean_instrument_has_no_findings(fx):
    _, res = fx
    assert _f(res, "000010", "price_jump").empty
    assert res.findings[res.findings["symbol"] == "000010"].empty
    assert res.symbols.set_index("symbol").at["000010", "audit_status"] == "ok"


def test_halt_markers_removed_as_info_with_ranges(fx):
    _, res = fx
    assert len(_f(res, "000020", "halt_marker_zero_ohl")) == 3
    assert len(_f(res, "000020", "halt_marker_flat_zero_volume")) == 2
    x = res.exclusions[res.exclusions["symbol"] == "000020"]
    assert x["rows"].sum() == 5 and set(x["scope"]) == {"row_removed"}
    assert (res.findings.loc[res.findings["symbol"] == "000020", "severity"] == "info").all()


def test_non_positive_prices_are_errors_with_observed_values(fx):
    _, res = fx
    f = _f(res, "000030", "non_positive_price")
    assert len(f) == 10
    assert (f["cause"] == "close_not_positive").all() and (f["recommendation"] == EXCLUDE).all()
    assert f["observed"].str.contains("C=0").all()
    # the removal leaves a 10-session hole, reported separately as a calendar gap
    assert len(_f(res, "000030", "missing_trading_days")) == 1


def test_ohlc_error_vs_rounding_repair_with_before_after(fx):
    _, res = fx
    assert len(_f(res, "000040", "ohlc_inconsistent")) == 1
    rep = _f(res, "000040", "ohlc_rounding_repaired")
    assert len(rep) == 1 and rep["auto_fix"].iloc[0] == "repaired"
    c = res.corrections[res.corrections["symbol"] == "000040"]
    assert len(c) == 1 and c["field"].iloc[0] == "high" and c["after"].iloc[0] == c["before"].iloc[0] + 1


def test_volume_not_adjusted_for_split_is_explained_and_windows_excluded(fx):
    _, res = fx
    f = _f(res, "000050", "volume_level_break")
    assert len(f) == 1 and f["cause"].iloc[0] == "volume_not_adjusted_for_split_or_merge(x10)"
    k = res.candidates[res.candidates["symbol"] == "000050"]
    assert k["implied_ratio"].iloc[0] == 10 and k["confidence"].iloc[0] == "medium"
    x = res.exclusions[(res.exclusions["symbol"] == "000050") & (res.exclusions["scope"] == "signals_excluded")]
    assert x["rows"].sum() == AuditConfig().signal_window
    assert _f(res, "000050", "volume_level_shift_no_gap").empty     # the gap case is not double reported


def test_price_jump_classification_uses_limit_and_gap(fx):
    _, res = fx
    f = _f(res, "000060", "price_jump").set_index("cause")
    assert f.at["exceeds_daily_price_limit", "recommendation"] == EXCLUDE
    assert f.at["base_price_reset_after_suspension", "recommendation"] == REVIEW


def test_duplicates_identical_kept_once_conflicting_removed(fx):
    _, res = fx
    assert _f(res, "000070", "duplicate_identical")["recommendation"].iloc[0] == OK
    assert _f(res, "000070", "duplicate_conflict")["recommendation"].iloc[0] == EXCLUDE
    x = res.exclusions[res.exclusions["symbol"] == "000070"].set_index("reason")
    assert x.at["duplicate_conflict", "rows"] == 2 and x.at["duplicate_identical", "rows"] == 1


def test_listing_window_and_delisting_checks(fx):
    _, res = fx
    assert "10 bars before" in _f(res, "000080", "bars_before_listing")["observed"].iloc[0]
    assert "5 bars after" in _f(res, "000080", "bars_after_delisting")["observed"].iloc[0]
    assert len(_f(res, "000090", "data_ends_before_delisting")) == 1
    assert len(_f(res, "000110", "no_bars_for_delisted")) == 1
    assert res.symbols.set_index("symbol").at["000110", "audit_status"] == "exclude"


def test_schema_errors_reported_once_per_row(fx):
    _, res = fx
    assert len(_f(res, "000100", "unparsable_date")) == 1
    assert len(_f(res, "000100", "non_numeric_value")) == 1
    assert _f(res, "000100", "missing_value").empty
    assert len(_f(res, "000120", "weekend_date")) == 1


def test_bonus_issue_like_shift_without_gap_is_info_candidate(fx):
    _, res = fx
    f = _f(res, "000130", "volume_level_shift_no_gap")
    assert len(f) == 1 and f["severity"].iloc[0] == "info" and f["cause"].iloc[0] == "possible_bonus_issue(x10)"
    assert (res.candidates["kind"] == "bonus_issue_suspected").sum() == 1


def test_findings_have_every_required_field(fx):
    _, res = fx
    F = res.findings
    for col in ("symbol", "check", "category", "severity", "observed", "expected", "rationale", "cause", "auto_fix",
                "backtest_use", "recommendation", "follow_up"):
        assert F[col].notna().all() and (F[col].astype(str) != "").all(), col
    assert set(F["severity"]) <= {"error", "warning", "info"}
    assert set(F["recommendation"]) <= {OK, REVIEW, EXCLUDE}


def test_audit_does_not_modify_its_input(fx):
    d, _ = fx
    bars, uni, cal = load_raw_naver(d)
    snapshot = bars.copy()
    audit_frame(bars, AuditConfig(), cal, uni)
    pd.testing.assert_frame_equal(bars, snapshot)


def test_usable_rows_reconcile_with_research_loader(fx):
    """The audit and the research loader must agree on exactly which rows a backtest uses."""
    from app.research.data import load_naver_raw
    d, res = fx
    ds = load_naver_raw(d)
    cols = ["symbol", "date", "open", "high", "low", "close", "volume"]
    a = res.usable[cols].sort_values(["symbol", "date"]).reset_index(drop=True)
    b = ds.bars[cols].sort_values(["symbol", "date"]).reset_index(drop=True)
    a["date"], b["date"] = pd.to_datetime(a["date"]), pd.to_datetime(b["date"])
    pd.testing.assert_frame_equal(a.astype({"volume": float}), b.astype({"volume": float}), check_dtype=False)


def test_rerun_reproduces_fingerprints(fx):
    d, res = fx
    bars, uni, cal = load_raw_naver(d)
    again = audit_frame(bars, AuditConfig(), cal, uni)
    for k in ("input_fingerprint", "findings_fingerprint", "usable_fingerprint"):
        assert again.summary[k] == res.summary[k]
    changed = audit_frame(bars, AuditConfig(jump_threshold=0.5), cal, uni)
    assert changed.summary["input_fingerprint"] == res.summary["input_fingerprint"]


def test_thresholds_are_configurable(fx):
    d, _ = fx
    bars, uni, cal = load_raw_naver(d, symbols=["000050"])
    strict = audit_frame(bars, AuditConfig(volume_break_factor=20.0), cal, uni)
    # check_frame's own factor is fixed at 3.0 in the loader; the audit reports with the loader's semantics
    assert len(strict.findings[strict.findings["check"] == "volume_level_break"]) == 1
    loose = audit_frame(bars, AuditConfig(split_volume_tolerance=0.0001), cal, uni)
    f = loose.findings[loose.findings["check"] == "volume_level_break"]
    assert f["cause"].iloc[0].startswith("volume_level_shift_after_suspension")


def test_cli_writes_outputs_refuses_unsafe_out_and_compares(fx, tmp_path):
    d, _ = fx
    out1, out2 = tmp_path / "a1", tmp_path / "a2"
    assert audit_main(["--data", str(d), "--out", str(out1)]) == 0
    assert audit_main(["--data", str(d), "--out", str(out2)]) == 0
    for f in ("findings.csv", "corrections.csv", "exclusions.csv", "corporate_action_candidates.csv", "symbol_summary.csv",
              "audit_summary.json", "audit_manifest.json", "AUDIT_REPORT.md"):
        assert (out1 / f).exists(), f
    assert compare_runs(out1, out2)["reproduced"]
    assert audit_main(["--compare", str(out1), str(out2)]) == 0
    # never overwrite silently, never write into the input folder or its raw/ files
    assert audit_main(["--data", str(d), "--out", str(out1)]) == 2
    assert audit_main(["--data", str(d), "--out", str(out1), "--overwrite"]) == 0
    assert audit_main(["--data", str(d), "--out", str(d)]) == 2
    assert audit_main(["--data", str(d), "--out", str(d / "raw" / "x")]) == 2
    with pytest.raises(ValueError):
        check_output_dir(d.parent, d, overwrite=True)
    raw_hash = {p.name: p.read_bytes() for p in (d / "raw").iterdir()}
    assert {p.name: p.read_bytes() for p in (d / "raw").iterdir()} == raw_hash
    m = json.loads((out1 / "audit_manifest.json").read_text(encoding="utf-8"))
    assert m["fingerprints"]["input_fingerprint"] and m["git"] and m["environment"]["packages"]["pandas"]


def test_price_limit_era():
    cfg = AuditConfig()
    lim = cfg.price_limit(pd.Series(pd.to_datetime(["2015-06-12", "2015-06-15"])))
    assert list(lim) == [0.15, 0.30]


def test_pre_2015_move_over_15pct_is_flagged():
    d = pd.DataFrame({"symbol": "000500", "market": "KR", "date": pd.bdate_range("2014-01-02", periods=5),
                      "open": [100, 100, 120, 120, 120], "high": [101, 101, 121, 121, 121], "low": [99, 99, 119, 119, 119],
                      "close": [100, 100, 120, 120, 120], "volume": 1000, "adjusted": True})
    res = audit_frame(d, AuditConfig())
    f = res.findings[res.findings["check"] == "price_jump"]
    assert len(f) == 1 and f["cause"].iloc[0] == "exceeds_daily_price_limit"


def test_fixture_is_test_data_only():
    raw, uni = build()
    assert len(calendar()) == 320 and set(uni["name"].str[:4]) == {"TEST"}
    assert all(np.isfinite(pd.to_numeric(df["close"], errors="coerce")).sum() > 100 for df in raw.values())


def test_research_loader_survives_bad_cells_with_a_date_window(fx):
    """Regression: a text value in a raw file used to crash load_naver_raw (turnover = close * volume on strings)."""
    from app.research.data import load_naver_raw
    d, _ = fx
    ds = load_naver_raw(d, start="2023-02-01", end="2023-12-31")
    assert ds.bars["date"].min() >= pd.Timestamp("2023-02-01")
    bad = ds.issues[(ds.issues["symbol"] == "000100") & (ds.issues["check"] == "missing_value")]
    assert len(bad) == 1                                          # the 'abc' volume row, recorded and rejected
    assert pd.api.types.is_float_dtype(ds.bars["volume"]) or pd.api.types.is_integer_dtype(ds.bars["volume"])


def test_all_zero_rows_follow_the_loader_rule(tmp_path):
    """Regression: the loader drops O=H=L=0 rows whatever the close; the audit used to require close > 0 and
    counted O=H=L=C=0 rows as non_positive_price errors instead, so its usable rows differed from the loader's."""
    from app.research.data import load_naver_raw as research_loader
    d = write_naver_raw(tmp_path / "data")
    f = d / "raw" / "000010.csv"
    df = pd.read_csv(f)
    df.loc[[200, 201], ["open", "high", "low", "close", "volume"]] = 0
    df.to_csv(f, index=False)
    bars, uni, cal = load_raw_naver(d)
    res = audit_frame(bars, AuditConfig(), cal, uni)
    assert len(_f(res, "000010", "halt_marker_zero_ohl")) == 2 and _f(res, "000010", "non_positive_price").empty
    ds = research_loader(d)
    assert len(ds.halts[ds.halts["symbol"] == "000010"]) == 2
    from app.data.audit import bars_fingerprint
    assert bars_fingerprint(res.usable) == bars_fingerprint(ds.bars)
