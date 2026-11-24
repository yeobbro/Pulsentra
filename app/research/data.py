"""Load REAL daily data for research, check it, and describe the sample honestly.

Two input formats:
  * `naver_raw`  : the output of scripts/collect_kr_daily.py (universe.csv, manifest.jsonl, raw/<code>.csv,
                   index_KOSPI.csv, index_KOSDAQ.csv, listing_*.csv)
  * `standard`   : a folder of CSV files in the Pulsentra standard (market,symbol,date,open,high,low,close,volume
                   [,turnover,adjusted]) + optional instruments.csv and index_<NAME>.csv (date,close)
Only REAL data is accepted: a synthetic / test source in the input stops the run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from app.data.quality import check_frame
from app.data.schema import REAL, source_type


@dataclass
class RealDataset:
    bars: pd.DataFrame                       # all valid bars (REAL), sorted by symbol/date
    instruments: pd.DataFrame                # symbol, name, exchange, status, listed/delisted dates
    indices: dict[str, pd.DataFrame]         # name -> date, close
    calendar: pd.DatetimeIndex               # market trading days (from the KOSPI index)
    issues: pd.DataFrame                     # quality issues (warnings + errors)
    halts: pd.DataFrame                      # provider halt-marker rows removed before checks
    rejected: pd.DataFrame
    provenance: dict = field(default_factory=dict)


def _read_index(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    dcol = "Date" if "Date" in df.columns else "date"
    ccol = "Close" if "Close" in df.columns else "close"
    out = pd.DataFrame({"date": pd.to_datetime(df[dcol]), "close": pd.to_numeric(df[ccol], errors="coerce")})
    return out.dropna().sort_values("date").drop_duplicates("date").reset_index(drop=True)


def load_naver_raw(data_dir: str | Path, start: str | None = None, end: str | None = None) -> RealDataset:
    d = Path(data_dir)
    uni = pd.read_csv(d / "universe.csv", dtype={"symbol": str})
    manifest = [json.loads(line) for line in (d / "manifest.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    latest: dict[str, dict] = {}
    for rec in manifest:
        latest[rec["symbol"]] = rec
    indices = {}
    for name in ("KOSPI", "KOSDAQ"):
        p = d / f"index_{name}.csv"
        if p.exists():
            idx = _read_index(p)
            if start:
                idx = idx[idx["date"] >= pd.Timestamp(start)]
            if end:
                idx = idx[idx["date"] <= pd.Timestamp(end)]
            indices[name] = idx.reset_index(drop=True)
    calendar = pd.DatetimeIndex(indices["KOSPI"]["date"]) if "KOSPI" in indices else None
    parts, halts, issues, rejected = [], [], [], []
    missing_files, empty = [], []
    for row in uni.itertuples():
        f = d / "raw" / f"{row.symbol}.csv"
        if not f.exists():
            missing_files.append(row.symbol)
            continue
        raw = pd.read_csv(f)
        # Coerce instead of trusting the file: one bad cell used to crash the run (turnover = close * volume on text).
        # Unparsable values become NaN/NaT and are rejected, with a recorded issue, by check_frame below.
        raw["date"] = pd.to_datetime(raw["date"], errors="coerce")
        for col in ("open", "high", "low", "close", "volume"):
            raw[col] = pd.to_numeric(raw[col], errors="coerce")
        if start:
            raw = raw[raw["date"] >= pd.Timestamp(start)]
        if end:
            raw = raw[raw["date"] <= pd.Timestamp(end)]
        if raw.empty:
            empty.append(row.symbol)
            continue
        # Suspended sessions are not tradable bars. Naver shows them in two ways:
        #   (a) open=high=low=0 with the previous close, (b) volume 0 with open=high=low=close (a flat filler bar).
        # Both are removed (and recorded); a position simply waits for the next real bar, where a gap is filled at the open.
        hm = (raw["open"] == 0) & (raw["high"] == 0) & (raw["low"] == 0)
        flat0 = (raw["volume"] == 0) & (raw["open"] == raw["high"]) & (raw["high"] == raw["low"]) & (raw["low"] == raw["close"]) & (raw["close"] > 0)
        if hm.any() or flat0.any():
            # concat only non-empty parts: an empty part makes pandas 2.3 + NumPy 2.5 emit a generic-timedelta warning
            parts_h = [raw.loc[m, ["date"]].assign(kind=k) for m, k in ((hm, "zero_ohl_marker"), (flat0, "zero_volume_flat_bar")) if m.any()]
            halts.append(pd.concat(parts_h).assign(symbol=row.symbol))
        raw = raw[~(hm | flat0)].copy()
        raw["market"] = "KR"
        raw["symbol"] = row.symbol
        raw["exchange"] = row.market
        raw["source"] = "naver_fchart"
        raw["adjusted"] = True           # prices are adjusted by the provider; volume is NOT (see quality report)
        raw["turnover"] = raw["close"] * raw["volume"]   # ESTIMATE: the provider has no 거래대금 field
        rep = check_frame(raw, calendar=calendar)
        if rep.issues:
            issues.append(pd.DataFrame(rep.issues))
        if len(rep.rejected):
            rejected.append(rep.rejected[["symbol", "date", "open", "high", "low", "close", "volume"]])
        parts.append(rep.valid)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    types = {source_type(s) for s in bars["source"].unique()} if not bars.empty else set()
    if types - {REAL}:
        raise ValueError(f"REAL 이외의 데이터가 섞여 있습니다: {types}")
    bars["source_type"] = REAL
    inst = pd.DataFrame({"market": "KR", "symbol": uni["symbol"], "name": uni["name"], "exchange": uni["market"],
                         "status": uni["status"], "listed_date": uni["listed_date"], "delisted_date": uni["delisted_date"],
                         "delist_reason": uni["delist_reason"], "sector": None})
    fetched_at = sorted(r.get("fetched_at") for r in latest.values() if r.get("fetched_at"))
    prov = {
        "format": "naver_raw", "data_dir": str(d), "universe_rows": int(len(uni)),
        "universe_listed": int((uni["status"] == "listed").sum()), "universe_delisted": int((uni["status"] == "delisted").sum()),
        "files_missing": missing_files, "symbols_without_bars_in_window": empty,
        "fetch_failures": [s for s, r in latest.items() if not r.get("ok", True)],
        "fetched_at_range": [fetched_at[0], fetched_at[-1]] if fetched_at else None,
        "price_source": "Naver Finance daily chart (fchart.stock.naver.com, unofficial endpoint, no key)",
        "listing_source": "FinanceDataReader StockListing('KRX'/'KRX-DELISTING') — KRX listing snapshots cached on GitHub",
        "index_source": "FinanceDataReader DataReader('KS11'/'KQ11') — KRX index history cached on GitHub",
        "manifest_sha256": {s: r.get("sha256") for s, r in latest.items()},
    }
    return RealDataset(bars=bars, instruments=inst, indices=indices, calendar=calendar,
                       issues=pd.concat(issues, ignore_index=True) if issues else pd.DataFrame(columns=["symbol", "date", "check", "severity", "detail"]),
                       halts=pd.concat(halts, ignore_index=True) if halts else pd.DataFrame(columns=["date", "kind", "symbol"]),
                       rejected=pd.concat(rejected, ignore_index=True) if rejected else pd.DataFrame(), provenance=prov)


def load_standard_csv(data_dir: str | Path, start: str | None = None, end: str | None = None) -> RealDataset:
    """User-supplied CSVs in the Pulsentra standard format (the default path when no provider is available)."""
    from app.data.ingest import parse_csv
    d = Path(data_dir)
    files = sorted(p for p in d.glob("*.csv") if p.name != "instruments.csv" and not p.name.startswith("index_"))
    indices = {p.stem.replace("index_", ""): _read_index(p) for p in d.glob("index_*.csv")}
    calendar = pd.DatetimeIndex(next(iter(indices.values()))["date"]) if indices else None
    parts, issues, rejected = [], [], []
    for f in files:
        df = parse_csv(f.read_bytes())
        if start:
            df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(start)]
        if end:
            df = df[pd.to_datetime(df["date"]) <= pd.Timestamp(end)]
        src = df["source"].iloc[0] if "source" in df and len(df) else "csv_import"
        df["source"] = src
        if source_type(src) != REAL:
            raise ValueError(f"{f.name}: REAL 데이터가 아닙니다 (source={src}).")
        if "turnover" not in df:
            df["turnover"] = df["close"] * df["volume"]
        rep = check_frame(df, calendar=calendar)
        if rep.issues:
            issues.append(pd.DataFrame(rep.issues))
        if len(rep.rejected):
            rejected.append(rep.rejected)
        parts.append(rep.valid)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if not bars.empty:
        bars["source_type"] = REAL
        bars["exchange"] = bars.get("exchange", "KR")
    inst_path = d / "instruments.csv"
    inst = pd.read_csv(inst_path, dtype={"symbol": str}) if inst_path.exists() else pd.DataFrame(
        {"market": bars["market"].unique() if not bars.empty else [], "symbol": None})
    if inst_path.exists() and "status" not in inst:
        inst["status"] = np.where(inst.get("delisted_date", pd.Series(dtype=object)).notna(), "delisted", "listed")
    return RealDataset(bars=bars, instruments=inst, indices=indices, calendar=calendar,
                       issues=pd.concat(issues, ignore_index=True) if issues else pd.DataFrame(columns=["symbol", "date", "check", "severity", "detail"]),
                       halts=pd.DataFrame(columns=["date", "symbol"]),
                       rejected=pd.concat(rejected, ignore_index=True) if rejected else pd.DataFrame(),
                       provenance={"format": "standard", "data_dir": str(d), "files": [p.name for p in files]})


def sample_description(ds: RealDataset) -> dict:
    b = ds.bars
    per = b.groupby("symbol")["date"].agg(["min", "max", "count"]) if not b.empty else pd.DataFrame()
    inst = ds.instruments.set_index("symbol") if "symbol" in ds.instruments and ds.instruments["symbol"].notna().any() else pd.DataFrame()
    delisted = inst.index[inst["status"] == "delisted"] if "status" in inst else []
    with_data = per.index.intersection(delisted) if len(per) else []
    lag = []
    for s in with_data:
        dd = inst.at[s, "delisted_date"]
        if isinstance(dd, str):
            lag.append((pd.Timestamp(dd) - per.at[s, "max"]).days)
    return {
        "instruments_with_bars": int(len(per)), "bars": int(len(b)),
        "first_date": b["date"].min().strftime("%Y-%m-%d") if len(b) else None,
        "last_date": b["date"].max().strftime("%Y-%m-%d") if len(b) else None,
        "by_exchange": b.drop_duplicates("symbol")["exchange"].value_counts().to_dict() if "exchange" in b else {},
        "delisted_in_universe": int(len(delisted)), "delisted_with_bars": int(len(with_data)),
        "delisting_gap_days_median": float(np.median(lag)) if lag else None,
        "survivorship": ("상장폐지 종목 포함 (목록 출처 기준). 목록에 없는 폐지 종목과 데이터가 없는 폐지 종목은 빠져 있음"
                         if len(with_data) else "상장폐지 종목 없음 — 생존편향 통제되지 않음"),
        "halt_marker_rows_removed": int(len(ds.halts)), "rows_rejected": int(len(ds.rejected)),
    }
