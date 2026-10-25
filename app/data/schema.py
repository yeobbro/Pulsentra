"""Data standard shared by every provider.

A stored daily bar has: market, symbol, exchange, date (exchange-local trading date), timezone,
open/high/low/close, volume, turnover, adjusted (True/False/None = unknown), source,
ingested_at (UTC) and quality_flags. Freshness and validation status are derived per series
(app.data.freshness) rather than stored, so they are always computed against "now".
"""
from __future__ import annotations

from enum import Enum


class DataStatus(str, Enum):
    LIVE = "LIVE"              # real-time stream (not implemented yet — never returned today)
    DELAYED = "DELAYED"        # real data, but end-of-day or a REST snapshot, not a stream
    HISTORICAL = "HISTORICAL"  # user-supplied or archived past data; freshness is not claimed
    DEMO = "DEMO"              # synthetic data for UI/testing only
    STALE = "STALE"            # real data whose last bar is older than the freshness threshold
    ERROR = "ERROR"            # last collection failed and no usable data exists


DEMO_SOURCES = {"synthetic_demo", "test"}
# source -> (provider label, kind). kind decides the status before the freshness check.
SOURCE_KINDS: dict[str, tuple[str, str]] = {
    "synthetic_demo": ("Pulsentra synthetic demo", "demo"),
    "test": ("test fixture", "demo"),
    "csv": ("CSV upload", "historical"),
    "csv_import": ("CSV upload", "historical"),
    "kis_eod": ("KIS Open API daily bars", "eod"),
    "kis_quote": ("KIS Open API REST quote", "snapshot"),
    "naver_fchart": ("Naver Finance daily chart (unofficial endpoint)", "eod"),
    "krx_openapi": ("KRX Open API daily trading info (research verification only)", "eod"),
}

# Sources whose licence does not allow them into the app's store, which is what the screens and API show.
# KRX OPEN API terms (2025-12-26): 제6조② non-commercial use only; 제11조② "API 이용자는 한국거래소로부터 제공받은
# 정보를 제3자에게 제공할 수 없다." Until a licence for showing or distributing the data is confirmed in writing,
# KRX answers stay local verification files (data/krxcheck/). See docs/DATA_SOURCES.md, "상용화 전 체크리스트".
LICENSE_RESTRICTED_SOURCES: dict[str, str] = {
    "krx_openapi": "KRX OPEN API 응답은 이용약관(제6조② 비상업적 이용, 제11조② 제3자 제공 금지)상 앱 저장소에 적재할 수 없습니다. "
                   "외부 제공 라이선스가 서면으로 확인되기 전까지 로컬 검증 자료(data/krxcheck/)로만 씁니다.",
}


def license_refusal(source) -> str | None:
    """Refusal message when `source` is licence-restricted, else None. Case and surrounding spaces do not matter
    ('KRX_OPENAPI', ' krx_openapi' are the same source)."""
    return LICENSE_RESTRICTED_SOURCES.get(str(source if source is not None else "").strip().lower())

# Provenance class stored with every bar. REAL and SYNTHETIC are never mixed in one series or one analysis.
REAL, SYNTHETIC, TEST = "REAL", "SYNTHETIC", "TEST"
SOURCE_TYPES: dict[str, str] = {"synthetic_demo": SYNTHETIC, "test": TEST}


def source_type(source: str | None) -> str:
    """REAL for every market-data source (CSV, KIS, Naver …); SYNTHETIC for the demo generator; TEST for fixtures."""
    return SOURCE_TYPES.get(source or "", REAL)


def source_kind(source: str | None) -> str:
    if not source:
        return "unknown"
    return SOURCE_KINDS.get(source, (source, "historical"))[1]


def is_demo_source(source: str | None) -> bool:
    return source in DEMO_SOURCES
