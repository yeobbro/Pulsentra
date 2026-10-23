"""Cross-source comparison logic and the live runner, offline (synthetic series and fake clients only)."""
import json
import os

import numpy as np
import pandas as pd
import pytest

from app.research.crosscheck import compare_daily, compare_listings, factor_segments, match_corporate_actions
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
