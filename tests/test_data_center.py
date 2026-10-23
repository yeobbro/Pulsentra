"""Required tests 13-14: CSV ingestion, quality checks, freshness status, provider errors, minute bars."""
from datetime import date, datetime, timedelta, timezone

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from app import db
from app.data.freshness import all_series_status, series_status
from app.data.ingest import ingest_frame, parse_csv, record_failure
from app.data.quality import check_frame
from app.providers.dart import DartClient, classify
from app.realtime import aggregate_ticks, feed_delay_status

CSV = b"""market,symbol,date,open,high,low,close,volume,turnover
KR,5930,2026-01-02,55000,55800,54700,55500,1234567,68518500000
KR,5930,2026-01-05,55500,56100,55200,56000,1100000,61600000000
KR,5930,2026-01-06,55500,55000,55200,56000,1100000,61600000000
KR,5930,2026-01-07,-1,56100,55200,56000,1100000,61600000000
KR,5930,2026-01-08,56000,56500,55800,56200,,1
"""


# 13. CSV load + quality checks
def test_parse_csv_pads_korean_codes():
    df = parse_csv(CSV)
    assert set(df["symbol"]) == {"005930"}


def test_quality_rejects_errors_and_keeps_valid_rows():
    rep = check_frame(parse_csv(CSV))
    checks = {i["check"] for i in rep.issues}
    assert {"ohlc_inconsistent", "non_positive_price", "missing_value"} <= checks
    assert len(rep.valid) == 2 and len(rep.rejected) == 3


def test_duplicates_identical_vs_conflicting(bars):
    df = bars(np.full(5, 100.0))
    dup_same = pd.concat([df, df.iloc[[2]]])
    rep = check_frame(dup_same)
    assert len(rep.valid) == 5 and any(i["check"] == "duplicate_identical" for i in rep.issues)
    conflict = df.iloc[[2]].copy()
    conflict["close"] = 100.5
    rep2 = check_frame(pd.concat([df, conflict]))
    assert len(rep2.valid) == 4 and any(i["check"] == "duplicate_conflict" for i in rep2.issues)


def test_warnings_jump_split_gap_zero_volume(bars):
    close = np.r_[np.full(10, 100.0), 50.0, 50.0, 80.0, 80.0]
    df = bars(close)
    df.loc[13, "volume"] = 0
    df.loc[13, "date"] = pd.Timestamp(df.loc[13, "date"]) + pd.Timedelta(days=14)
    rep = check_frame(df)
    by = {i["check"] for i in rep.issues}
    assert {"possible_corporate_action", "price_jump", "missing_trading_days", "zero_volume"} <= by
    assert rep.valid.loc[10, "quality_flags"] == "possible_corporate_action"
    assert len(rep.rejected) == 0  # warnings never delete data


def test_adjusted_series_not_flagged_as_split(bars):
    df = bars(np.r_[np.full(5, 100.0), 50.0])
    df["adjusted"] = True
    rep = check_frame(df)
    assert {i["check"] for i in rep.issues} == {"price_jump"}


def test_ingest_records_run_and_issues(tmp_db):
    res = ingest_frame(parse_csv(CSV), "csv_import", db_path=tmp_db)
    assert res["status"] == "partial" and res["rows_saved"] == 2
    runs = db.fetch_all("SELECT * FROM ingestion_runs", (), tmp_db)
    assert runs[0]["rows_rejected"] == 3
    assert len(db.fetch_all("SELECT * FROM quality_issues", (), tmp_db)) == 3
    assert len(db.load_candles(db_path=tmp_db)) == 2


def test_migration_from_mvp_schema(tmp_path):
    path = tmp_path / "mvp.sqlite3"
    with db.connect(path) as conn:  # the exact MVP table
        conn.execute("""CREATE TABLE candles (market TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL NOT NULL,
            high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, turnover REAL,
            source TEXT NOT NULL DEFAULT 'csv', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (market, symbol, date))""")
        conn.execute("INSERT INTO candles (market, symbol, date, open, high, low, close, volume) VALUES ('KR','000001','2026-01-02',1,1,1,1,1)")
    db.init_db(path)
    cols = {r[1] for r in db.connect(path).execute("PRAGMA table_info(candles)")}
    assert {"adjusted", "ingested_at", "quality_flags"} <= cols
    assert len(db.load_candles(db_path=path)) == 1


