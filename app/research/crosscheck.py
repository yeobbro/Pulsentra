"""Cross-source verification: the primary daily series vs an official one, and suspected events vs filings.

Nothing here calls an API; the functions take frames, so they are tested offline and the live runner
(`python -m app.crosscheck`) only adds the fetching. Results separate three things that a raw "difference count"
would mix up:
  * coverage      sessions present in one source and not the other
  * adjustment    the two series differ by a constant factor on a stretch of dates (different adjustment basis)
  * record errors isolated disagreements that no constant factor explains
"""
from __future__ import annotations

import re

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

# OpenDART report_nm prefixes that, per the official list.json guide, mean "a report already filed under this report
# name, resubmitted with changes" (기재정정·첨부정정·첨부추가·발행조건확정). They point back to an earlier filing.
CORRECTION_PREFIXES = ("[기재정정]", "[첨부정정]", "[첨부추가]", "[발행조건확정]")
CORRECTION_LINK_DAYS = 365       # an original filed longer ago than this is not linked to a correction
SHARE_BASE_KINDS = {"split", "merge", "bonus_issue", "capital_reduction"}


def _base_title(report_nm) -> str:
    """Report name without the leading bracketed tags: '[기재정정]주식분할결정' -> '주식분할결정'."""
    return re.sub(r"^(\s*\[[^\]]*\]\s*)+", "", str(report_nm or "")).strip()


def _is_correction(report_nm) -> bool:
    name = str(report_nm or "").lstrip()
    return any(name.startswith(p) for p in CORRECTION_PREFIXES)


def group_filings(filings: pd.DataFrame, link_days: int = CORRECTION_LINK_DAYS) -> pd.DataFrame:
    """One corporate action per original filing, with its corrections attached.

    A correction (CORRECTION_PREFIXES) is linked to the earlier non-correction filing of the same symbol with the same
    base report name, filed at most `link_days` before it — only when exactly ONE such original exists. When the
    optional `rm` column is present, originals marked '정' (OpenDART: "a correction was filed after this report")
    are preferred. Several possible originals -> the correction forms its own action with action_status 'ambiguous';
    none -> its own action ('correction_without_original'). Two originals are never merged.
    Adds: action_id, is_correction, link_basis, action_status ('ok' | 'ambiguous').
    """
    f = filings.copy().reset_index(drop=True)
    f["filed"] = pd.to_datetime(f["rcept_dt"].astype(str), format="%Y%m%d", errors="coerce") if len(f) else pd.Series(dtype="datetime64[ns]")
    f["is_correction"] = f["report_nm"].map(_is_correction) if len(f) else pd.Series(dtype=bool)
    f["_base"] = f["report_nm"].map(_base_title) if len(f) else pd.Series(dtype=object)
    f["action_id"], f["link_basis"], f["action_status"] = None, None, "ok"
    sort_cols = ["filed"] + (["rcept_no"] if "rcept_no" in f.columns else [])
    order = f.sort_values(sort_cols, kind="mergesort").index
    n = 0
    for i in order:                                   # originals first: each is its own action
        if not f.at[i, "is_correction"]:
            n += 1
            f.at[i, "action_id"], f.at[i, "link_basis"] = f"A{n:04d}", "original"
    for i in order:
        if not f.at[i, "is_correction"]:
            continue
        r = f.loc[i]
        cand = f[(~f["is_correction"]) & (f["symbol"] == r["symbol"]) & (f["_base"] == r["_base"])
                 & (f["filed"] <= r["filed"]) & (f["filed"] >= r["filed"] - pd.Timedelta(link_days, unit="D"))]
        basis = "official_correction_prefix"
        if len(cand) > 1 and "rm" in f.columns:
            marked = cand[cand["rm"].astype(str).str.contains("정", na=False)]
            if len(marked) == 1:
                cand, basis = marked, basis + "+rm_정"
        elif len(cand) == 1 and "rm" in f.columns and "정" in str(cand.iloc[0]["rm"]):
            basis = basis + "+rm_정"
        if len(cand) == 1:
            f.at[i, "action_id"], f.at[i, "link_basis"] = cand.iloc[0]["action_id"], basis
            continue
        n += 1
        f.at[i, "action_id"] = f"A{n:04d}"
        if len(cand) == 0:
            f.at[i, "link_basis"] = "correction_without_original"
        else:
            f.at[i, "link_basis"], f.at[i, "action_status"] = f"ambiguous: {len(cand)} possible originals", "ambiguous"
    # an action whose documents disagree on the kind is not trusted either
    kinds = f.groupby("action_id")["corporate_action"].nunique(dropna=True) if len(f) else pd.Series(dtype=int)
    for a in kinds[kinds > 1].index:
        f.loc[f["action_id"] == a, "action_status"] = "ambiguous"
    return f.drop(columns=["_base"])


