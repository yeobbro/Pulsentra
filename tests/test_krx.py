"""KRX Open API adapter and the KRX verification path, offline (mock responses and TEST data only)."""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
import requests

from app.providers.krx import (INDEX_FIELDS, SAMPLE_BASE_URL, SERVICES, STOCK_FIELDS, KRXAPIError, KRXClient, KRXResult,
                               parse_stock_rows, short_code)
from app.research.krx_crosscheck import (ADJUSTED_BASIS, KRX_DAY_EMPTY, MATCH, MISSING_IN_KRX, MISSING_IN_PRIMARY,
                                         NOT_COMPARABLE, PRICE_MISMATCH, REQUEST_FAILED, VOLUME_MISMATCH, candidate_verdicts,
                                         classify_pair, compare_sample, krx_evidence_for_candidate, summarize)
from tests.audit_fixtures import write_naver_raw

KEY = "test-krx-key-SECRET-123"


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Session:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def get(self, url, **kw):
        self.calls.append({"url": url, **kw})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def stock_row(code="005930", d="20260105", o="1,000", h="1,100", l="990", c="1,050", v="12,345", **kw):
    row = {f: "" for f in STOCK_FIELDS}
    row.update({"BAS_DD": d, "ISU_CD": code, "ISU_NM": "테스트", "MKT_NM": "KOSPI", "SECT_TP_NM": "-", "TDD_OPNPRC": o,
                "TDD_HGPRC": h, "TDD_LWPRC": l, "TDD_CLSPRC": c, "ACC_TRDVOL": v, "ACC_TRDVAL": "12,962,250",
                "CMPPREVDD_PRC": "50", "FLUC_RT": "5.00", "MKTCAP": "-", "LIST_SHRS": "1000"})
    row.update(kw)
    return row


def index_row(d="20260105", name="코스피", close="2,500.12"):
    row = {f: "" for f in INDEX_FIELDS}
    row.update({"BAS_DD": d, "IDX_CLSS": "KOSPI", "IDX_NM": name, "CLSPRC_IDX": close, "CMPPREVDD_IDX": "-3.20", "FLUC_RT": "-0.13",
                "OPNPRC_IDX": "2,510.00", "HGPRC_IDX": "2,520.00", "LWPRC_IDX": "2,490.00", "ACC_TRDVOL": "400,000",
                "ACC_TRDVAL": "9,000,000", "MKTCAP": "2,000,000,000"})
    return row


def client(script, **kw):
    sleeps = []
    c = KRXClient(api_key=KEY, session=Session(script), spec_confirmed=True, min_interval=0, sleep=sleeps.append, **kw)
    return c, sleeps


# --------------------------------------------------------------------------- adapter

def test_stock_answer_parsed_raw_kept_and_numbers_normalised():
    c, _ = client([Resp(payload={"OutBlock_1": [stock_row(), stock_row("KR7000660001", o="-", v="abc")]})])
    res = c.fetch("kospi_stock", "20260105")
    assert res.status == "ok" and res.raw[0]["TDD_OPNPRC"] == "1,000"          # raw text untouched
    f = res.frame
    assert list(f["symbol"]) == ["005930", "000660"] and f.loc[0, "open"] == 1000 and f.loc[0, "volume"] == 12345
    assert np.isnan(f.loc[1, "open"]) and np.isnan(f.loc[1, "volume"])        # '-' and text -> missing, never 0
    assert res.unparsed_values == 1                                            # only the text cell counts
    assert not f["adjusted"].any() and (f["source"] == "krx_openapi").all()


def test_index_answer_parsed():
    c, _ = client([Resp(payload={"OutBlock_1": [index_row()]})])
    res = c.fetch("kospi_index", "20260105")
    assert res.frame.loc[0, "close"] == pytest.approx(2500.12) and res.frame.loc[0, "index_name"] == "코스피"


