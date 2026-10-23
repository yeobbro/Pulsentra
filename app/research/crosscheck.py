"""Cross-source verification: the primary daily series vs an official one, and suspected events vs filings.

Nothing here calls an API; the functions take frames, so they are tested offline and the live runner
(`python -m app.crosscheck`) only adds the fetching. Results separate three things that a raw "difference count"
would mix up:
  * coverage      sessions present in one source and not the other
  * adjustment    the two series differ by a constant factor on a stretch of dates (different adjustment basis)
  * record errors isolated disagreements that no constant factor explains
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CLOSE_TOLERANCE = 0.001          # relative close difference treated as equal (tick rounding of adjusted prices)
FACTOR_TOLERANCE = 0.002         # consecutive price ratios within this are the same adjustment segment
VOLUME_TOLERANCE = 0.01


def compare_daily(primary: pd.DataFrame, reference: pd.DataFrame, symbol: str | None = None,
                  close_tol: float = CLOSE_TOLERANCE, factor_tol: float = FACTOR_TOLERANCE) -> dict:
    """Compare two daily series of one instrument on their common date range.

    primary / reference: date, close, volume (open/high/low optional). Returns a summary dict plus `detail`
    (per-date rows) and `segments` (stretches with a constant primary/reference price factor).
    """
    p = primary[["date", "close", "volume"]].copy()
    r = reference[["date", "close", "volume"]].copy()
    for d in (p, r):
        d["date"] = pd.to_datetime(d["date"])
    lo, hi = max(p["date"].min(), r["date"].min()), min(p["date"].max(), r["date"].max())
    p, r = p[(p["date"] >= lo) & (p["date"] <= hi)], r[(r["date"] >= lo) & (r["date"] <= hi)]
    m = p.merge(r, on="date", how="outer", suffixes=("_p", "_r"), indicator=True).sort_values("date").reset_index(drop=True)
    both = m["_merge"] == "both"
    only_p, only_r = int((m["_merge"] == "left_only").sum()), int((m["_merge"] == "right_only").sum())
    b = m[both].copy()
    b["close_ratio"] = b["close_p"] / b["close_r"]
    b["close_rel_diff"] = b["close_ratio"] - 1
    b["volume_ratio"] = np.where(b["volume_r"] > 0, b["volume_p"] / b["volume_r"], np.nan)
    segs = factor_segments(b["date"], b["close_ratio"].to_numpy(), factor_tol)
    # a date is a record error when it disagrees with its own segment's factor (not with the reference level)
    b["segment"] = -1
    for k, s in enumerate(segs):
        b.loc[(b["date"] >= s["start"]) & (b["date"] <= s["end"]), "segment"] = k
    seg_factor = b["segment"].map({k: s["factor"] for k, s in enumerate(segs)})
    b["record_error"] = (b["close_ratio"] / seg_factor - 1).abs() > close_tol
    mismatched = (b["close_rel_diff"].abs() > close_tol)
    main = max(segs, key=lambda s: s["rows"]) if segs else None
    out = {
        "symbol": symbol, "start": lo, "end": hi,
        "sessions_primary": int(len(p)), "sessions_reference": int(len(r)), "sessions_common": int(both.sum()),
        "missing_in_primary": only_r, "missing_in_reference": only_p,
        "missing_rate_primary": only_r / max(len(r), 1), "missing_rate_reference": only_p / max(len(p), 1),
        "close_mismatch_days": int(mismatched.sum()),
        "close_abs_rel_diff_median": float(b["close_rel_diff"].abs().median()) if len(b) else None,
        "close_abs_rel_diff_max": float(b["close_rel_diff"].abs().max()) if len(b) else None,
        "volume_ratio_median": float(np.nanmedian(b["volume_ratio"])) if b["volume_ratio"].notna().any() else None,
        "volume_mismatch_days": int(((b["volume_ratio"] - 1).abs() > VOLUME_TOLERANCE).sum()),
        "adjustment_segments": len(segs), "main_segment_factor": main["factor"] if main else None,
        "adjustment_basis_differs": bool(len(segs) > 1 or (main is not None and abs(main["factor"] - 1) > close_tol)),
        "record_error_days": int(b["record_error"].sum()),
    }
    out["verdict"] = verdict(out)
    out["detail"] = b[["date", "close_p", "close_r", "close_rel_diff", "volume_p", "volume_r", "volume_ratio", "segment", "record_error"]]
    out["segments"] = segs
    return out


def factor_segments(dates: pd.Series, ratio: np.ndarray, tol: float = FACTOR_TOLERANCE, min_rows: int = 3) -> list[dict]:
    """Split the series where the primary/reference price ratio steps; each stretch has one factor (its median).

    Isolated outliers (a single date off the factor and back) do not start a segment of their own.
    """
    d = pd.to_datetime(pd.Series(dates)).reset_index(drop=True)
    r = np.asarray(ratio, dtype=float)
    ok = np.isfinite(r) & (r > 0)
    d, r = d[ok].reset_index(drop=True), r[ok]
    if not len(r):
        return []
    lr = np.log(r)
    segs, start = [], 0
    i = 1
    while i < len(lr):
        level = np.median(lr[start:i])
        if abs(lr[i] - level) > np.log1p(tol):
            # persistent step: the next rows agree with the new level, not the old one
            nxt = lr[i:i + min_rows]
            if len(nxt) >= min_rows and np.all(np.abs(nxt - np.median(nxt)) <= np.log1p(tol)) and abs(np.median(nxt) - level) > np.log1p(tol):
                segs.append((start, i))
                start = i
        i += 1
    segs.append((start, len(lr)))
    return [{"start": d[a], "end": d[b - 1], "rows": int(b - a), "factor": float(np.exp(np.median(lr[a:b])))} for a, b in segs]


def verdict(s: dict) -> str:
    if s["sessions_common"] == 0:
        return "no_overlap"
    if s["record_error_days"] == 0 and not s["adjustment_basis_differs"] and s["close_mismatch_days"] == 0:
        return "consistent"
    if s["adjustment_basis_differs"] and s["record_error_days"] == 0:
        return "adjustment_basis_differs"
    return "record_errors"


# ------------------------------------------------------------------------------------------------ filings

def match_corporate_actions(candidates: pd.DataFrame, filings: pd.DataFrame, before_days: int = 120, after_days: int = 10) -> pd.DataFrame:
    """Pair each suspected event with the nearest share-base filing of the same instrument.

    candidates: symbol, date, kind (from the audit). filings: symbol, rcept_dt (YYYYMMDD or date), report_nm,
    corporate_action. A filing counts when it was made up to `before_days` before the event date (decisions
    precede the effective date) or up to `after_days` after it (late or corrected filings).
    Returns one row per candidate with the match status, plus one row per filing that matched no candidate
    (`undetected_event`) — those are events the price/volume checks missed.
    """
    cand = candidates.copy()
    cand["date"] = pd.to_datetime(cand["date"])
    f = filings.copy()
    f = f[f["corporate_action"].notna()] if "corporate_action" in f else f.iloc[0:0]
    f["filed"] = pd.to_datetime(f["rcept_dt"].astype(str), format="%Y%m%d", errors="coerce") if len(f) else pd.Series(dtype="datetime64[ns]")
    expected = {"split": {"split"}, "merge": {"merge", "capital_reduction"}, "volume_level_break": {"split", "merge", "bonus_issue", "capital_reduction"},
                "bonus_issue_suspected": {"bonus_issue"}}
    recs, used = [], set()
    for c in cand.itertuples(index=False):
        g = f[(f["symbol"] == c.symbol) & (f["filed"] >= c.date - pd.Timedelta(before_days, unit="D"))
              & (f["filed"] <= c.date + pd.Timedelta(after_days, unit="D"))]
        if g.empty:
            recs.append({"symbol": c.symbol, "date": c.date, "kind": c.kind, "status": "no_filing_found", "filing": None,
                         "filing_kind": None, "filed": None, "days_before_event": None})
            continue
        g = g.assign(dist=(c.date - g["filed"]).dt.days.abs()).sort_values("dist")
        ok = g[g["corporate_action"].isin(expected.get(c.kind, set()))]
        best = ok.iloc[0] if len(ok) else g.iloc[0]
        used.add(best.name)
        recs.append({"symbol": c.symbol, "date": c.date, "kind": c.kind,
                     "status": "confirmed_kind_match" if len(ok) else "filing_kind_mismatch",
                     "filing": best["report_nm"], "filing_kind": best["corporate_action"], "filed": best["filed"],
                     "days_before_event": int((c.date - best["filed"]).days)})
    share_base = {"split", "merge", "bonus_issue", "capital_reduction"}
    for idx, r in f.iterrows():
        if idx not in used and r["corporate_action"] in share_base:
            recs.append({"symbol": r["symbol"], "date": None, "kind": None, "status": "undetected_event", "filing": r["report_nm"],
                         "filing_kind": r["corporate_action"], "filed": r["filed"], "days_before_event": None})
    return pd.DataFrame(recs, columns=["symbol", "date", "kind", "status", "filing", "filing_kind", "filed", "days_before_event"])


# ------------------------------------------------------------------------------------------------ listings

def compare_listings(universe: pd.DataFrame, reference: pd.DataFrame, start: str | None = None) -> dict:
    """Delisted names in a reference list (e.g. a KRX KIND export) that the research universe lacks, and vice versa.

    Both frames: symbol, delisted_date (reference may carry more columns). Only delistings on/after `start` count.
    """
    u = universe[universe.get("status", pd.Series("", index=universe.index)) == "delisted"].copy() if "status" in universe else universe.copy()
    ref = reference.copy()
    for d in (u, ref):
        d["delisted_date"] = pd.to_datetime(d["delisted_date"], errors="coerce")
        d["symbol"] = d["symbol"].astype(str).str.zfill(6)
    if start:
        u, ref = u[u["delisted_date"] >= pd.Timestamp(start)], ref[ref["delisted_date"] >= pd.Timestamp(start)]
    missing = sorted(set(ref["symbol"]) - set(u["symbol"]))
    extra = sorted(set(u["symbol"]) - set(ref["symbol"]))
    by_year = ref.assign(missing=ref["symbol"].isin(missing)).groupby(ref["delisted_date"].dt.year)["missing"].agg(["sum", "size"])
    return {"reference_delisted": int(len(ref)), "universe_delisted": int(len(u)), "missing_from_universe": missing,
            "not_in_reference": extra, "coverage": 1 - len(missing) / max(len(ref), 1),
            "missing_by_year": {int(y): {"missing": int(r["sum"]), "reference": int(r["size"])} for y, r in by_year.iterrows()}}
