"""Cross-source comparison logic and the live runner, offline (synthetic series and fake clients only)."""
import json
import os

import numpy as np
import pandas as pd
import pytest

from app.research.crosscheck import compare_daily, compare_listings, factor_segments, group_filings, match_corporate_actions
from tests.audit_fixtures import write_naver_raw


def _series(n=200, seed=0):
    rng = np.random.default_rng(seed)
    d = pd.bdate_range("2024-01-02", periods=n)
    c = (10_000 * np.cumprod(1 + rng.uniform(-0.01, 0.01, n))).round()
    v = (1e4 * np.exp(rng.normal(0, 0.2, n))).round()
    return pd.DataFrame({"date": d, "close": c, "volume": v})


def test_identical_series_are_consistent():
    p = _series()
    r = compare_daily(p, p.copy(), "X")
    assert r["verdict"] == "consistent" and r["close_mismatch_days"] == 0 and r["sessions_common"] == 200


def test_missing_sessions_are_coverage_not_errors():
    p, r = _series(), _series()
    r = r.drop(index=[10, 11, 50]).reset_index(drop=True)
    res = compare_daily(p, r)
    assert res["missing_in_reference"] == 3 and res["missing_in_primary"] == 0
    assert res["verdict"] == "consistent"


def test_constant_factor_on_a_stretch_is_an_adjustment_difference():
    """Reference adjusted for a 1:5 split on day 120, primary not before that day: one factor step, no record errors."""
    p, r = _series(), _series()
    r.loc[:119, "close"] = r.loc[:119, "close"] / 5
    res = compare_daily(p, r)
    assert res["adjustment_segments"] == 2 and res["record_error_days"] == 0
    assert res["verdict"] == "adjustment_basis_differs"
    assert res["segments"][0]["factor"] == pytest.approx(5, rel=1e-3) and res["segments"][1]["factor"] == pytest.approx(1)


def test_isolated_disagreements_are_record_errors():
    p, r = _series(), _series()
    r.loc[[30, 140], "close"] *= 1.07
    res = compare_daily(p, r)
    assert res["adjustment_segments"] == 1 and res["record_error_days"] == 2 and res["verdict"] == "record_errors"


def test_volume_ratio_reveals_unadjusted_volume():
    p, r = _series(), _series()
    p.loc[:99, "volume"] = p.loc[:99, "volume"] / 10        # primary volume not adjusted before a 1:10 split
    res = compare_daily(p, r)
    assert res["volume_mismatch_days"] == 100 and res["verdict"] == "consistent"   # prices agree; volume reported separately


def test_factor_segments_ignore_single_outliers():
    d = pd.bdate_range("2024-01-01", periods=10)
    ratio = np.array([1, 1, 1, 1.5, 1, 1, 1, 1, 1, 1.0])
    assert len(factor_segments(d, ratio)) == 1


def test_match_corporate_actions_statuses():
    cand = pd.DataFrame({"symbol": ["000050", "000060", "000070"], "date": ["2024-08-01", "2024-05-01", "2024-03-01"],
                         "kind": ["volume_level_break", "split", "volume_level_break"]})
    filings = pd.DataFrame({"symbol": ["000050", "000060", "000090"], "rcept_dt": ["20240610", "20240420", "20240301"],
                            "report_nm": ["주식분할결정", "유상증자결정", "무상증자결정"],
                            "corporate_action": ["split", "rights_issue", "bonus_issue"]})
    m = match_corporate_actions(cand, filings).set_index("symbol")
    assert m.at["000050", "status"] == "confirmed_kind_match" and m.at["000050", "days_before_event"] == 52
    assert m.at["000060", "status"] == "filing_kind_mismatch"
    assert m.at["000070", "status"] == "no_filing_found"
    assert m.at["000090", "status"] == "undetected_event"        # a bonus issue the price/volume checks missed


def test_filing_after_window_does_not_match():
    cand = pd.DataFrame({"symbol": ["A"], "date": ["2024-01-10"], "kind": ["split"]})
    filings = pd.DataFrame({"symbol": ["A"], "rcept_dt": ["20240301"], "report_nm": ["주식분할결정"], "corporate_action": ["split"]})
    m = match_corporate_actions(cand, filings)
    assert set(m["status"]) == {"no_filing_found", "undetected_event"}


def test_compare_listings_reports_missing_delistings():
    uni = pd.DataFrame({"symbol": ["000010", "000020"], "status": ["delisted", "delisted"], "delisted_date": ["2018-05-01", "2020-01-02"]})
    ref = pd.DataFrame({"symbol": [10, 20, 30, 40], "delisted_date": ["2018-05-01", "2020-01-02", "2020-06-30", "2015-01-01"]})
    r = compare_listings(uni, ref, start="2016-01-01")
    assert r["missing_from_universe"] == ["000030"] and r["coverage"] == pytest.approx(2 / 3)
    assert r["missing_by_year"][2020] == {"missing": 1, "reference": 2}