def test_request_uses_auth_header_never_url_and_refuses_redirects():
    c, _ = client([Resp(payload={"OutBlock_1": [stock_row()]})])
    c.fetch("kosdaq_stock", "20260105")
    call = c.session.calls[0]
    assert call["url"] == "https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd"
    assert call["headers"]["AUTH_KEY"] == KEY and call["params"] == {"basDd": "20260105"}
    assert KEY not in call["url"] and KEY not in json.dumps(call["params"])
    assert call["allow_redirects"] is False and call["timeout"] > 0
    assert not any(s.url.startswith(SAMPLE_BASE_URL) for s in SERVICES.values())


@pytest.mark.parametrize("resp,kind", [
    (Resp(401), "auth"), (Resp(403), "forbidden"), (Resp(429), "rate_limit"), (Resp(302), "redirect"), (Resp(404), "bad_response"),
    (Resp(payload={"respCode": "401", "respMsg": "Unauthorized API Call"}), "auth"),
    (Resp(payload={"respCode": "500", "respMsg": "x"}), "bad_response"),
    (Resp(payload=ValueError("not json")), "bad_response"), (Resp(payload=[1, 2]), "bad_response"),
    (Resp(payload={"rows": []}), "bad_response"), (Resp(payload={"OutBlock_1": {"a": 1}}), "bad_response"),
    (Resp(payload={"OutBlock_1": [{"BAS_DD": "20260105", "ISU_CD": "005930"}]}), "bad_response"),
    (Resp(payload={"OutBlock_1": [stock_row(d="20260102")]}), "bad_response"),
])
def test_errors_are_classified_and_never_echo_the_key(resp, kind):
    c, _ = client([resp])
    with pytest.raises(KRXAPIError) as e:
        c.fetch("kospi_stock", "20260105")
    assert e.value.kind == kind and KEY not in str(e.value)


def test_server_errors_retried_with_limit_but_429_not():
    c, sleeps = client([Resp(503), Resp(503), Resp(payload={"OutBlock_1": [stock_row()]})])
    assert c.fetch("kospi_stock", "20260105").status == "ok" and sleeps == [1.0, 2.0]
    c, _ = client([Resp(500)] * 3)
    with pytest.raises(KRXAPIError, match="server"):
        c.fetch("kospi_stock", "20260105")
    c, sleeps = client([Resp(429)])
    with pytest.raises(KRXAPIError) as e:
        c.fetch("kospi_stock", "20260105")
    assert e.value.kind == "rate_limit" and sleeps == [] and len(c.session.calls) == 1
    c, _ = client([requests.ConnectionError("x")] * 3)
    with pytest.raises(KRXAPIError) as e:
        c.fetch("kospi_stock", "20260105")
    assert e.value.kind == "network" and KEY not in str(e.value)


def test_empty_answer_is_a_status_not_an_error():
    c, _ = client([Resp(payload={"OutBlock_1": []})])
    res = c.fetch("kospi_stock", "20260101")
    assert res.status == "empty" and res.frame.empty


def test_no_call_without_key_or_spec_confirmation_and_repr_hides_key():
    s = Session([])
    with pytest.raises(KRXAPIError) as e:
        KRXClient(api_key="", session=s, spec_confirmed=True).fetch("kospi_stock", "20260105")
    assert e.value.kind == "not_configured"
    with pytest.raises(KRXAPIError) as e:
        KRXClient(api_key=KEY, session=s).fetch("kospi_stock", "20260105")
    assert e.value.kind == "spec_unconfirmed" and s.calls == []
    assert KEY not in repr(KRXClient(api_key=KEY))
    with pytest.raises(ValueError):
        KRXClient(api_key=KEY, session=s, spec_confirmed=True).fetch("kospi_stock", "2026-01-05")


def test_short_code_accepts_both_forms():
    assert short_code("005930") == "005930" and short_code("KR7005930003") == "005930"
    assert short_code(" 0001A0 ") == "0001A0" and short_code("12345") is None and short_code(None) is None


# --------------------------------------------------------------------------- comparison

P = {"open": 1000, "high": 1100, "low": 990, "close": 1050, "volume": 5000}


