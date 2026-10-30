"""Refresh a provenance-stamped Korean instrument-master snapshot.

FinanceDataReader's KRX listing is a third-party cached snapshot, not an official
real-time API. This command is for research/reference use pending source-terms review.
It never downloads price history and never writes to the candles table.

Usage:
  python scripts/refresh_instrument_master.py --out data/reference/instruments
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

SOURCE = "financedatareader_stocklisting_krx"
SOURCE_URL = "https://github.com/FinanceData/FinanceDataReader"
LICENSE_NOTE = (
    "Third-party cached KRX listing snapshot. Not an official real-time API. "
    "Review upstream terms and redistribution rights before public/commercial use."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_listing(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize FDR StockListing('KRX') while retaining the full returned universe."""
    if frame.empty:
        raise ValueError("KRX listing response is empty; refusing to replace a prior snapshot")
    required = {"Code", "Name", "Market"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"KRX listing is missing required columns: {', '.join(sorted(missing))}")

    out = pd.DataFrame()
    out["market"] = frame["Market"].astype(str).str.strip().str.upper().replace(
        {"KOSDAQ GLOBAL": "KOSDAQ"}
    )
    out["symbol"] = frame["Code"].astype(str).str.strip().str.zfill(6)
    out["name"] = frame["Name"].astype(str).str.strip()
    out["exchange"] = out["market"]
    out["sector"] = frame["Sector"].astype(str).str.strip() if "Sector" in frame else None
    out["listed_date"] = (
        pd.to_datetime(frame["ListingDate"], errors="coerce").dt.strftime("%Y-%m-%d")
        if "ListingDate" in frame else None
    )
    out["instrument_type"] = (
        frame["Dept"].astype(str).str.strip() if "Dept" in frame else None
    )
    out = out[out["symbol"].str.fullmatch(r"\d{6}") & out["name"].ne("")].copy()
    if out.empty:
        raise ValueError("No valid six-digit instruments in KRX listing response")
    if out["symbol"].duplicated().any():
        duplicates = out.loc[out["symbol"].duplicated(), "symbol"].head(5).tolist()
        raise ValueError(f"Duplicate instrument codes in listing response: {duplicates}")
    return out.sort_values(["symbol"], kind="stable").reset_index(drop=True)


def write_snapshot(frame: pd.DataFrame, out_dir: Path, fetched_at: str | None = None) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = fetched_at or utc_now()
    csv_path = out_dir / "krx_instruments.csv"
    temp_path = csv_path.with_suffix(".csv.tmp")
    frame.to_csv(temp_path, index=False, encoding="utf-8-sig")
    digest = hashlib.sha256(temp_path.read_bytes()).hexdigest()
    temp_path.replace(csv_path)
    manifest = {
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "source_type": "REFERENCE_SNAPSHOT",
        "fetched_at": stamp,
        "rows": int(len(frame)),
        "sha256": digest,
        "file": csv_path.name,
        "license_note": LICENSE_NOTE,
        "contains_market_prices": False,
        "redistribution_approved": False,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def fetch_listing() -> pd.DataFrame:
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise RuntimeError(
            "FinanceDataReader is not installed. Install project requirements first."
        ) from exc
    return fdr.StockListing("KRX")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data/reference/instruments")
    args = parser.parse_args()
    listing = fetch_listing()
    normalized = normalize_listing(listing)
    manifest = write_snapshot(normalized, Path(args.out))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
