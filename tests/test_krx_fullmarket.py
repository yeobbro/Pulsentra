"""Whole-market KRX check (app/krxcheck/fullmarket.py): offline only — mock KRX answers and TEST data, never market data."""
import json

import numpy as np
import pandas as pd
import pytest

from app.krxcheck import fullmarket as fm
from app.providers.krx import SERVICES, KRXAPIError, KRXResult, parse_stock_rows
from app.research.krx_crosscheck import ADJUSTED_BASIS, PRICE_MISMATCH, classify_pair
from tests.test_krx import KEY, Resp, Session, stock_row

DAY, BAS = "2026-01-05", "20260105"
SVC = SERVICES["kospi_stock"]


def krx_frame(rows):
    return parse_stock_rows(rows, SVC)[0]


def row(code, o=1000, h=1100, l=990, c=1050, v=5000):
    return stock_row(code, BAS, o=f"{o:,}", h=f"{h:,}", l=f"{l:,}", c=f"{c:,}", v=f"{v:,}")


def write_local(root, code, rows):
    (root / "raw").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).to_csv(root / "raw" / f"{code}.csv", index=False)


def bar(day=DAY, o=1000, h=1100, l=990, c=1050, v=5000):
    return [day, o, h, l, c, v]


@pytest.fixture
def market(tmp_path):
    root = tmp_path / "data"
    write_local(root, "000010", [bar("2026-01-02"), bar()])                       # match
    write_local(root, "000020", [bar(c=1040)])                                    # close only
    write_local(root, "000030", [bar(v=4900)])                                    # volume only
    write_local(root, "000040", [bar(h=1090, l=995, c=1040, v=4000)])             # several fields
    write_local(root, "000050", [bar(h=1090)])                                    # high only (close equal)
    write_local(root, "000060", [bar("2026-01-02")])                              # file, no row for the date
    write_local(root, "000070", [bar(), bar()])                                   # duplicate date
    write_local(root, "000080", [bar(c="x")])                                     # unparsable value
    write_local(root, "000090", [bar()])                                          # KRX: no trade
    write_local(root, "000110", [bar(o=0, h=0, l=0)])                             # local halt marker
    pd.DataFrame({"symbol": ["000010", "000020", "000030", "000040", "000050", "000060", "000070", "000080", "000090",
                             "000110", "000100"], "status": "listed"}).to_csv(root / "universe.csv", index=False)
    krx = krx_frame([row("000010"), row("000020"), row("000030"), row("000040"), row("000050"), row("000060"), row("000070"),
                     row("000080"), stock_row("000090", BAS, o="0", h="0", l="0", c="1,050", v="0"), row("000110"),
                     row("000100"),            # in the universe, no local file
                     row("000105"),            # not in the universe (e.g. a preferred share), no local file
                     row("000120"), row("000120"),   # code repeated in the KRX answer
                     stock_row("12AB", BAS)])  # unusable code
    return root, krx


def _status(detail):
    return dict(zip(detail["symbol"].fillna(detail["isu_cd_raw"]), detail["status"]))