def test_classify_match_and_mismatches():
    assert classify_pair(P, dict(P))["status"] == MATCH
    assert classify_pair(P, {**P, "volume": 4000})["status"] == VOLUME_MISMATCH
    assert classify_pair(P, {**P, "close": 1060})["status"] == PRICE_MISMATCH
    assert classify_pair(P, None)["status"] == MISSING_IN_KRX
    assert classify_pair(None, P)["status"] == MISSING_IN_PRIMARY
    assert classify_pair({**P, "open": 0, "high": 0, "low": 0}, P)["status"] == NOT_COMPARABLE
    assert classify_pair(P, {**P, "volume": 0})["status"] == NOT_COMPARABLE
    assert classify_pair(P, {**P, "open": np.nan})["status"] == NOT_COMPARABLE


def test_split_adjusted_primary_is_an_adjustment_basis_difference():
    raw = {"open": 2650000, "high": 2660000, "low": 2600000, "close": 2650000, "volume": 300}   # traded prices before 1:50
    adj = {"open": 53000, "high": 53200, "low": 52000, "close": 53000, "volume": 300}          # primary: prices /50, volume not
    r = classify_pair(adj, raw)
    assert r["status"] == ADJUSTED_BASIS and r["split_like_ratio"] == 50 and r["volume_status"] == "equal"
    assert r["factor"] == pytest.approx(1 / 50)
    both = classify_pair({**adj, "volume": 15000}, raw)
    assert both["volume_status"] == "adjusted_with_prices"
    rights = classify_pair({k: v * 0.93 if k != "volume" else v for k, v in P.items()}, P)
    assert rights["status"] == ADJUSTED_BASIS and rights["split_like_ratio"] is None


def test_compare_sample_keeps_missing_and_failed_days_apart():
    prim = pd.DataFrame([{"symbol": "A", "date": "2026-01-05", **P}, {"symbol": "B", "date": "2026-01-05", **P},
                         {"symbol": "A", "date": "2026-01-06", **P}, {"symbol": "A", "date": "2026-01-07", **P}])
    k = pd.DataFrame([{"symbol": "A", **P}])
    d = compare_sample(prim, {"20260105": k, "20260106": "empty", "20260107": "failed: server"},
                       [("A", "2026-01-05"), ("B", "2026-01-05"), ("A", "2026-01-06"), ("A", "2026-01-07"), ("C", "2026-01-05")])
    assert list(d["status"]) == [MATCH, MISSING_IN_KRX, KRX_DAY_EMPTY, REQUEST_FAILED, NOT_COMPARABLE]  # C: absent on both sides
    s = summarize(d)
    assert s["comparable"] == 1 and s["disagreements"] == 0 and s["passed"]


def test_candidate_needs_filing_and_krx_evidence_to_be_verified():
    detail = pd.DataFrame([
        {"symbol": "005930", "date": "2018-05-03", "status": ADJUSTED_BASIS, "factor": 0.02},
        {"symbol": "005930", "date": "2018-05-04", "status": MATCH, "factor": 1.0}])
    ev = krx_evidence_for_candidate(detail, "005930", "2018-05-03", "2018-05-04", 50.0)
    assert ev["krx_evidence"] == "consistent_with_split"
    assert krx_evidence_for_candidate(detail, "005930", "2018-05-03", "2018-05-04", 10.0)["krx_evidence"] == "not_consistent"
    assert krx_evidence_for_candidate(detail.iloc[:1], "005930", "2018-05-03", "2018-05-04", 50.0)["krx_evidence"] == "unavailable"
    base = {"symbol": "005930", "date": pd.Timestamp("2018-05-04"), "kind": "volume_level_break", "filing": None,
            "filing_kind": None, "filed": None, "days_before_event": None}
    rows = pd.DataFrame([{**base, "status": "confirmed_kind_match"}])
    assert candidate_verdicts(rows, {("005930", "2018-05-04"): ev}).iloc[0]["verdict"] == "verified"
    for st in ("no_filing_found", "no_corp_code", "filing_kind_mismatch", "filings_not_checked"):
        v = candidate_verdicts(pd.DataFrame([{**base, "status": st}]), {("005930", "2018-05-04"): ev}).iloc[0]
        assert v["verdict"] == "unverified" and st in v["verdict_reason"]
    no_krx = candidate_verdicts(rows, {}).iloc[0]
    assert no_krx["verdict"] == "unverified" and "not_requested" in no_krx["verdict_reason"]


