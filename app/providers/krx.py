"""KRX Open API (openapi.krx.co.kr) read-only adapter: daily index series and daily stock trading info.

SPECIFICATION STATUS — read before a live call
  Written without access to the official pages, from two public client packages (krx-openapi 0.2.2, pykrx-openapi
  0.1.1, read as text only). On 2026-10-24 the official service pages were read (openapi.krx.co.kr, 서비스 이용 →
  각 API 상세: the page embeds the service definition, "최근 수정일 2026/01/16"). For the four SERVICES below the
  official definition matches: path apis/{group}/{service}, header AUTH_KEY, input basDd (YYYYMMDD), and the output
  fields of OutBlock_1 in this order. It also shows that ISU_CD is the short code (SUBSTR(ISU_SRT_CD, 2)), that
  stk_bydd_trd covers SECUGRP ST/DR/MF/RT/SC/IF/FS (so preferred shares, REITs and funds are included), that rows
  are filtered on AGG_BAS_TP_CD = '0' (meaning of the code not documented on the page), and that a date is served
  only once it is in the past and, for the latest session, from 08:00 the next morning. Rate limit in the terms:
  10,000 requests per key per day; use is non-commercial only. Error codes and HTTP statuses are NOT documented
  there and stay as observed. Live calls still need spec_confirmed=True (CLI --spec-confirmed): the person running
  them confirms their own approved services. Responses are validated strictly, so a different schema fails loudly.

  Real service base : https://data-dbg.krx.co.kr/svc/apis/{group}/{service}
  Sample-only base  : https://data-dbg.krx.co.kr/svc/sample/apis/...  (public sample key, fixed date — never used here)
  Request           : GET, query basDd=YYYYMMDD, header AUTH_KEY=<key>
  Answer            : JSON {"OutBlock_1": [ {field: "string", ...}, ... ]} — one row per instrument for that date.
                      Errors may come as HTTP 200 with respCode/respMsg in the body (401 = bad key OR service not
                      approved), or as HTTP 403 (wrong key/URL, sample path with a real key, plain http) / 429 (limit).

Every number arrives as text. Normalised columns are parsed separately from the raw rows, which are kept as-is;
"-" or "" become missing, never 0, and nothing absent from the answer is inferred.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests

from app.providers.http import ProviderError, request_with_retry
from app.settings import KRX_API_KEY, KRX_MIN_INTERVAL_SECONDS, KRX_TIMEOUT_SECONDS

BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
SAMPLE_BASE_URL = "https://data-dbg.krx.co.kr/svc/sample/apis"   # listed so it can be refused, not used
SPEC_SOURCE = ("official openapi.krx.co.kr service definitions read 2026-10-24 (last modified 2026/01/16): path, AUTH_KEY "
               "header, basDd and output fields match for kospi/kosdaq index and stock services; error codes not documented "
               "officially (as observed). Originally written from krx-openapi 0.2.2 / pykrx-openapi 0.1.1")


@dataclass(frozen=True)
class Service:
    key: str
    group: str
    path: str
    name_ko: str
    kind: str                      # "index" | "stock"
    market: str                    # KOSPI | KOSDAQ
    fields: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.group}/{self.path}"


INDEX_FIELDS = ("BAS_DD", "IDX_CLSS", "IDX_NM", "CLSPRC_IDX", "CMPPREVDD_IDX", "FLUC_RT", "OPNPRC_IDX", "HGPRC_IDX",
                "LWPRC_IDX", "ACC_TRDVOL", "ACC_TRDVAL", "MKTCAP")
STOCK_FIELDS = ("BAS_DD", "ISU_CD", "ISU_NM", "MKT_NM", "SECT_TP_NM", "TDD_CLSPRC", "CMPPREVDD_PRC", "FLUC_RT",
                "TDD_OPNPRC", "TDD_HGPRC", "TDD_LWPRC", "ACC_TRDVOL", "ACC_TRDVAL", "MKTCAP", "LIST_SHRS")

SERVICES: dict[str, Service] = {s.key: s for s in (
    Service("kospi_index", "idx", "kospi_dd_trd", "KOSPI 시리즈 일별시세정보", "index", "KOSPI", INDEX_FIELDS),
    Service("kosdaq_index", "idx", "kosdaq_dd_trd", "KOSDAQ 시리즈 일별시세정보", "index", "KOSDAQ", INDEX_FIELDS),
    Service("kospi_stock", "sto", "stk_bydd_trd", "유가증권 일별매매정보", "stock", "KOSPI", STOCK_FIELDS),
    Service("kosdaq_stock", "sto", "ksq_bydd_trd", "코스닥 일별매매정보", "stock", "KOSDAQ", STOCK_FIELDS),
)}
STOCK_SERVICE_FOR_MARKET = {"KOSPI": "kospi_stock", "KOSDAQ": "kosdaq_stock"}
INDEX_SERVICE_FOR_MARKET = {"KOSPI": "kospi_index", "KOSDAQ": "kosdaq_index"}


class KRXAPIError(RuntimeError):
    """kind: not_configured | spec_unconfirmed | auth | forbidden | rate_limit | server | network | redirect | bad_response"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"KRX {kind}: {message}")
        self.kind = kind


