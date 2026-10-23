"""Ingestion with an audit trail: every load gets an `ingestion_runs` row and its quality issues."""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from app import db
from app.data.quality import check_frame

REQUIRED = ["date", "open", "high", "low", "close", "volume"]


def start_run(source: str, market: str | None = None, symbol: str | None = None, db_path: Path | str | None = None) -> int:
    return db.execute("INSERT INTO ingestion_runs (source, market, symbol, started_at, status) VALUES (?, ?, ?, ?, 'running')",
                      (source, market, symbol, db.utc_now()), db_path)


def finish_run(run_id: int, status: str, received: int = 0, saved: int = 0, rejected: int = 0, warnings: int = 0,
               error: str | None = None, db_path: Path | str | None = None) -> None:
    db.execute("""UPDATE ingestion_runs SET finished_at=?, status=?, rows_received=?, rows_saved=?, rows_rejected=?,
                  warnings=?, error=? WHERE id=?""",
               (db.utc_now(), status, received, saved, rejected, warnings, (error or "")[:500] or None, run_id), db_path)


def record_failure(source: str, market: str | None, symbol: str | None, error: str, db_path: Path | str | None = None) -> int:
    run_id = start_run(source, market, symbol, db_path)
    finish_run(run_id, "error", error=error, db_path=db_path)
    return run_id


def ingest_frame(df: pd.DataFrame, source: str, adjusted: bool | None = None, market: str | None = None,
                 symbol: str | None = None, db_path: Path | str | None = None) -> dict:
    """Validate, write the valid rows, store every issue. Returns a summary for the API/UI."""
    run_id = start_run(source, market, symbol, db_path)
    try:
        work = df.copy()
        work["source"] = source
        if "adjusted" not in work:
            work["adjusted"] = adjusted
        # Never let REAL and SYNTHETIC/TEST bars share a series.
        from app.data.schema import source_type
        new_type = source_type(source)
        keys = work[["market", "symbol"]].drop_duplicates() if {"market", "symbol"} <= set(work.columns) else pd.DataFrame()
        if not keys.empty:
            with db.connect(db_path) as conn:
                for m, s_ in keys.itertuples(index=False):
                    rows = conn.execute("SELECT DISTINCT source FROM candles WHERE market=? AND symbol=?", (str(m).upper(), str(s_).upper())).fetchall()
                    clash = [r[0] for r in rows if source_type(r[0]) != new_type]
                    if clash:
                        raise ValueError(f"{m}/{s_}: 기존 데이터({', '.join(clash)}) 와 출처 유형이 다릅니다 ({new_type}). REAL과 합성 데이터는 섞지 않습니다.")
        report = check_frame(work)
        saved = db.upsert_candles(report.valid, db_path) if not report.valid.empty else 0
        if report.issues:
            with db.connect(db_path) as conn:
                conn.executemany("""INSERT INTO quality_issues (run_id, market, symbol, date, check_name, severity, detail)
                                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                 [(run_id, i["market"], i["symbol"], i["date"], i["check"], i["severity"], i["detail"]) for i in report.issues])
                conn.commit()
        status = "ok" if not report.rejected.shape[0] else ("partial" if saved else "error")
        finish_run(run_id, status, len(df), saved, len(report.rejected), report.warning_count,
                   None if status != "error" else "all rows rejected by quality checks", db_path)
        return {"run_id": run_id, "status": status, "rows_received": int(len(df)), "rows_saved": int(saved),
                "quality": report.summary(), "issues_preview": report.issues[:50]}
    except Exception as exc:  # recorded, then re-raised for the caller to map to an HTTP error
        finish_run(run_id, "error", len(df), 0, 0, 0, str(exc), db_path)
        raise


def parse_csv(content: bytes, market: str = "KR", symbol: str = "") -> pd.DataFrame:
    """Normalise a user CSV. Accepts the MVP columns plus common aliases. Never invents values."""
    data = pd.read_csv(io.BytesIO(content))
    data.columns = [str(c).strip().lower() for c in data.columns]
    aliases = {"datetime": "date", "trade_date": "date", "vol": "volume", "value": "turnover", "amount": "turnover"}
    data = data.rename(columns={k: v for k, v in aliases.items() if k in data.columns and v not in data.columns})
    if "adj close" in data.columns and "close" not in data.columns:
        data = data.rename(columns={"adj close": "close"})
        data["adjusted"] = True
    missing = [c for c in REQUIRED if c not in data.columns]
    if missing:
        raise ValueError(f"필수 열이 없습니다: {', '.join(missing)} (필수: {', '.join(REQUIRED)})")
    if "symbol" not in data.columns:
        if not symbol.strip():
            raise ValueError("symbol 열이 없으면 폼에 종목코드를 입력해야 합니다.")
        data["symbol"] = symbol.strip().upper()
    data["symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    # Korean codes lose leading zeros in spreadsheets (5930 -> 005930).
    data["symbol"] = data["symbol"].map(lambda s: s.zfill(6) if s.isdigit() and len(s) < 6 else s)
    data["market"] = data["market"].astype(str).str.upper() if "market" in data.columns else market.strip().upper()
    if "adjusted" in data.columns:
        data["adjusted"] = data["adjusted"].map(lambda v: None if pd.isna(v) else str(v).strip().lower() in {"1", "true", "y", "yes"})
    return data
