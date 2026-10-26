"""Robustness Gate MVP: checks a Factor Lab run for one factor and returns PASS / WATCH / FAIL / INSUFFICIENT_DATA.

The gate does not pick parameters and does not re-run anything on the test period: it reads one Factor Lab output
folder (per-date table + manifest), optionally the experiment ledger, and applies fixed checks.

Direction is fixed on the dev period only (sign of dev mean rank IC). Validation and test are evaluated in that
direction; neither is used to choose a direction, a factor or a threshold. The overall status is the worst check:
FAIL > INSUFFICIENT_DATA > WATCH > PASS, so PASS requires every check to pass.

Thresholds are `GateConfig` defaults (documented in docs/ROBUSTNESS_GATE.md). A pre-registered spec may declare its
own `gate` section; thresholds passed at evaluation time that differ from the registered ones make the
`thresholds` check WATCH, because they were chosen after the spec was fixed.

Not implemented (reported as such, never estimated): Probability of Backtest Overfitting (PBO, CSCV), Deflated
Sharpe Ratio, White's Reality Check / SPA, regime-conditional checks.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

from app.research.manifest import file_sha256

PASS, WATCH, FAIL, INSUFFICIENT = "PASS", "WATCH", "FAIL", "INSUFFICIENT_DATA"
SEVERITY = {PASS: 0, WATCH: 1, INSUFFICIENT: 2, FAIL: 3}
NOT_IMPLEMENTED = ["PBO (probability of backtest overfitting)", "Deflated Sharpe Ratio", "White Reality Check / Hansen SPA",
                   "regime-conditional performance", "short-sale borrow costs and restrictions"]


@dataclass
class GateConfig:
    min_dates_dev: int = 24                # non-overlapping holding windows
    min_dates_validation: int = 8
    min_dates_test: int = 8
    min_names_mean: float = 50.0
    min_coverage: float = 0.5              # names used / names eligible, mean over dates
    dev_rank_ic_t_min: float = 2.0         # dev must show an effect before anything else is looked at
    validation_rank_ic_t_watch: float = 1.0
    walk_forward_min_hit: float = 0.6      # share of years whose sign (from earlier years only) is right
    walk_forward_fail_below: float = 0.4
    cost_stress_multipliers: tuple[float, ...] = (2.0, 3.0)
    max_audit_review_share: float = 0.5

    @classmethod
    def from_dict(cls, d: dict | None) -> "GateConfig":
        d = d or {}
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown gate thresholds {unknown}")
        vals = {k: (tuple(v) if k == "cost_stress_multipliers" else v) for k, v in d.items()}
        return cls(**vals)


def _check(name: str, status: str, detail: str, **values) -> dict:
    return {"check": name, "status": status, "detail": detail, "values": values}


def _t(x: pd.Series) -> float:
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else float("nan")


def long_leg_costs(g: pd.DataFrame, leg: str, buy: float, sell: float, mult: float = 1.0) -> pd.Series:
    """Cost of holding one quantile leg long, recomputed from its turnover (same convention as the Factor Lab)."""
    first = g["first_in_period"].astype(bool)
    last = g["last_in_period"].astype(bool)
    turn = g[f"turnover_{leg}"]
    c = np.where(first, buy, turn * (buy + sell)) + np.where(last, sell, 0.0)
    return pd.Series(c * mult, index=g.index)


def oriented(df: pd.DataFrame, sign: int, buy: float, sell: float, mult: float = 1.0) -> pd.DataFrame:
    """Per-date metrics in the dev direction: rank IC x sign; long the top (sign>0) or bottom (sign<0) quantile."""
    leg = "top" if sign > 0 else "bottom"
    out = pd.DataFrame({"date": df["date"], "period": df["period"], "year": df["year"]})
    out["rank_ic"] = df["rank_ic"] * sign
    out["long_gross"] = df[leg]
    out["long_net"] = df[leg] - long_leg_costs(df, leg, buy, sell, mult)
    out["excess_net"] = out["long_net"] - df["ew_universe"]
    out["excess_gross"] = df[leg] - df["ew_universe"]
    return out


def walk_forward_sign(o_raw: pd.DataFrame) -> pd.DataFrame:
    """For each year after the first two (dev + validation only): sign from all earlier years, scored on that year."""
    yearly = o_raw.groupby("year")["rank_ic"].mean().sort_index()
    rows = []
    for i, (y, v) in enumerate(yearly.items()):
        if i < 2:
            continue
        prior = yearly.iloc[:i].mean()
        if prior == 0 or np.isnan(prior) or np.isnan(v):
            continue
        rows.append({"year": int(y), "sign_from_prior": int(np.sign(prior)), "year_rank_ic": float(v), "hit": bool(np.sign(v) == np.sign(prior))})
    return pd.DataFrame(rows)


def evaluate(factorlab_dir: Path, factor: str, gate: GateConfig | None = None, ledger_state: dict | None = None,
             ledger_id: str | None = None, registered_gate: dict | None = None) -> dict:
    d = Path(factorlab_dir)
    m = json.loads((d / "run_manifest.json").read_text(encoding="utf-8"))
    by = pd.read_csv(d / "factor_ic_by_date.csv", parse_dates=["date", "entry_date", "exit_date"])
    gate = gate or GateConfig.from_dict(registered_gate)
    checks: list[dict] = []
    f = by[by["factor"] == factor].sort_values("date").reset_index(drop=True)
    cfg = m.get("config", {})
    ca = m.get("cost_assumptions", {})
    buy, sell = ca.get("buy_side_bps", 0) / 1e4, ca.get("sell_side_bps", 0) / 1e4
    if factor not in (cfg.get("factors") or {}):
        raise ValueError(f"factor {factor!r} is not in this Factor Lab run ({sorted(cfg.get('factors') or {})})")

    # 1. thresholds fixed in advance
    reg = GateConfig.from_dict(registered_gate) if registered_gate is not None else GateConfig()
    diff = {k: {"registered_or_default": v, "used": asdict(gate)[k]} for k, v in asdict(reg).items() if asdict(gate)[k] != v}
    checks.append(_check("thresholds", WATCH if diff else PASS,
                         "thresholds differ from the registered/default values (chosen after the spec)" if diff else
                         ("registered thresholds" if registered_gate is not None else "default thresholds (docs/ROBUSTNESS_GATE.md)"), changed=diff))

    # 2. period separation (config and the actual rows)
    per = {k: (pd.Timestamp(a), pd.Timestamp(b)) for k, (a, b) in (cfg.get("periods") or {}).items()}
    spans = sorted((a, b, k) for k, (a, b) in per.items())
    overlap = [f"{k1}/{k2}" for (a1, b1, k1), (a2, _, k2) in zip(spans, spans[1:]) if a2 <= b1]
    need = int(np.ceil((cfg.get("horizon", 0) + 1) * 7 / 5))
    leaks = int(sum(((f["period"] == k) & ((f["exit_date"] > b) | (f["date"] < a))).sum() for k, (a, b) in per.items()))
    order_bad = int(((f["entry_date"] <= f["date"]) | (f["exit_date"] <= f["entry_date"])).sum())
    bad = bool(overlap) or cfg.get("embargo_days", 0) < need or leaks or order_bad or "test" not in per or "dev" not in per
    checks.append(_check("period_separation", FAIL if bad else PASS,
                         "periods overlap, embargo too short, a holding window leaves its period, or timing is not t < entry < exit" if bad
                         else "dev/validation/test do not overlap; every holding window ends inside its period",
                         overlapping=overlap, embargo_days=cfg.get("embargo_days"), embargo_needed=need, rows_crossing=leaks, rows_bad_timing=order_bad))

    # 3. pre-registration
    st = (ledger_state or {}).get(ledger_id) if ledger_id else None
    if not ledger_id or st is None:
        checks.append(_check("preregistration", WATCH, "no registered experiment linked; results cannot be told apart from a search"))
    else:
        reg_at = pd.Timestamp(st["registered_at"])
        run_at = pd.Timestamp(m["started_utc"]) if m.get("started_utc") else None
        after = run_at is not None and reg_at > run_at
        amended = [a for a in st["amendments"] if a["after_results"]]
        linked = m.get("ledger_experiment") or {}
        stale = bool(linked) and linked.get("spec_hash") != st["spec_hash"]
        status = FAIL if after or (linked and linked.get("id") != ledger_id) else (WATCH if amended or stale or not linked else PASS)
        checks.append(_check("preregistration", status,
                             "registered after the run started" if after else
                             "spec amended after results were seen" if amended else
                             "run was made against an older spec version" if stale else
                             "run not started through the ledger" if not linked else "registered before the run, current spec",
                             registered_at=st["registered_at"], run_started=m.get("started_utc"), amendments_after_results=len(amended),
                             spec_version=st["version"]))
        planned, total = st["spec"].get("planned_experiments"), m.get("experiments_total")
        if planned is not None and total is not None:
            checks.append(_check("experiment_count", WATCH if total > planned else PASS,
                                 f"{total} configurations evaluated vs {planned} planned" + (" — more than declared" if total > planned else ""),
                                 planned=planned, actual=total))
        fp = st["spec"].get("data_fingerprint")
        if fp:
            checks.append(_check("data_fingerprint_matches_spec", PASS if fp == m.get("data_fingerprint") else FAIL,
                                 "manifest data fingerprint equals the registered one" if fp == m.get("data_fingerprint") else "data differs from the registered fingerprint",
                                 registered=fp, manifest=m.get("data_fingerprint")))
        sp = st["spec"].get("periods")
        if isinstance(sp, dict):
            same = {k: list(map(str, v)) for k, v in sp.items()} == {k: list(map(str, v)) for k, v in (cfg.get("periods") or {}).items()}
            checks.append(_check("periods_match_spec", PASS if same else FAIL, "run periods equal the registered periods" if same
                                 else "run periods differ from the registered periods", registered=sp, run=cfg.get("periods")))

    # 4. sample size and coverage
    counts = {p: int((f["period"] == p).sum()) for p in ("dev", "validation", "test")}
    mins = {"dev": gate.min_dates_dev, "validation": gate.min_dates_validation, "test": gate.min_dates_test}
    short = {p: counts[p] for p in mins if counts[p] < mins[p]}
    names = float(f["n"].mean()) if len(f) else 0.0
    cov = float((f["n"] / f["n_eligible"]).mean()) if len(f) else 0.0
    insufficient = bool(short) or names < gate.min_names_mean or cov < gate.min_coverage
    checks.append(_check("sample_size", INSUFFICIENT if insufficient else PASS,
                         "too few dates, names or coverage" if insufficient else "enough dates, names and coverage",
                         dates=counts, minimum=mins, names_mean=names, coverage_mean=cov, min_names=gate.min_names_mean, min_coverage=gate.min_coverage))

    # 5. dev effect -> direction
    dev = f[f["period"] == "dev"]
    dev_mean = float(dev["rank_ic"].mean()) if len(dev) else float("nan")
    dev_t = _t(dev["rank_ic"])
    sign = int(np.sign(dev_mean)) if np.isfinite(dev_mean) and dev_mean != 0 else 0
    ok_dev = sign != 0 and np.isfinite(dev_t) and abs(dev_t) >= gate.dev_rank_ic_t_min
    checks.append(_check("dev_effect", PASS if ok_dev else (INSUFFICIENT if len(dev) < gate.min_dates_dev else FAIL),
                         f"dev rank IC t = {dev_t:.2f} (|t| >= {gate.dev_rank_ic_t_min} needed); direction {'+' if sign > 0 else '-' if sign < 0 else 'none'}",
                         dev_rank_ic_mean=dev_mean, dev_rank_ic_t=dev_t, direction=sign))
    o = oriented(f, sign or 1, buy, sell)

    def period_check(p: str) -> dict:
        g = o[o["period"] == p]
        if len(g) < mins[p] or not sign:
            return _check(f"{p}_out_of_sample", INSUFFICIENT, f"{len(g)} dates or no dev direction", dates=len(g))
        ric, ric_t = float(g["rank_ic"].mean()), _t(g["rank_ic"])
        ex, ex_g = float(g["excess_net"].mean()), float(g["excess_gross"].mean())
        status = FAIL if ric <= 0 or ex <= 0 else (WATCH if not (ric_t >= gate.validation_rank_ic_t_watch) else PASS)
        return _check(f"{p}_out_of_sample", status,
                      "direction reversed or no net excess over the equal-weight universe" if status == FAIL else
                      "same direction with net excess, but weak" if status == WATCH else "same direction, positive net excess",
                      dates=len(g), rank_ic_mean=ric, rank_ic_t=ric_t, long_net_excess_ew_mean=ex, long_gross_excess_ew_mean=ex_g,
                      long_net_mean=float(g["long_net"].mean()))
    v = period_check("validation")
    checks.append(v)
    # 6. walk-forward on years inside dev + validation (test is not used)
    wf = walk_forward_sign(o[o["period"].isin(["dev", "validation"])].assign(rank_ic=f["rank_ic"]))
    if len(wf) < 3:
        checks.append(_check("walk_forward", INSUFFICIENT, f"{len(wf)} scorable years (3 needed)", years=wf.to_dict("records")))
    else:
        hit = float(wf["hit"].mean())
        checks.append(_check("walk_forward", PASS if hit >= gate.walk_forward_min_hit else (FAIL if hit < gate.walk_forward_fail_below else WATCH),
                             f"sign from earlier years was right in {hit:.0%} of later years", hit_share=hit, years=wf.to_dict("records")))
    # 7. cost stress (validation, dev direction)
    gv = f[f["period"] == "validation"]
    stress = {}
    for mult in gate.cost_stress_multipliers:
        ov = oriented(gv, sign or 1, buy, sell, mult) if len(gv) else pd.DataFrame()
        stress[str(mult)] = float(ov["excess_net"].mean()) if len(ov) else float("nan")
    first_mult = str(gate.cost_stress_multipliers[0]) if gate.cost_stress_multipliers else None
    if not len(gv) or not sign:
        checks.append(_check("cost_stress", INSUFFICIENT, "no validation dates or direction", excess_by_multiplier=stress))
    else:
        bad = [k for k, x in stress.items() if not x > 0]
        checks.append(_check("cost_stress", PASS if not bad else (WATCH if first_mult not in bad else FAIL),
                             "net excess stays positive under higher costs" if not bad else f"net excess <= 0 at cost x{', x'.join(bad)}",
                             excess_by_multiplier=stress, turnover_mean=float(gv[f"turnover_{'top' if sign > 0 else 'bottom'}"].mean())))
    # 8. parameter neighbourhood: same kind, other windows, in the same run (dev + validation, no test)
    kind = cfg["factors"][factor]["kind"]
    neigh = [n for n, s in cfg["factors"].items() if s.get("kind") == kind and n != factor]
    if not neigh:
        checks.append(_check("parameter_neighbourhood", WATCH, f"no other {kind} windows in this run; stability not tested"))
    else:
        dv = by[by["period"].isin(["dev", "validation"])]
        signs = {n: float(dv.loc[dv["factor"] == n, "rank_ic"].mean()) for n in neigh}
        flip = [n for n, x in signs.items() if not (np.sign(x) == sign)]
        checks.append(_check("parameter_neighbourhood", PASS if not flip else WATCH,
                             "neighbouring windows point the same way" if not flip else f"neighbours with the other sign or no data: {flip}",
                             neighbour_rank_ic_mean_dev_validation=signs))
    # 9. benchmark (secondary KOSPI, reported)
    gvo = o[o["period"] == "validation"]
    kos = float((gv[("top" if sign > 0 else "bottom")] - gv["kospi_cc"]).mean()) if len(gv) and gv["kospi_cc"].notna().any() else None
    checks.append(_check("benchmark", INSUFFICIENT if not len(gvo) or not sign else (PASS if gvo["excess_net"].mean() > 0 else FAIL),
                         "net of costs vs equal-weight universe (same timing); KOSPI close-to-close shown for reference only",
                         validation_net_excess_ew=float(gvo["excess_net"].mean()) if len(gvo) else None, validation_gross_excess_kospi_cc=kos))
    # 10. audit
    au = m.get("audit")
    if not au:
        checks.append(_check("audit_applied", WATCH, "run without --audit: known bad bars were not excluded"))
    else:
        rs = au.get("review_share")
        checks.append(_check("audit_applied", WATCH if rs is not None and rs > gate.max_audit_review_share else PASS,
                             f"audit applied; {rs:.0%} of instruments still need review" if rs is not None else "audit applied",
                             review_share=rs, symbols_dropped=au.get("symbols_dropped")))
    # 11. data validity
    vs = (m.get("validity") or {}).get("status")
    checks.append(_check("data_validity", {"VERIFIED": PASS, "PROVISIONAL": WATCH}.get(vs, FAIL), f"manifest validity {vs}",
                         unverified=(m.get("validity") or {}).get("unverified_items"), ineligible=(m.get("validity") or {}).get("ineligible_reasons")))
    # 12. reproducibility
    git = m.get("git") or {}
    mism = []
    for o_ in m.get("outputs") or []:
        p = d / o_["path"]
        if not p.is_file() or file_sha256(p) != o_["sha256"]:
            mism.append(o_["path"])
    status = FAIL if mism else (PASS if git.get("reproducible_from_commit") else WATCH)
    checks.append(_check("reproducibility", status,
                         f"output files changed after the run: {mism}" if mism else
                         "code committed, outputs match the manifest" if status == PASS else "uncommitted code at run time",
                         commit=git.get("commit"), uncommitted=git.get("uncommitted_code_files"), changed_outputs=mism))
    # test period: confirmation only, after everything above, never used to pick direction or thresholds
    t = period_check("test")
    checks.append(t)
    overall = max((c["status"] for c in checks), key=SEVERITY.get)
    return {"factor": factor, "factorlab_dir": str(d), "status": overall, "direction_from_dev": sign,
            "checks": checks, "gate_config": asdict(gate), "not_implemented": NOT_IMPLEMENTED,
            "manifest_sha256": file_sha256(d / "run_manifest.json"), "data_fingerprint": m.get("data_fingerprint"),
            "experiments_total_in_run": m.get("experiments_total"), "ledger_id": ledger_id,
            "note": "PASS means every implemented check passed on this data; it is not proof of a persistent edge."}


def report_md(res: dict) -> str:
    lines = [f"# Robustness Gate — {res['factor']}: **{res['status']}**", "",
             f"- Factor Lab: `{res['factorlab_dir']}` (manifest sha256 `{res['manifest_sha256'][:16]}`), data `{res['data_fingerprint']}`",
             f"- Direction fixed on dev: {res['direction_from_dev']:+d}" if res["direction_from_dev"] else "- Direction: none (no dev effect)",
             "", "| 검사 | 상태 | 내용 |", "|---|---|---|"]
    for c in res["checks"]:
        lines.append(f"| {c['check']} | {c['status']} | {c['detail']} |")
    lines += ["", "미구현(추정하지 않음): " + ", ".join(res["not_implemented"]), "", res["note"]]
    return "\n".join(lines) + "\n"