def test_every_case_gets_one_exclusive_status(market):
    root, krx = market
    uni = pd.read_csv(root / "universe.csv", dtype=str)
    detail, info = fm.compare_market(krx, root, DAY, uni)
    st = _status(detail)
    assert st["000010"] == fm.MATCH
    assert st["000020"] == fm.CLOSE_MISMATCH
    assert st["000030"] == fm.VOLUME_ONLY
    assert st["000040"] == fm.CLOSE_MISMATCH
    assert st["000050"] == fm.OHL_MISMATCH
    assert st["000060"] == fm.MISSING_LOCAL_DATE
    assert st["000070"] == fm.LOCAL_ROW_INVALID
    assert st["000080"] == fm.LOCAL_ROW_INVALID
    assert st["000090"] == fm.NOT_COMPARABLE
    assert st["000110"] == fm.NOT_COMPARABLE
    assert st["000100"] == fm.MISSING_LOCAL_FILE and st["000105"] == fm.MISSING_LOCAL_FILE
    assert st["000120"] == fm.KRX_ROW_INVALID and st["12AB"] == fm.KRX_ROW_INVALID
    assert len(detail) == len(krx)                                   # one row per KRX row, nothing dropped
    assert set(detail["status"]) <= set(fm.STATUS_ORDER)
    by = detail.set_index(detail["symbol"].fillna(detail["isu_cd_raw"]))
    assert bool(by.loc["000100", "in_universe"]) and not bool(by.loc["000105", "in_universe"])
    assert "collection universe but no local file" in by.loc["000100", "reason"]
    r = detail[detail["symbol"] == "000040"].iloc[0]
    assert not r["close_equal"] and not r["high_equal"] and not r["low_equal"] and r["open_equal"] and not r["volume_equal"]
    assert r["close_abs_diff"] == 10 and r["volume_diff"] == -1000 and r["volume_ratio_local_to_krx"] == pytest.approx(0.8)
    assert info["local_files_read"] == 10                            # files that exist for KRX codes


def test_summary_counts_and_denominators(market):
    root, krx = market
    detail, _ = fm.compare_market(krx, root, DAY, pd.read_csv(root / "universe.csv", dtype=str))
    s = fm.summarize_market(detail)
    assert s["krx_rows"] == len(krx) == sum(s["by_status"].values())
    assert s["comparable"] == 5 and s["not_compared"] == len(krx) - 5
    r = s["rates_over_comparable"]
    assert r["denominator"] == 5 and r["all_ohlcv_match"] == pytest.approx(1 / 5)
    assert r["close_equal"] == pytest.approx(3 / 5) and r["volume_equal"] == pytest.approx(3 / 5)
    o = s["overlapping_totals"]
    assert o["ohlc_mismatch_any"] == 3 and o["close_mismatch"] == 2 and o["volume_mismatch_any"] == 2
    assert s["missing_local_file_in_universe"] == 1
    assert [x["symbol"] for x in s["top_close_diff"]] == ["000020", "000040"]


def test_empty_comparable_set_has_no_rate():
    s = fm.summarize_market(pd.DataFrame([{"symbol": "000010", "status": fm.MISSING_LOCAL_FILE, "in_universe": False}]))
    assert s["comparable"] == 0 and s["rates_over_comparable"]["all_ohlcv_match"] is None


@pytest.mark.parametrize("raw,code", [(5930, "005930"), ("5930", "005930"), ("005930", "005930"), (5930.0, "005930"),
                                      ("KR7005930003", "005930"), (" 0001a0 ", "0001A0"), ("12AB", None), (None, None)])
def test_codes_keep_leading_zeros(raw, code):
    assert fm.norm_code(raw) == code


def test_leading_zero_codes_from_an_int_typed_krx_file(tmp_path):
    root = tmp_path / "data"
    write_local(root, "005930", [bar()])
    k = krx_frame([row("005930")])
    k["isu_cd_raw"] = 5930                                           # as if the KRX CSV had been read without dtype=str
    detail, _ = fm.compare_market(k, root, DAY)
    assert detail.loc[0, "symbol"] == "005930" and detail.loc[0, "status"] == fm.MATCH


def test_classifier_does_not_call_a_one_tick_difference_an_adjustment():
    # three fields equal -> median ratio exactly 1; v0.4 labelled this "adjusted_price_basis x 1.000000"
    loc = {"open": 172000, "high": 176800, "low": 170000, "close": 173900, "volume": 100}
    krx = {**loc, "close": 174000}
    assert classify_pair(loc, krx)["status"] == PRICE_MISMATCH
    # two fields equal, two a tick off (median 0.9993): also a disagreement, not an adjustment
    two = {"open": 1190, "high": 1190, "low": 1140, "close": 1178, "volume": 100}
    assert classify_pair(two, {**two, "low": 1142, "close": 1180})["status"] == PRICE_MISMATCH
    # a real constant factor is still an adjustment
    assert classify_pair({k: (v * 0.95 if k != "volume" else v) for k, v in loc.items()}, loc)["status"] == ADJUSTED_BASIS