# 14. API errors and data latency status
def test_status_rules():
    today = date(2026, 10, 23)  # Friday
    assert series_status(["synthetic_demo"], "2026-10-23", today=today)["status"] == "DEMO"
    assert series_status(["csv_import"], "2020-01-02", today=today)["status"] == "HISTORICAL"
    assert series_status(["kis_eod"], "2026-10-22", today=today)["status"] == "DELAYED"
    stale = series_status(["kis_eod"], "2026-10-16", today=today)
    assert stale["status"] == "STALE" and stale["age_business_days"] == 5
    assert series_status([], None, "error", today=today)["status"] == "ERROR"
    assert "LIVE" not in {series_status(["kis_eod"], "2026-10-23", today=today)["status"]}  # EOD data is never LIVE


def test_failed_collection_shows_error(tmp_db):
    record_failure("kis_eod", "KR", "123456", "KIS API request failed (HTTP 500)", tmp_db)
    st = {s["symbol"]: s for s in all_series_status(tmp_db)}
    assert st["123456"]["status"] == "ERROR" and "HTTP 500" in st["123456"]["last_error"]


def test_feed_delay_status():
    now = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
    assert feed_delay_status(now - timedelta(seconds=5), now)["status"] == "LIVE"
    assert feed_delay_status(now - timedelta(seconds=90), now)["status"] == "DELAYED"
    assert feed_delay_status(None, now)["status"] == "ERROR"


def test_minute_bars_from_ticks():
    ticks = pd.DataFrame({"ts": pd.to_datetime(["2026-10-23 09:00:05", "2026-10-23 09:00:40", "2026-10-23 09:02:10"]),
                          "price": [100.0, 102.0, 101.0], "volume": [10, 5, 7]})
    bars = aggregate_ticks(ticks, "1min")
    assert len(bars) == 2  # 09:01 had no trade: no invented bar
    first = bars.iloc[0]
    assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == (100.0, 102.0, 100.0, 102.0, 15)
    assert len(aggregate_ticks(ticks, "5min")) == 1


class _Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content = status, payload, content

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None, **_):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_dart_adapter_parses_documented_fields():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", "<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
                                    "<stock_code>005930</stock_code><modify_date>20260101</modify_date></list>"
                                    "<list><corp_code>99999999</corp_code><corp_name>비상장</corp_name><stock_code> </stock_code></list></result>")
    page = {"status": "000", "message": "정상", "page_no": 1, "total_page": 1,
            "list": [{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "report_nm": "주요사항보고서(유상증자결정)",
                      "rcept_no": "20260105000001", "rcept_dt": "20260105"}]}
    s = _Session([_Resp(content=buf.getvalue()), _Resp(payload=page)])
    client = DartClient(api_key="test-key", session=s, min_interval=0)
    codes = client.corp_codes()
    assert codes == {"005930": {"corp_code": "00126380", "corp_name": "삼성전자"}}
    rows = client.disclosures("00126380", "20260101", "20260131")
    assert rows[0]["risk_flags"] == "dilution:유상증자" and rows[0]["symbol"] == "005930"
    assert s.calls[1][1]["page_count"] == 100


def test_dart_error_status_raises():
    from app.providers.dart import DartAPIError
    client = DartClient(api_key="k", session=_Session([_Resp(payload={"status": "020", "message": "요청 제한 초과"})]), min_interval=0)
    with pytest.raises(DartAPIError):
        client.disclosures("00126380", "20260101", "20260131")


def test_disclosure_classification():
    assert classify("전환사채권발행결정") == "dilution:전환사채"
    assert classify("분기보고서 (2026.09)") == ""
