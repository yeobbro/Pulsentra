"""CLI: python -m app.krxcheck --data data/real [--symbols ...] [--sample-symbols N] [--dates ...] [--n-dates N]
                               [--candidate SYMBOL:YYYY-MM-DD --audit <audit dir>] [--with-index] [--dart]
                               [--max-requests N] [--out DIR] [--live --spec-confirmed]

KRX Open API verification, separate from the KIS-based `python -m app.crosscheck`.
Without --live it only writes plan.json (dates, symbols, the exact KRX requests it would make) and uses no network.
With --live --spec-confirmed it makes those requests — one per (date, market service), never one per symbol — compares
the sampled primary bars with KRX, and (with --dart) pairs the named candidates with OpenDART filings.
Inputs (data/real, the audit folder) are only read. KRX_API_KEY / DART_API_KEY values are never printed or written.
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
from app.providers.krx import INDEX_SERVICE_FOR_MARKET, SERVICES, SPEC_SOURCE, STOCK_SERVICE_FOR_MARKET, KRXAPIError, KRXClient
from app.research.crosscheck import match_corporate_actions
from app.research.krx_crosscheck import (KRX_DAY_EMPTY, REQUEST_FAILED, candidate_verdicts, compare_sample,
                                         krx_evidence_for_candidate, summarize)
from app.research.manifest import environment_info, git_info, guard_output_dir

STOP_KINDS = {"not_configured", "spec_unconfirmed", "auth", "forbidden", "rate_limit", "redirect"}


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_plan(data: Path, symbols: list[str], sample_n: int, dates: list[str], n_dates: int, candidates: list[str],
               audit: Path | None, seed: int, start: str, end: str | None, with_index: bool) -> tuple[dict, pd.DataFrame]:
    """Choose symbols, dates and (symbol, date) pairs; list the KRX requests. Reads local files only."""
    uni = pd.read_csv(data / "universe.csv", dtype={"symbol": str})
    market_of = dict(zip(uni["symbol"], uni["market"].replace({"KOSDAQ GLOBAL": "KOSDAQ"})))
    rng = np.random.default_rng(seed)
    cand_rows = []
    if candidates:
        if audit is None:
            raise ValueError("--candidate needs --audit (the folder with corporate_action_candidates.csv)")
        ac = pd.read_csv(audit / "corporate_action_candidates.csv", dtype={"symbol": str})
        ac["d"] = pd.to_datetime(ac["date"]).dt.strftime("%Y-%m-%d")
        for c in candidates:
            sym, day = c.split(":", 1)
            hit = ac[(ac["symbol"] == sym) & (ac["d"] == day)]
            if hit.empty:
                raise ValueError(f"candidate {c} is not in {audit}/corporate_action_candidates.csv")
            cand_rows.append(hit.iloc[0].to_dict())
    chosen = list(dict.fromkeys([*symbols, *(r["symbol"] for r in cand_rows)]))
    pool = [s for s in uni.loc[uni["status"] == "listed", "symbol"] if s not in chosen and (data / "raw" / f"{s}.csv").exists()]
    if sample_n and pool:
        chosen += sorted(rng.choice(pool, size=min(sample_n, len(pool)), replace=False).tolist())
    unknown = [s for s in chosen if market_of.get(s) not in STOCK_SERVICE_FOR_MARKET]
    chosen = [s for s in chosen if s not in unknown]
    bars, _, cal = load_raw_naver(data, start, end, chosen)
    prim = bars.copy()
    prim["date"] = pd.to_datetime(prim["date"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        prim[c] = pd.to_numeric(prim[c], errors="coerce")
    sessions = pd.DatetimeIndex(cal) if cal is not None else pd.DatetimeIndex(sorted(prim["date"].dropna().unique()))
    cand_syms = {r["symbol"] for r in cand_rows}
    plain_syms = [s for s in chosen if s not in cand_syms or s in symbols]   # candidates only get their event dates
    plain = list(dates)
    have = sorted(set(prim.loc[prim["symbol"].isin(plain_syms), "date"].dropna().dt.strftime("%Y-%m-%d"))
                  & set(sessions.strftime("%Y-%m-%d")))
    if n_dates and have:
        plain += sorted(rng.choice(have, size=min(n_dates, len(have)), replace=False).tolist())
    pairs: list[tuple[str, str]] = [(s, d) for d in dict.fromkeys(plain) for s in plain_syms]
    cands = []
    for r in cand_rows:
        ev = pd.Timestamp(r["d"])
        # last session with a real primary bar before the event: splits are usually preceded by a trading halt,
        # so the previous calendar session often has no bar on either side
        s_rows = prim[(prim["symbol"] == r["symbol"]) & (prim["date"] < ev) & ~((prim["open"] == 0) & (prim["high"] == 0) & (prim["low"] == 0))]
        if s_rows.empty:
            raise ValueError(f"no primary bar for {r['symbol']} before {r['d']}")
        b = s_rows["date"].max().strftime("%Y-%m-%d")
        cands.append({"symbol": r["symbol"], "date": r["d"], "before": b, "kind": r["kind"], "implied_ratio": r.get("implied_ratio"),
                      "confidence": r.get("confidence")})
        pairs += [(r["symbol"], b), (r["symbol"], r["d"])]
    pairs = list(dict.fromkeys(pairs))
    reqs: dict[str, set[str]] = {}
    for s, d in pairs:
        reqs.setdefault(d.replace("-", ""), set()).add(STOCK_SERVICE_FOR_MARKET[market_of[s]])
        if with_index:
            reqs[d.replace("-", "")].add(INDEX_SERVICE_FOR_MARKET[market_of[s]])
    requests_ = [{"basDd": d, "service": svc, "url": SERVICES[svc].url, "name_ko": SERVICES[svc].name_ko}
                 for d in sorted(reqs) for svc in sorted(reqs[d])]
    plan = {"symbols": {s: market_of[s] for s in chosen}, "symbols_skipped_unknown_market": unknown, "pairs": pairs,
            "candidates": cands, "requests": requests_, "request_count": len(requests_), "spec_source": SPEC_SOURCE,
            "seed": seed, "data_dir": str(data), "audit_dir": str(audit) if audit else None}
    return plan, prim


def run(argv: list[str] | None = None, krx_client=None, dart_client=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.krxcheck")
    ap.add_argument("--data", required=True)
    ap.add_argument("--audit", default=None)
    ap.add_argument("--symbols", nargs="*", default=[])
    ap.add_argument("--sample-symbols", type=int, default=3)
    ap.add_argument("--dates", nargs="*", default=[], help="YYYY-MM-DD")
    ap.add_argument("--n-dates", type=int, default=1)
    ap.add_argument("--candidate", nargs="*", default=[], help="SYMBOL:YYYY-MM-DD rows of corporate_action_candidates.csv")
    ap.add_argument("--with-index", action="store_true")
    ap.add_argument("--dart", action="store_true", help="match the --candidate rows with OpenDART filings")
    ap.add_argument("--dart-max-pages", type=int, default=2)
    ap.add_argument("--dart-max-requests", type=int, default=5, help="refuse --dart plans needing more OpenDART requests")
    ap.add_argument("--max-requests", type=int, default=6, help="refuse plans that need more KRX requests")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--live", action="store_true", help="make the planned KRX (and --dart) requests")
    ap.add_argument("--spec-confirmed", action="store_true", help="I compared app/providers/krx.py SERVICES with the official spec")
    a = ap.parse_args(argv)
    data = Path(a.data)
    out = Path(a.out) if a.out else Path("data/krxcheck") / pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    try:
        guard_output_dir(out, data, a.overwrite)
        for d in a.dates:
            pd.Timestamp(d)
        plan, prim = build_plan(data, a.symbols, a.sample_symbols, a.dates, a.n_dates, a.candidate,
                                Path(a.audit) if a.audit else None, a.seed, a.start, a.end, a.with_index)
    except (ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if plan["request_count"] > a.max_requests:
        print(f"plan needs {plan['request_count']} KRX requests > --max-requests {a.max_requests}; narrow the dates/symbols", file=sys.stderr)
        return 2
    # OpenDART: corpCode.xml once + up to --dart-max-pages list.json pages per candidate (an upper bound)
    plan["dart_request_upper_bound"] = (1 + len(plan["candidates"]) * a.dart_max_pages) if a.dart and plan["candidates"] else 0
    if plan["dart_request_upper_bound"] > a.dart_max_requests:
        print(f"plan may need {plan['dart_request_upper_bound']} OpenDART requests > --dart-max-requests {a.dart_max_requests}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    _write(out / "plan.json", plan)
    print(f"plan: {len(plan['symbols'])} symbols, {len(plan['pairs'])} (symbol, date) pairs, {plan['request_count']} KRX requests"
          f"{' + OpenDART' if a.dart and plan['candidates'] else ''} -> {out}/plan.json", flush=True)
    if not a.live:
        print("no --live: nothing was requested", flush=True)
        return 0
    if not a.spec_confirmed:
        print("--live needs --spec-confirmed: compare app/providers/krx.py SERVICES with the official KRX API spec first", file=sys.stderr)
        return 2
    krx = krx_client or KRXClient(spec_confirmed=True)
    status = {"started_utc": pd.Timestamp.now(tz="UTC").isoformat(), "git": git_info(), "environment": environment_info(),
              "spec_source": SPEC_SOURCE, "krx": None, "dart": None}
    if not krx.configured:
        status["krx"] = {"ran": False, "reason": "KRX_API_KEY not set"}
        _write(out / "krxcheck_status.json", status)
        print("KRX_API_KEY not set: nothing requested", file=sys.stderr)
        return 3
    t = time.time()
    frames: dict[str, list[pd.DataFrame]] = {}
    day_status: dict[str, str] = {}
    svc_status: dict[tuple[str, str], str] = {}     # (basDd, service) -> ok | empty | failed: <kind>
    index_rows, errors, stopped = [], [], None
    want = set(plan["symbols"])
    for r in plan["requests"]:
        if stopped:
            break
        svc, d = r["service"], r["basDd"]
        try:
            res = krx.fetch(svc, d)
        except KRXAPIError as exc:
            errors.append({"basDd": d, "service": svc, "kind": exc.kind, "error": str(exc)[:200]})
            day_status.setdefault(d, f"failed: {exc.kind}")
            svc_status[(d, svc)] = f"failed: {exc.kind}"
            if exc.kind in STOP_KINDS:
                stopped = exc.kind
            continue
        svc_status[(d, svc)] = res.status
        if res.status == "empty":
            day_status.setdefault(d, "empty")
            continue
        f = res.frame
        if SERVICES[svc].kind == "stock":
            sub = f[f["symbol"].isin(want)]
            sub.to_csv(out / f"krx_{svc}_{d}.csv", index=False)       # sampled rows only, not the whole market
            frames.setdefault(d, []).append(sub)
            day_status[d] = "ok"
        else:
            f.to_csv(out / f"krx_{svc}_{d}.csv", index=False)
            index_rows.append(f)
        if res.unparsed_values:
            errors.append({"basDd": d, "service": svc, "kind": "unparsed_values", "count": res.unparsed_values})
    krx_by_date = {d: (pd.concat(frames[d]) if d in frames else day_status.get(d, "not fetched")) for d in {r["basDd"] for r in plan["requests"]}}
    detail = compare_sample(prim, krx_by_date, [tuple(p) for p in plan["pairs"]])
    # a symbol whose own market service failed / was empty / was never sent must not read as "missing in KRX"
    for i, r in detail.iterrows():
        st = svc_status.get((r["date"].replace("-", ""), STOCK_SERVICE_FOR_MARKET[plan["symbols"][r["symbol"]]]))
        if st != "ok":
            detail.loc[i, ["status", "reason"]] = ([KRX_DAY_EMPTY, "KRX answered with no rows"] if st == "empty"
                                                   else [REQUEST_FAILED, st or "not sent (run stopped earlier)"])
    detail.to_csv(out / "comparison_detail.csv", index=False)
    summ = summarize(detail)
    status["krx"] = {"ran": True, "requests_sent": krx.calls, "http_attempts": getattr(krx, "http_attempts", None),
                     "stopped_on": stopped, "errors": errors, **summ,
                     "isu_cd_forms_seen": sorted({("isin" if len(str(x)) == 12 else "short") for f in frames.values() for x in pd.concat(f)["isu_cd_raw"]}) if frames else []}
    if index_rows:
        status["krx"]["index_rows"] = int(sum(len(x) for x in index_rows))
    # candidates: KRX price evidence + optional OpenDART filings
    evidence = {(c["symbol"], c["date"]): krx_evidence_for_candidate(detail, c["symbol"], c["before"], c["date"],
                                                                     float(c["implied_ratio"]) if c.get("implied_ratio") not in (None, "") and pd.notna(c.get("implied_ratio")) else None)
                for c in plan["candidates"]}
    if plan["candidates"]:
        cand_df = pd.DataFrame(plan["candidates"])[["symbol", "date", "kind"]]
        filings = pd.DataFrame(columns=["symbol", "rcept_dt", "report_nm", "corporate_action"])
        no_code: list[str] = []
        if a.dart:
            if dart_client is None:
                from app.providers.dart import DartClient
                dart_client = DartClient()
            if not dart_client.configured:
                status["dart"] = {"ran": False, "reason": "DART_API_KEY not set"}
            else:
                try:
                    codes = dart_client.corp_codes()
                    rows = []
                    for c in plan["candidates"]:
                        code = codes.get(c["symbol"])
                        if not code:
                            no_code.append(c["symbol"])
                            continue
                        ev = pd.Timestamp(c["date"])
                        rows += dart_client.disclosures(code["corp_code"], (ev - pd.Timedelta(150, unit="D")).strftime("%Y%m%d"),
                                                        (ev + pd.Timedelta(15, unit="D")).strftime("%Y%m%d"), max_pages=a.dart_max_pages)
                    if rows:
                        filings = pd.DataFrame(rows)
                        filings.to_csv(out / "dart_filings.csv", index=False)
                    status["dart"] = {"ran": True, "filings": len(rows), "no_corp_code": no_code}
                except Exception as exc:  # DartAPIError and transport errors: recorded, never retried here
                    status["dart"] = {"ran": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
        else:
            status["dart"] = {"ran": False, "reason": "--dart not given"}
        matches = match_corporate_actions(cand_df, filings)
        if not (status["dart"] or {}).get("ran"):
            matches.loc[matches["kind"].notna(), "status"] = "filings_not_checked"
        matches.loc[matches["symbol"].isin(no_code) & matches["kind"].notna(), "status"] = "no_corp_code"
        matches.to_csv(out / "corporate_action_matches.csv", index=False)
        verd = candidate_verdicts(matches, evidence)
        verd.to_csv(out / "candidate_verdicts.csv", index=False)
        status["candidates"] = verd[["symbol", "date", "verdict", "verdict_reason"]].to_dict("records") if len(verd) else []
    status["seconds"] = round(time.time() - t, 1)
    _write(out / "krxcheck_status.json", status)
    print(f"KRX requests sent {krx.calls}; statuses {summ['by_status']}; stopped_on={stopped} -> {out}/krxcheck_status.json", flush=True)
    return 4 if stopped else 0


if __name__ == "__main__":
    sys.exit(run())
