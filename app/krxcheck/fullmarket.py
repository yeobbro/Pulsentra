"""Whole-market KRX check for one date: fetch the market answer ONCE and keep it, then compare it with the local
naver_raw CSVs OFFLINE as often as needed.

  python -m app.krxcheck.fullmarket fetch   --date YYYY-MM-DD --market KOSPI --out DIR [--live --spec-confirmed]
  python -m app.krxcheck.fullmarket compare --krx-dir DIR --data data/real --date YYYY-MM-DD [--out DIR/compare]

fetch  : one request for (market service, date), no retries; refuses a non-empty --out so the same date is not fetched
         twice by accident. Writes the answer exactly as received (all values as text), the normalised frame and
         fetch_meta.json; on failure error.json. The key is never printed or written.
compare: reads only local files (the saved KRX answer and data_root/raw/<symbol>.csv); makes no network call and
         never writes under data_root.

Statuses in comparison_detail.csv are mutually exclusive and decided in this order (first that applies):
  krx_row_invalid        KRX row with an unusable code, a code repeated in the answer, or an unparsable OHLCV value
  missing_local_file     no data_root/raw/<symbol>.csv  (in_universe tells collection gap vs. out of scope)
  local_row_invalid      the date appears more than once in the local file, or a local OHLCV value is unparsable
  missing_local_date     the local file has no row for the date
  not_comparable         no trade on one side (KRX volume 0 / price missing or 0, or a local halt marker O=H=L=0)
  match                  O, H, L, C and volume all equal
  volume_mismatch_only   O, H, L, C equal, volume differs
  close_mismatch         close differs (other fields may differ too; see the *_equal columns)
  ohl_mismatch_close_eq  close equal, at least one of O/H/L differs
"OHLC mismatch (any)" = close_mismatch + ohl_mismatch_close_eq is reported separately as an overlapping total.
Rates use only comparable rows (match + volume_mismatch_only + close_mismatch + ohl_mismatch_close_eq) as the
denominator, and the denominator is always reported next to the rate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from app.providers.krx import SERVICES, SPEC_SOURCE, STOCK_SERVICE_FOR_MARKET, KRXAPIError, KRXClient, parse_stock_rows, short_code
from app.research.krx_crosscheck import classify_pair
from app.research.manifest import environment_info, git_info, guard_output_dir

FIELDS = ("open", "high", "low", "close", "volume")
PRICE = ("open", "high", "low", "close")

KRX_ROW_INVALID = "krx_row_invalid"
MISSING_LOCAL_FILE = "missing_local_file"
LOCAL_ROW_INVALID = "local_row_invalid"
MISSING_LOCAL_DATE = "missing_local_date"
NOT_COMPARABLE = "not_comparable"
MATCH = "match"
VOLUME_ONLY = "volume_mismatch_only"
CLOSE_MISMATCH = "close_mismatch"
OHL_MISMATCH = "ohl_mismatch_close_eq"
STATUS_ORDER = (KRX_ROW_INVALID, MISSING_LOCAL_FILE, LOCAL_ROW_INVALID, MISSING_LOCAL_DATE, NOT_COMPARABLE,
                MATCH, VOLUME_ONLY, CLOSE_MISMATCH, OHL_MISMATCH)
COMPARABLE = (MATCH, VOLUME_ONLY, CLOSE_MISMATCH, OHL_MISMATCH)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _num(v) -> float:
    """Text -> float; '', '-', None or junk -> NaN (never 0)."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    s = str(v).strip().replace(",", "")
    if s in ("", "-"):
        return np.nan
    try:
        x = float(s)
    except ValueError:
        return np.nan
    return x if np.isfinite(x) else np.nan