# --------------------------------------------------------------------------- CLI (fake clients, TEST data)

class FakeKRX:
    """Serves KRX-shaped text rows built from the TEST raw files; for EVENT symbol it returns 'traded' prices
    (primary x RATIO) before EVENT_DAY, like an exchange quoting the pre-split price."""
    configured = True

    def __init__(self, data_dir, event_symbol=None, event_day=None, ratio=1):
        self.d, self.calls, self.requests = data_dir, 0, []
        self.event_symbol, self.event_day, self.ratio = event_symbol, event_day, ratio

    def fetch(self, service, bas_dd):
        self.calls += 1
        self.requests.append((service, bas_dd))
        svc = SERVICES[service]
        if svc.kind == "index":
            from app.providers.krx import parse_index_rows
            rows = [index_row(bas_dd, "코스닥")]
            f, _ = parse_index_rows(rows, svc)
            return KRXResult(service, bas_dd, "ok", rows, f)
        uni = pd.read_csv(self.d / "universe.csv", dtype={"symbol": str})
        rows = []
        for sym in uni.loc[uni["market"] == svc.market, "symbol"]:
            p = self.d / "raw" / f"{sym}.csv"
            if not p.exists():
                continue
            raw = pd.read_csv(p, dtype=str)
            hit = raw[raw["date"].str.replace("-", "") == bas_dd]
            if hit.empty:
                continue
            r = hit.iloc[0]
            m = self.ratio if sym == self.event_symbol and bas_dd < self.event_day.replace("-", "") else 1
            px = lambda x: f"{float(x) * m:,.0f}"  # noqa: E731
            rows.append(stock_row(sym, bas_dd, px(r["open"]), px(r["high"]), px(r["low"]), px(r["close"]), f"{float(r['volume']):,.0f}"))
        if not rows:
            return KRXResult(service, bas_dd, "empty")
        f, bad = parse_stock_rows(rows, svc)
        return KRXResult(service, bas_dd, "ok", rows, f, bad)


class FakeDart:
    configured = True

    def __init__(self, filings):
        self.filings, self.calls = filings, 0

    def corp_codes(self):
        self.calls += 1
        return {"000050": {"corp_code": "C50", "corp_name": "T"}}

    def disclosures(self, corp_code, bgn, end, max_pages=5):
        self.calls += 1
        return self.filings


class Exploding:
    configured = True

    def fetch(self, *a, **k):
        raise AssertionError("network used")

    def corp_codes(self):
        raise AssertionError("network used")


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    from app.audit.__main__ import main as audit_main
    base = tmp_path_factory.mktemp("krx")
    d = write_naver_raw(base / "data")
    assert audit_main(["--data", str(d), "--out", str(base / "audit")]) == 0
    return base, d


def _hashes(*dirs):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for d in dirs for p in sorted(d.rglob("*")) if p.is_file()}


