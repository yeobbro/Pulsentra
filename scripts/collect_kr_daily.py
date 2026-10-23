"""Collect REAL Korean daily bars for research (v0.3).

Sources (none of them is an official, keyed API — see docs/DATA_SOURCES.md):
  * prices  : Naver Finance chart endpoint
              https://fchart.stock.naver.com/sise.nhn?timeframe=day&count=6000&requestType=0&symbol=<code>
              -> date|open|high|low|close|volume. Prices are ADJUSTED for splits/rights; VOLUME IS NOT.
              No turnover (거래대금) field.
  * listings: FinanceDataReader StockListing('KRX') / ('KRX-DELISTING') — snapshots of KRX data
              cached on GitHub by the FinanceData project.
  * indices : FinanceDataReader DataReader('KS11'/'KQ11') — KRX index history cached on GitHub.
The raw files are kept locally for research; check each provider's terms before any other use.

Usage:
  python scripts/collect_kr_daily.py --out data/real --start 2016-01-01 [--limit N] [--sleep 0.15] [--workers 2]
Re-running resumes: symbols that already have a raw file are skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

NAVER_URL = "https://fchart.stock.naver.com/sise.nhn?timeframe=day&count={count}&requestType=0&symbol={code}"
UA = {"User-Agent": "Mozilla/5.0 (research; Pulsentra v0.3 data collection)"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_naver(code: str, count: int = 6000, timeout: float = 20.0, retries: int = 2) -> pd.DataFrame:
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(NAVER_URL.format(code=code, count=count), headers=UA, timeout=timeout)
            r.raise_for_status()
            items = re.findall(r'<item data="(.*?)" />', r.text, re.DOTALL)
            if not items:
                return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
            df = pd.read_csv(StringIO("\n".join(items)), sep="|", header=None, dtype={0: str})
            df.columns = ["date", "open", "high", "low", "close", "volume"]
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            return df.sort_values("date").reset_index(drop=True)
        except Exception as exc:  # network / parse
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{code}: {last}")


def build_universe(out: Path, start: str) -> pd.DataFrame:
    import FinanceDataReader as fdr
    listed = fdr.StockListing("KRX")
    listed.to_csv(out / "listing_current.csv", index=False)
    delisted = fdr.StockListing("KRX-DELISTING")
    delisted.to_csv(out / "listing_delisted.csv", index=False)
    cur = listed[listed["Market"].isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"])].copy()
    cur = cur[cur["Code"].str.fullmatch(r"\d{5}0") & ~cur["Name"].str.contains("스팩", na=False)]
    cur = pd.DataFrame({"symbol": cur["Code"], "name": cur["Name"], "market": cur["Market"].replace({"KOSDAQ GLOBAL": "KOSDAQ"}),
                        "listed_date": None, "delisted_date": None, "delist_reason": None, "status": "listed"})
    d = delisted.copy()
    d["DelistingDate"] = pd.to_datetime(d["DelistingDate"], errors="coerce")
    d = d[d["Market"].isin(["KOSPI", "KOSDAQ"]) & (d["SecuGroup"] == "주권") & (d["Kind"] == "보통주")
          & (d["DelistingDate"] >= pd.Timestamp(start)) & d["Symbol"].astype(str).str.fullmatch(r"\d{5}0")
          & ~d["Name"].str.contains("스팩", na=False)]
    d = pd.DataFrame({"symbol": d["Symbol"].astype(str), "name": d["Name"], "market": d["Market"],
                      "listed_date": pd.to_datetime(d["ListingDate"], errors="coerce").dt.strftime("%Y-%m-%d"),
                      "delisted_date": d["DelistingDate"].dt.strftime("%Y-%m-%d"), "delist_reason": d["Reason"], "status": "delisted"})
    # a code can be re-used after delisting; keep the current listing and the delisted record separately keyed by status
    uni = pd.concat([cur, d[~d["symbol"].isin(cur["symbol"])]], ignore_index=True).drop_duplicates("symbol", keep="first")
    uni.to_csv(out / "universe.csv", index=False)
    return uni


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/real")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--workers", type=int, default=2)
    a = ap.parse_args()
    out = Path(a.out)
    raw = out / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    uni_path = out / "universe.csv"
    uni = pd.read_csv(uni_path, dtype={"symbol": str}) if uni_path.exists() else build_universe(out, a.start)
    if a.limit:
        uni = uni.head(a.limit)
    manifest = out / "manifest.jsonl"
    # bars needed from --start to today, plus a margin (count = number of most recent bars returned)
    count = int(min(6000, max(300, (pd.Timestamp.today() - pd.Timestamp(a.start)).days * 0.75 + 60)))
    todo = [r for r in uni.itertuples() if not (raw / f"{r.symbol}.csv").exists()]
    lock = threading.Lock()

    def work(row) -> None:
        f = raw / f"{row.symbol}.csv"
        rec = {"symbol": row.symbol, "status": row.status, "source": "naver_fchart",
               "url": NAVER_URL.format(code=row.symbol, count=count), "fetched_at": now()}
        try:
            df = fetch_naver(row.symbol, count)
            df = df[df["date"] >= pd.Timestamp(a.start)]
            df.to_csv(f, index=False)
            rec.update({"rows": int(len(df)), "first": df["date"].min().strftime("%Y-%m-%d") if len(df) else None,
                        "last": df["date"].max().strftime("%Y-%m-%d") if len(df) else None,
                        "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "ok": True})
        except Exception as exc:
            rec.update({"ok": False, "error": str(exc)[:300]})
        with lock:
            with manifest.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        time.sleep(a.sleep)

    with ThreadPoolExecutor(max_workers=max(1, min(a.workers, 3))) as ex:  # at most 3 concurrent requests
        for k, _ in enumerate(ex.map(work, todo), 1):
            if k % 100 == 0:
                print(f"{k}/{len(todo)} fetched", flush=True)
    # indices (KRX index history cached on GitHub by the FinanceData project)
    import FinanceDataReader as fdr
    for code, name in (("KS11", "KOSPI"), ("KQ11", "KOSDAQ")):
        p = out / f"index_{name}.csv"
        if p.exists():
            continue
        idx = fdr.DataReader(code, a.start).reset_index()
        idx.to_csv(p, index=False)
        with manifest.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"symbol": name, "status": "index", "source": "fdr_krx_index_cache(github)", "fetched_at": now(),
                                 "rows": int(len(idx)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "ok": True}) + "\n")
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