# --------------------------------------------------------------------------- runner with fake clients

class FakeKIS:
    configured = True

    def __init__(self, data_dir):
        self.d = data_dir

    def daily_bars_range(self, symbol, start, end):
        df = pd.read_csv(self.d / "raw" / f"{symbol}.csv")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        for c in ("close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df[(df["close"] > 0)].dropna(subset=["date", "close"])


class FakeDart:
    configured = True

    def corp_codes(self):
        return {"000050": {"corp_code": "C50", "corp_name": "T"}}

    def disclosures(self, corp_code, bgn, end, max_pages=5):
        return [{"symbol": "000050", "rcept_dt": "20230701", "report_nm": "주식분할결정", "corporate_action": "split", "rcept_no": "1"}]


class Unconfigured:
    configured = False


def test_runner_skips_without_keys_and_says_why(tmp_path):
    from app.crosscheck.__main__ import run
    d = write_naver_raw(tmp_path / "data")
    out = tmp_path / "cc"
    assert run(["--data", str(d), "--out", str(out), "--sample", "4"], Unconfigured(), Unconfigured()) == 3
    st = json.loads((out / "crosscheck_status.json").read_text(encoding="utf-8"))
    assert st["price_check"] == {"ran": False, "reason": "KIS_APP_KEY / KIS_APP_SECRET not set"}
    assert st["filing_check"]["ran"] is False
    assert st["verified"] == {"cross_source_price_check": False, "corporate_action_disclosure_match": False}


def test_runner_with_fake_clients(tmp_path):
    from app.audit.__main__ import main as audit_main
    from app.crosscheck.__main__ import run
    d = write_naver_raw(tmp_path / "data")
    assert audit_main(["--data", str(d), "--out", str(tmp_path / "audit")]) == 0
    out = tmp_path / "cc"
    assert run(["--data", str(d), "--audit", str(tmp_path / "audit"), "--out", str(out), "--symbols", "000010", "000050"],
               FakeKIS(d), FakeDart()) == 0
    st = json.loads((out / "crosscheck_status.json").read_text(encoding="utf-8"))
    assert st["price_check"]["symbols_compared"] == 2 and st["price_check"]["verdicts"] == {"consistent": 2}
    assert st["filing_check"]["statuses"]["confirmed_kind_match"] == 1
    pc = pd.read_csv(out / "price_comparison.csv", dtype={"symbol": str})
    assert set(pc["symbol"]) == {"000010", "000050"}


def test_sample_prefers_audit_candidates():
    from app.crosscheck.__main__ import pick_sample
    cand = pd.DataFrame({"symbol": ["000050", "000130"], "confidence": ["medium", "low"]})
    s = pick_sample([f"{i:06d}" for i in range(10, 200, 10)], cand, 6, seed=1)
    assert s[:2] == ["000050", "000130"] and len(s) == 6 and len(set(s)) == 6


# --------------------------------------------------------------------------- live (opt-in)

@pytest.mark.integration
@pytest.mark.skipif(os.getenv("PULSENTRA_RUN_INTEGRATION") != "1", reason="live API test: set PULSENTRA_RUN_INTEGRATION=1 and the keys")
def test_live_kis_daily_bars_smoke():
    from app.providers.kis import KISClient
    c = KISClient()
    if not c.configured:
        pytest.skip("KIS keys not set")
    df = c.daily_bars_range("005930", "2026-01-02", "2026-01-30")
    assert len(df) > 10 and (df["close"] > 0).all()


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("PULSENTRA_RUN_INTEGRATION") != "1", reason="live API test: set PULSENTRA_RUN_INTEGRATION=1 and the keys")
def test_live_dart_listing_smoke():
    from app.providers.dart import DartClient
    c = DartClient()
    if not c.configured:
        pytest.skip("DART key not set")
    codes = c.corp_codes()
    assert "005930" in codes


def test_runner_refuses_to_overwrite_results_or_write_into_data(tmp_path):
    from app.crosscheck.__main__ import run
    d = write_naver_raw(tmp_path / "data")
    out = tmp_path / "cc"
    out.mkdir()
    (out / "crosscheck_status.json").write_text("{}")
    assert run(["--data", str(d), "--out", str(out)], Unconfigured(), Unconfigured()) == 2
    assert (out / "crosscheck_status.json").read_text() == "{}"
    assert run(["--data", str(d), "--out", str(d / "raw" / "cc")], Unconfigured(), Unconfigured()) == 2
    assert run(["--data", str(d), "--out", str(out), "--overwrite"], Unconfigured(), Unconfigured()) == 3


# --------------------------------------------------------------------------- corporate actions: original + corrections
# Synthetic filings shaped like OpenDART list.json rows (005930 2018 split: 주식분할결정 2018-01-31, [기재정정] 2018-03-16).

def _f(no, dt, nm, kind="split", sym="005930", rm=None):
    return {"symbol": sym, "rcept_no": no, "rcept_dt": dt, "report_nm": nm, "corporate_action": kind, "rm": rm}