def test_plan_only_uses_no_network_and_counts_requests_per_date(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx
    out = tmp_path / "plan"
    assert run(["--data", str(d), "--symbols", "000010", "000020", "--sample-symbols", "0", "--dates", "2023-03-02", "2023-03-03",
                "--n-dates", "0", "--out", str(out)], Exploding(), Exploding()) == 0
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert plan["request_count"] == 2                      # 2 dates x 1 market (both KOSDAQ), not 2 x 2 symbols
    assert {r["service"] for r in plan["requests"]} == {"kosdaq_stock"} and len(plan["pairs"]) == 4
    assert not (out / "comparison_detail.csv").exists()


def test_request_cap_and_spec_confirmation_are_enforced(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx
    args = ["--data", str(d), "--symbols", "000010", "--sample-symbols", "0", "--dates", "2023-03-02", "2023-03-03", "2023-03-06",
            "--n-dates", "0", "--with-index"]
    assert run(args + ["--max-requests", "5", "--out", str(tmp_path / "a")], Exploding(), Exploding()) == 2   # needs 6
    assert run(args + ["--max-requests", "6", "--live", "--out", str(tmp_path / "b")], Exploding(), Exploding()) == 2  # no --spec-confirmed
    assert run(["--data", str(d), "--candidate", "000050:2023-08-03", "--out", str(tmp_path / "c")], Exploding(), Exploding()) == 2  # needs --audit
    assert run(["--data", str(d), "--candidate", "000050:2020-01-01", "--audit", str(base / "audit"),
                "--out", str(tmp_path / "d")], Exploding(), Exploding()) == 2   # not an audit candidate


def test_live_run_with_fake_clients_verifies_only_with_both_sources(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx
    before = _hashes(d, base / "audit")
    krx = FakeKRX(d, event_symbol="000050", event_day="2023-08-03", ratio=10)
    filing = [{"symbol": "000050", "rcept_dt": "20230620", "report_nm": "주식분할결정", "corporate_action": "split", "rcept_no": "1"}]
    out = tmp_path / "live"
    rc = run(["--data", str(d), "--audit", str(base / "audit"), "--candidate", "000050:2023-08-03", "--symbols", "000010",
              "--sample-symbols", "0", "--dates", "2023-03-02", "--n-dates", "0", "--with-index", "--dart", "--live",
              "--spec-confirmed", "--out", str(out)], krx, FakeDart(filing))
    assert rc == 0
    st = json.loads((out / "krxcheck_status.json").read_text(encoding="utf-8"))
    plan = json.loads((out / "plan.json").read_text(encoding="utf-8"))
    assert krx.calls == plan["request_count"] == len(set(krx.requests))     # one call per (date, service)
    assert st["krx"]["by_status"].get(MATCH, 0) >= 2 and st["krx"]["by_status"].get(ADJUSTED_BASIS) == 1
    assert st["krx"]["disagreements"] == 0
    assert st["candidates"] == [{"symbol": "000050", "date": "2023-08-03", "verdict": "verified",
                                 "verdict_reason": "filing of the expected kind and KRX price step agree"}]
    assert KEY not in (out / "krxcheck_status.json").read_text(encoding="utf-8")
    assert _hashes(d, base / "audit") == before                              # inputs untouched


def test_candidate_stays_unverified_without_filings_or_corp_code(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx
    args = ["--data", str(d), "--audit", str(base / "audit"), "--candidate", "000050:2023-08-03", "--sample-symbols", "0",
            "--n-dates", "0", "--live", "--spec-confirmed"]
    krx = FakeKRX(d, event_symbol="000050", event_day="2023-08-03", ratio=10)
    assert run(args + ["--out", str(tmp_path / "nodart")], krx, Exploding()) == 0          # no --dart: no DART call
    st = json.loads((tmp_path / "nodart" / "krxcheck_status.json").read_text(encoding="utf-8"))
    assert st["candidates"][0]["verdict"] == "unverified" and "filings_not_checked" in st["candidates"][0]["verdict_reason"]

    class NoCode(FakeDart):
        def corp_codes(self):
            return {}
    assert run(args + ["--dart", "--out", str(tmp_path / "nocode")], FakeKRX(d, "000050", "2023-08-03", 10), NoCode([])) == 0
    st = json.loads((tmp_path / "nocode" / "krxcheck_status.json").read_text(encoding="utf-8"))
    assert st["candidates"][0]["verdict"] == "unverified" and "no_corp_code" in st["candidates"][0]["verdict_reason"]


def test_auth_failure_stops_further_requests(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx

    class Denied:
        configured, calls = True, 0

        def fetch(self, service, bas_dd):
            self.calls += 1
            raise KRXAPIError("auth", "kosdaq: respCode 401")
    den = Denied()
    rc = run(["--data", str(d), "--symbols", "000010", "--sample-symbols", "0", "--dates", "2023-03-02", "2023-03-03",
              "--n-dates", "0", "--live", "--spec-confirmed", "--out", str(tmp_path / "den")], den, Exploding())
    st = json.loads((tmp_path / "den" / "krxcheck_status.json").read_text(encoding="utf-8"))
    assert rc == 4 and den.calls == 1 and st["krx"]["stopped_on"] == "auth"
    assert st["krx"]["by_status"] == {REQUEST_FAILED: 2} and st["krx"]["passed"] is False


def test_missing_key_writes_reason_and_sends_nothing(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx

    class NoKey:
        configured = False
    rc = run(["--data", str(d), "--symbols", "000010", "--sample-symbols", "0", "--dates", "2023-03-02", "--n-dates", "0",
              "--live", "--spec-confirmed", "--out", str(tmp_path / "nokey")], NoKey(), Exploding())
    st = json.loads((tmp_path / "nokey" / "krxcheck_status.json").read_text(encoding="utf-8"))
    assert rc == 3 and st["krx"] == {"ran": False, "reason": "KRX_API_KEY not set"}


def test_key_never_reaches_logs(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    c, _ = client([Resp(payload={"OutBlock_1": [stock_row()]}), Resp(401)])
    c.fetch("kospi_stock", "20260105")
    with pytest.raises(KRXAPIError):
        c.fetch("kospi_stock", "20260105")
    assert KEY not in caplog.text


def test_one_market_failing_is_request_failed_not_missing(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx
    import pandas as pd
    uni = pd.read_csv(d / "universe.csv", dtype={"symbol": str})
    uni.loc[uni["symbol"] == "000010", "market"] = "KOSPI"            # put one symbol on the other market
    d2 = tmp_path / "data2"
    import shutil
    shutil.copytree(d, d2)
    uni.to_csv(d2 / "universe.csv", index=False)

    class HalfDown(FakeKRX):
        def fetch(self, service, bas_dd):
            if service == "kospi_stock":
                self.calls += 1
                raise KRXAPIError("server", "stk_bydd_trd: HTTP 503")
            return super().fetch(service, bas_dd)
    krx = HalfDown(d2)
    assert run(["--data", str(d2), "--symbols", "000010", "000020", "--sample-symbols", "0", "--dates", "2023-03-02",
                "--n-dates", "0", "--live", "--spec-confirmed", "--out", str(tmp_path / "half")], krx, Exploding()) == 0
    det = pd.read_csv(tmp_path / "half" / "comparison_detail.csv", dtype={"symbol": str}).set_index("symbol")
    assert det.at["000010", "status"] == REQUEST_FAILED and det.at["000020", "status"] == MATCH


def test_http_attempts_count_retries():
    c, _ = client([Resp(503), Resp(payload={"OutBlock_1": [stock_row()]})])
    c.fetch("kospi_stock", "20260105")
    assert c.calls == 1 and c.http_attempts == 2 == len(c.session.calls)


def test_unparsable_bas_dd_is_rejected():
    c, _ = client([Resp(payload={"OutBlock_1": [stock_row(d="2026-01-05")]})])
    with pytest.raises(KRXAPIError) as e:
        c.fetch("kospi_stock", "20260105")
    assert e.value.kind == "bad_response"


def test_krx_adapter_is_read_only_and_separate_from_kis():
    import re
    from pathlib import Path
    src = Path("app/providers/krx.py").read_text(encoding="utf-8")
    literals = re.findall(r'"([^"\n]*)"', src)
    assert not any(x in lit.lower() for lit in literals for x in ("order", "/trading/", "koreainvestment"))
    assert {s.url.split("/svc/apis/")[0] for s in SERVICES.values()} == {"https://data-dbg.krx.co.kr"}
    for mod in ("app/krxcheck/__main__.py", "app/research/krx_crosscheck.py"):
        assert "providers.kis" not in Path(mod).read_text(encoding="utf-8")
    assert "krx" not in Path("app/crosscheck/__main__.py").read_text(encoding="utf-8").lower()


def test_dart_request_cap(fx, tmp_path):
    from app.krxcheck.__main__ import run
    base, d = fx
    args = ["--data", str(d), "--audit", str(base / "audit"), "--candidate", "000050:2023-08-03", "--sample-symbols", "0",
            "--n-dates", "0", "--dart"]
    assert run(args + ["--dart-max-pages", "5", "--dart-max-requests", "5", "--out", str(tmp_path / "cap")], Exploding(), Exploding()) == 2
    assert run(args + ["--out", str(tmp_path / "ok")], Exploding(), Exploding()) == 0          # 1 + 1 x 2 = 3 <= 5
    plan = json.loads((tmp_path / "ok" / "plan.json").read_text(encoding="utf-8"))
    assert plan["dart_request_upper_bound"] == 3
