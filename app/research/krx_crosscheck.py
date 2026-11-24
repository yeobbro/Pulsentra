"""Compare sampled primary (Naver) daily bars with KRX Open API daily trading info, and pair audit candidates with
OpenDART filings plus the KRX price evidence. Pure functions: no network here (see app/krxcheck/__main__.py).

Why the comparison has more than "match / mismatch":
  * KRX daily trading info gives the prices actually traded on that date; the Naver series is split-adjusted
    (docs/DATA_SOURCES.md). Before a split every OHLC field differs by the same factor while volume (not adjusted by
    Naver) is equal — that is an adjustment-basis difference, not an error.
  * A symbol absent from one side, a session without trades, or a provider halt marker cannot be compared at all;
    those get their own statuses so they are never counted as disagreements.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.data.audit import _nearest_ratio

PRICE_FIELDS = ("open", "high", "low", "close")
SPLIT_RATIOS = (2, 3, 4, 5, 10, 20, 25, 50)
EQUAL_REL = 1e-4                 # integer KRW prices: equal means equal (allowing float noise)
VOLUME_REL = 1e-3               # kept for reference; volume equality is exact (share counts are integers)

# statuses (overall)
MATCH = "match"
ADJUSTED_BASIS = "adjusted_price_basis"
PRICE_MISMATCH = "price_mismatch"
VOLUME_MISMATCH = "volume_mismatch"
MISSING_IN_KRX = "missing_in_krx"
MISSING_IN_PRIMARY = "missing_in_primary"
NOT_COMPARABLE = "not_comparable"
KRX_DAY_EMPTY = "krx_day_empty"
REQUEST_FAILED = "request_failed"
DISAGREEMENTS = {PRICE_MISMATCH, VOLUME_MISMATCH}


def _f(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return np.nan
    return v if np.isfinite(v) else np.nan


def classify_pair(primary: dict | None, krx: dict | None) -> dict:
    """One (symbol, date). primary/krx: dicts with open, high, low, close, volume (or None when the row is absent)."""
    if krx is None and primary is None:
        return {"status": NOT_COMPARABLE, "price_status": None, "volume_status": None, "factor": None, "reason": "no bar on either side (suspended?)"}
    if krx is None:
        return {"status": MISSING_IN_KRX, "price_status": None, "volume_status": None, "factor": None, "reason": "symbol not in the KRX answer for that date"}
    if primary is None:
        return {"status": MISSING_IN_PRIMARY, "price_status": None, "volume_status": None, "factor": None, "reason": "no primary bar for that date"}
    p = {k: _f(primary.get(k)) for k in (*PRICE_FIELDS, "volume")}
    k = {f: _f(krx.get(f)) for f in (*PRICE_FIELDS, "volume")}
    if p["open"] == 0 and p["high"] == 0 and p["low"] == 0:
        return {"status": NOT_COMPARABLE, "price_status": None, "volume_status": None, "factor": None, "reason": "primary row is a halt marker (O=H=L=0)"}
    if any(not np.isfinite(k[f]) or k[f] <= 0 for f in PRICE_FIELDS) or not np.isfinite(k["volume"]) or k["volume"] == 0:
        return {"status": NOT_COMPARABLE, "price_status": None, "volume_status": None, "factor": None, "reason": "no trade on KRX that day (zero/missing price or volume)"}
    if any(not np.isfinite(p[f]) or p[f] <= 0 for f in PRICE_FIELDS) or not np.isfinite(p["volume"]):
        return {"status": NOT_COMPARABLE, "price_status": None, "volume_status": None, "factor": None, "reason": "primary price/volume missing or not positive"}
    ratios = np.array([p[f] / k[f] for f in PRICE_FIELDS])
    med = float(np.median(ratios))
    # an adjusted price is rounded to 1 KRW, so each field may be off by 0.5 KRW from exact proportionality
    tol = np.array([0.5 / p[f] for f in PRICE_FIELDS]) * 2 + 1e-6
    if np.all(np.abs(ratios - 1) <= EQUAL_REL):
        price_status, factor = "equal", 1.0
    elif ((np.all(ratios > 1 + EQUAL_REL) or np.all(ratios < 1 - EQUAL_REL))
          and np.all(np.abs(ratios / med - 1) <= tol + 1e-3)):
        # an adjustment moves every price by the same factor, so every field differs and all in the same direction.
        # Without this, fields that are equal or a tick off on either side put the median at (or next to) 1 and the
        # 0.1% tolerance passes a real disagreement as "x 1.000000". (All 217 adjustment rows seen against KRX on
        # 2025-02-27 / 2025-06-12 move one way; see docs/DATA_SOURCES.md.)
        price_status, factor = "constant_factor", med
    else:
        price_status, factor = "inconsistent", med
    vr = p["volume"] / k["volume"]
    if abs(p["volume"] - k["volume"]) < 0.5:
        volume_status = "equal"
    elif price_status == "constant_factor" and abs(vr * factor - 1) <= 0.01:
        volume_status = "adjusted_with_prices"
    else:
        volume_status = "different"
    near, dist = _nearest_ratio(1 / factor, SPLIT_RATIOS) if price_status == "constant_factor" else (np.nan, np.inf)
    split_like = bool(dist < np.log(1.03))
    if price_status == "equal":
        status = MATCH if volume_status == "equal" else VOLUME_MISMATCH
        reason = "" if status == MATCH else f"prices equal, volume ratio {vr:.4f}"
    elif price_status == "constant_factor":
        status = ADJUSTED_BASIS
        reason = (f"all OHLC = KRX x {factor:.6f}" + (f" (split-like 1:{near:g})" if split_like else " (non-split adjustment, e.g. rights)")
                  + f"; volume {volume_status}")
    else:
        status = PRICE_MISMATCH
        reason = "OHLC ratios to KRX differ field by field: " + ", ".join(f"{f}={r:.5f}" for f, r in zip(PRICE_FIELDS, ratios))
        if max(abs(p[f] - k[f]) for f in PRICE_FIELDS) <= 1:
            reason += " (every difference <= 1 KRW: a tiny adjustment and a provider rounding difference cannot be told apart)"
    return {"status": status, "price_status": price_status, "volume_status": volume_status, "factor": factor,
            "split_like_ratio": near if split_like else None, "volume_ratio": vr, "reason": reason}


def compare_sample(primary: pd.DataFrame, krx_by_date: dict[str, pd.DataFrame | str], pairs: list[tuple[str, str]]) -> pd.DataFrame:
    """primary: symbol, date, open, high, low, close, volume (raw, as collected).
    krx_by_date: 'YYYYMMDD' -> normalised KRX stock frame (all markets fetched for that date, concatenated), or a
    status string ('empty' / 'failed: …') when the date has no usable answer.
    pairs: (symbol, 'YYYY-MM-DD') to compare. Returns one row per pair."""
    prim = primary.copy()
    prim["d"] = pd.to_datetime(prim["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    pidx = {(r["symbol"], r["d"]): r for r in prim.to_dict("records")}
    pdup = set(map(tuple, prim.loc[prim.duplicated(["symbol", "d"], keep=False), ["symbol", "d"]].to_numpy().tolist()))
    out = []
    for sym, day in pairs:
        key = day.replace("-", "")
        k = krx_by_date.get(key)
        p = pidx.get((sym, day))
        base = {"symbol": sym, "date": day, **{f"primary_{c}": (p or {}).get(c) for c in (*PRICE_FIELDS, "volume")}}
        if isinstance(k, str) or k is None:
            st = KRX_DAY_EMPTY if k == "empty" else REQUEST_FAILED
            out.append({**base, **{f"krx_{c}": None for c in (*PRICE_FIELDS, "volume")}, "status": st, "price_status": None,
                        "volume_status": None, "factor": None, "reason": k or "date not fetched"})
            continue
        rows = k[k["symbol"] == sym]
        kr = rows.iloc[0].to_dict() if len(rows) else None
        if (sym, day) in pdup or len(rows) > 1:
            # which row to trust is unknown, so the pair is not compared (taking the first or last would make the
            # result depend on row order)
            side = "primary" if (sym, day) in pdup else "KRX"
            res = {"status": NOT_COMPARABLE, "price_status": None, "volume_status": None, "factor": None,
                   "reason": f"duplicate {side} rows for the symbol and date"}
        else:
            res = classify_pair(p, kr)
        out.append({**base, **{f"krx_{c}": (kr or {}).get(c) for c in (*PRICE_FIELDS, "volume")}, **res})
    return pd.DataFrame(out)


def summarize(detail: pd.DataFrame) -> dict:
    counts = detail["status"].value_counts().to_dict() if len(detail) else {}
    comparable = int(detail["status"].isin([MATCH, ADJUSTED_BASIS, PRICE_MISMATCH, VOLUME_MISMATCH]).sum()) if len(detail) else 0
    return {"pairs": int(len(detail)), "comparable": comparable, "by_status": counts,
            "disagreements": int(detail["status"].isin(DISAGREEMENTS).sum()) if len(detail) else 0,
            "passed": bool(comparable) and not detail["status"].isin(DISAGREEMENTS).any()}


def krx_evidence_for_candidate(detail: pd.DataFrame, symbol: str, before_day: str, event_day: str, implied_ratio: float | None) -> dict:
    """A split of 1:k seen in the KRX data means: before the event the (adjusted) primary = KRX / k, from the event on equal."""
    d = detail[detail["symbol"] == symbol].set_index("date")
    b = d.loc[before_day] if before_day in d.index else None
    a = d.loc[event_day] if event_day in d.index else None
    if b is None or a is None or b["status"] not in (MATCH, ADJUSTED_BASIS) or a["status"] not in (MATCH, ADJUSTED_BASIS):
        return {"krx_evidence": "unavailable", "factor_before": None if b is None else b.get("factor"),
                "factor_after": None if a is None else a.get("factor")}
    fb, fa = float(b["factor"]), float(a["factor"])
    consistent = abs(fa - 1) <= 1e-3 and implied_ratio is not None and np.isfinite(implied_ratio) and abs(fb * implied_ratio - 1) <= 0.02
    return {"krx_evidence": "consistent_with_split" if consistent else "not_consistent", "factor_before": fb, "factor_after": fa}


def candidate_verdicts(matches: pd.DataFrame, evidence: dict[tuple[str, str], dict]) -> pd.DataFrame:
    """Combine the OpenDART filing match and the KRX price evidence. 'verified' needs BOTH a filing of the expected kind
    and KRX prices that step by the implied ratio; everything else stays 'unverified' with the reason."""
    out = []
    for m in matches[matches["kind"].notna()].to_dict("records"):
        day = pd.Timestamp(m["date"]).strftime("%Y-%m-%d")
        ev = evidence.get((m["symbol"], day), {"krx_evidence": "not_requested"})
        if m["status"] == "confirmed_kind_match" and ev.get("krx_evidence") == "consistent_with_split":
            verdict, why = "verified", "filing of the expected kind and KRX price step agree"
        else:
            verdict = "unverified"
            why = "; ".join(x for x in (f"filing: {m['status']}" if m["status"] != "confirmed_kind_match" else "",
                                         f"KRX: {ev.get('krx_evidence')}" if ev.get("krx_evidence") != "consistent_with_split" else "") if x)
        out.append({**m, "date": day, **ev, "verdict": verdict, "verdict_reason": why})
    return pd.DataFrame(out)
