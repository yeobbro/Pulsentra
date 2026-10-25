"""Multi-date report (app/krxcheck/history.py): offline only, built from detail files that the real
fullmarket.compare_market writes for TEST data (never market data, never the network)."""
import json
import socket

import pandas as pd
import pytest

from app.krxcheck import fullmarket as fm
from app.krxcheck import history as hist
from app.providers.krx import SERVICES, parse_stock_rows
from tests.test_krx import stock_row

SVC = SERVICES["kospi_stock"]


def krow(code, d, o=1000, h=1100, l=990, c=1050, v=5000):
    return stock_row(code, d.replace("-", ""), o=f"{o:,}", h=f"{h:,}", l=f"{l:,}", c=f"{c:,}", v=f"{v:,}")


def make_compare(tmp_path, day, krx_rows, local):
    """local: {code: [(o, h, l, c, v), ...]} rows for `day`. Writes data root + compare dir, returns the compare dir."""
    root = tmp_path / f"data_{day}"
    (root / "raw").mkdir(parents=True)
    for code, bars in local.items():
        pd.DataFrame([[day, *b] for b in bars], columns=["date", "open", "high", "low", "close", "volume"]).to_csv(root / "raw" / f"{code}.csv", index=False)
    frame = parse_stock_rows(krx_rows, SVC)[0]
    detail, info = fm.compare_market(frame, root, day)
    out = tmp_path / f"full_market_{day.replace('-', '')}" / "compare"
    out.mkdir(parents=True)
    detail.to_csv(out / "comparison_detail.csv", index=False)
    s = {"krx_rows": len(detail), **fm.summarize_market(detail)}
    (out / "comparison_summary.json").write_text(json.dumps(s, default=str), encoding="utf-8")
    return out


B = (1000, 1100, 990, 1050, 5000)


@pytest.fixture
def two_dates(tmp_path):
    d1 = make_compare(tmp_path, "2026-01-05",
                      [krow("000010", "2026-01-05"), krow("000020", "2026-01-05"), krow("000030", "2026-01-05"), krow("000040", "2026-01-05")],
                      {"000010": [B], "000020": [(1000, 1100, 990, 1040, 5000)], "000030": [(1000, 1100, 990, 1050, 4900)]})   # 000040: no file
    d2 = make_compare(tmp_path, "2026-01-06",
                      [krow("000010", "2026-01-06"), krow("000020", "2026-01-06"), krow("000030", "2026-01-06")],
                      {"000010": [(1000, 1120, 980, 1060, 4000)],            # wider range, less volume
                       "000020": [(1000, 1100, 990, 1040, 5000)], "000030": [B]})
    return tmp_path, d1, d2


def test_multi_date_counts_rates_and_denominators(two_dates):
    _, d1, d2 = two_dates
    rep = hist.build_report([d2, d1])                                     # order given does not matter
    assert rep["dates"] == ["2026-01-05", "2026-01-06"]
    a, b = rep["by_date"]["2026-01-05"], rep["by_date"]["2026-01-06"]
    assert a["krx_rows"] == 4 and a["comparable"] == 3 and a["missing"]["missing_local_file"] == 1
    assert a["all_ohlcv_match"] == 1 and a["all_ohlcv_match_rate"] == pytest.approx(1 / 3)       # denominator 3, not 4
    assert a["field_equal_rate"]["close"] == pytest.approx(2 / 3) and a["field_equal_rate"]["volume"] == pytest.approx(2 / 3)
    assert a["exclusive"] == {"match": 1, "volume_mismatch_only": 1, "close_mismatch": 1, "ohl_mismatch_close_eq": 0}
    assert a["overlapping"] == {"price_mismatch_any": 1, "volume_mismatch_any": 1}
    assert b["krx_rows"] == 3 and b["comparable"] == 3 and b["all_ohlcv_match_rate"] == pytest.approx(1 / 3)
    assert sum(a["by_status"].values()) == a["krx_rows"]
    assert b["shape"]["local_high_above_krx"] == 1 and b["shape"]["local_low_below_krx"] == 1 and b["shape"]["local_volume_below_krx"] == 1
    assert b["pattern"]["range"] == "local_wider_or_equal" and b["pattern"]["volume"] == "local_less_or_equal"
    assert a["summary_consistency"] == [] and b["summary_consistency"] == []
    c = rep["cross_date"]
    assert c["symbols_compared_on_every_date"] == 3 and c["of_those_unexplained_on_2plus_dates"] == 1      # 000020 both days
    assert c["last_two_dates"]["unexplained_on_both"] == 1
    assert rep["conclusion"]["comparable_rows_total"] == 6


