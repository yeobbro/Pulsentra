"""KIS / OpenDART adapters against mocked responses: bounded retries, schema validation, read-only paths, no secrets."""
import re
from pathlib import Path

import pytest
import requests

from app.providers.dart import DartAPIError, DartClient, classify_corporate_action
from app.providers.http import ProviderError, request_with_retry
from app.providers.kis import ALLOWED_PATHS, KISClient, KisAPIError, parse_daily_rows


class Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content = status, payload, content

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Session:
    """Replays a script of responses or exceptions and records each call (url, params, headers)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def _next(self, url, kw):
        self.calls.append((url, kw.get("params"), kw.get("headers"), kw.get("timeout")))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next(url, kw)

    def post(self, url, **kw):
        return self._next(url, kw)


TOKEN = Resp(payload={"access_token": "tok", "expires_in": 86400})


def _row(d, c=10000, v=1000):
    return {"stck_bsop_date": d, "stck_oprc": str(c), "stck_hgpr": str(c + 100), "stck_lwpr": str(c - 100),
            "stck_clpr": str(c), "acml_vol": str(v), "acml_tr_pbmn": str(c * v)}


def kis(script, **kw):
    sleeps = []
    c = KISClient(session=Session(script), app_key="key-value", app_secret="secret-value", min_interval=0,
                  sleep=sleeps.append, **kw)
    return c, sleeps


# --------------------------------------------------------------------------- shared retry helper

def test_retry_is_bounded_with_exponential_backoff():
    sleeps = []
    s = Session([requests.ConnectionError("x")] * 4)
    with pytest.raises(ProviderError, match="4 attempts"):
        request_with_retry(s, "get", "https://h/p", timeout=1, max_retries=3, backoff=0.5, sleep=sleeps.append)
    assert len(s.calls) == 4 and sleeps == [0.5, 1.0, 2.0]


def test_retry_only_transient_statuses():
    s = Session([Resp(503), Resp(200, {})])
    assert request_with_retry(s, "get", "u", timeout=1, sleep=lambda _: None).status_code == 200
    s = Session([Resp(401)])
    assert request_with_retry(s, "get", "u", timeout=1, sleep=lambda _: None).status_code == 401
    assert len(s.calls) == 1                     # auth errors are not retried
    s = Session([Resp(500)] * 3)
    assert request_with_retry(s, "get", "u", timeout=1, max_retries=2, sleep=lambda _: None).status_code == 500
    with pytest.raises(ValueError):
        request_with_retry(s, "get", "u", timeout=1, max_retries=-1)


def test_timeout_is_always_passed():
    s = Session([Resp(200, {})])
    request_with_retry(s, "get", "u", timeout=7.5)
    assert s.calls[0][3] == 7.5


# --------------------------------------------------------------------------- KIS

def test_kis_daily_range_parses_validates_and_windows():
    script = [TOKEN] + [Resp(payload={"rt_cd": "0", "output2": [_row("20260105"), _row("20260102"), {}]})] + \
             [Resp(payload={"rt_cd": "0", "output2": []})] * 5
    c, _ = kis(script)
    df = c.daily_bars_range("005930", "2025-06-01", "2026-01-05")
    assert list(df["date"].dt.strftime("%Y%m%d")) == ["20260102", "20260105"]
    assert (df["source"] == "kis_eod").all() and df["adjusted"].all()
    params = [p for _, p, _, _ in c.session.calls if p]
    assert all(p["FID_ORG_ADJ_PRC"] == "0" and p["FID_PERIOD_DIV_CODE"] == "D" for p in params)
    spans = [(int(p["FID_INPUT_DATE_2"]) , int(p["FID_INPUT_DATE_1"])) for p in params]
    import pandas as pd
    for hi, lo in spans:   # each window holds fewer weekdays than the 100-row answer limit
        assert len(pd.bdate_range(pd.Timestamp(str(lo)), pd.Timestamp(str(hi)))) < 100


def test_kis_rate_limit_code_is_retried_then_succeeds():
    script = [TOKEN, Resp(payload={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}),
              Resp(payload={"rt_cd": "0", "output": {"stck_prpr": "70000", "prdy_ctrt": "1.2", "acml_vol": "10", "acml_tr_pbmn": "700000"}})]
    c, sleeps = kis(script)
    q = c.current_quote("005930")
    assert q["price"] == 70000 and sleeps == [1.0]


def test_kis_rate_limit_gives_up_after_max_retries():
    limited = Resp(payload={"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "limit"})
    c, sleeps = kis([TOKEN] + [limited] * 3, max_retries=2)
    with pytest.raises(KisAPIError):
        c.current_quote("005930")
    assert len(sleeps) == 2


def test_kis_schema_errors_raise():
    with pytest.raises(KisAPIError, match="missing fields"):
        parse_daily_rows([{"stck_bsop_date": "20260105", "stck_clpr": "1"}], "005930")
    with pytest.raises(KisAPIError, match="not a list"):
        parse_daily_rows({"a": 1}, "005930")
    df, dropped = parse_daily_rows([_row("20260105"), {**_row("20260106"), "stck_clpr": "N/A"}], "005930")
    assert len(df) == 1 and dropped == 1
    c, _ = kis([TOKEN, Resp(payload=ValueError("not json"))])
    with pytest.raises(KisAPIError, match="non-JSON"):
        c.current_quote("005930")


def test_kis_errors_never_contain_credentials():
    c, _ = kis([Resp(403)])
    with pytest.raises(KisAPIError) as e:
        c.current_quote("005930")
    assert "secret-value" not in str(e.value) and "key-value" not in str(e.value)
    c, _ = kis([TOKEN, Resp(500), Resp(500), Resp(500), Resp(500)])
    with pytest.raises(KisAPIError) as e:
        c.current_quote("005930")
    assert "secret" not in str(e.value) and "tok" not in str(e.value).replace("token", "")


def test_kis_unconfigured_makes_no_call():
    c = KISClient(session=Session([]), app_key="", app_secret="")
    with pytest.raises(KisAPIError, match="설정되지"):
        c.daily_bars_range("005930", "2026-01-01", "2026-01-31")
    assert c.session.calls == []


def test_kis_adapter_is_read_only():
    src = Path("app/providers/kis.py").read_text(encoding="utf-8")
    paths = set(re.findall(r'"(/(?:uapi|oauth2)[^"]*)"', src))
    assert paths <= set(ALLOWED_PATHS)
    literals = re.findall(r'"([^"\n]*)"', src)
    assert not any("/trading/" in x or "order" in x.lower() for x in literals)
    c, _ = kis([])
    with pytest.raises(KisAPIError, match="not allowed"):
        c._send("post", "/uapi/domestic-stock/v1/trading/order-cash")


# --------------------------------------------------------------------------- OpenDART

def _page(items, total_page=1, status="000"):
    return Resp(payload={"status": status, "message": "정상", "page_no": 1, "total_page": total_page, "list": items})


def _item(nm, dt="20260105"):
    return {"corp_code": "00126380", "corp_name": "테스트", "stock_code": "005930", "report_nm": nm, "rcept_no": "1", "rcept_dt": dt}


def test_dart_retries_server_errors_and_validates_rows():
    sleeps = []
    s = Session([Resp(502), _page([_item("주식분할결정")])])
    rows = DartClient(api_key="k", session=s, min_interval=0, sleep=sleeps.append).disclosures("00126380", "20260101", "20260131")
    assert rows[0]["corporate_action"] == "split" and len(s.calls) == 2 and sleeps == [1.0]
    bad = Session([_page([{**_item("x"), "rcept_dt": "2026-01-05"}])])
    with pytest.raises(DartAPIError, match="rcept_dt"):
        DartClient(api_key="k", session=bad, min_interval=0).disclosures("c", "a", "b")


def test_dart_status_codes():
    assert DartClient(api_key="k", session=Session([_page([], status="013")]), min_interval=0).disclosures("c", "a", "b") == []
    with pytest.raises(DartAPIError, match="한도"):
        DartClient(api_key="k", session=Session([_page([], status="020")]), min_interval=0).disclosures("c", "a", "b")
    with pytest.raises(DartAPIError, match="JSON"):
        DartClient(api_key="k", session=Session([Resp(payload=ValueError())]), min_interval=0).disclosures("c", "a", "b")


def test_dart_pagination_is_bounded():
    s = Session([_page([_item("a")], total_page=99)] * 3)
    rows = DartClient(api_key="k", session=s, min_interval=0).disclosures("c", "a", "b", max_pages=3)
    assert len(rows) == 3 and len(s.calls) == 3


def test_dart_unconfigured_makes_no_call():
    s = Session([])
    with pytest.raises(DartAPIError):
        DartClient(api_key="", session=s).disclosures("c", "a", "b")
    assert s.calls == []


@pytest.mark.parametrize("name,kind", [("주식분할결정", "split"), ("[기재정정]주식병합결정", "merge"), ("무상증자결정", "bonus_issue"),
                                       ("주요사항보고서(유무상증자결정)", "bonus_issue"), ("주요사항보고서(감자결정)", "capital_reduction"),
                                       ("주요사항보고서(유상증자결정)", "rights_issue"), ("회사합병결정", "merger"),
                                       ("감사보고서제출", None), ("분기보고서 (2026.09)", None)])
def test_corporate_action_classification(name, kind):
    assert classify_corporate_action(name) == kind


def test_kis_daily_event_fields_are_kept_verbatim():
    rows = [{**_row("20260105"), "flng_cls_code": "00", "prtt_rate": "0.00", "mod_yn": "N", "revl_issu_reas": ""},
            {**_row("20260106"), "flng_cls_code": "02", "prtt_rate": "50.00", "mod_yn": "Y", "revl_issu_reas": "03"}]
    df, _ = parse_daily_rows(rows, "005930")
    assert list(df["kis_split_ratio"]) == ["0.00", "50.00"] and list(df["kis_modified"]) == ["N", "Y"]
    df2, _ = parse_daily_rows([_row("20260105")], "005930")          # older answers without the fields still parse
    assert "kis_split_ratio" not in df2


def test_dart_keeps_the_rm_field():
    s = Session([_page([{**_item("주식분할결정"), "rm": "유정"}, _item("[기재정정]주식분할결정")])])
    rows = DartClient(api_key="k", session=s, min_interval=0).disclosures("c", "a", "b")
    assert rows[0]["rm"] == "유정" and rows[1]["rm"] is None
