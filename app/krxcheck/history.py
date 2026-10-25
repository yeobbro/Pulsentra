"""Multi-date data-quality report from SAVED whole-market comparisons. Offline: reads only the files written by
`python -m app.krxcheck.fullmarket compare` (comparison_detail.csv, and comparison_summary.json for a consistency
check); never calls KRX, Naver or any other network service, and never writes next to its inputs.

  python -m app.krxcheck.history --compare-dir DIR [DIR ...] --out data/krxcheck/history_<name>
  python -m app.krxcheck.history --krx-root data/krxcheck --out data/krxcheck/history_<name>   # every full_market_*/compare

Counting rules
  * Statuses in comparison_detail.csv are mutually exclusive (see fullmarket.STATUS_ORDER); per-date counts use them as is.
  * Rates use only comparable rows (match + volume_mismatch_only + close_mismatch + ohl_mismatch_close_eq) as the
    denominator, per date. "OHLC mismatch (any)" and "volume differs (any)" overlap each other and are reported apart.
  * A (symbol, date) that appears more than once among comparable rows is left out of the rates and counted as
    `detail_duplicates` (which row to trust is unknown).
  * A date whose detail file is missing, empty, holds several dates, or lacks required columns is listed under `inputs`
    with the problem and left out of every statistic — nothing is filled in.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from app.krxcheck.fullmarket import (CLOSE_MISMATCH, COMPARABLE, KRX_ROW_INVALID, LOCAL_ROW_INVALID, MATCH, MISSING_LOCAL_DATE,
                                     MISSING_LOCAL_FILE, NOT_COMPARABLE, OHL_MISMATCH, STATUS_ORDER, VOLUME_ONLY)

FIELDS = ("open", "high", "low", "close", "volume")
REQUIRED = ("symbol", "date", "status")
REQUIRED_COMPARABLE = (*(f"krx_{f}" for f in FIELDS), *(f"local_{f}" for f in FIELDS), *(f"{f}_equal" for f in FIELDS))
SAMPLE_SYMBOL = "005930"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rate(x: int, d: int):
    return None if d == 0 else round(x / d, 6)


def _bool(s: pd.Series) -> pd.Series:
    return s.map(lambda v: str(v).strip().lower() in ("true", "1", "1.0"))


def load_detail(compare_dir: Path) -> tuple[pd.DataFrame | None, dict]:
    """One saved comparison. Returns (frame or None, input record). Never raises on bad input; the record says why."""
    compare_dir = Path(compare_dir)
    rec = {"compare_dir": str(compare_dir), "detail_file": str(compare_dir / "comparison_detail.csv"), "status": "ok"}
    f = compare_dir / "comparison_detail.csv"
    if not f.exists():
        return None, {**rec, "status": "missing", "problem": "comparison_detail.csv not found"}
    rec["detail_sha256"] = _sha256(f)
    if f.stat().st_size == 0:
        return None, {**rec, "status": "empty", "problem": "file is empty"}
    try:
        df = pd.read_csv(f, dtype={"symbol": str, "isu_cd_raw": str}, keep_default_na=True)
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as exc:
        return None, {**rec, "status": "unreadable", "problem": str(exc)[:200]}
    if df.empty:
        return None, {**rec, "status": "empty", "problem": "no rows"}
    miss = [c for c in REQUIRED if c not in df.columns]
    if df["status"].isin(COMPARABLE).any() if "status" in df.columns else False:
        miss += [c for c in REQUIRED_COMPARABLE if c not in df.columns]
    if miss:
        return None, {**rec, "status": "schema_error", "problem": f"missing columns {miss}"}
    days = sorted(pd.to_datetime(df["date"], errors="coerce").dropna().dt.strftime("%Y-%m-%d").unique())
    if len(days) != 1:
        return None, {**rec, "status": "schema_error", "problem": f"expected one date, found {days[:5]}"}
    unknown = sorted(set(df["status"].dropna()) - set(STATUS_ORDER))
    if unknown:
        return None, {**rec, "status": "schema_error", "problem": f"unknown statuses {unknown}"}
    rec["date"] = days[0]
    s = compare_dir / "comparison_summary.json"
    rec["summary_file_present"] = s.exists()
    if s.exists():
        try:
            rec["_summary"] = json.loads(s.read_text(encoding="utf-8"))
        except ValueError:
            rec["summary_file_present"] = "unreadable"
    for c in ("symbol",):
        df[c] = df[c].astype(str).str.strip().where(df[c].notna(), None)
    return df, rec


def date_stats(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Per-date statistics. Returns (stats, comparable rows actually used)."""
    n = len(df)
    by = {s: int((df["status"] == s).sum()) for s in STATUS_ORDER}
    comp = df[df["status"].isin(COMPARABLE)].copy()
    key = comp["symbol"].astype(str) + "|" + comp["date"].astype(str)
    dup = key.duplicated(keep=False)
    comp = comp[~dup].copy()
    for c in REQUIRED_COMPARABLE:      # a file with no comparable rows may lack these (load_detail allows that only then)
        if c not in comp.columns:
            comp[c] = pd.Series(dtype=object)
    for f in FIELDS:
        comp[f"{f}_equal"] = _bool(comp[f"{f}_equal"]).astype(bool)   # bool even when empty (else ~ / masks misbehave)
        for side in ("krx", "local"):
            comp[f"{side}_{f}"] = pd.to_numeric(comp[f"{side}_{f}"], errors="coerce")
    m = len(comp)
    eq = {f: int(comp[f"{f}_equal"].sum()) for f in FIELDS}
    match = int((comp["status"] == MATCH).sum())
    adj = comp.get("classifier_status")
    st = {
        "krx_rows": n, "by_status": by, "detail_duplicates": int(dup.sum()), "comparable": m,
        "not_compared": n - int(df["status"].isin(COMPARABLE).sum()),
        "missing": {"missing_local_file": by[MISSING_LOCAL_FILE], "missing_local_date": by[MISSING_LOCAL_DATE]},
        "invalid_or_duplicate": {"krx_row_invalid": by[KRX_ROW_INVALID], "local_row_invalid": by[LOCAL_ROW_INVALID],
                                 "detail_duplicates": int(dup.sum())},
        "not_comparable": by[NOT_COMPARABLE],
        "all_ohlcv_match": match, "all_ohlcv_match_rate": _rate(match, m),
        "field_equal_rate": {f: _rate(eq[f], m) for f in FIELDS},
        "exclusive": {"match": match, "volume_mismatch_only": int((comp["status"] == VOLUME_ONLY).sum()),
                      "close_mismatch": int((comp["status"] == CLOSE_MISMATCH).sum()),
                      "ohl_mismatch_close_eq": int((comp["status"] == OHL_MISMATCH).sum())},
        "overlapping": {"price_mismatch_any": int((~(comp["open_equal"] & comp["high_equal"] & comp["low_equal"] & comp["close_equal"])).sum()),
                        "volume_mismatch_any": m - eq["volume"]},
    }
    # how the local bar sits relative to the KRX bar. Rows the classifier explains as an adjusted-price basis are left
    # out: an adjustment moves every field by a factor, which says nothing about the shape of the day's bar.
    sh_rows = comp if adj is None else comp[comp["classifier_status"] != "adjusted_price_basis"]
    st["shape_rows"] = int(len(sh_rows))
    st["shape"] = {"local_high_above_krx": int((sh_rows["local_high"] > sh_rows["krx_high"]).sum()),
                   "local_high_below_krx": int((sh_rows["local_high"] < sh_rows["krx_high"]).sum()),
                   "local_low_below_krx": int((sh_rows["local_low"] < sh_rows["krx_low"]).sum()),
                   "local_low_above_krx": int((sh_rows["local_low"] > sh_rows["krx_low"]).sum()),
                   "local_volume_above_krx": int((sh_rows["local_volume"] > sh_rows["krx_volume"]).sum()),
                   "local_volume_below_krx": int((sh_rows["local_volume"] < sh_rows["krx_volume"]).sum())}
    price_diff = comp[~comp["status"].isin([MATCH, VOLUME_ONLY])]
    if adj is not None:
        cs = price_diff["classifier_status"].fillna("unknown")
        st["price_mismatch_by_classifier"] = {k: int(v) for k, v in sorted(cs.value_counts().items())}
    else:
        st["price_mismatch_by_classifier"] = None          # older detail files have no classifier column
    sh = st["shape"]
    wider, narrower = sh["local_high_above_krx"] + sh["local_low_below_krx"], sh["local_high_below_krx"] + sh["local_low_above_krx"]
    more, less = sh["local_volume_above_krx"], sh["local_volume_below_krx"]
    st["pattern"] = {
        "range": ("no_rows" if not len(sh_rows) else "equal" if not (wider or narrower) else
                  "local_wider_or_equal" if not narrower else "local_narrower_or_equal" if not wider else "mixed"),
        "volume": ("no_rows" if not len(sh_rows) else "equal" if not (more or less) else
                   "local_less_or_equal" if not more else "local_more_or_equal" if not less else "mixed"),
        "basis": f"{len(sh_rows)} comparable rows not explained as adjusted-price basis",
    }
    return st, comp