# --------------------------------------------------------------------------- fetch / CLI

class FakeClient:
    configured = True

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls, self.http_attempts = result, error, 0, 0

    def fetch(self, service, bas_dd):
        self.calls += 1
        self.http_attempts += 1
        if self.error:
            raise self.error
        return self.result


def test_fetch_saves_raw_answer_meta_and_compare_reads_it_offline(tmp_path, market, monkeypatch):
    root, _ = market
    raw_rows = [row("000010"), row("000020")]                       # local 000020 has close 1040
    res = KRXResult("kospi_stock", BAS, "ok", raw=raw_rows, frame=krx_frame(raw_rows))
    out = tmp_path / "krx"
    assert fm.main(["fetch", "--date", DAY, "--out", str(out), "--live", "--spec-confirmed"], client=FakeClient(res)) == 0
    saved = pd.read_csv(out / f"krx_raw_kospi_stock_{BAS}.csv", dtype=str)
    assert saved.loc[0, "TDD_CLSPRC"] == "1,050" and saved.loc[0, "ISU_CD"] == "000010"     # text exactly as received
    meta = json.loads((out / "fetch_meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == 2 and meta["requests_sent"] == 1 and meta["duplicate_codes"] == 0 and "completeness" in meta
    # a second fetch into the same folder is refused before any request
    again = FakeClient(res)
    assert fm.main(["fetch", "--date", DAY, "--out", str(out), "--live", "--spec-confirmed"], client=again) == 2 and again.calls == 0
    # compare: no network (requests is not touched) and nothing written under the data root
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    before = sorted(p.name for p in (root / "raw").iterdir())
    assert fm.main(["compare", "--krx-dir", str(out), "--data", str(root), "--date", DAY]) == 0
    assert sorted(p.name for p in (root / "raw").iterdir()) == before
    s = json.loads((out / "compare" / "comparison_summary.json").read_text(encoding="utf-8"))
    assert s["by_status"][fm.MATCH] == 1 and s["by_status"][fm.CLOSE_MISMATCH] == 1 and s["comparable"] == 2
    assert (out / "compare" / "COMPARISON.md").exists()
    # the comparison folder is not overwritten by a second run
    assert fm.main(["compare", "--krx-dir", str(out), "--data", str(root), "--date", DAY]) == 2


def test_fetch_failure_writes_error_and_never_retries(tmp_path):
    cl = FakeClient(error=KRXAPIError("auth", "stk_bydd_trd: HTTP 401 — key rejected or service not approved"))
    out = tmp_path / "krx"
    assert fm.main(["fetch", "--date", DAY, "--out", str(out), "--live", "--spec-confirmed"], client=cl) == 4
    err = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert err["status"] == "failed" and err["error_kind"] == "auth" and cl.calls == 1
    assert not (out / f"krx_raw_kospi_stock_{BAS}.csv").exists()


def test_fetch_without_live_sends_nothing(tmp_path):
    cl = FakeClient()
    assert fm.main(["fetch", "--date", DAY, "--out", str(tmp_path / "krx")], client=cl) == 0
    assert cl.calls == 0 and not (tmp_path / "krx").exists()


def test_real_client_fetch_once_does_not_leak_the_key(tmp_path):
    from app.providers.krx import KRXClient
    sess = Session([Resp(payload={"OutBlock_1": [row("000010")]})])
    cl = KRXClient(api_key=KEY, session=sess, spec_confirmed=True, min_interval=0, max_retries=0, sleep=lambda s: None)
    code, meta = fm.fetch_once(cl, "KOSPI", BAS, tmp_path)
    assert code == 0 and cl.http_attempts == 1 and sess.calls[0]["headers"]["AUTH_KEY"] == KEY
    for p in tmp_path.iterdir():
        assert KEY not in p.read_text(encoding="utf-8")


def test_empty_krx_answer_is_not_saved_as_data(tmp_path):
    code, meta = fm.fetch_once(FakeClient(KRXResult("kospi_stock", BAS, "empty")), "KOSPI", BAS, tmp_path)
    assert code == 5 and meta["rows"] == 0 and not list(tmp_path.glob("krx_raw_*"))
    assert np.isnan(fm._num("-")) and np.isnan(fm._num("")) and fm._num("1,234") == 1234


# --------------------------------------------------------------------------- classifier review (both error directions)
# Synthetic numbers shaped like the cases met against KRX on 2025-02-27 / 2025-06-12 / 2026-10-22 / 2026-10-23.

def _adj(krx, f, vol_adjusted=True, rnd=round):
    """What a provider's adjusted bar looks like: every price x f, rounded to 1 KRW; volume / f when adjusted."""
    out = {k: float(rnd(v * f)) for k, v in krx.items() if k != "volume"}
    out["volume"] = float(round(krx["volume"] / f)) if vol_adjusted else krx["volume"]
    return out


K = {"open": 2760, "high": 2840, "low": 2655, "close": 2655, "volume": 1_000_000}


@pytest.mark.parametrize("f", [1 / 50, 0.2, 0.5, 0.93, 0.9512, 4.7627, 11.5765, 2.0])
def test_real_adjustments_stay_adjustments(f):
    r = classify_pair(_adj(K, f), K)
    assert r["status"] == ADJUSTED_BASIS and r["factor"] == pytest.approx(f, rel=2e-3)
    assert r["volume_status"] == "adjusted_with_prices"


def test_consolidation_rounding_of_a_few_krw_is_still_an_adjustment():
    # x11.58 consolidation: adjusted prices off by up to ~2.4 KRW from exact proportionality (seen on real rows)
    krx = {"open": 1046, "high": 1048, "low": 1026, "close": 1028, "volume": 5000}
    loc = {"open": 12110, "high": 12130, "low": 11880, "close": 11900, "volume": 432}
    assert classify_pair(loc, krx)["status"] == ADJUSTED_BASIS


def test_unadjusted_volume_with_adjusted_prices_is_an_adjustment_with_equal_volume():
    r = classify_pair(_adj(K, 0.5, vol_adjusted=False), K)
    assert r["status"] == ADJUSTED_BASIS and r["volume_status"] == "equal" and r["split_like_ratio"] == 2


@pytest.mark.parametrize("loc", [
    {"open": 172000, "high": 176800, "low": 170000, "close": 173900},     # one field one tick off, three equal
    {"open": 1190, "high": 1190, "low": 1140, "close": 1178},             # two equal, two off (median 0.9993)
    {"open": 1001, "high": 1099, "low": 991, "close": 1049},              # all off by 1 KRW, mixed directions
    {"open": 1000 * 1.0006, "high": 1100 * 0.9994, "low": 990 * 1.0006, "close": 1050 * 0.9994},  # mixed, within 0.1%
])
def test_one_tick_disagreements_are_not_adjustments(loc):
    krx = {"open": 172000, "high": 176800, "low": 170000, "close": 174000} if loc["open"] == 172000 else \
          ({"open": 1190, "high": 1190, "low": 1142, "close": 1180} if loc["open"] == 1190 else
           {"open": 1000, "high": 1100, "low": 990, "close": 1050})
    r = classify_pair({**loc, "volume": 100}, {**krx, "volume": 100})
    assert r["status"] == PRICE_MISMATCH and r["price_status"] == "inconsistent"


def test_sub_krw_ambiguity_is_reported_not_hidden():
    # every field within 1 KRW, two of them equal: could be a ~0.03% adjustment or rounding — stays a mismatch, says why
    r = classify_pair({"open": 3904, "high": 3934, "low": 3860, "close": 3910, "volume": 34915},
                      {"open": 3905, "high": 3935, "low": 3860, "close": 3910, "volume": 34915})
    assert r["status"] == PRICE_MISMATCH and "cannot be told apart" in r["reason"]


def test_volume_equality_is_exact():
    p = {"open": 1000, "high": 1100, "low": 990, "close": 1050}
    assert classify_pair({**p, "volume": 15_892_890}, {**p, "volume": 15_893_000})["status"] == "volume_mismatch"   # 0.0007%
    assert classify_pair({**p, "volume": 5000}, {**p, "volume": 5000.0})["status"] == "match"


# --------------------------------------------------------------------------- licence gate

def test_krx_answers_cannot_enter_the_app_store(tmp_path):
    from app import db
    from app.data.ingest import ingest_frame
    from app.data.schema import LICENSE_RESTRICTED_SOURCES
    path = tmp_path / "t.db"
    db.init_db(path)
    frame = krx_frame([row("000010")])
    with pytest.raises(ValueError, match="제3자 제공"):
        ingest_frame(frame, source="krx_openapi", market="KR", db_path=path)
    assert db.fetch_all("SELECT COUNT(*) AS n FROM candles", db_path=path)[0]["n"] == 0
    assert db.fetch_all("SELECT COUNT(*) AS n FROM ingestion_runs", db_path=path)[0]["n"] == 0
    assert "krx_openapi" in LICENSE_RESTRICTED_SOURCES


def test_provider_status_does_not_claim_krx_distribution():
    from app.main import _provider_status
    krx = [p for p in _provider_status()["providers"] if p["name"] == "KRX OPEN API"][0]
    assert krx["implemented"] is False and krx["status"] == "NOT_IMPLEMENTED" and "라이선스" in krx["note"]


@pytest.mark.parametrize("src", ["krx_openapi", "KRX_OPENAPI", " krx_openapi ", "Krx_OpenApi"])
def test_licence_gate_ignores_case_and_spaces(tmp_path, src):
    from app import db
    from app.data.ingest import ingest_frame
    path = tmp_path / "t.db"
    db.init_db(path)
    with pytest.raises(ValueError, match="제3자 제공"):
        ingest_frame(krx_frame([row("000010")]), source=src, market="KR", db_path=path)
    assert db.fetch_all("SELECT COUNT(*) AS n FROM candles", db_path=path)[0]["n"] == 0
    assert db.fetch_all("SELECT COUNT(*) AS n FROM ingestion_runs", db_path=path)[0]["n"] == 0


def test_licence_gate_holds_at_the_low_level_write(tmp_path):
    from app import db
    path = tmp_path / "t.db"
    db.init_db(path)
    f = krx_frame([row("000010")]).assign(market="KR")             # carries source = krx_openapi
    with pytest.raises(ValueError, match="제3자 제공"):
        db.upsert_candles(f, path)
    mixed = pd.concat([f.assign(source="csv_import", symbol="000020"), f.assign(source=" KRX_OpenAPI")])
    with pytest.raises(ValueError, match="제3자 제공"):
        db.upsert_candles(mixed, path)                               # one restricted row refuses the whole write
    assert db.fetch_all("SELECT COUNT(*) AS n FROM candles", db_path=path)[0]["n"] == 0


def test_saved_krx_file_uploaded_as_csv_is_refused(tmp_path):
    """krx_<service>_<basDd>.csv written by `fullmarket fetch` has a source column; the upload relabels rows as
    csv_import, so without the frame check it would enter the store under another name."""
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app
    csv = krx_frame([row("000010")]).to_csv(index=False).encode()
    with TestClient(app) as c:                                       # startup creates the tables
        before_runs = db.fetch_all("SELECT COUNT(*) AS n FROM ingestion_runs")[0]["n"]
        r = c.post("/api/data/import", files={"file": ("krx_kospi_stock_20260105.csv", csv, "text/csv")}, data={"market": "KR"})
    assert r.status_code == 400 and "제3자 제공" in r.json()["detail"]
    assert db.fetch_all("SELECT COUNT(*) AS n FROM candles WHERE symbol='000010' AND market='KR'")[0]["n"] == 0
    assert db.fetch_all("SELECT COUNT(*) AS n FROM ingestion_runs")[0]["n"] == before_runs


def test_licence_gate_does_not_block_other_sources(tmp_path):
    from app import db
    from app.data.ingest import ingest_frame
    from app.data.schema import license_refusal
    path = tmp_path / "t.db"
    db.init_db(path)
    f = krx_frame([row("000010")]).assign(market="KR")
    res = ingest_frame(f.drop(columns=["source"]), source="csv_import", market="KR", db_path=path)
    assert res["rows_saved"] == 1
    res = ingest_frame(f.assign(source="naver", symbol="000020"), source="csv_import", market="KR", db_path=path)  # other provider label
    assert res["rows_saved"] == 1
    assert {r["source"] for r in db.fetch_all("SELECT source FROM candles", db_path=path)} == {"csv_import"}
    for s in ("csv_import", "kis_eod", "naver_fchart", "synthetic_demo", "test", None, "", "krx", "krx_openapi_x"):
        assert license_refusal(s) is None


def test_compare_sample_does_not_pick_one_of_duplicate_rows():
    from app.research.krx_crosscheck import NOT_COMPARABLE, compare_sample
    P = {"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5000}
    for order in (1, -1):                                             # same answer whatever the row order
        prim = pd.DataFrame([{"symbol": "A", "date": "2026-01-05", **P}, {"symbol": "A", "date": "2026-01-05", **P, "close": 1040}][::order])
        r = compare_sample(prim, {"20260105": pd.DataFrame([{"symbol": "A", **P}])}, [("A", "2026-01-05")]).iloc[0]
        assert r["status"] == NOT_COMPARABLE and "duplicate primary" in r["reason"]
        k = pd.DataFrame([{"symbol": "B", **P}, {"symbol": "B", **P, "close": 1040}][::order])
        r = compare_sample(pd.DataFrame([{"symbol": "B", "date": "2026-01-05", **P}]), {"20260105": k}, [("B", "2026-01-05")]).iloc[0]
        assert r["status"] == NOT_COMPARABLE and "duplicate KRX" in r["reason"]


@pytest.mark.parametrize("p,k,why", [
    ({"open": 0, "high": 0, "low": 0, "close": 1050, "volume": 0}, {"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5}, "halt"),
    ({"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5}, {"open": 0, "high": 0, "low": 0, "close": 1050, "volume": 0}, "no trade"),
    ({"open": 1000, "high": 1100, "low": 990, "close": np.nan, "volume": 5}, {"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5}, "missing"),
    ({"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": np.nan}, {"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5}, "missing"),
    ({"open": 1000, "high": 1100, "low": 990, "close": "x", "volume": 5}, {"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5}, "missing"),
])
def test_missing_zero_and_text_values_are_not_comparable(p, k, why):
    from app.research.krx_crosscheck import NOT_COMPARABLE
    r = classify_pair(p, k)
    assert r["status"] == NOT_COMPARABLE and r["price_status"] is None


def test_float_noise_in_parsed_values_is_still_equal():
    a = {"open": 1000.0, "high": 1100.0, "low": 990.0, "close": 1050.0, "volume": 5000.0}
    b = {"open": 1000.0000001, "high": 1099.9999999, "low": 990.0, "close": 1050.0, "volume": 5000.0000001}
    assert classify_pair(a, b)["status"] == "match"
