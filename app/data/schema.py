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
}

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