def match_corporate_actions(candidates: pd.DataFrame, filings: pd.DataFrame, before_days: int = 120, after_days: int = 10) -> pd.DataFrame:
    """Pair each suspected event with the nearest corporate action (an original filing plus its corrections) of the
    same instrument.

    candidates: symbol, date, kind (from the audit). filings: symbol, rcept_dt (YYYYMMDD or date), report_nm,
    corporate_action, and optionally rcept_no and rm. A document counts when it was filed up to `before_days` before
    the event date (decisions precede the effective date) or up to `after_days` after it (late or corrected filings).
    Filings are grouped into actions first (`group_filings`), so an original and its correction are ONE action:
    a candidate matches the action, and an action no candidate matched is reported once as `undetected_event`.
    Several candidates may still match the same action (e.g. a volume break and a split flagged on adjacent days).
    Status `ambiguous` = the nearest fitting action could not be told apart from another one (see group_filings).
    Returns one row per candidate plus one per unmatched share-base action; the columns of earlier versions are kept
    (filing / filed / days_before_event describe the nearest in-window document) and the action columns are added.
    """
    cols = ["symbol", "date", "kind", "status", "filing", "filing_kind", "filed", "days_before_event",
            "action_id", "action_documents", "rcept_nos", "action_filings", "link_basis"]
    cand = candidates.copy()
    cand["date"] = pd.to_datetime(cand["date"])
    f = filings.copy()
    f = f[f["corporate_action"].notna()] if "corporate_action" in f else f.iloc[0:0]
    if "rcept_dt" not in f:
        f["rcept_dt"] = pd.Series(dtype=object)
    f = group_filings(f) if len(f) else f.assign(filed=pd.Series(dtype="datetime64[ns]"), action_id=None, is_correction=False,
                                                link_basis=None, action_status="ok")
    expected = {"split": {"split"}, "merge": {"merge", "capital_reduction"}, "volume_level_break": {"split", "merge", "bonus_issue", "capital_reduction"},
                "bonus_issue_suspected": {"bonus_issue"}}

    def action_info(a: str) -> dict:
        docs = f[f["action_id"] == a].sort_values("filed", kind="mergesort")
        nos = docs["rcept_no"].astype(str).tolist() if "rcept_no" in docs else []
        bases = [b for b in docs["link_basis"] if b != "original"]
        return {"action_id": a, "action_documents": int(len(docs)), "rcept_nos": "|".join(nos) or None,
                "action_filings": "|".join(docs["report_nm"].astype(str)), "link_basis": ";".join(dict.fromkeys(bases)) or "original"}

    recs, used = [], set()
    for c in cand.itertuples(index=False):
        g = f[(f["symbol"] == c.symbol) & (f["filed"] >= c.date - pd.Timedelta(before_days, unit="D"))
              & (f["filed"] <= c.date + pd.Timedelta(after_days, unit="D"))]
        if g.empty:
            recs.append({"symbol": c.symbol, "date": c.date, "kind": c.kind, "status": "no_filing_found", "filing": None,
                         "filing_kind": None, "filed": None, "days_before_event": None})
            continue
        g = g.assign(dist=(c.date - g["filed"]).dt.days.abs()).sort_values(["dist", "filed"], kind="mergesort")
        ok = g[g["corporate_action"].isin(expected.get(c.kind, set()))]
        best = ok.iloc[0] if len(ok) else g.iloc[0]
        a = best["action_id"]
        used.add(a)
        status = "confirmed_kind_match" if len(ok) else "filing_kind_mismatch"
        if (f.loc[f["action_id"] == a, "action_status"] == "ambiguous").any():
            status = "ambiguous"
        elif len(ok) and ok["action_id"].nunique() > 1 and ok.iloc[0]["dist"] == ok[ok["action_id"] != a]["dist"].min():
            status = "ambiguous"                  # two different fitting actions equally close
        recs.append({"symbol": c.symbol, "date": c.date, "kind": c.kind, "status": status,
                     "filing": best["report_nm"], "filing_kind": best["corporate_action"], "filed": best["filed"],
                     "days_before_event": int((c.date - best["filed"]).days), **action_info(a)})
    for a, docs in f.groupby("action_id", sort=True):
        if a in used or not docs["corporate_action"].isin(SHARE_BASE_KINDS).any():
            continue
        first = docs.sort_values(["is_correction", "filed"], kind="mergesort").iloc[0]     # the original when there is one
        recs.append({"symbol": first["symbol"], "date": None, "kind": None,
                     "status": "ambiguous" if (docs["action_status"] == "ambiguous").any() else "undetected_event",
                     "filing": first["report_nm"], "filing_kind": first["corporate_action"], "filed": first["filed"],
                     "days_before_event": None, **action_info(a)})
    return pd.DataFrame(recs, columns=cols)


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
