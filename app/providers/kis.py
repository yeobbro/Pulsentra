from __future__ import annotations
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Any, Callable
import time
import requests
import pandas as pd
from app.providers.http import ProviderError, request_with_retry
from app.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL, KIS_MIN_INTERVAL_SECONDS, KIS_PAPER, KIS_TIMEOUT_SECONDS

# Read-only quotation endpoints this adapter may call. Nothing under /trading/ (orders, balances) is ever used;
# tests/test_providers.py fails if another path appears in this module.
ALLOWED_PATHS = ("/oauth2/tokenP", "/uapi/domestic-stock/v1/quotations/inquire-price",
                 "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice")
DAILY_FIELDS = {"stck_bsop_date": "date", "stck_oprc": "open", "stck_hgpr": "high", "stck_lwpr": "low",
                "stck_clpr": "close", "acml_vol": "volume", "acml_tr_pbmn": "turnover"}
# Per-day corporate-action markers in the same answer (names from KIS's official sample code,
# koreainvestment/open-trading-api chk_inquire_daily_itemchartprice.py). Kept verbatim as text: the code values
# are not interpreted here until a live answer has been inspected.
DAILY_EVENT_FIELDS = {"flng_cls_code": "kis_lock_code", "prtt_rate": "kis_split_ratio", "mod_yn": "kis_modified",
                      "revl_issu_reas": "kis_revaluation_reason"}
RATE_LIMIT_CODES = ("EGW00201",)      # KIS: per-second call limit exceeded (retried, bounded)
DAILY_CHUNK_DAYS = 130                # 131 calendar days hold at most 94 weekdays < the 100-row answer limit


class KisAPIError(RuntimeError):
    pass


def parse_daily_rows(rows: Any, symbol: str) -> tuple[pd.DataFrame, int]:
    """Validate and normalise FHKST03010100 `output2` rows. Returns (bars, rows dropped as unparsable).

    Empty dict rows (KIS pads some answers) are ignored; a row missing a documented field fails the whole response,
    because a silently different schema would corrupt a comparison.
    """
    cols = ["market", "symbol", "date", "open", "high", "low", "close", "volume", "turnover", "source", "adjusted", "exchange"]
    if rows is None:
        rows = []
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise KisAPIError("Unexpected KIS daily response: output2 is not a list of objects")
    rows = [r for r in rows if any(str(v).strip() for v in r.values())]
    if not rows:
        return pd.DataFrame(columns=cols), 0
    df = pd.DataFrame(rows)
    missing = set(DAILY_FIELDS) - set(df.columns)
    if missing:
        raise KisAPIError(f"Unexpected KIS daily response; missing fields: {sorted(missing)}")
    events = [c for c in DAILY_EVENT_FIELDS if c in df.columns]
    df = df.rename(columns={**DAILY_FIELDS, **DAILY_EVENT_FIELDS})[list(DAILY_FIELDS.values()) + [DAILY_EVENT_FIELDS[c] for c in events]]
    for c in events:
        df[DAILY_EVENT_FIELDS[c]] = df[DAILY_EVENT_FIELDS[c]].astype(str).str.strip()
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    n0 = len(df)
    df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"]).drop_duplicates(subset=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["market"], df["symbol"], df["source"], df["exchange"] = "KR", symbol, "kis_eod", "KRX"
    df["adjusted"] = True  # FID_ORG_ADJ_PRC = "0" (수정주가)
    return df[cols + [DAILY_EVENT_FIELDS[c] for c in events]], n0 - len(df)