def test_missing_empty_and_bad_schema_inputs_are_listed_not_used(two_dates, tmp_path):
    _, d1, d2 = two_dates
    gone = tmp_path / "full_market_20260107" / "compare"
    gone.mkdir(parents=True)                                              # folder without detail file
    empty = tmp_path / "full_market_20260108" / "compare"
    empty.mkdir(parents=True)
    (empty / "comparison_detail.csv").write_text("", encoding="utf-8")
    bad = tmp_path / "full_market_20260109" / "compare"
    bad.mkdir(parents=True)
    pd.read_csv(d1 / "comparison_detail.csv").drop(columns=["close_equal"]).to_csv(bad / "comparison_detail.csv", index=False)
    two = tmp_path / "full_market_20260110" / "compare"
    two.mkdir(parents=True)
    pd.concat([pd.read_csv(d1 / "comparison_detail.csv"), pd.read_csv(d2 / "comparison_detail.csv")]).to_csv(two / "comparison_detail.csv", index=False)
    rep = hist.build_report([d1, gone, empty, bad, two, d2])
    st = {r["compare_dir"]: (r["status"], r.get("problem", "")) for r in rep["inputs"]}
    assert st[str(gone)][0] == "missing" and st[str(empty)][0] == "empty"
    assert st[str(bad)][0] == "schema_error" and "close_equal" in st[str(bad)][1]
    assert st[str(two)][0] == "schema_error" and "one date" in st[str(two)][1]
    assert rep["dates"] == ["2026-01-05", "2026-01-06"]                   # bad inputs add nothing
    assert len(rep["conclusion"]["dates_skipped"]) == 4


def test_same_date_twice_is_used_once(two_dates, tmp_path):
    _, d1, _ = two_dates
    dup = tmp_path / "copy" / "compare"
    dup.mkdir(parents=True)
    (dup / "comparison_detail.csv").write_bytes((d1 / "comparison_detail.csv").read_bytes())
    rep = hist.build_report([d1, dup])
    assert rep["dates"] == ["2026-01-05"] and [r["status"] for r in rep["inputs"]].count("duplicate_date") == 1


def test_duplicate_rows_in_a_detail_file_leave_the_rates(two_dates, tmp_path):
    _, d1, _ = two_dates
    d = pd.read_csv(d1 / "comparison_detail.csv", dtype={"symbol": str})
    dupdir = tmp_path / "dup" / "compare"
    dupdir.mkdir(parents=True)
    pd.concat([d, d[d["symbol"] == "000020"]]).to_csv(dupdir / "comparison_detail.csv", index=False)
    rep = hist.build_report([dupdir])
    s = rep["by_date"]["2026-01-05"]
    assert s["detail_duplicates"] == 2 and s["comparable"] == 2 and s["all_ohlcv_match_rate"] == pytest.approx(1 / 2)
    assert s["invalid_or_duplicate"]["detail_duplicates"] == 2


def test_no_comparable_rows_gives_no_rate(tmp_path):
    d = make_compare(tmp_path, "2026-01-05", [krow("000010", "2026-01-05")], {})
    s = hist.build_report([d])["by_date"]["2026-01-05"]
    assert s["comparable"] == 0 and s["all_ohlcv_match_rate"] is None and s["field_equal_rate"]["close"] is None


def test_cli_is_reproducible_offline_and_never_writes_inputs(two_dates, tmp_path, monkeypatch):
    root, d1, d2 = two_dates
    import requests

    def no_net(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(requests, "get", no_net)
    monkeypatch.setattr(socket, "create_connection", no_net)
    before = {p: p.read_bytes() for p in (*d1.iterdir(), *d2.iterdir())}
    assert hist.main(["--compare-dir", str(d1), str(d2), "--out", str(tmp_path / "h1")]) == 0
    assert hist.main(["--compare-dir", str(d2), str(d1), "--out", str(tmp_path / "h2")]) == 0
    for f in ("history_by_date.csv", "history_samples.csv", "history_summary.json", "HISTORY.md"):
        assert (tmp_path / "h1" / f).read_bytes() == (tmp_path / "h2" / f).read_bytes(), f
    assert {p: p.read_bytes() for p in before} == before
    assert hist.main(["--krx-root", str(root), "--out", str(tmp_path / "h3")]) == 0          # glob finds both
    assert json.loads((tmp_path / "h3" / "history_summary.json").read_text(encoding="utf-8"))["dates"] == ["2026-01-05", "2026-01-06"]


def test_cli_refuses_previous_report_and_input_folders(two_dates, tmp_path):
    _, d1, _ = two_dates
    assert hist.main(["--compare-dir", str(d1), "--out", str(tmp_path / "h")]) == 0
    assert hist.main(["--compare-dir", str(d1), "--out", str(tmp_path / "h")]) == 2            # not empty
    assert hist.main(["--compare-dir", str(d1), "--out", str(d1 / "sub")]) == 2                # inside an input
    assert hist.main(["--krx-root", str(tmp_path / "nothing"), "--out", str(tmp_path / "h4")]) == 2


def test_samples_include_reference_symbol_and_only_real_volume_differences(tmp_path):
    d = make_compare(tmp_path, "2026-01-05", [krow("005930", "2026-01-05"), krow("000020", "2026-01-05")],
                     {"005930": [(1000, 1100, 990, 1040, 4000)], "000020": [(1000, 1100, 990, 1040, 5000)]})
    rep = hist.build_report([d])
    reasons = {(r["symbol"], r["sample_reason"]) for r in rep["samples"]}
    assert ("005930", "reference symbol") in reasons
    assert ("005930", "largest unexplained volume difference") in reasons
    assert ("000020", "largest unexplained volume difference") not in reasons                 # its volume is equal
