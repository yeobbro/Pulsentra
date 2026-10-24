"""SQLite storage. Schema changes are additive so a Market Signal Lab MVP database keeps working."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.settings import DB_PATH

CANDLE_COLUMNS = ["market", "symbol", "date", "open", "high", "low", "close", "volume", "turnover", "source"]
EXTRA_CANDLE_COLUMNS = ["adjusted", "ingested_at", "quality_flags", "exchange", "timezone", "source_type"]

# Columns added after the MVP. `ALTER TABLE ... ADD COLUMN` runs only when a column is missing.
_CANDLE_MIGRATIONS = {
    "adjusted": "INTEGER",            # 1 = adjusted for splits/dividends, 0 = raw, NULL = unknown
    "ingested_at": "TEXT",            # UTC ISO time the row was written
    "quality_flags": "TEXT",          # comma-separated warning codes from app.data.quality
    "exchange": "TEXT",
    "timezone": "TEXT DEFAULT 'Asia/Seoul'",
    "source_type": "TEXT",            # REAL | SYNTHETIC | TEST (app.data.schema.source_type)
}

_TABLES = [
    """CREATE TABLE IF NOT EXISTS candles (
        market TEXT NOT NULL, symbol TEXT NOT NULL, date TEXT NOT NULL,
        open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
        volume REAL NOT NULL, turnover REAL, source TEXT NOT NULL DEFAULT 'csv',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (market, symbol, date))""",
    "CREATE INDEX IF NOT EXISTS idx_candles_symbol_date ON candles(market, symbol, date)",
    """CREATE TABLE IF NOT EXISTS instruments (
        market TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT, exchange TEXT, sector TEXT,
        listed_date TEXT, delisted_date TEXT, is_benchmark INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (market, symbol))""",
    """CREATE TABLE IF NOT EXISTS ingestion_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, market TEXT, symbol TEXT,
        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
        rows_received INTEGER DEFAULT 0, rows_saved INTEGER DEFAULT 0, rows_rejected INTEGER DEFAULT 0,
        warnings INTEGER DEFAULT 0, error TEXT)""",
    """CREATE TABLE IF NOT EXISTS quality_issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, market TEXT, symbol TEXT, date TEXT,
        check_name TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX IF NOT EXISTS idx_quality_symbol ON quality_issues(market, symbol, date)",
    """CREATE TABLE IF NOT EXISTS saved_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, params TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS strategy_configs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, strategy TEXT NOT NULL,
        params TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS backtest_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL,
        request TEXT NOT NULL, data_fingerprint TEXT, sources TEXT, quality_warnings TEXT,
        summary TEXT, synthetic INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS model_registry (
        id INTEGER PRIMARY KEY AUTOINCREMENT, version TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        task TEXT NOT NULL, horizon INTEGER, selected_model TEXT, adopted INTEGER NOT NULL DEFAULT 0,
        adoption_reason TEXT, data_start TEXT, data_end TEXT, sources TEXT, data_fingerprint TEXT,
        report TEXT, model_path TEXT, synthetic INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS disclosures (
        rcept_no TEXT PRIMARY KEY, market TEXT, symbol TEXT, corp_code TEXT, corp_name TEXT,
        report_nm TEXT, rcept_dt TEXT, risk_flags TEXT, source TEXT, fetched_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_disclosures_symbol ON disclosures(market, symbol, rcept_dt)",
    """CREATE TABLE IF NOT EXISTS paper_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, initial_cash REAL NOT NULL,
        cash REAL NOT NULL, strategy TEXT, params TEXT, created_at TEXT NOT NULL,
        last_processed_date TEXT, peak_equity REAL)""",
    """CREATE TABLE IF NOT EXISTS paper_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, market TEXT NOT NULL,
        symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, order_type TEXT NOT NULL,
        limit_price REAL, stop_price REAL, target_price REAL, status TEXT NOT NULL,
        created_date TEXT NOT NULL, expires_date TEXT, filled_date TEXT, fill_price REAL,
        reason TEXT, note TEXT)""",
    """CREATE TABLE IF NOT EXISTS paper_positions (
        account_id INTEGER NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
        entry_price REAL NOT NULL, entry_date TEXT NOT NULL, stop_price REAL, target_price REAL,
        initial_risk REAL, PRIMARY KEY (account_id, market, symbol))""",
    """CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, market TEXT, symbol TEXT,
        qty INTEGER, entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
        reason TEXT, pnl REAL, costs REAL)""",
    """CREATE TABLE IF NOT EXISTS paper_equity (
        account_id INTEGER NOT NULL, date TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL,
        positions INTEGER NOT NULL, PRIMARY KEY (account_id, date))""",
    """CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, market TEXT, symbol TEXT,
        bar_date TEXT, dedupe_key TEXT NOT NULL UNIQUE, message TEXT NOT NULL, snapshot TEXT,
        created_at TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)""",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    with connect(db_path) as conn:
        for ddl in _TABLES:
            conn.execute(ddl)
        paper_cols = {row[1] for row in conn.execute("PRAGMA table_info(paper_accounts)")}
        if "halt_until" not in paper_cols:
            conn.execute("ALTER TABLE paper_accounts ADD COLUMN halt_until TEXT")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(candles)")}
        for col, ddl in _CANDLE_MIGRATIONS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE candles ADD COLUMN {col} {ddl}")
        conn.commit()


def upsert_candles(df: pd.DataFrame, db_path: Path | str | None = None) -> int:
    """Low-level write. Raises on structurally invalid rows; use `app.data.ingest.ingest_frame`
    to filter invalid rows, record quality issues and keep an ingestion log."""
    if df.empty:
        return 0
    required = {"market", "symbol", "date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV/dataframe is missing required columns: {', '.join(sorted(missing))}")
    working = df.copy()
    if "turnover" not in working:
        working["turnover"] = working["close"] * working["volume"]
    if "source" not in working:
        working["source"] = "csv"
    from app.data.schema import license_refusal   # the write itself refuses licence-restricted rows, whoever calls it
    for src in working["source"].dropna().unique():
        refusal = license_refusal(src)
        if refusal:
            raise ValueError(refusal)
    working["date"] = pd.to_datetime(working["date"], errors="raise").dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        working[col] = pd.to_numeric(working[col], errors="raise")
    if (working[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be greater than zero")
    if (working["volume"] < 0).any():
        raise ValueError("Volume cannot be negative")
    invalid_range = (working["high"] < working[["open", "low", "close"]].max(axis=1)) | (
        working["low"] > working[["open", "high", "close"]].min(axis=1))
    if invalid_range.any():
        raise ValueError("Invalid OHLC row: high must be >= open/close/low and low must be <= open/close/high")
    now = utc_now()
    for col, default in {"adjusted": None, "ingested_at": now, "quality_flags": "", "exchange": None, "timezone": "Asia/Seoul"}.items():
        if col not in working:
            working[col] = default
    from app.data.schema import source_type as _stype
    working["source_type"] = working["source"].map(_stype)
    working["adjusted"] = working["adjusted"].map(lambda v: None if v is None or pd.isna(v) else int(bool(v)))
    cols = CANDLE_COLUMNS + EXTRA_CANDLE_COLUMNS
    records = [tuple(None if (isinstance(v, float) and pd.isna(v)) else v for v in row)
               for row in working[cols].itertuples(index=False, name=None)]
    with connect(db_path) as conn:
        conn.executemany(f"""
        INSERT INTO candles ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})
        ON CONFLICT(market, symbol, date) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
          volume=excluded.volume, turnover=excluded.turnover, source=excluded.source,
          adjusted=excluded.adjusted, ingested_at=excluded.ingested_at, quality_flags=excluded.quality_flags,
          exchange=COALESCE(excluded.exchange, candles.exchange), timezone=excluded.timezone, source_type=excluded.source_type,
          updated_at=CURRENT_TIMESTAMP
        """, records)
        conn.commit()
    return len(records)


def load_candles(symbol: str | None = None, market: str | None = None, db_path: Path | str | None = None,
                 sources: Iterable[str] | None = None, exclude_sources: Iterable[str] | None = None) -> pd.DataFrame:
    cols = CANDLE_COLUMNS + EXTRA_CANDLE_COLUMNS
    query = f"SELECT {', '.join(cols)} FROM candles"
    clauses: list[str] = []
    params: list[Any] = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol.upper())
    if market:
        clauses.append("market = ?")
        params.append(market.upper())
    if sources:
        s = list(sources)
        clauses.append(f"source IN ({', '.join('?' * len(s))})")
        params.extend(s)
    if exclude_sources:
        s = list(exclude_sources)
        clauses.append(f"source NOT IN ({', '.join('?' * len(s))})")
        params.extend(s)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY market, symbol, date"
    with connect(db_path) as conn:
        frame = pd.read_sql_query(query, conn, params=params)
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def list_symbols(db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("""
            SELECT c.market, c.symbol, MIN(c.date) AS first_date, MAX(c.date) AS last_date,
                   COUNT(*) AS bars, GROUP_CONCAT(DISTINCT c.source) AS sources, GROUP_CONCAT(DISTINCT c.source_type) AS source_types,
                   MAX(c.ingested_at) AS last_ingested_at,
                   SUM(CASE WHEN c.quality_flags IS NOT NULL AND c.quality_flags != '' THEN 1 ELSE 0 END) AS flagged_bars,
                   i.name AS name, i.sector AS sector, i.delisted_date AS delisted_date, i.is_benchmark AS is_benchmark
            FROM candles c LEFT JOIN instruments i ON i.market = c.market AND i.symbol = c.symbol
            GROUP BY c.market, c.symbol ORDER BY c.market, c.symbol
        """).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        srcs = sorted(filter(None, (d.get("sources") or "").split(",")))
        d["sources"] = srcs
        d["source"] = srcs[0] if len(srcs) == 1 else ("mixed" if srcs else None)  # MVP field, now explicit about mixing
        out.append(d)
    return out


def clear_source(source: str, db_path: Path | str | None = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM candles WHERE source = ?", (source,))
        conn.commit()
        return cur.rowcount


def upsert_instruments(rows: list[dict], db_path: Path | str | None = None) -> int:
    if not rows:
        return 0
    with connect(db_path) as conn:
        for r in rows:
            conn.execute("""
                INSERT INTO instruments (market, symbol, name, exchange, sector, listed_date, delisted_date, is_benchmark, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(market, symbol) DO UPDATE SET name=COALESCE(excluded.name, instruments.name),
                  exchange=COALESCE(excluded.exchange, instruments.exchange), sector=COALESCE(excluded.sector, instruments.sector),
                  listed_date=COALESCE(excluded.listed_date, instruments.listed_date), delisted_date=excluded.delisted_date,
                  is_benchmark=excluded.is_benchmark, updated_at=CURRENT_TIMESTAMP
            """, (str(r["market"]).upper(), str(r["symbol"]).upper(), r.get("name"), r.get("exchange"), r.get("sector"),
                  r.get("listed_date"), r.get("delisted_date"), int(bool(r.get("is_benchmark", False)))))
        conn.commit()
    return len(rows)


def load_instruments(db_path: Path | str | None = None) -> pd.DataFrame:
    with connect(db_path) as conn:
        return pd.read_sql_query("SELECT * FROM instruments ORDER BY market, symbol", conn)


def fetch_all(sql: str, params: tuple = (), db_path: Path | str | None = None) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def execute(sql: str, params: tuple = (), db_path: Path | str | None = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid or cur.rowcount or 0)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