def norm_code(v) -> str | None:
    """Symbol code as a 6-character string: 5930 / '5930' / '005930' / 'KR7005930003' -> '005930'.
    Integers lose their leading zeros when a CSV is read without dtype=str; this puts them back."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return short_code(s)


# ---------------------------------------------------------------- fetch (network, once)

def fetch_once(client, market: str, bas_dd: str, out: Path) -> tuple[int, dict]:
    """One request for the whole market on bas_dd. Returns (exit code, meta). Never retries a failed call."""
    svc_key = STOCK_SERVICE_FOR_MARKET[market]
    svc = SERVICES[svc_key]
    meta = {"service": svc_key, "service_name_ko": svc.name_ko, "url": svc.url, "basDd": bas_dd, "market": market,
            "requested_at_utc": pd.Timestamp.now(tz="UTC").isoformat(), "spec_source": SPEC_SOURCE,
            "git": git_info(), "environment": environment_info()}
    out.mkdir(parents=True, exist_ok=True)
    try:
        res = client.fetch(svc_key, bas_dd)
    except KRXAPIError as exc:
        meta.update({"status": "failed", "error_kind": exc.kind, "error": str(exc)[:300],
                     "requests_sent": getattr(client, "calls", None), "http_attempts": getattr(client, "http_attempts", None)})
        _write_json(out / "error.json", meta)
        return 4, meta
    meta.update({"received_at_utc": pd.Timestamp.now(tz="UTC").isoformat(), "status": res.status,
                 "requests_sent": getattr(client, "calls", None), "http_attempts": getattr(client, "http_attempts", None)})
    if res.status == "empty":
        meta.update({"rows": 0, "completeness": "KRX answered with no rows (holiday, a date not yet published, or no data)"})
        _write_json(out / "fetch_meta.json", meta)
        return 5, meta
    raw_path = out / f"krx_raw_{svc_key}_{bas_dd}.csv"
    pd.DataFrame(res.raw).astype(str).to_csv(raw_path, index=False)        # values exactly as received (text)
    norm_path = out / f"krx_{svc_key}_{bas_dd}.csv"
    res.frame.to_csv(norm_path, index=False)
    codes = res.frame["symbol"]
    meta.update({
        "rows": len(res.raw), "unique_codes": int(codes.nunique()), "unusable_codes": int(codes.isna().sum()),
        "duplicate_codes": int(codes.dropna().duplicated().sum()), "unparsed_values": res.unparsed_values,
        "isu_cd_forms_seen": sorted({"isin" if len(str(x)) == 12 else "short" for x in res.frame["isu_cd_raw"]}),
        "files": {raw_path.name: _sha256(raw_path), norm_path.name: _sha256(norm_path)},
        "completeness": ("one answer, no paging in the official spec (OutBlock_1 only); completeness cannot be proven "
                         "from the answer itself — compare 'rows' with an independent listing count"),
    })
    _write_json(out / "fetch_meta.json", meta)
    return 0, meta


# ---------------------------------------------------------------- compare (offline)

def load_krx_answer(krx_dir: Path, bas_dd: str, market: str = "KOSPI") -> tuple[pd.DataFrame, dict]:
    """Saved raw answer -> normalised frame via the same parser the live client uses."""
    svc_key = STOCK_SERVICE_FOR_MARKET[market]
    raw_path = krx_dir / f"krx_raw_{svc_key}_{bas_dd}.csv"
    raw = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
    frame, _ = parse_stock_rows(raw.to_dict("records"), SERVICES[svc_key])
    meta_path = krx_dir / "fetch_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["krx_raw_file"], meta["krx_raw_sha256"] = str(raw_path), _sha256(raw_path)
    return frame, meta


def _local_rows(path: Path, day: str) -> list[dict]:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    d = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df[d == day].to_dict("records")


def compare_market(krx: pd.DataFrame, data_root: Path, day: str, universe: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """krx: normalised KRX frame for one date (parse_stock_rows output). day: 'YYYY-MM-DD'.
    Returns (one row per KRX row, {'local_files_read': n, 'local_fingerprint': sha256 of the read files' hashes})."""
    raw_dir = Path(data_root) / "raw"
    uni = {} if universe is None else {norm_code(s): st for s, st in zip(universe["symbol"], universe["status"])}
    k = krx.copy()
    k["code"] = [norm_code(x) for x in k["isu_cd_raw"]]
    dup = k["code"].notna() & k["code"].duplicated(keep=False)
    rows, hashes = [], []
    for i, r in k.iterrows():
        code = r["code"]
        kv = {f: _num(r.get(f)) for f in FIELDS}
        base = {"symbol": code, "isu_cd_raw": r.get("isu_cd_raw"), "name": r.get("name"), "date": day,
                "in_universe": code in uni, "universe_status": uni.get(code),
                **{f"krx_{f}": kv[f] for f in FIELDS}}
        out = {**base, **{f"local_{f}": np.nan for f in FIELDS}, "local_file": False, "local_rows_for_date": 0}

        def done(status, reason):
            rows.append({**out, "status": status, "reason": reason})

        if code is None:
            done(KRX_ROW_INVALID, f"unusable KRX code {r.get('isu_cd_raw')!r}")
            continue
        if dup.iloc[i]:
            done(KRX_ROW_INVALID, "code appears more than once in the KRX answer")
            continue
        # a KRX row without trades is legitimate ('-' or 0 prices); only text that is present but not a number is invalid
        bad_krx = [f for f in FIELDS if np.isnan(kv[f]) and str(r.get(f, "")).strip() not in ("", "-", "nan")]
        if bad_krx:
            done(KRX_ROW_INVALID, f"unparsable KRX values: {bad_krx}")
            continue
        path = raw_dir / f"{code}.csv"
        if not path.exists():
            done(MISSING_LOCAL_FILE, "in the collection universe but no local file" if code in uni
                 else "not in the collection universe (e.g. preferred share, REIT, fund, foreign listing)")
            continue
        out["local_file"] = True
        hashes.append(_sha256(path))
        lr = _local_rows(path, day)
        out["local_rows_for_date"] = len(lr)
        if len(lr) > 1:
            done(LOCAL_ROW_INVALID, f"{len(lr)} local rows for the date")
            continue
        if not lr:
            done(MISSING_LOCAL_DATE, "local file has no row for the date")
            continue
        lv = {f: _num(lr[0].get(f)) for f in FIELDS}
        out.update({f"local_{f}": lv[f] for f in FIELDS})
        bad_loc = [f for f in FIELDS if np.isnan(lv[f])]
        if bad_loc:
            done(LOCAL_ROW_INVALID, f"missing/unparsable local values: {bad_loc}")
            continue
        if any(np.isnan(kv[f]) or kv[f] <= 0 for f in PRICE) or np.isnan(kv["volume"]) or kv["volume"] == 0:
            done(NOT_COMPARABLE, "no trade on KRX that day (zero/missing price or zero volume)")
            continue
        if lv["open"] == 0 and lv["high"] == 0 and lv["low"] == 0:
            done(NOT_COMPARABLE, "local row is a halt marker (O=H=L=0)")
            continue
        eq = {f: bool(abs(lv[f] - kv[f]) < 0.5) for f in FIELDS}      # integer KRW prices and share counts
        out.update({f"{f}_equal": eq[f] for f in FIELDS})
        out["close_abs_diff"] = abs(lv["close"] - kv["close"])
        out["close_rel_diff"] = (lv["close"] - kv["close"]) / kv["close"]
        out["volume_diff"] = lv["volume"] - kv["volume"]
        out["volume_ratio_local_to_krx"] = lv["volume"] / kv["volume"]
        out["classifier_status"] = classify_pair(lv, kv)["status"]   # the existing sample classifier, for cross-reference
        if all(eq.values()):
            done(MATCH, "")
        elif all(eq[f] for f in PRICE):
            done(VOLUME_ONLY, f"volume local {lv['volume']:.0f} vs KRX {kv['volume']:.0f}")
        elif not eq["close"]:
            others = [f for f in ("open", "high", "low", "volume") if not eq[f]]
            done(CLOSE_MISMATCH, f"close local {lv['close']:.0f} vs KRX {kv['close']:.0f}" + (f"; also {others}" if others else ""))
        else:
            done(OHL_MISMATCH, "differs: " + ", ".join(f for f in ("open", "high", "low", "volume") if not eq[f]))
    detail = pd.DataFrame(rows)
    fp = hashlib.sha256("".join(hashes).encode()).hexdigest()[:16]
    return detail, {"local_files_read": len(hashes), "local_fingerprint": fp}


def summarize_market(detail: pd.DataFrame, top: int = 15) -> dict:
    n = len(detail)
    counts = {s: int((detail["status"] == s).sum()) if n else 0 for s in STATUS_ORDER}
    comp = detail[detail["status"].isin(COMPARABLE)].copy() if n else detail
    m = len(comp)
    for f in FIELDS:                    # *_equal is empty for rows that were not compared -> object dtype in the frame
        if m:
            comp[f"{f}_equal"] = comp[f"{f}_equal"].astype(bool)

    def rate(x: int, d: int):
        return None if d == 0 else round(x / d, 6)
    close_eq = int(comp["close_equal"].sum()) if m else 0
    vol_eq = int(comp["volume_equal"].sum()) if m else 0
    ohlc_any = counts[CLOSE_MISMATCH] + counts[OHL_MISMATCH]
    s = {
        "krx_rows": n, "by_status": counts, "comparable": m,
        "comparable_definition": "match + volume_mismatch_only + close_mismatch + ohl_mismatch_close_eq",
        "not_compared": n - m,
        "rates_over_comparable": {"denominator": m, "all_ohlcv_match": rate(counts[MATCH], m), "close_equal": rate(close_eq, m),
                                  "volume_equal": rate(vol_eq, m), "open_equal": rate(int(comp["open_equal"].sum()) if m else 0, m),
                                  "high_equal": rate(int(comp["high_equal"].sum()) if m else 0, m),
                                  "low_equal": rate(int(comp["low_equal"].sum()) if m else 0, m)},
        "overlapping_totals": {"ohlc_mismatch_any": ohlc_any, "close_mismatch": counts[CLOSE_MISMATCH],
                               "volume_mismatch_any": m - vol_eq, "price_equal_volume_differs": counts[VOLUME_ONLY]},
        "missing_local_file_in_universe": int(((detail["status"] == MISSING_LOCAL_FILE) & detail["in_universe"]).sum()) if n else 0,
    }
    if m:
        c = comp.assign(abs_vol_dev=(comp["volume_ratio_local_to_krx"] - 1).abs())
        keep = ["symbol", "name", "krx_close", "local_close", "close_abs_diff", "close_rel_diff", "krx_volume", "local_volume",
                "volume_ratio_local_to_krx", "status"]
        s["top_close_diff"] = c[c["close_abs_diff"] > 0].sort_values("close_abs_diff", ascending=False)[keep].head(top).to_dict("records")
        s["top_close_rel_diff"] = c[c["close_abs_diff"] > 0].assign(a=c["close_rel_diff"].abs()).sort_values("a", ascending=False)[keep].head(top).to_dict("records")
        s["top_volume_diff"] = c[~c["volume_equal"]].sort_values("abs_vol_dev", ascending=False)[keep].head(top).to_dict("records")
        vr = comp.loc[~comp["volume_equal"], "volume_ratio_local_to_krx"]
        s["volume_ratio_when_different"] = ({"n": int(len(vr)), "min": float(vr.min()), "median": float(vr.median()),
                                             "max": float(vr.max()), "share_local_below_krx": round(float((vr < 1).mean()), 4)}
                                            if len(vr) else {"n": 0})
        cr = comp.loc[~comp["close_equal"], "close_rel_diff"]
        s["close_rel_diff_when_different"] = ({"n": int(len(cr)), "min": float(cr.min()), "median": float(cr.median()),
                                               "max": float(cr.max()), "median_abs": float(cr.abs().median())} if len(cr) else {"n": 0})
    return s


def _md(summary: dict, meta: dict) -> str:
    L = [f"# KRX vs local (naver_raw) — {summary['date']} {summary['market']}", "",
         f"- KRX answer: {meta.get('krx_raw_file')} (sha256 {str(meta.get('krx_raw_sha256'))[:16]}…), fetched {meta.get('received_at_utc')}",
         f"- local files read: {summary['local_files_read']} (fingerprint {summary['local_fingerprint']}) — offline, no network",
         f"- KRX rows: **{summary['krx_rows']}**, comparable: **{summary['comparable']}**, not compared: {summary['not_compared']}", "",
         "| status (mutually exclusive) | rows |", "|---|---:|"]
    L += [f"| `{k}` | {v} |" for k, v in summary["by_status"].items()]
    r = summary["rates_over_comparable"]
    L += ["", f"Rates over comparable rows (denominator {r['denominator']}):", "",
          "| measure | rate |", "|---|---:|"]
    L += [f"| {k} | {'—' if v is None else f'{v:.2%}'} |" for k, v in r.items() if k != "denominator"]
    o = summary["overlapping_totals"]
    L += ["", f"Overlapping totals: OHLC mismatch (any) {o['ohlc_mismatch_any']}, close mismatch {o['close_mismatch']}, "
          f"volume differs (any) {o['volume_mismatch_any']}.",
          f"In the collection universe but no local file: {summary['missing_local_file_in_universe']}."]
    for key, title in (("top_close_diff", "Largest close differences (KRW)"), ("top_volume_diff", "Largest volume differences (ratio)")):
        rows = summary.get(key) or []
        if rows:
            L += ["", f"## {title}", "", "| symbol | name | KRX close | local close | KRX volume | local volume | local/KRX vol | status |",
                  "|---|---|---:|---:|---:|---:|---:|---|"]
            L += [f"| {x['symbol']} | {x['name']} | {x['krx_close']:,.0f} | {x['local_close']:,.0f} | {x['krx_volume']:,.0f} | "
                  f"{x['local_volume']:,.0f} | {x['volume_ratio_local_to_krx']:.4f} | {x['status']} |" for x in rows]
    L += ["", "Statuses and their order are defined in `app/krxcheck/fullmarket.py`. A disagreement is not by itself proof "
          "that either side is wrong — see docs/DATA_SOURCES.md."]
    return "\n".join(L) + "\n"


def run_compare(krx_dir: Path, data_root: Path, day: str, out: Path, market: str = "KOSPI", overwrite: bool = False) -> dict:
    guard_output_dir(out, data_root, overwrite)
    bas_dd = pd.Timestamp(day).strftime("%Y%m%d")
    krx, meta = load_krx_answer(krx_dir, bas_dd, market)
    uni_path = Path(data_root) / "universe.csv"
    uni = pd.read_csv(uni_path, dtype=str) if uni_path.exists() else None
    detail, info = compare_market(krx, data_root, pd.Timestamp(day).strftime("%Y-%m-%d"), uni)
    summary = {"date": pd.Timestamp(day).strftime("%Y-%m-%d"), "market": market, **info,
               "krx_raw_sha256": meta["krx_raw_sha256"], "krx_fetch_received_at_utc": meta.get("received_at_utc"),
               **summarize_market(detail), "compared_at_utc": pd.Timestamp.now(tz="UTC").isoformat(), "git": git_info()}
    out.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out / "comparison_detail.csv", index=False)
    _write_json(out / "comparison_summary.json", summary)
    (out / "COMPARISON.md").write_text(_md(summary, meta), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None, client=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.krxcheck.fullmarket")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="one KRX request for the whole market on one date")
    f.add_argument("--date", required=True)
    f.add_argument("--market", default="KOSPI", choices=sorted(STOCK_SERVICE_FOR_MARKET))
    f.add_argument("--out", required=True)
    f.add_argument("--live", action="store_true")
    f.add_argument("--spec-confirmed", action="store_true")
    c = sub.add_parser("compare", help="offline comparison of a saved KRX answer with local CSVs")
    c.add_argument("--krx-dir", required=True)
    c.add_argument("--data", required=True)
    c.add_argument("--date", required=True)
    c.add_argument("--market", default="KOSPI", choices=sorted(STOCK_SERVICE_FOR_MARKET))
    c.add_argument("--out", default=None, help="default: <krx-dir>/compare")
    c.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    try:
        bas_dd = pd.Timestamp(a.date).strftime("%Y%m%d")
    except ValueError:
        print("--date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if a.cmd == "fetch":
        out = Path(a.out)
        if out.exists() and any(out.iterdir()):
            print(f"{out} is not empty: refusing to fetch again (the saved answer is reused by 'compare')", file=sys.stderr)
            return 2
        if not (a.live and a.spec_confirmed):
            print(f"dry run: would send 1 request {SERVICES[STOCK_SERVICE_FOR_MARKET[a.market]].url}?basDd={bas_dd}"
                  " — add --live --spec-confirmed to send it", flush=True)
            return 0
        cl = client or KRXClient(spec_confirmed=True, max_retries=0)
        if not cl.configured:
            print("KRX_API_KEY not set: nothing requested", file=sys.stderr)
            return 3
        code, meta = fetch_once(cl, a.market, bas_dd, out)
        print(f"{meta['status']}: rows={meta.get('rows')} requests_sent={meta.get('requests_sent')} "
              f"http_attempts={meta.get('http_attempts')} {meta.get('error_kind') or ''} -> {out}", flush=True)
        return code
    out = Path(a.out) if a.out else Path(a.krx_dir) / "compare"
    try:
        s = run_compare(Path(a.krx_dir), Path(a.data), a.date, out, a.market, a.overwrite)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"KRX rows {s['krx_rows']}, comparable {s['comparable']}, statuses {s['by_status']} -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