ORIG = _f("20180131800068", "20180131", "주식분할결정")
CORR = _f("20180316800856", "20180316", "[기재정정]주식분할결정")
CAND = pd.DataFrame({"symbol": ["005930"], "date": ["2018-05-04"], "kind": ["volume_level_break"]})


def test_original_and_correction_are_one_action_for_the_candidate():
    m = match_corporate_actions(CAND, pd.DataFrame([ORIG, CORR]))
    assert len(m) == 1                                                   # no separate row for the original
    r = m.iloc[0]
    assert r["status"] == "confirmed_kind_match" and r["action_documents"] == 2
    assert r["rcept_nos"] == "20180131800068|20180316800856" and r["action_filings"] == "주식분할결정|[기재정정]주식분할결정"
    assert r["link_basis"] == "official_correction_prefix"
    assert r["filing"] == "[기재정정]주식분할결정" and r["days_before_event"] == 49     # nearest document, as before


def test_unmatched_original_and_correction_count_as_one_undetected_event():
    m = match_corporate_actions(CAND.iloc[0:0], pd.DataFrame([ORIG, CORR]))
    assert list(m["status"]) == ["undetected_event"] and m.iloc[0]["action_documents"] == 2
    assert m.iloc[0]["filing"] == "주식분할결정"                          # represented by the original


def test_two_separate_actions_are_not_merged():
    other = _f("20210301000001", "20210301", "주식분할결정")              # a second split three years later
    m = match_corporate_actions(CAND, pd.DataFrame([ORIG, CORR, other]))
    assert set(m["status"]) == {"confirmed_kind_match", "undetected_event"}
    und = m[m["status"] == "undetected_event"].iloc[0]
    assert und["rcept_nos"] == "20210301000001" and und["action_documents"] == 1
    assert m[m["status"] == "confirmed_kind_match"].iloc[0]["action_documents"] == 2


def test_correction_only():
    m = match_corporate_actions(CAND, pd.DataFrame([CORR]))
    r = m.iloc[0]
    assert len(m) == 1 and r["status"] == "confirmed_kind_match" and r["action_documents"] == 1
    assert r["link_basis"] == "correction_without_original"


def test_original_only():
    m = match_corporate_actions(CAND, pd.DataFrame([ORIG]))
    r = m.iloc[0]
    assert len(m) == 1 and r["status"] == "confirmed_kind_match" and r["link_basis"] == "original" and r["action_documents"] == 1


def test_several_candidates_may_share_one_action_as_before():
    cand = pd.DataFrame({"symbol": ["005930", "005930"], "date": ["2018-05-04", "2018-05-07"], "kind": ["volume_level_break", "split"]})
    m = match_corporate_actions(cand, pd.DataFrame([ORIG, CORR]))
    assert len(m) == 2 and set(m["status"]) == {"confirmed_kind_match"} and m["action_id"].nunique() == 1


def test_weak_evidence_is_not_confirmed():
    # two possible originals within the link window: the correction cannot be attached to either
    a = _f("20180110000001", "20180110", "주식분할결정")
    m = match_corporate_actions(CAND, pd.DataFrame([a, ORIG, CORR]))
    r = m[m["kind"].notna()].iloc[0]
    assert r["status"] == "ambiguous"
    groups = group_filings(pd.DataFrame([a, ORIG, CORR]))
    assert groups.loc[groups["rcept_no"] == CORR["rcept_no"], "link_basis"].iloc[0].startswith("ambiguous")
    # ... unless OpenDART's own rm '정' marks exactly one of them as the corrected report
    g2 = group_filings(pd.DataFrame([a, {**ORIG, "rm": "유정"}, CORR]))
    linked = g2.set_index("rcept_no")
    assert linked.at[CORR["rcept_no"], "action_id"] == linked.at[ORIG["rcept_no"], "action_id"]
    assert linked.at[CORR["rcept_no"], "link_basis"] == "official_correction_prefix+rm_정"
    # a correction of a different report name is not linked to a split decision
    g3 = group_filings(pd.DataFrame([ORIG, _f("20180316000002", "20180316", "[기재정정]주식병합결정", kind="merge")]))
    assert g3["action_id"].nunique() == 2
    # an original filed more than a year before the correction is not linked
    g4 = group_filings(pd.DataFrame([_f("20160101000001", "20160104", "주식분할결정"), CORR]))
    assert g4["action_id"].nunique() == 2


def test_two_fitting_actions_equally_close_are_ambiguous():
    cand = pd.DataFrame({"symbol": ["A"], "date": ["2024-05-10"], "kind": ["volume_level_break"]})
    f = pd.DataFrame([_f("1", "20240501", "주식분할결정", sym="A"), _f("2", "20240519", "무상증자결정", kind="bonus_issue", sym="A")])
    assert match_corporate_actions(cand, f).iloc[0]["status"] == "ambiguous"
