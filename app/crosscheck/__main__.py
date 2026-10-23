"""CLI: python -m app.crosscheck --data data/real [--audit <audit dir>] [--sample 30] [--symbols ...] [--out <dir>]

Live cross-source verification. Needs KIS_APP_KEY/KIS_APP_SECRET for the price comparison and DART_API_KEY for the
filing match; a missing key skips that part and says so in crosscheck_status.json (nothing is faked).
Only read-only quotation and disclosure endpoints are called. Key values are never printed or written.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from app.audit import load_raw_naver
from app.data.audit import AuditConfig, audit_frame
from app.research.crosscheck import compare_daily, match_corporate_actions
from app.research.manifest import environment_info, git_info


def pick_sample(symbols: list[str], candidates: pd.DataFrame | None, n: int, seed: int) -> list[str]:
    """Half the sample from audit candidates (most suspicious first), the rest uniformly at random."""
    rng = np.random.default_rng(seed)
    chosen: list[str] = []
    if candidates is not None and len(candidates):
        order = {"high": 0, "medium": 1, "low": 2}
        c = candidates.assign(_o=candidates["confidence"].map(order).fillna(3)).sort_values(["_o", "symbol"])
        for s in c["symbol"].astype(str).str.zfill(6):
            if s in symbols and s not in chosen and len(chosen) < n // 2:
                chosen.append(s)
    rest = [s for s in symbols if s not in chosen]
    extra = rng.choice(rest, size=min(n - len(chosen), len(rest)), replace=False).tolist() if rest else []
    return chosen + sorted(extra)


def run(argv: list[str] | None = None, kis_client=None, dart_client=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.crosscheck")
    ap.add_argument("--data", required=True)
    ap.add_argument("--audit", default=None, help="audit output folder (corporate_action_candidates.csv)")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else Path("data/crosscheck") / pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    if kis_client is None:
        from app.providers.kis import KISClient
        kis_client = KISClient()
    if dart_client is None:
        from app.providers.dart import DartClient
        dart_client = DartClient()
    status = {"started_utc": pd.Timestamp.now(tz="UTC").isoformat(), "git": git_info(), "environment": environment_info(),
              "args": {k: v for k, v in vars(a).items()}, "price_check": None, "filing_check": None}
    uni = pd.read_csv(Path(a.data) / "universe.csv", dtype={"symbol": str})
    cand = pd.read_csv(Path(a.audit) / "corporate_action_candidates.csv", dtype={"symbol": str}) if a.audit else None
    symbols = a.symbols or pick_sample(sorted(uni["symbol"]), cand, a.sample, a.seed)
    status["symbols"] = symbols
    t = time.time()

    # 1. prices: primary (as the research loader would use it) vs KIS adjusted daily bars
    if not kis_client.configured:
        status["price_check"] = {"ran": False, "reason": "KIS_APP_KEY / KIS_APP_SECRET not set"}
    else:
        recs, errors = [], {}
        for s in symbols:
            bars, u, cal = load_raw_naver(a.data, a.start, a.end, [s])
            prim = audit_frame(bars, AuditConfig(no_gap_volume_shift=False), cal, u).usable if len(bars) else pd.DataFrame()
            if prim.empty:
                errors[s] = "no primary bars"
                continue
            try:
                ref = kis_client.daily_bars_range(s, prim["date"].min().date(), prim["date"].max().date())
            except Exception as exc:  # recorded per symbol; the message carries no credentials
                errors[s] = f"{type(exc).__name__}: {str(exc)[:200]}"
                continue
            ev_cols = [c for c in ("kis_lock_code", "kis_split_ratio", "kis_modified", "kis_revaluation_reason") if c in ref]
            if ev_cols:  # days KIS itself marks (raw codes, interpreted only after inspection)
                marked = ref[(ref.get("kis_modified", "") == "Y") | (pd.to_numeric(ref.get("kis_split_ratio"), errors="coerce").fillna(0) != 0)]
                if len(marked):
                    marked[["date", *ev_cols]].to_csv(out / f"kis_event_days_{s}.csv", index=False)
            res = compare_daily(prim, ref, s)
            res["detail"].to_csv(out / f"price_detail_{s}.csv", index=False)
            recs.append({k: v for k, v in res.items() if k not in ("detail", "segments")} | {"segments": json.dumps(res["segments"], default=str)})
        summary = pd.DataFrame(recs)
        summary.to_csv(out / "price_comparison.csv", index=False)
        status["price_check"] = {"ran": True, "symbols_compared": int(len(summary)), "errors": errors,
                                 "verdicts": summary["verdict"].value_counts().to_dict() if len(summary) else {},
                                 "passed": bool(len(summary)) and set(summary["verdict"]) <= {"consistent", "adjustment_basis_differs"}}

    # 2. filings: audit candidates vs OpenDART share-base filings
    if not dart_client.configured:
        status["filing_check"] = {"ran": False, "reason": "DART_API_KEY not set"}
    elif cand is None or cand.empty:
        status["filing_check"] = {"ran": False, "reason": "no audit candidates (run python -m app.audit first and pass --audit)"}
    else:
        codes = dart_client.corp_codes()
        cand = cand[cand["symbol"].astype(str).str.zfill(6).isin(symbols)].copy()
        cand["symbol"] = cand["symbol"].astype(str).str.zfill(6)
        filings, missing_codes = [], []
        for s, g in cand.groupby("symbol"):
            code = codes.get(s)
            if not code:
                missing_codes.append(s)        # corpCode lists current stock codes; delisted names may be absent
                continue
            lo = (pd.to_datetime(g["date"]).min() - pd.Timedelta(150, unit="D")).strftime("%Y%m%d")
            hi = (pd.to_datetime(g["date"]).max() + pd.Timedelta(15, unit="D")).strftime("%Y%m%d")
            filings.extend(dart_client.disclosures(code["corp_code"], lo, hi))
        fdf = pd.DataFrame(filings)
        if len(fdf):
            fdf.to_csv(out / "filings.csv", index=False)
        m = match_corporate_actions(cand, fdf if len(fdf) else pd.DataFrame(columns=["symbol", "rcept_dt", "report_nm", "corporate_action"]))
        m.to_csv(out / "corporate_action_matches.csv", index=False)
        cm = m[m["kind"].notna()]
        status["filing_check"] = {"ran": True, "candidates": int(len(cand)), "no_corp_code": missing_codes,
                                  "statuses": m["status"].value_counts().to_dict() if len(m) else {},
                                  "passed": bool(len(cm)) and not missing_codes and bool((cm["status"] == "confirmed_kind_match").all())}
    status["seconds"] = round(time.time() - t, 1)
    status["verified"] = {"cross_source_price_check": bool(status["price_check"] and status["price_check"].get("passed")),
                          "corporate_action_disclosure_match": bool(status["filing_check"] and status["filing_check"].get("passed"))}
    (out / "crosscheck_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ran = [k for k in ("price_check", "filing_check") if status[k] and status[k].get("ran")]
    print(f"crosscheck: ran {ran or 'nothing'} -> {out}/crosscheck_status.json", flush=True)
    return 0 if ran else 3


if __name__ == "__main__":
    sys.exit(run())