@dataclass
class KRXResult:
    service: str
    bas_dd: str
    status: str                    # "ok" | "empty" (valid answer with no rows: holiday, or no data for that date)
    raw: list[dict] = field(default_factory=list)
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)
    unparsed_values: int = 0       # cells that were present but not a number (left missing)


def _num(value: Any) -> float:
    """Text from KRX -> float. '', '-' and unparsable text -> NaN (never 0)."""
    if value is None:
        return np.nan
    s = str(value).strip().replace(",", "")
    if s in ("", "-"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def short_code(isu_cd: Any) -> str | None:
    """Six-character KRX short code from ISU_CD, which may be the short code itself or a 12-character ISIN
    (KR7005930003 -> 005930). The official definition returns the short code (observed too); ISINs stay accepted."""
    s = str(isu_cd or "").strip().upper()
    if re.fullmatch(r"[0-9A-Z]{6}", s):
        return s
    if re.fullmatch(r"KR[0-9A-Z]{10}", s):
        return s[3:9]
    return None


def _validate_rows(rows: Any, svc: Service) -> list[dict]:
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise KRXAPIError("bad_response", f"{svc.path}: OutBlock_1 is not a list of objects")
    if rows:
        missing = [f for f in svc.fields if f not in rows[0]]
        if missing:
            raise KRXAPIError("bad_response", f"{svc.path}: answer lacks documented fields {missing}")
    return rows


def parse_index_rows(rows: list[dict], svc: Service) -> tuple[pd.DataFrame, int]:
    df = pd.DataFrame(rows)
    out = pd.DataFrame({"date": pd.to_datetime(df["BAS_DD"].astype(str), format="%Y%m%d", errors="coerce"),
                        "market": svc.market, "index_class": df["IDX_CLSS"], "index_name": df["IDX_NM"]})
    cols = {"close": "CLSPRC_IDX", "change": "CMPPREVDD_IDX", "change_pct": "FLUC_RT", "open": "OPNPRC_IDX",
            "high": "HGPRC_IDX", "low": "LWPRC_IDX", "volume": "ACC_TRDVOL", "turnover": "ACC_TRDVAL", "market_cap": "MKTCAP"}
    bad = 0
    for k, f in cols.items():
        out[k] = df[f].map(_num)
        bad += int((out[k].isna() & ~df[f].astype(str).str.strip().isin(["", "-"])).sum())
    out["source"] = "krx_openapi"
    return out, bad


def parse_stock_rows(rows: list[dict], svc: Service) -> tuple[pd.DataFrame, int]:
    df = pd.DataFrame(rows)
    out = pd.DataFrame({"date": pd.to_datetime(df["BAS_DD"].astype(str), format="%Y%m%d", errors="coerce"),
                        "symbol": df["ISU_CD"].map(short_code), "isu_cd_raw": df["ISU_CD"], "name": df["ISU_NM"],
                        "market": svc.market, "market_name_raw": df["MKT_NM"], "section_raw": df["SECT_TP_NM"]})
    cols = {"open": "TDD_OPNPRC", "high": "TDD_HGPRC", "low": "TDD_LWPRC", "close": "TDD_CLSPRC", "change": "CMPPREVDD_PRC",
            "change_pct": "FLUC_RT", "volume": "ACC_TRDVOL", "turnover": "ACC_TRDVAL", "market_cap": "MKTCAP",
            "listed_shares": "LIST_SHRS"}
    bad = 0
    for k, f in cols.items():
        out[k] = df[f].map(_num)
        bad += int((out[k].isna() & ~df[f].astype(str).str.strip().isin(["", "-"])).sum())
    out["source"] = "krx_openapi"
    # Not split-adjusted: the official service definition returns TDD_* prices from TBMD_BYDD_ISU_TRDPRC. In the live
    # answers fetched 2026-10-24 for 2025-02-27 and 2025-06-12, 217 of the 218 rows differing from the adjusted Naver
    # series differed by one constant factor (the remaining row by at most 1 KRW per field).
    out["adjusted"] = False
    return out, bad


class KRXClient:
    """Read-only. One call returns the whole market for one date, so callers should fetch per date, not per symbol."""

    def __init__(self, api_key: str | None = None, session: Any = None, spec_confirmed: bool = False,
                 max_retries: int = 2, backoff: float = 1.0, min_interval: float | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float | None = None) -> None:
        self._key = (KRX_API_KEY if api_key is None else api_key).strip()
        self.session = session or requests
        self.spec_confirmed = spec_confirmed
        self.max_retries, self.backoff, self._sleep = max_retries, backoff, sleep
        self.min_interval = KRX_MIN_INTERVAL_SECONDS if min_interval is None else min_interval
        self.timeout = KRX_TIMEOUT_SECONDS if timeout is None else timeout
        self._last = 0.0
        self.calls = 0                  # fetch() calls that reached the network step (count only)
        self.http_attempts = 0          # HTTP requests actually sent, retries included (what a quota counts)

    def __repr__(self) -> str:          # never shows the key
        return f"KRXClient(configured={self.configured}, spec_confirmed={self.spec_confirmed})"

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def fetch(self, service: str, bas_dd: str) -> KRXResult:
        svc = SERVICES.get(service)
        if svc is None:
            raise ValueError(f"unknown KRX service {service!r}; known: {sorted(SERVICES)}")
        if not re.fullmatch(r"\d{8}", str(bas_dd)):
            raise ValueError("bas_dd must be YYYYMMDD")
        if not self.configured:
            raise KRXAPIError("not_configured", "KRX_API_KEY is not set")
        if not self.spec_confirmed:
            raise KRXAPIError("spec_unconfirmed", "compare SERVICES with the official API spec, then pass spec_confirmed=True")
        if svc.url.startswith(SAMPLE_BASE_URL):
            raise KRXAPIError("bad_response", "sample endpoint refused")
        self._throttle()
        self.calls += 1
        client = self

        class _Counting:                # counts every attempt request_with_retry makes, retries included
            @staticmethod
            def get(url, **kw):
                client.http_attempts += 1
                return client.session.get(url, **kw)
        try:
            res = request_with_retry(_Counting, "get", svc.url, timeout=self.timeout, max_retries=self.max_retries,
                                     backoff=self.backoff, sleep=self._sleep, label=f"KRX {svc.path}",
                                     retry_status=(500, 502, 503, 504),   # 429 is a daily quota here: retrying only burns calls
                                     params={"basDd": bas_dd}, headers={"AUTH_KEY": self._key, "Accept": "application/json"},
                                     allow_redirects=False)   # a redirect would carry the AUTH_KEY header to another URL
        except ProviderError as exc:
            raise KRXAPIError("network", f"{svc.path}: {exc}") from exc
        code = res.status_code
        if 300 <= code < 400:
            raise KRXAPIError("redirect", f"{svc.path}: HTTP {code} redirect refused (check the URL scheme/path)")
        if code == 401:
            raise KRXAPIError("auth", f"{svc.path}: HTTP 401 — key rejected or service not approved")
        if code == 403:
            raise KRXAPIError("forbidden", f"{svc.path}: HTTP 403 — wrong key/URL or service not approved")
        if code == 429:
            raise KRXAPIError("rate_limit", f"{svc.path}: HTTP 429 — call limit reached")
        if code >= 500:
            raise KRXAPIError("server", f"{svc.path}: HTTP {code}")
        if code >= 400:
            raise KRXAPIError("bad_response", f"{svc.path}: HTTP {code}")
        try:
            body = res.json()
        except ValueError as exc:
            raise KRXAPIError("bad_response", f"{svc.path}: body is not JSON") from exc
        if not isinstance(body, dict):
            raise KRXAPIError("bad_response", f"{svc.path}: body is not a JSON object")
        resp_code = str(body.get("respCode", "") or "").strip()
        if resp_code:
            msg = str(body.get("respMsg", "") or "")[:120]
            kind = "auth" if resp_code == "401" else ("rate_limit" if resp_code == "429" else "bad_response")
            raise KRXAPIError(kind, f"{svc.path}: respCode {resp_code} {msg}".strip())
        if "OutBlock_1" not in body:
            raise KRXAPIError("bad_response", f"{svc.path}: no OutBlock_1 in answer (keys: {sorted(body)[:5]})")
        rows = _validate_rows(body["OutBlock_1"], svc)
        if not rows:
            return KRXResult(svc.key, bas_dd, "empty")
        frame, bad = (parse_index_rows if svc.kind == "index" else parse_stock_rows)(rows, svc)
        if frame["date"].isna().any() or (frame["date"].dt.strftime("%Y%m%d") != bas_dd).any():
            raise KRXAPIError("bad_response", f"{svc.path}: BAS_DD missing, unparsable or other than basDd {bas_dd}")
        return KRXResult(svc.key, bas_dd, "ok", raw=rows, frame=frame, unparsed_values=bad)
