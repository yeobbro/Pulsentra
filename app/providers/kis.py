from __future__ import annotations
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo
from typing import Any
import time
import requests
import pandas as pd
from app.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL, KIS_MIN_INTERVAL_SECONDS, KIS_PAPER, KIS_TIMEOUT_SECONDS


class KisAPIError(RuntimeError):
    pass


class KISClient:
    """Small read-only adapter for KIS Open API. No order endpoints are implemented.

    Daily bars use 국내주식기간별시세(FHKST03010100) with FID_ORG_ADJ_PRC="0", which KIS documents as
    수정주가 (adjusted); rows are stored with adjusted=True. One call returns at most 100 bars, so
    the range is requested in windows. Calls are spaced by KIS_MIN_INTERVAL_SECONDS because KIS
    applies per-second limits (EGW00201); check the official portal for current limits.
    """
    def __init__(self, session: Any = None) -> None:
        self.app_key = KIS_APP_KEY
        self.app_secret = KIS_APP_SECRET
        self.base_url = "https://openapivts.koreainvestment.com:29443" if KIS_PAPER else KIS_BASE_URL
        self.timeout = KIS_TIMEOUT_SECONDS
        self.session = session or requests
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = KIS_MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret)

    def _ensure_token(self) -> str:
        if not self.configured:
            raise KisAPIError("KIS_APP_KEY / KIS_APP_SECRET 환경변수가 설정되지 않았습니다.")
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        self._throttle()
        response = self.session.post(
            f"{self.base_url}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret},
            headers={"content-type": "application/json; charset=utf-8"},
            timeout=self.timeout,
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
        self._throttle()
        response = self.session.get(f"{self.base_url}{path}", headers=headers, params=params, timeout=self.timeout)
        if response.status_code >= 400:
            # Never include headers/tokens in the error string.
            raise KisAPIError(f"KIS API request failed (HTTP {response.status_code}) for {path}")
        payload = response.json()
        if payload.get("rt_cd") not in (None, "0"):
            raise KisAPIError(f"KIS API error {payload.get('msg_cd', '')}: {payload.get('msg1', 'unknown error')}")
        return payload

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
        symbol = symbol.strip().upper()
        if not symbol.isdigit() or len(symbol) != 6:
            raise KisAPIError("MVP 일봉 동기화는 국내 6자리 종목코드만 지원합니다.")
        end = datetime.now(ZoneInfo("Asia/Seoul")).date()
        start = end - timedelta(days=max(90, min(lookback_calendar_days, 1600)))
        chunks = []
        # Small date windows keep results within endpoint response limits.
        chunk_end = end
        while chunk_end >= start:
            chunk_start = max(start, chunk_end - timedelta(days=80))
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
            rows = data.get("output2") or []
            if rows:
                chunks.extend(rows)
            chunk_end = chunk_start - timedelta(days=1)
        if not chunks:
            return pd.DataFrame(columns=["market", "symbol", "date", "open", "high", "low", "close", "volume", "turnover", "source"])
        df = pd.DataFrame(chunks)
        required_map = {
            "stck_bsop_date": "date", "stck_oprc": "open", "stck_hgpr": "high",
            "stck_lwpr": "low", "stck_clpr": "close", "acml_vol": "volume", "acml_tr_pbmn": "turnover",
        }
        missing = set(required_map) - set(df.columns)
        if missing:
            raise KisAPIError(f"Unexpected KIS daily response; missing fields: {sorted(missing)}")
        df = df.rename(columns=required_map)[list(required_map.values())]
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"]).drop_duplicates(subset=["date"])
        df = df.sort_values("date").reset_index(drop=True)
        df["market"] = "KR"
        df["symbol"] = symbol
        df["source"] = "kis_eod"
        df["adjusted"] = True  # FID_ORG_ADJ_PRC = "0" (수정주가)
        df["exchange"] = "KRX"
        return df[["market", "symbol", "date", "open", "high", "low", "close", "volume", "turnover", "source", "adjusted", "exchange"]]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