def _check_summary(rec: dict, st: dict) -> list[str]:
    s = rec.get("_summary")
    if not isinstance(s, dict):
        return []
    out = []
    if s.get("krx_rows") not in (None, st["krx_rows"]):
        out.append(f"krx_rows {s.get('krx_rows')} in summary vs {st['krx_rows']} in detail")
    if s.get("comparable") not in (None, st["comparable"] + st["detail_duplicates"]):
        out.append(f"comparable {s.get('comparable')} in summary vs {st['comparable']} in detail")
    for k, v in (s.get("by_status") or {}).items():
        if k in st["by_status"] and v != st["by_status"][k]:
            out.append(f"{k} {v} in summary vs {st['by_status'][k]} in detail")
    return out


SAMPLE_COLS = ["date", "symbol", "name", "status", "classifier_status", *(f"krx_{f}" for f in FIELDS), *(f"local_{f}" for f in FIELDS),
               "close_abs_diff", "volume_ratio_local_to_krx"]


def build_report(dirs: list[Path], top: int = 5) -> dict:
    inputs, stats, comps = [], {}, []
    for d in dirs:
        df, rec = load_detail(d)
        if df is None:
            inputs.append(rec)
            continue
        day = rec["date"]
        if day in stats:
            inputs.append({**{k: v for k, v in rec.items() if k != "_summary"}, "status": "duplicate_date",
                           "problem": f"{day} already read from another folder; this one is not used"})
            continue
        st, comp = date_stats(df)
        st["summary_consistency"] = _check_summary(rec, st) if rec.get("summary_file_present") is True else "summary not available"
        stats[day] = st
        comps.append(comp)
        inputs.append({k: v for k, v in rec.items() if k != "_summary"})
    days = sorted(stats)
    inputs = sorted(inputs, key=lambda r: (r.get("date") or "9999", r["compare_dir"]))
    rep = {"dates": days, "inputs": inputs, "by_date": {d: stats[d] for d in days}}
    if not days:
        rep["conclusion"] = {"dates_used": 0, "note": "no usable comparison files"}
        return rep
    allc = pd.concat(comps, ignore_index=True).sort_values(["date", "symbol"], kind="mergesort")
    for c in SAMPLE_COLS:              # columns fullmarket writes only for comparable rows may be absent
        if c not in allc.columns:
            allc[c] = pd.Series(dtype=object)
    for f in FIELDS:
        allc[f"{f}_equal"] = allc[f"{f}_equal"].astype(bool)
    allc["price_differs"] = ~allc["status"].isin([MATCH, VOLUME_ONLY])
    allc["unexplained"] = allc["price_differs"] & (allc.get("classifier_status", pd.Series("price_mismatch", index=allc.index)) != "adjusted_price_basis")
    # cross-date: symbols compared on every usable date, and how often each one disagreed
    per = allc.groupby("symbol").agg(dates_compared=("date", "nunique"), price_differs=("price_differs", "sum"),
                                     unexplained=("unexplained", "sum"), volume_differs=("volume_equal", lambda s: int((~s).sum())))
    per = per.astype(int)
    everywhere = per[per["dates_compared"] == len(days)]
    rep["cross_date"] = {
        "symbols_compared_on_every_date": int(len(everywhere)),
        "of_those_price_differs_on_every_date": int((everywhere["price_differs"] == len(days)).sum()),
        "of_those_unexplained_on_2plus_dates": int((everywhere["unexplained"] >= 2).sum()),
        "unexplained_dates_histogram": {int(k): int(v) for k, v in everywhere["unexplained"].value_counts().sort_index().items()},
    }
    if len(days) >= 2:
        last2 = days[-2:]
        sub = allc[allc["date"].isin(last2)].pivot_table(index="symbol", columns="date", values="unexplained", aggfunc="first")
        sub = sub.dropna()
        rep["cross_date"]["last_two_dates"] = {"dates": last2, "symbols_on_both": int(len(sub)),
                                               "unexplained_on_both": int(sub.all(axis=1).sum()),
                                               "unexplained_on_neither": int((~sub.any(axis=1)).sum())}
    # samples: 005930 on every date, then the largest unexplained close and volume differences per date
    samples = []
    for d in days:
        c = allc[allc["date"] == d]
        s = c[c["symbol"] == SAMPLE_SYMBOL]
        samples += [dict(r, sample_reason="reference symbol") for r in s.reindex(columns=SAMPLE_COLS).to_dict("records")]
        u = c[c["unexplained"]]
        for r in u.sort_values(["close_abs_diff", "symbol"], ascending=[False, True]).head(top).reindex(columns=SAMPLE_COLS).to_dict("records"):
            samples.append(dict(r, sample_reason="largest unexplained close difference"))
        v = u[~u["volume_equal"]]
        v = v.assign(_dev=(v["local_volume"] / v["krx_volume"] - 1).abs()).sort_values(["_dev", "symbol"], ascending=[False, True])
        for r in v.head(top).reindex(columns=SAMPLE_COLS).to_dict("records"):
            samples.append(dict(r, sample_reason="largest unexplained volume difference"))
    rep["samples"] = samples
    tot = sum(stats[d]["comparable"] for d in days)
    rep["conclusion"] = {
        "dates_used": len(days), "dates_skipped": [r for r in inputs if r["status"] != "ok"],
        "comparable_rows_total": tot,
        "all_ohlcv_match_by_date": {d: {"match": stats[d]["all_ohlcv_match"], "denominator": stats[d]["comparable"],
                                        "rate": stats[d]["all_ohlcv_match_rate"]} for d in days},
        "pattern_by_date": {d: {k: v for k, v in stats[d]["pattern"].items() if k != "basis"} for d in days},
        "note": ("Rates are per date over comparable rows only; dates are not pooled into one rate because they are "
                 "different samples. A pattern label describes how the local bar sits relative to the KRX bar; it is not a cause."),
    }
    return rep