class KISClient:
    """Small read-only adapter for KIS Open API. No order endpoints are implemented.

    Daily bars use 국내주식기간별시세(FHKST03010100) with FID_ORG_ADJ_PRC="0", which KIS documents as
    수정주가 (adjusted); rows are stored with adjusted=True. One call returns at most 100 bars, so
    the range is requested in windows. Calls are spaced by KIS_MIN_INTERVAL_SECONDS because KIS
    applies per-second limits (EGW00201); check the official portal for current limits.
    """
    def __init__(self, session: Any = None, max_retries: int = 3, backoff: float = 1.0, min_interval: float | None = None,
                 sleep: Callable[[float], None] = time.sleep, app_key: str | None = None, app_secret: str | None = None) -> None:
        self.app_key = KIS_APP_KEY if app_key is None else app_key
        self.app_secret = KIS_APP_SECRET if app_secret is None else app_secret
        self.base_url = "https://openapivts.koreainvestment.com:29443" if KIS_PAPER else KIS_BASE_URL
        self.timeout = KIS_TIMEOUT_SECONDS
        self.session = session or requests
        self.max_retries, self.backoff, self._sleep = max_retries, backoff, sleep
        self.min_interval = KIS_MIN_INTERVAL_SECONDS if min_interval is None else min_interval
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_call = 0.0
        self.calls = 0                     # requests sent (for logs and tests; never the content)

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    def _send(self, method: str, path: str, **kwargs):
        if path not in ALLOWED_PATHS:
            raise KisAPIError(f"path not allowed for this read-only adapter: {path}")
        self._throttle()
        self.calls += 1
        try:
            return request_with_retry(self.session, method, f"{self.base_url}{path}", timeout=self.timeout,
                                      max_retries=self.max_retries, backoff=self.backoff, sleep=self._sleep,
                                      label=f"KIS {path}", **kwargs)
        except ProviderError as exc:
            raise KisAPIError(str(exc)) from exc

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def _ensure_token(self) -> str:
        if not self.configured:
            raise KisAPIError("KIS_APP_KEY / KIS_APP_SECRET 환경변수가 설정되지 않았습니다.")
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = self._send(
            "post", "/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret},
            headers={"content-type": "application/json; charset=utf-8"},
        )
        if response.status_code >= 400:
            raise KisAPIError(f"KIS token request failed (HTTP {response.status_code})")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise KisAPIError(f"KIS token response did not contain access_token: {payload.get('error_description', 'unknown error')}")
        self._token = token
        expires_in = float(payload.get("expires_in", 86400))
        self._token_expires_at = time.monotonic() + max(60.0, expires_in - 60.0)
        return token

    def _get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict:
        token = self._ensure_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        for attempt in range(self.max_retries + 1):
            response = self._send("get", path, headers=headers, params=params)
            if response.status_code >= 400:
                # Never include headers/tokens in the error string.
                raise KisAPIError(f"KIS API request failed (HTTP {response.status_code}) for {path}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise KisAPIError(f"KIS API returned a non-JSON body for {path}") from exc
            if not isinstance(payload, dict):
                raise KisAPIError(f"KIS API returned an unexpected body for {path}")
            if payload.get("msg_cd") in RATE_LIMIT_CODES and attempt < self.max_retries:
                self._sleep(self.backoff * (2 ** attempt))
                continue
            if payload.get("rt_cd") not in (None, "0"):
                raise KisAPIError(f"KIS API error {payload.get('msg_cd', '')}: {payload.get('msg1', 'unknown error')}")
            return payload
        raise KisAPIError(f"KIS rate limit persisted after {self.max_retries + 1} attempts for {path}")

    def current_quote(self, symbol: str) -> dict:
        symbol = symbol.strip().upper()
        if not symbol.isdigit() or len(symbol) != 6:
            raise KisAPIError("MVP 실시간 현재가 조회는 국내 6자리 종목코드만 지원합니다.")
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        out = data.get("output") or {}
        return {
            "market": "KR", "symbol": symbol,
            "price": _number(out.get("stck_prpr")),
            "change_pct": _number(out.get("prdy_ctrt")),
            "volume": _number(out.get("acml_vol")),
            "turnover": _number(out.get("acml_tr_pbmn")),
            "data_source": "KIS Open API", "data_type": "current_quote", "data_status": "DELAYED",
            "status_note": "REST 현재가 조회 시점 스냅샷 (실시간 스트림 아님)",
        }

    def daily_bars(self, symbol: str, lookback_calendar_days: int = 420) -> pd.DataFrame:
        end = datetime.now(ZoneInfo("Asia/Seoul")).date()
        start = end - timedelta(days=max(90, min(lookback_calendar_days, 1600)))
        return self.daily_bars_range(symbol, start, end)

    def daily_bars_range(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """Adjusted daily bars for [start, end], requested in windows of DAILY_CHUNK_DAYS (newest first)."""
        symbol = symbol.strip().upper()
        if not symbol.isdigit() or len(symbol) != 6:
            raise KisAPIError("MVP 일봉 동기화는 국내 6자리 종목코드만 지원합니다.")
        start, end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        if start > end:
            raise KisAPIError("start is after end")
        chunks = []
        self.last_dropped_rows = 0
        chunk_end = end
        while chunk_end >= start:
            chunk_start = max(start, chunk_end - timedelta(days=DAILY_CHUNK_DAYS))
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": chunk_start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": chunk_end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
            )
            rows = data.get("output2")
            if rows:
                chunks.extend(rows if isinstance(rows, list) else [rows])
            chunk_end = chunk_start - timedelta(days=1)
        df, dropped = parse_daily_rows(chunks, symbol)
        self.last_dropped_rows = dropped
        if len(df):
            df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))].reset_index(drop=True)
        return df


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
