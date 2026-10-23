"""Runtime settings for Pulsentra.

Every value is read from environment variables (or a local `.env` that is never committed).
`PULSENTRA_*` names are preferred; the original `MSL_*` names from the Market Signal Lab MVP are
still honoured so an existing installation keeps working.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


def _env(name: str, legacy: str | None = None, default: str = "") -> str:
    value = os.getenv(name)
    if value is None and legacy:
        value = os.getenv(legacy)
    return (value if value is not None else default).strip()


def _float(name: str, default: float, legacy: str | None = None) -> float:
    raw = _env(name, legacy, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


APP_NAME = "Pulsentra"
APP_NAME_KO = "펄센트라"
APP_TAGLINE = "AI-Powered Market Intelligence & Strategy Validation Platform"
APP_VERSION = "0.2.0"

DATA_DIR = Path(_env("PULSENTRA_DATA_DIR", "MSL_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
# The MVP database file name is kept so existing local data is reused.
DB_PATH = Path(_env("PULSENTRA_DB_PATH", "MSL_DB_PATH", str(DATA_DIR / "market_signal_lab.sqlite3")))
MODEL_DIR = Path(_env("PULSENTRA_MODEL_DIR", None, str(DATA_DIR / "models")))
MODEL_PATH = Path(_env("PULSENTRA_MODEL_PATH", "MSL_MODEL_PATH", str(DATA_DIR / "model.joblib")))
MODEL_META_PATH = Path(_env("PULSENTRA_MODEL_META_PATH", "MSL_MODEL_META_PATH", str(DATA_DIR / "model_meta.json")))

KIS_APP_KEY = _env("KIS_APP_KEY")
KIS_APP_SECRET = _env("KIS_APP_SECRET")
KIS_BASE_URL = _env("KIS_BASE_URL", None, "https://openapi.koreainvestment.com:9443").rstrip("/")
KIS_PAPER = _env("KIS_PAPER", None, "false").lower() in {"1", "true", "yes"}
KIS_TIMEOUT_SECONDS = _float("KIS_TIMEOUT_SECONDS", 12.0)
# Pause between KIS REST calls. Rate limits are set by KIS; check the official portal for current values.
KIS_MIN_INTERVAL_SECONDS = _float("KIS_MIN_INTERVAL_SECONDS", 0.35)

DART_API_KEY = _env("DART_API_KEY")
DART_TIMEOUT_SECONDS = _float("DART_TIMEOUT_SECONDS", 12.0)

# Research assumptions, not broker-accurate fee quotes. Kept for the MVP `/api/backtest` endpoint.
DEFAULT_ROUND_TRIP_COST_BPS = _float("DEFAULT_ROUND_TRIP_COST_BPS", 40.0)
# Portfolio engine defaults (per side). Verify against your broker and the current tax law before use.
DEFAULT_COMMISSION_BPS = _float("PULSENTRA_COMMISSION_BPS", 1.5)
DEFAULT_SELL_TAX_BPS = _float("PULSENTRA_SELL_TAX_BPS", 20.0)
DEFAULT_SLIPPAGE_BPS = _float("PULSENTRA_SLIPPAGE_BPS", 10.0)

# A daily (end-of-day) series is STALE when its last bar is older than this many business days.
STALE_AFTER_BUSINESS_DAYS = int(_float("PULSENTRA_STALE_AFTER_BUSINESS_DAYS", 2))
