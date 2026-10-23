"""OpenDART (금융감독원 전자공시) read-only adapter.

Endpoints and fields follow the official developer guide (opendart.fss.or.kr, checked 2026-10):
  * GET https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key=…  -> ZIP with CORPCODE.xml
    (list/corp_code, corp_name, corp_eng_name, stock_code, modify_date)
  * GET https://opendart.fss.or.kr/api/list.json  (crtfc_key, corp_code, bgn_de, end_de, page_no, page_count ≤ 100)
    -> status ("000" = OK), message, page_no, page_count, total_count, total_page,
       list[corp_cls, corp_name, corp_code, stock_code, report_nm, rcept_no, flr_nm, rcept_dt, rm]
This adapter has only been tested against mocked responses; the first live call should be checked.
`rcept_dt` (접수일) is the earliest date a disclosure can be used in a backtest or model.
"""
from __future__ import annotations

import io
import time
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

import requests

from app.settings import DART_API_KEY, DART_TIMEOUT_SECONDS

BASE = "https://opendart.fss.or.kr/api"

# Report-name keywords -> risk flag. Dilution flags matter for the scanner's negative evidence.
RISK_KEYWORDS: list[tuple[str, str]] = [
    ("유상증자", "dilution:유상증자"), ("전환사채", "dilution:전환사채"), ("신주인수권부사채", "dilution:신주인수권부사채"),
    ("교환사채", "dilution:교환사채"), ("주식매수선택권", "dilution:주식매수선택권"), ("감자", "capital:감자"),
    ("상장폐지", "listing:상장폐지"), ("관리종목", "listing:관리종목"), ("불성실공시", "governance:불성실공시"),
    ("횡령", "governance:횡령·배임"), ("배임", "governance:횡령·배임"), ("소송", "legal:소송"),
    ("영업정지", "operation:영업정지"), ("회생", "credit:회생절차"),
]


class DartAPIError(RuntimeError):
    pass


def classify(report_nm: str) -> str:
    flags = []
    for kw, flag in RISK_KEYWORDS:
        if kw in (report_nm or "") and flag not in flags:
            flags.append(flag)
    return ",".join(flags)


class DartClient:
    def __init__(self, api_key: str | None = None, session: Any = None, min_interval: float = 0.2) -> None:
        self.api_key = (api_key if api_key is not None else DART_API_KEY).strip()
        self.session = session or requests
        self.min_interval = min_interval
        self._last = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _wait(self) -> None:
        delay = self.min_interval - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def _require_key(self) -> None:
        if not self.configured:
            raise DartAPIError("DART_API_KEY 환경변수가 설정되지 않았습니다.")

    def corp_codes(self) -> dict[str, dict]:
        """stock_code (6 digits) -> {corp_code, corp_name}. Unlisted companies are skipped."""
        self._require_key()
        self._wait()
        res = self.session.get(f"{BASE}/corpCode.xml", params={"crtfc_key": self.api_key}, timeout=DART_TIMEOUT_SECONDS)
        if res.status_code >= 400:
            raise DartAPIError(f"OpenDART corpCode 요청 실패 (HTTP {res.status_code})")
        try:
            with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
                name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
                root = ET.fromstring(zf.read(name))
        except (zipfile.BadZipFile, StopIteration) as exc:
            # An error answer comes back as XML/JSON instead of a ZIP.
            raise DartAPIError("OpenDART corpCode 응답이 ZIP이 아닙니다 (인증키·이용 한도 확인)") from exc
        out = {}
        for item in root.iter("list"):
            stock = (item.findtext("stock_code") or "").strip()
            if stock:
                out[stock] = {"corp_code": (item.findtext("corp_code") or "").strip(), "corp_name": (item.findtext("corp_name") or "").strip()}
        return out

    def disclosures(self, corp_code: str, bgn_de: str, end_de: str, max_pages: int = 5) -> list[dict]:
        self._require_key()
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            self._wait()
            res = self.session.get(f"{BASE}/list.json", params={"crtfc_key": self.api_key, "corp_code": corp_code,
                                                                "bgn_de": bgn_de, "end_de": end_de, "page_no": page, "page_count": 100},
                                   timeout=DART_TIMEOUT_SECONDS)
            if res.status_code >= 400:
                raise DartAPIError(f"OpenDART list 요청 실패 (HTTP {res.status_code})")
            body = res.json()
            status = str(body.get("status", ""))
            if status == "013":  # documented as "no data"
                break
            if status != "000":
                raise DartAPIError(f"OpenDART 오류 {status}: {body.get('message', '')}")
            for d in body.get("list") or []:
                rows.append({"rcept_no": d.get("rcept_no"), "corp_code": d.get("corp_code"), "corp_name": d.get("corp_name"),
                             "symbol": (d.get("stock_code") or "").strip() or None, "report_nm": d.get("report_nm"),
                             "rcept_dt": d.get("rcept_dt"), "risk_flags": classify(d.get("report_nm", ""))})
            if page >= int(body.get("total_page") or 1):
                break
        return rows