def _md(rep: dict) -> str:
    L = ["# KRX vs local — multi-date data-quality report", "", "Offline: built from saved `comparison_detail.csv` files only.", "",
         "## Inputs", "", "| date | folder | status | problem |", "|---|---|---|---|"]
    L += [f"| {r.get('date', '—')} | {r['compare_dir']} | {r['status']} | {r.get('problem', '')} |" for r in rep["inputs"]]
    if not rep["dates"]:
        return "\n".join(L + ["", "No usable comparison files."]) + "\n"
    L += ["", "## By date (rates over comparable rows; denominator shown)", "",
          "| date | KRX rows | comparable | OHLCV match | open | high | low | close | volume | price differs (any) | volume differs (any) | not comparable | missing file | missing date | invalid/dup | pattern |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    pct = lambda x: "—" if x is None else f"{x:.1%}"
    for d in rep["dates"]:
        s = rep["by_date"][d]
        r = s["field_equal_rate"]
        inv = sum(s["invalid_or_duplicate"].values())
        L.append(f"| {d} | {s['krx_rows']} | {s['comparable']} | {s['all_ohlcv_match']} ({pct(s['all_ohlcv_match_rate'])}) | "
                 f"{pct(r['open'])} | {pct(r['high'])} | {pct(r['low'])} | {pct(r['close'])} | {pct(r['volume'])} | "
                 f"{s['overlapping']['price_mismatch_any']} | {s['overlapping']['volume_mismatch_any']} | {s['not_comparable']} | "
                 f"{s['missing']['missing_local_file']} | {s['missing']['missing_local_date']} | {inv} | range `{s['pattern']['range']}`, volume `{s['pattern']['volume']}` |")
    L += ["", "Price/volume 'any' columns overlap; the exclusive statuses are in history_summary.json (`by_status`).", "",
          "## Price differences by classifier", "", "| date | breakdown |", "|---|---|"]
    L += [f"| {d} | {rep['by_date'][d]['price_mismatch_by_classifier']} |" for d in rep["dates"]]
    L += ["", "## Bar shape (local vs KRX; comparable rows not explained as adjusted-price basis)", "",
          "| date | rows | high above | high below | low below | low above | volume above | volume below |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for d in rep["dates"]:
        h = rep["by_date"][d]["shape"]
        L.append(f"| {d} | {rep['by_date'][d]['shape_rows']} | {h['local_high_above_krx']} | {h['local_high_below_krx']} | {h['local_low_below_krx']} | {h['local_low_above_krx']} | "
                 f"{h['local_volume_above_krx']} | {h['local_volume_below_krx']} |")
    c = rep["cross_date"]
    L += ["", "## Across dates", "", f"- symbols compared on every date: {c['symbols_compared_on_every_date']}",
          f"- of those, price differs on every date: {c['of_those_price_differs_on_every_date']}",
          f"- of those, an unexplained (not adjusted-basis) difference on 2+ dates: {c['of_those_unexplained_on_2plus_dates']}",
          f"- unexplained-dates histogram: {c['unexplained_dates_histogram']}"]
    if "last_two_dates" in c:
        t = c["last_two_dates"]
        L.append(f"- {t['dates'][0]} and {t['dates'][1]}: {t['symbols_on_both']} symbols on both, unexplained on both {t['unexplained_on_both']}, on neither {t['unexplained_on_neither']}")
    L += ["", "## Samples", "", "| date | symbol | name | reason | status | KRX O/H/L/C | local O/H/L/C | KRX vol | local vol |",
          "|---|---|---|---|---|---|---|---:|---:|"]
    f0 = lambda x: "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.0f}"
    for r in rep["samples"]:
        L.append(f"| {r['date']} | {r['symbol']} | {r.get('name', '')} | {r['sample_reason']} | {r['status']} | "
                 + "/".join(f0(r[f'krx_{f}']) for f in ("open", "high", "low", "close")) + " | "
                 + "/".join(f0(r[f'local_{f}']) for f in ("open", "high", "low", "close")) + f" | {f0(r['krx_volume'])} | {f0(r['local_volume'])} |")
    L += ["", "## Conclusion", "", rep["conclusion"]["note"],
          "Causes of the differences are not determined by this report (see docs/DATA_SOURCES.md)."]
    return "\n".join(L) + "\n"


def write_report(rep: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in rep["dates"]:
        s = rep["by_date"][d]
        rows.append({"date": d, "krx_rows": s["krx_rows"], "comparable": s["comparable"], "not_compared": s["not_compared"],
                     **{k: v for k, v in s["by_status"].items()}, "detail_duplicates": s["detail_duplicates"],
                     "all_ohlcv_match_rate": s["all_ohlcv_match_rate"], **{f"{f}_equal_rate": v for f, v in s["field_equal_rate"].items()},
                     "price_mismatch_any": s["overlapping"]["price_mismatch_any"], "volume_mismatch_any": s["overlapping"]["volume_mismatch_any"],
                     "shape_rows": s["shape_rows"], **s["shape"], "range_pattern": s["pattern"]["range"],
                     "volume_pattern": s["pattern"]["volume"]})
    pd.DataFrame(rows).to_csv(out / "history_by_date.csv", index=False)
    pd.DataFrame(rep.get("samples", []), columns=[*SAMPLE_COLS, "sample_reason"]).to_csv(out / "history_samples.csv", index=False)
    (out / "history_summary.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str, sort_keys=True), encoding="utf-8")
    (out / "HISTORY.md").write_text(_md(rep), encoding="utf-8")


def find_compare_dirs(krx_root: Path) -> list[Path]:
    return sorted(p for p in Path(krx_root).glob("full_market_*/compare") if p.is_dir())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.krxcheck.history")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--compare-dir", nargs="+", help="folders written by `fullmarket compare`")
    g.add_argument("--krx-root", help="use every <krx-root>/full_market_*/compare")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)
    dirs = [Path(d) for d in a.compare_dir] if a.compare_dir else find_compare_dirs(Path(a.krx_root))
    out = Path(a.out)
    outr = out.resolve()
    for d in dirs:
        dr = Path(d).resolve()
        if outr == dr or dr in outr.parents or outr in dr.parents:
            print(f"--out must not be an input folder or contain one: {d}", file=sys.stderr)
            return 2
    if out.exists() and any(out.iterdir()):
        print(f"{out} is not empty: refusing to overwrite a previous report", file=sys.stderr)
        return 2
    if not dirs:
        print("no comparison folders given or found", file=sys.stderr)
        return 2
    rep = build_report(dirs, a.top)
    write_report(rep, out)
    bad = [r for r in rep["inputs"] if r["status"] != "ok"]
    print(f"dates used {rep['dates']}; inputs skipped {len(bad)} -> {out}", flush=True)
    return 0 if rep["dates"] else 3


if __name__ == "__main__":
    sys.exit(main())
