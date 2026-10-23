"""A synthetic `naver_raw` folder with one injected anomaly per instrument (TEST data — never market data).

Prices are a smooth random walk (|daily move| < 2%) and volumes vary by ~10%, so nothing is flagged except what is
injected. `EXPECTED` documents the anomaly each symbol carries; tests/test_audit.py asserts the audit finds it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

N = 320
HOLIDAYS = ["2023-03-01", "2023-05-05", "2023-06-06"]


def calendar() -> pd.DatetimeIndex:
    d = pd.bdate_range("2023-01-02", periods=N + len(HOLIDAYS))
    return d[~d.isin(pd.to_datetime(HOLIDAYS))][:N]


def _base(seed: int, n: int = N, price: float = 10_000.0, volume: float = 10_000.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = price * np.cumprod(1 + rng.uniform(-0.015, 0.015, n))
    open_ = close * (1 + rng.uniform(-0.004, 0.004, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.01, n))
    vol = volume * np.exp(rng.normal(0, 0.1, n))
    df = pd.DataFrame({"date": calendar()[:n], "open": open_.round(0), "high": high.round(0), "low": low.round(0),
                       "close": close.round(0), "volume": vol.round(0)})
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    return df


def build() -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    raw: dict[str, pd.DataFrame] = {}
    uni = []

    def add(sym, df, status="listed", listed=None, delisted=None):
        raw[sym] = df
        uni.append({"symbol": sym, "name": f"TEST{sym}", "market": "KOSDAQ", "listed_date": listed, "delisted_date": delisted,
                    "delist_reason": None, "status": status})

    add("000010", _base(1))                                          # clean
    d = _base(2)                                                      # halt markers: 3 zero-OHL + 2 flat zero-volume
    for i in (100, 101, 102):
        d.loc[i, ["open", "high", "low"]] = 0
        d.loc[i, "close"] = d.loc[99, "close"]
        d.loc[i, "volume"] = 0
    for i in (200, 201):
        d.loc[i, ["open", "high", "low", "close"]] = d.loc[199, "close"]
        d.loc[i, "volume"] = 0
    add("000020", d)
    d = _base(3)                                                      # close = 0 on 10 rows
    d.loc[150:159, "close"] = 0
    add("000030", d)
    d = _base(4)                                                      # one broken OHLC row + one rounding-off row
    d.loc[120, ["open", "high", "low", "close"]] = [10_000, 9_000, 9_500, 10_000]
    d.loc[220, "close"] = d.loc[220, "high"] + 1
    d.loc[220, "open"] = d.loc[220, "low"]
    add("000040", d)
    d = _base(5)                                                      # split: adjusted prices, volume x10, 3-session halt
    d.loc[150:, "volume"] *= 10
    d = d.drop(index=[147, 148, 149]).reset_index(drop=True)
    add("000050", d)
    d = _base(6)                                                      # +45% on consecutive sessions; +35% after a 2-session gap
    d.loc[100:, ["open", "high", "low", "close"]] *= 1.45
    d.loc[250:, ["open", "high", "low", "close"]] *= 1.35
    d = d.drop(index=[248, 249]).reset_index(drop=True)
    add("000060", d)
    d = _base(7)                                                      # one identical and one conflicting duplicate
    dup1 = d.loc[[50]]
    dup2 = d.loc[[60]].assign(close=d.loc[60, "close"] * 1.01, high=d.loc[60, "high"] * 1.01)
    d = pd.concat([d, dup1, dup2]).sort_values("date", kind="stable").reset_index(drop=True)
    add("000070", d)
    cal = calendar()
    add("000080", _base(8), status="delisted", listed=cal[10].strftime("%Y-%m-%d"), delisted=cal[N - 6].strftime("%Y-%m-%d"))
    add("000090", _base(9, n=200), status="delisted", delisted=(cal[199] + pd.Timedelta(60, unit="D")).strftime("%Y-%m-%d"))
    d = _base(10).astype({"date": str, "volume": str})                # unparsable date and a non-numeric volume
    d.loc[30, "date"] = "2023-13-45"
    d.loc[40, "volume"] = "abc"
    add("000100", d)
    uni.append({"symbol": "000110", "name": "TEST000110", "market": "KOSPI", "listed_date": None, "delisted_date": "2023-06-30",
                "delist_reason": "test", "status": "delisted"})         # delisted, no file
    d = _base(12)                                                     # a Saturday row
    sat = d.loc[[80]].copy()
    sat["date"] = pd.DatetimeIndex(["2023-04-22"]).astype(d["date"].dtype)   # same unit as d, so concat stays clean
    d = pd.concat([d, sat]).sort_values("date", kind="stable").reset_index(drop=True)
    add("000120", d)
    d = _base(13)                                                     # volume x10 with no gap (bonus-issue-like)
    d.loc[160:, "volume"] *= 10
    add("000130", d)
    return raw, pd.DataFrame(uni)


def write_naver_raw(path: Path) -> Path:
    raw, uni = build()
    (path / "raw").mkdir(parents=True, exist_ok=True)
    uni.to_csv(path / "universe.csv", index=False)
    lines = []
    for sym, df in raw.items():
        f = path / "raw" / f"{sym}.csv"
        out = df.copy()
        if pd.api.types.is_datetime64_any_dtype(out["date"]):
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
        out.to_csv(f, index=False)
        lines.append(json.dumps({"symbol": sym, "source": "test", "fetched_at": "2026-10-23T00:00:00+00:00", "rows": len(df),
                                 "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "ok": True}))
    (path / "manifest.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cal = calendar()
    idx = pd.DataFrame({"Date": cal.strftime("%Y-%m-%d"), "Close": np.linspace(2500, 2600, len(cal))})
    idx.to_csv(path / "index_KOSPI.csv", index=False)
    idx.to_csv(path / "index_KOSDAQ.csv", index=False)
    return path
