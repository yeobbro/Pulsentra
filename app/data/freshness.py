"""Per-series data status (LIVE / DELAYED / HISTORICAL / DEMO / STALE / ERROR)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from app import db
from app.data.schema import DataStatus, source_kind
from app.settings import STALE_AFTER_BUSINESS_DAYS

KST = ZoneInfo("Asia/Seoul")


def business_days_between(last: date, today: date) -> int:
    """Business days after `last` up to and including `today` (weekends excluded, holidays not)."""
    if last >= today:
        return 0
    return int(np.busday_count(last + _one_day(), today + _one_day()))


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def series_status(sources: list[str], last_date: str | None, last_run_status: str | None = None,
                  today: date | None = None, stale_after: int = STALE_AFTER_BUSINESS_DAYS) -> dict:
    today = today or datetime.now(KST).date()
    kinds = {source_kind(s) for s in sources}
    age = business_days_between(date.fromisoformat(last_date[:10]), today) if last_date else None
    if not sources or last_date is None:
        status = DataStatus.ERROR if last_run_status == "error" else DataStatus.STALE
        reason = "수집 실패, 사용 가능한 데이터 없음" if status == DataStatus.ERROR else "데이터 없음"
    elif "demo" in kinds:
        status, reason = DataStatus.DEMO, "합성 데모 데이터가 포함됨 (실제 시세 아님)"
    elif kinds <= {"eod", "snapshot"}:
        if age is not None and age > stale_after:
            status, reason = DataStatus.STALE, f"마지막 일봉 이후 {age} 영업일 경과 (기준 {stale_after}일)"
        else:
            status, reason = DataStatus.DELAYED, "실제 일봉/REST 조회 데이터 (실시간 스트림 아님)"
    else:
        status, reason = DataStatus.HISTORICAL, "업로드된 과거 데이터 (최신성은 보장하지 않음)"
    return {"status": status.value, "reason": reason, "age_business_days": age, "last_date": last_date,
            "last_run_status": last_run_status}


def all_series_status(db_path: Path | str | None = None, today: date | None = None) -> list[dict]:
    symbols = db.list_symbols(db_path)
    runs = {(r["market"], r["symbol"]): r for r in db.fetch_all("""
        SELECT r.market, r.symbol, r.status, r.error, r.finished_at FROM ingestion_runs r
        JOIN (SELECT market, symbol, MAX(id) AS id FROM ingestion_runs WHERE symbol IS NOT NULL GROUP BY market, symbol) m
          ON m.id = r.id""", (), db_path)}
    out = []
    seen = set()
    for s in symbols:
        run = runs.get((s["market"], s["symbol"]))
        st = series_status(s["sources"], s["last_date"], run["status"] if run else None, today)
        if run and run["status"] == "error":
            st["last_error"] = run["error"]
        out.append({**s, **st})
        seen.add((s["market"], s["symbol"]))
    for key, run in runs.items():  # symbols that only ever failed
        if key not in seen and run["status"] == "error":
            out.append({"market": key[0], "symbol": key[1], "bars": 0, "sources": [],
                        **series_status([], None, "error", today), "last_error": run["error"]})
    return out
