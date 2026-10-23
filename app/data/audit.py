"""Instrument-level data audit: what is wrong, where, why it may be wrong, and what the pipeline does about it.

The audit never edits its input. It runs the same cleaning the research loader runs (`check_frame`, halt-marker
removal) so "rows a backtest uses" in the audit and in the study are the same rows, and then explains each finding:

  findings      one row per (symbol, date, check): observed value, expected condition, rationale, likely cause,
                what the pipeline does today (`backtest_use`) and what the audit recommends (`recommendation`)
  corrections   before/after values of every automatic repair (today: adjusted-price rounding only)
  exclusions    contiguous ranges of rows removed from the data or excluded from signals, with the reason
  candidates    suspected corporate actions (split/merge/bonus issue) to verify against OpenDART filings
  symbols       per-instrument summary and status (ok / review / exclude)

Thresholds live in `AuditConfig`; docs/DATA_AUDIT.md explains the basis and the limits of each one.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from app.data.quality import ERROR, WARNING, check_frame, missing_sessions

INFO = "info"
REQUIRED = ["date", "open", "high", "low", "close", "volume"]
FINDING_COLS = ["symbol", "date", "check", "category", "severity", "observed", "expected", "rationale", "cause",
                "review_reason", "auto_fix", "backtest_use", "recommendation", "follow_up"]
CORRECTION_COLS = ["symbol", "date", "field", "before", "after", "rule"]
EXCLUSION_COLS = ["symbol", "start", "end", "rows", "scope", "reason"]
CANDIDATE_COLS = ["symbol", "date", "kind", "implied_ratio", "price_ratio", "volume_ratio", "gap_sessions", "confidence",
                  "evidence", "verify_with"]

# recommendation values
OK, REVIEW, EXCLUDE = "ok", "review", "exclude_until_verified"


@dataclass
class AuditConfig:
    # same defaults as the research loader, so audit and study agree on which rows are usable
    jump_threshold: float = 0.30
    rounding_tolerance: float = 0.002
    gap_sessions: int = 5
    volume_break_factor: float = 3.0
    volume_window: int = 60
    signal_window: int = 40                # study.prepare(): signals whose 40-bar volume window spans a break are dropped
    remove_halt_markers: bool = True       # Naver marks suspended sessions with O=H=L=0 or a flat zero-volume bar
    # KRX daily price limit: +/-15% until 2015-06-12, +/-30% from 2015-06-15 (relative to the previous close/base price)
    price_limit_change_date: str = "2015-06-15"
    price_limit_before: float = 0.15
    price_limit_after: float = 0.30
    price_limit_slack: float = 0.005       # tick-size rounding of the limit price
    split_ratios: tuple[int, ...] = (2, 3, 4, 5, 10, 20, 25, 50)
    split_price_tolerance: float = 0.03    # |ratio*k - 1| for a price step to count as split-like
    split_volume_tolerance: float = 0.25   # |log(volume ratio / k)| <= log(1 + tol) for a volume step to match k
    no_gap_volume_shift: bool = True       # also look for volume level shifts without a trading gap (info only)
    no_gap_shift_factor: float = 5.0
    delisting_gap_days: int = 30           # last bar this many calendar days before the delisting date -> flag
    symbol_pattern: str = r"\d{6}"         # KRX short code (KR market)

    def price_limit(self, dates: pd.Series) -> np.ndarray:
        cut = pd.Timestamp(self.price_limit_change_date)
        return np.where(pd.to_datetime(dates) >= cut, self.price_limit_after, self.price_limit_before)


@dataclass
class AuditResult:
    findings: pd.DataFrame
    corrections: pd.DataFrame
    exclusions: pd.DataFrame
    candidates: pd.DataFrame
    symbols: pd.DataFrame
    usable: pd.DataFrame                   # rows the research loader would keep (after repairs), for reconciliation
    summary: dict = field(default_factory=dict)


def _f(row: dict, check: str, category: str, severity: str, observed: str, expected: str, rationale: str, cause: str,
       auto_fix: str, backtest_use: str, recommendation: str, follow_up: str, review_reason: str = "") -> dict:
    return {"symbol": row.get("symbol"), "date": row.get("date"), "check": check, "category": category, "severity": severity,
            "observed": observed, "expected": expected, "rationale": rationale, "cause": cause, "review_reason": review_reason,
            "auto_fix": auto_fix, "backtest_use": backtest_use, "recommendation": recommendation, "follow_up": follow_up}


def _ohlc(r) -> str:
    return f"O={r['open']} H={r['high']} L={r['low']} C={r['close']}"


def _nearest_ratio(r: float, ratios: tuple[int, ...]) -> tuple[float, float]:
    """Closest k or 1/k to r (k in ratios) and its log distance."""
    if not np.isfinite(r) or r <= 0:
        return float("nan"), float("inf")
    cands = np.array([*ratios, *[1 / k for k in ratios]], dtype=float)
    d = np.abs(np.log(r / cands))
    i = int(np.argmin(d))
    return float(cands[i]), float(d[i])


def _ranges(dates: pd.Series, mask: np.ndarray) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Contiguous runs of True in `mask` (positional) as (first date, last date, rows)."""
    out = []
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return out
    d = pd.to_datetime(dates).to_numpy()
    edges = np.flatnonzero(np.diff(np.r_[0, m.astype(int), 0]))
    for a, b in zip(edges[::2], edges[1::2]):
        out.append((pd.Timestamp(d[a]), pd.Timestamp(d[b - 1]), int(b - a)))
    return out


# ------------------------------------------------------------------------------------------------ one instrument

def audit_symbol(raw: pd.DataFrame, cfg: AuditConfig, calendar: pd.DatetimeIndex | None = None,
                 instrument: dict | None = None) -> dict:
    """Audit one instrument's raw rows. Returns dict of lists/frames; `raw` is not modified."""
    sym = str(raw["symbol"].iloc[0]) if "symbol" in raw and len(raw) else (instrument or {}).get("symbol")
    findings, corrections, exclusions, candidates = [], [], [], []
    work = raw.copy()
    if "market" not in work:
        work["market"] = "KR"
    work["symbol"] = sym

    # 0. schema: missing columns stop the instrument; unparsable values and dates are reported row by row
    miss_cols = [c for c in REQUIRED if c not in work]
    if miss_cols:
        findings.append(_f({"symbol": sym, "date": None}, "missing_columns", "schema", ERROR, ", ".join(miss_cols),
                           ", ".join(REQUIRED), "a daily bar needs all OHLCV fields", "file_format", "none", "exclude", EXCLUDE,
                           "fix the source file / loader mapping"))
        return {"findings": findings, "corrections": corrections, "exclusions": exclusions, "candidates": candidates,
                "usable": work.iloc[0:0], "rows": len(work)}
    raw_dates = work["date"]
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    bad_date = work["date"].isna()
    for i in work.index[bad_date]:
        findings.append(_f({"symbol": sym, "date": None}, "unparsable_date", "schema", ERROR, repr(raw_dates.at[i])[:40],
                           "ISO date (YYYY-MM-DD) or YYYYMMDD", "the row cannot be placed in time", "file_format", "removed",
                           "exclude", EXCLUDE, "correct the date format at the source"))
    non_numeric_dates = set()
    for col in ["open", "high", "low", "close", "volume"]:
        num = pd.to_numeric(work[col], errors="coerce")
        bad = num.isna() & work[col].notna()
        non_numeric_dates.update(work.loc[bad, "date"].dropna())
        for i in work.index[bad]:
            findings.append(_f({"symbol": sym, "date": work.at[i, "date"]}, "non_numeric_value", "schema", ERROR,
                               f"{col}={str(work.at[i, col])[:30]!r}", f"{col} numeric", "text in a numeric field",
                               "file_format", "removed", "exclude", EXCLUDE, "inspect the raw file around this date"))
        work[col] = num
    ok_date = work["date"].notna()
    if ok_date.any():
        times = work.loc[ok_date, "date"] != work.loc[ok_date, "date"].dt.normalize()
        for i in times[times].index:
            findings.append(_f({"symbol": sym, "date": work.at[i, "date"]}, "date_has_time_component", "schema", WARNING,
                               str(work.at[i, "date"]), "a trading date without time", "daily bars are dated by session",
                               "timezone_or_timestamp_export", "none", "include_flagged", REVIEW, "normalise dates in the loader"))
        wkend = work.loc[ok_date, "date"].dt.dayofweek >= 5
        for i in wkend[wkend].index:
            findings.append(_f({"symbol": sym, "date": work.at[i, "date"]}, "weekend_date", "calendar", ERROR,
                               work.at[i, "date"].strftime("%a"), "Mon–Fri session", "KRX does not trade on weekends",
                               "timezone_shift_or_bad_record", "none", "include_flagged", EXCLUDE,
                               "check timezone handling and the provider record"))
        if calendar is not None and len(calendar):
            cal = pd.DatetimeIndex(calendar).normalize()
            inside = (work["date"] >= cal.min()) & (work["date"] <= cal.max()) & ok_date & (work["date"].dt.dayofweek < 5)
            off = inside & ~work["date"].dt.normalize().isin(cal)
            for i in work.index[off]:
                findings.append(_f({"symbol": sym, "date": work.at[i, "date"]}, "date_not_in_market_calendar", "calendar", WARNING,
                                   work.at[i, "date"].strftime("%Y-%m-%d"), "a session in the index calendar",
                                   "the market index has no bar on this weekday (holiday or index gap)",
                                   "holiday_record_or_index_gap", "none", "include_flagged", REVIEW, "compare with the KRX holiday list"))
    if str(work["market"].iloc[0] if len(work) else "KR").upper() == "KR":
        if not re.fullmatch(cfg.symbol_pattern, sym or ""):
            findings.append(_f({"symbol": sym, "date": None}, "symbol_format", "identifier", WARNING, repr(sym),
                               f"matches {cfg.symbol_pattern}", "KRX short codes are 6 digits; leading zeros are often lost in spreadsheets",
                               "identifier_formatting", "none", "include", REVIEW, "zero-pad or map the identifier"))

    # 1. provider halt markers (suspended sessions shown as bars)
    usable_in = work[ok_date].copy()
    if cfg.remove_halt_markers and len(usable_in):
        hm = (usable_in["open"] == 0) & (usable_in["high"] == 0) & (usable_in["low"] == 0) & (usable_in["close"] > 0)
        flat0 = ((usable_in["volume"] == 0) & (usable_in["open"] == usable_in["high"]) & (usable_in["high"] == usable_in["low"])
                 & (usable_in["low"] == usable_in["close"]) & (usable_in["close"] > 0))
        for mask, check, cause, obs in ((hm, "halt_marker_zero_ohl", "provider_halt_marker", "O=H=L=0, C>0"),
                                        (flat0, "halt_marker_flat_zero_volume", "suspended_session_filler", "O=H=L=C, V=0")):
            for a, b, n in _ranges(usable_in["date"], mask.to_numpy()):
                exclusions.append({"symbol": sym, "start": a, "end": b, "rows": n, "scope": "row_removed", "reason": check})
            for i in usable_in.index[mask]:
                findings.append(_f({"symbol": sym, "date": usable_in.at[i, "date"]}, check, "suspension", INFO, obs,
                                   "a traded session", "the provider prints suspended sessions as bars; they are not tradable",
                                   cause, "removed", "exclude", OK, "none (expected provider behaviour)"))
        usable_in = usable_in[~(hm | flat0)]

    # 2. the loader's own checks (same function, same thresholds). check_frame keeps positional indices, so a sorted,
    #    re-indexed input lets rejected rows be mapped back to their position for the exclusion ranges.
    usable_in = usable_in.sort_values("date", kind="stable").reset_index(drop=True)
    rep = check_frame(usable_in, jump_threshold=cfg.jump_threshold, gap_business_days=cfg.gap_sessions, calendar=calendar,
                      detect_volume_breaks=True, rounding_tolerance=cfg.rounding_tolerance)
    valid = rep.valid
    before = usable_in.set_index("date")
    vidx = valid.set_index("date") if len(valid) else valid
    for iss in rep.issues:
        d = pd.Timestamp(iss["date"]) if iss.get("date") else None
        r = before.loc[d] if d is not None and d in before.index else None
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        row = {"symbol": sym, "date": d}
        c = iss["check"]
        if c == "non_positive_price":
            zeros = [k for k in ("open", "high", "low", "close") if r is not None and r[k] <= 0]
            cause = "all_prices_zero" if len(zeros) == 4 else ("close_not_positive" if "close" in zeros else "partial_zero_prices")
            findings.append(_f(row, c, "price", ERROR, _ohlc(r) if r is not None else iss["detail"], "open, high, low, close > 0",
                               f"non-positive: {', '.join(zeros)}", cause, "removed", "exclude", EXCLUDE,
                               "compare the date with KIS/KRX; a run of such rows usually means a broken provider series",
                               "a price of 0 cannot be traded or adjusted"))
        elif c == "ohlc_inconsistent":
            hi_need = max(r["open"], r["close"], r["low"]) if r is not None else np.nan
            lo_need = min(r["open"], r["close"], r["high"]) if r is not None else np.nan
            viol = max(hi_need - r["high"], r["low"] - lo_need, 0) / max(r["high"], 1e-9) if r is not None else np.nan
            k, dist = _nearest_ratio(hi_need / r["high"], cfg.split_ratios) if r is not None and r["high"] > 0 else (np.nan, np.inf)
            cause = "mixed_adjusted_and_unadjusted_fields" if dist < np.log(1 + cfg.split_price_tolerance) else "provider_record_error"
            findings.append(_f(row, c, "price", ERROR, _ohlc(r) if r is not None else iss["detail"], "low <= open, close <= high",
                               f"violation {viol:.2%} of high (above rounding tolerance {cfg.rounding_tolerance:.1%})", cause,
                               "removed", "exclude", EXCLUDE, "cross-check the date with KIS daily bars",
                               "beyond rounding: some field is wrong, and which one cannot be known from this row"))
        elif c == "ohlc_rounding_repaired":
            a = vidx.loc[d] if d in vidx.index else None
            if isinstance(a, pd.DataFrame):
                a = a.iloc[0]
            for fld in ("high", "low"):
                if a is not None and r is not None and a[fld] != r[fld]:
                    corrections.append({"symbol": sym, "date": d, "field": fld, "before": float(r[fld]), "after": float(a[fld]),
                                        "rule": f"adjusted row, inconsistency <= {cfg.rounding_tolerance:.1%}: widen high/low to cover open/close"})
            findings.append(_f(row, c, "price", WARNING, _ohlc(r) if r is not None else "", "low <= open, close <= high",
                               "adjusted prices are rounded field by field, so close can exceed high by a unit",
                               "adjustment_rounding", "repaired", "include_flagged", OK,
                               "none; before/after values are in corrections.csv"))
        elif c in ("duplicate_identical", "duplicate_conflict"):
            sev = WARNING if c == "duplicate_identical" else ERROR
            findings.append(_f(row, c, "duplicate", sev, iss["detail"], "one bar per symbol and date",
                               "the same session appears more than once", "collection_overlap" if sev == WARNING else "conflicting_records",
                               "removed", "include" if sev == WARNING else "exclude", OK if sev == WARNING else EXCLUDE,
                               "none" if sev == WARNING else "decide which record is right with a second source"))
        elif c == "missing_value":
            if d in non_numeric_dates:
                continue  # already reported as non_numeric_value (the text became a missing number)
            findings.append(_f(row, c, "schema", ERROR, iss["detail"], "all OHLCV present", "a bar with a missing field is unusable",
                               "provider_gap_or_parse_error", "removed", "exclude", EXCLUDE, "inspect the raw file"))
        elif c == "negative_volume":
            findings.append(_f(row, c, "volume", ERROR, iss["detail"], "volume >= 0", "negative volume is impossible",
                               "provider_record_error", "removed", "exclude", EXCLUDE, "inspect the raw file"))
        elif c == "zero_volume":
            findings.append(_f(row, c, "volume", WARNING, "volume=0 with a price range", "volume > 0 on a session with prices",
                               "prices without trades, or volume missing", "no_trades_or_missing_volume", "none", "include_flagged", REVIEW,
                               "compare volume with KIS for the date"))
        elif c == "missing_trading_days":
            findings.append(_f(row, c, "calendar", WARNING, iss["detail"], f"< {cfg.gap_sessions} missing sessions",
                               "a gap in the series: suspension, or rows missing at the provider", "suspension_or_missing_rows",
                               "none", "include_flagged", REVIEW, "check the KRX trading-halt history / KIS for the gap"))
        elif c in ("price_jump", "possible_corporate_action", "volume_level_break"):
            pass  # classified below with the series context
        else:
            findings.append(_f(row, c, "other", iss["severity"], iss["detail"], "", "", "unclassified", "none",
                               "include_flagged", REVIEW, "review"))

    # 3. series-level classification on the rows the study would use
    if len(valid):
        v = valid.sort_values("date").reset_index(drop=True)
        prev_close = v["close"].shift(1)
        ratio = v["close"] / prev_close
        gaps = missing_sessions(v["date"], v["date"].shift(1), calendar)
        limit = cfg.price_limit(v["date"])
        adj = v["adjusted"].map(lambda x: x is True or x == 1) if "adjusted" in v else pd.Series(False, index=v.index)
        flags = v["quality_flags"].fillna("").astype(str) if "quality_flags" in v else pd.Series("", index=v.index)
        chg_all = (ratio - 1).abs().to_numpy()
        cand = np.isfinite(chg_all) & ((chg_all > cfg.jump_threshold) | (chg_all > limit + cfg.price_limit_slack))
        cand[0] = False
        for i in np.flatnonzero(cand):
            r_ = float(ratio.iat[i])
            chg = r_ - 1
            g = int(gaps.iat[i])
            over_limit = abs(chg) > limit[i] + cfg.price_limit_slack
            k, dist = _nearest_ratio(r_, cfg.split_ratios)
            split_like = dist < np.log(1 + cfg.split_price_tolerance)
            row = {"symbol": sym, "date": v.at[i, "date"]}
            obs = f"close {prev_close.at[i]:g} -> {v.at[i, 'close']:g} ({chg:+.1%}), {g} missing sessions before"
            exp = f"|change| <= {limit[i]:.0%} (KRX daily limit) on consecutive sessions"
            if g >= 1 and split_like:
                cause, rec, sev = ("unadjusted_split_or_merge" if not adj.at[i] else "adjustment_missing_after_suspension"), EXCLUDE, ERROR
                candidates.append({"symbol": sym, "date": v.at[i, "date"], "kind": "split" if r_ < 1 else "merge",
                                   "implied_ratio": round(1 / k, 4), "price_ratio": round(float(r_), 4), "volume_ratio": None,
                                   "gap_sessions": g, "confidence": "high" if not adj.at[i] else "medium",
                                   "evidence": "price step equals a split/merge ratio across a suspension",
                                   "verify_with": "OpenDART list.json report_nm containing 주식분할/주식병합 near this date"})
            elif g >= 1:
                cause, rec, sev = "base_price_reset_after_suspension", REVIEW, WARNING
            elif split_like and not adj.at[i]:
                cause, rec, sev = "unadjusted_split_or_merge", EXCLUDE, ERROR
                candidates.append({"symbol": sym, "date": v.at[i, "date"], "kind": "split" if r_ < 1 else "merge",
                                   "implied_ratio": round(1 / k, 4), "price_ratio": round(float(r_), 4), "volume_ratio": None,
                                   "gap_sessions": g, "confidence": "high", "evidence": "split-like price step on a series not marked adjusted",
                                   "verify_with": "OpenDART list.json report_nm containing 주식분할/주식병합 near this date"})
            elif over_limit:
                cause, rec, sev = ("split_like_step_on_adjusted_series" if split_like else "exceeds_daily_price_limit"), EXCLUDE, ERROR
            else:
                cause, rec, sev = "large_move_within_limit", OK, INFO
            findings.append(_f(row, "price_jump", "corporate_action" if "split" in cause or "merge" in cause else "price", sev, obs, exp,
                               "a consecutive-session move beyond the daily limit cannot be a normal trade; across a suspension "
                               "the base price can be reset (listing, merger, capital reduction)", cause, "none",
                               "include_flagged", rec, "verify the date against KIS daily bars and OpenDART filings",
                               "" if rec == OK else "the return on this bar may be an artefact of adjustment"))
        # volume level breaks across a trading gap (the study excludes the 40-bar windows that contain one)
        brk = flags.str.contains("volume_level_break").to_numpy()
        vol = v["volume"].astype(float)
        bmed = vol.shift(1).rolling(cfg.volume_window, min_periods=20).median()
        amed = vol[::-1].rolling(cfg.volume_window, min_periods=20).median()[::-1]
        for i in np.flatnonzero(brk):
            vr = float(amed.iat[i] / bmed.iat[i]) if bmed.iat[i] > 0 else float("nan")
            pr = float(ratio.iat[i]) if i else float("nan")
            k, dist = _nearest_ratio(vr, cfg.split_ratios)
            matches = dist <= np.log(1 + cfg.split_volume_tolerance)
            price_continuous = np.isfinite(pr) and abs(pr - 1) <= limit[i]
            if matches and price_continuous:
                cause, conf = f"volume_not_adjusted_for_split_or_merge(x{k:g})", "medium"
            elif matches:
                cause, conf = f"split_or_merge_both_series(x{k:g})", "medium"
            else:
                cause, conf = "volume_level_shift_after_suspension(real_change_or_unknown_event)", "low"
            candidates.append({"symbol": sym, "date": v.at[i, "date"], "kind": "volume_level_break", "implied_ratio": round(k, 4) if matches else None,
                               "price_ratio": round(pr, 4) if np.isfinite(pr) else None, "volume_ratio": round(vr, 4),
                               "gap_sessions": int(gaps.iat[i]), "confidence": conf,
                               "evidence": f"{cfg.volume_window}-session median volume x{vr:.2f} across a trading gap; price ratio {pr:.3f}",
                               "verify_with": "OpenDART list.json (주식분할/주식병합/무상증자) and KIS volume around the date"})
            findings.append(_f({"symbol": sym, "date": v.at[i, "date"]}, "volume_level_break", "volume", WARNING,
                               f"median volume x{vr:.2f}, price ratio {pr:.3f}, gap {int(gaps.iat[i])} sessions",
                               f"volume level within x{cfg.volume_break_factor:g} across a gap",
                               "prices are provider-adjusted but volume may not be; volume features would see a fake surge",
                               cause, "none", "include_flagged (signals excluded for the window)", REVIEW,
                               "match the date to a filing; adjust volume only after it is confirmed",
                               "volume ratios across this date mix two share bases"))
        if brk.any():
            contam = pd.Series(brk.astype(float)).rolling(cfg.signal_window, min_periods=1).max().to_numpy() > 0
            for a, b, n in _ranges(v["date"], contam):
                exclusions.append({"symbol": sym, "start": a, "end": b, "rows": n, "scope": "signals_excluded",
                                   "reason": f"{cfg.signal_window}-bar volume window spans a volume_level_break"})
        # volume level shifts WITHOUT a trading gap (bonus issues can happen without a suspension) — info only
        if cfg.no_gap_volume_shift and len(v) >= 2 * cfg.volume_window:
            r_all = (amed / bmed).to_numpy()
            # rows whose median windows touch a trading gap are covered by the gap-based check above
            near_gap = pd.Series((gaps.to_numpy() > 0).astype(float)).rolling(2 * cfg.volume_window + 1, center=True,
                                                                             min_periods=1).max().to_numpy() > 0
            ok = np.isfinite(r_all) & ((r_all >= cfg.no_gap_shift_factor) | (r_all <= 1 / cfg.no_gap_shift_factor)) & ~near_gap
            if ok.any():
                lr = np.abs(np.log(np.where(np.isfinite(r_all) & (r_all > 0), r_all, 1)))
                for a_, b_ in zip(*[np.flatnonzero(np.diff(np.r_[0, ok.astype(int), 0]))[j::2] for j in (0, 1)]):
                    i = int(a_ + np.argmax(lr[a_:b_]))         # strongest point of each run
                    vr = float(r_all[i])
                    k, dist = _nearest_ratio(vr, cfg.split_ratios)
                    near = dist <= np.log(1 + cfg.split_volume_tolerance)
                    findings.append(_f({"symbol": sym, "date": v.at[i, "date"]}, "volume_level_shift_no_gap", "volume", INFO,
                                       f"median volume x{vr:.2f} with no trading gap", f"volume level within x{cfg.no_gap_shift_factor:g}",
                                       "usually a real change in trading interest; a ratio equal to a split ratio may be a bonus issue",
                                       f"possible_bonus_issue(x{k:g})" if near else "market_volume_change", "none", "include",
                                       REVIEW if near else OK, "check OpenDART for 무상증자 near the date" if near else "none"))
                    if near:
                        candidates.append({"symbol": sym, "date": v.at[i, "date"], "kind": "bonus_issue_suspected", "implied_ratio": round(k, 4),
                                           "price_ratio": None, "volume_ratio": round(vr, 4), "gap_sessions": 0, "confidence": "low",
                                           "evidence": "volume level shift equal to a split ratio without a suspension",
                                           "verify_with": "OpenDART list.json report_nm containing 무상증자"})

    # 4. listing window (identifier ↔ listing period)
    inst = instrument or {}
    if len(valid):
        first, last = valid["date"].min(), valid["date"].max()
        ld = pd.to_datetime(inst.get("listed_date"), errors="coerce") if inst.get("listed_date") else pd.NaT
        dd = pd.to_datetime(inst.get("delisted_date"), errors="coerce") if inst.get("delisted_date") else pd.NaT
        if pd.notna(ld) and first < ld:
            n = int((valid["date"] < ld).sum())
            findings.append(_f({"symbol": sym, "date": first}, "bars_before_listing", "identifier", WARNING,
                               f"{n} bars before listed_date {ld.date()}", "no bars before listing",
                               "the code may have belonged to another company, or the listing date is wrong", "code_reuse_or_wrong_listing_date",
                               "none", "include_flagged", REVIEW, "confirm the code's history in the KRX listing records"))
        if pd.notna(dd):
            n = int((valid["date"] > dd).sum())
            if n:
                findings.append(_f({"symbol": sym, "date": dd}, "bars_after_delisting", "identifier", ERROR,
                                   f"{n} bars after delisted_date {dd.date()}", "no bars after delisting",
                                   "another company may now use the code (code reuse)", "code_reuse",
                                   "none", "include_flagged", EXCLUDE, "split the series at the delisting date"))
            gap = (dd - last).days
            if gap > cfg.delisting_gap_days:
                findings.append(_f({"symbol": sym, "date": last}, "data_ends_before_delisting", "identifier", WARNING,
                                   f"last bar {last.date()}, delisted {dd.date()} ({gap} days later)", f"<= {cfg.delisting_gap_days} days",
                                   "a long suspension before delisting: the final (often near-zero) liquidation price is missing, "
                                   "so a backtest closes at a stale price and understates the loss", "suspension_before_delisting",
                                   "none", "include_flagged", REVIEW, "find the liquidation-trading prices (정리매매) from a second source"))
    elif inst.get("status") == "delisted":
        findings.append(_f({"symbol": sym, "date": None}, "no_bars_for_delisted", "identifier", WARNING, "0 usable bars",
                           "bars until the delisting date", "survivorship: this delisted company is missing from the sample",
                           "missing_history", "none", "exclude", REVIEW, "collect from a second source"))

    # 5. rows removed by the quality checks, as contiguous ranges (in date order) per reason
    if len(rep.rejected):
        reasons: dict[str, str] = {}
        for iss in rep.issues:
            if iss["severity"] == ERROR and iss.get("date"):
                reasons.setdefault(iss["date"], iss["check"])
        rj = rep.rejected
        rj_reason = rj["date"].dt.strftime("%Y-%m-%d").map(reasons).fillna("duplicate_identical")
        for reason in sorted(rj_reason.unique()):
            mask = np.zeros(len(usable_in), dtype=bool)
            mask[rj.index[rj_reason == reason].to_numpy()] = True
            for a, b, n in _ranges(usable_in["date"], mask):
                exclusions.append({"symbol": sym, "start": a, "end": b, "rows": n, "scope": "row_removed", "reason": reason})
    if bad_date.any():
        exclusions.append({"symbol": sym, "start": pd.NaT, "end": pd.NaT, "rows": int(bad_date.sum()), "scope": "row_removed",
                           "reason": "unparsable_date"})
    return {"findings": findings, "corrections": corrections, "exclusions": exclusions, "candidates": candidates,
            "usable": valid, "rows": len(raw)}


# ------------------------------------------------------------------------------------------------ whole dataset

def audit_frame(bars: pd.DataFrame, cfg: AuditConfig | None = None, calendar: pd.DatetimeIndex | None = None,
                instruments: pd.DataFrame | None = None) -> AuditResult:
    """Audit every instrument in `bars` (raw rows, halt markers included). Instruments without bars are reported too."""
    cfg = cfg or AuditConfig()
    inst = {}
    if instruments is not None and len(instruments) and "symbol" in instruments:
        inst = {str(r["symbol"]): r for r in instruments.to_dict("records")}
    parts: dict[str, list] = {"findings": [], "corrections": [], "exclusions": [], "candidates": []}
    usable, per_sym = [], []
    seen = set()
    groups = bars.groupby(bars["symbol"].astype(str), sort=True) if len(bars) else []
    for sym, raw in groups:
        seen.add(sym)
        res = audit_symbol(raw, cfg, calendar, inst.get(sym, {"symbol": sym}) if inst else None)
        for k in parts:
            parts[k].extend(res[k])
        usable.append(res["usable"])
        per_sym.append((sym, res))
    for sym, info in inst.items():
        if sym not in seen:
            res = audit_symbol(pd.DataFrame({"symbol": [sym]}).iloc[0:0].assign(**{c: [] for c in REQUIRED}), cfg, calendar, info)
            for k in parts:
                parts[k].extend(res[k])
            per_sym.append((sym, res))
    F = pd.DataFrame(parts["findings"], columns=FINDING_COLS)
    C = pd.DataFrame(parts["corrections"], columns=CORRECTION_COLS)
    X = pd.DataFrame(parts["exclusions"], columns=EXCLUSION_COLS)
    K = pd.DataFrame(parts["candidates"], columns=CANDIDATE_COLS)
    for df_, col in ((F, "date"), (C, "date"), (K, "date"), (X, "start"), (X, "end")):
        df_[col] = pd.to_datetime(df_[col])
    F = F.sort_values(["symbol", "date", "check"], na_position="first", kind="stable").reset_index(drop=True)
    U = pd.concat(usable, ignore_index=True) if usable else pd.DataFrame()
    S = symbol_table(per_sym, F, X, K, inst)
    summary = summarize(bars, F, C, X, K, S, U, cfg)
    return AuditResult(F, C, X, K, S, U, summary)


def symbol_table(per_sym, F: pd.DataFrame, X: pd.DataFrame, K: pd.DataFrame, inst: dict) -> pd.DataFrame:
    recs = []
    fg = dict(tuple(F.groupby("symbol"))) if len(F) else {}
    xg = dict(tuple(X.groupby("symbol"))) if len(X) else {}
    kg = K["symbol"].value_counts().to_dict() if len(K) else {}
    for sym, res in per_sym:
        f = fg.get(sym, F.iloc[0:0])
        x = xg.get(sym, X.iloc[0:0])
        u = res["usable"]
        n_err = int((f["severity"] == ERROR).sum())
        rec = {"symbol": sym, "status_listing": inst.get(sym, {}).get("status") if inst else None,
               "rows_raw": int(res["rows"]), "rows_usable": int(len(u)),
               "first_date": u["date"].min() if len(u) else pd.NaT, "last_date": u["date"].max() if len(u) else pd.NaT,
               "rows_removed": int(x.loc[x["scope"] == "row_removed", "rows"].sum()),
               "rows_signal_excluded": int(x.loc[x["scope"] == "signals_excluded", "rows"].sum()),
               "errors": n_err, "warnings": int((f["severity"] == WARNING).sum()), "infos": int((f["severity"] == INFO).sum()),
               "rows_repaired": int((f["check"] == "ohlc_rounding_repaired").sum()),
               "corporate_action_candidates": int(kg.get(sym, 0)),
               "needs_review": int((f["recommendation"] == REVIEW).sum()),
               "exclude_until_verified": int((f["recommendation"] == EXCLUDE).sum())}
        removed_err = int((f["auto_fix"].eq("removed") & f["severity"].eq(ERROR)).sum())
        if rec["rows_usable"] == 0 or (rec["rows_raw"] and removed_err / max(rec["rows_raw"], 1) > 0.05):
            status = "exclude"
        elif rec["exclude_until_verified"] or rec["needs_review"]:
            status = "review"
        else:
            status = "ok"
        rec["audit_status"] = status
        recs.append(rec)
    return pd.DataFrame(recs)


def _hash_frame(df: pd.DataFrame) -> str | None:
    if df is None or df.empty:
        return None
    h = hashlib.sha256(pd.util.hash_pandas_object(df.astype(str), index=False).values.tobytes())
    return h.hexdigest()[:16]


def bars_fingerprint(bars: pd.DataFrame) -> str | None:
    """Dtype-independent hash of (symbol, date, OHLCV): the same rows give the same value in the audit and the study."""
    if bars is None or bars.empty:
        return None
    b = pd.DataFrame({"symbol": bars["symbol"].astype(str), "date": pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")})
    for c in ("open", "high", "low", "close", "volume"):
        b[c] = pd.to_numeric(bars[c], errors="coerce").astype(float).round(6)
    b = b.sort_values(["symbol", "date"], kind="stable")
    return hashlib.sha256(pd.util.hash_pandas_object(b, index=False).values.tobytes()).hexdigest()[:16]


def summarize(bars, F, C, X, K, S, U, cfg: AuditConfig) -> dict:
    by_check = []
    if len(F):
        g = F.groupby(["check", "category", "severity"])
        agg = g.agg(rows=("symbol", "size"), symbols=("symbol", "nunique")).reset_index()
        top = F.groupby(["check", "symbol"]).size().reset_index(name="n").sort_values(["check", "n"], ascending=[True, False])
        top = top.drop_duplicates("check").set_index("check")
        for r in agg.itertuples(index=False):
            t = top.loc[r.check]
            by_check.append({"check": r.check, "category": r.category, "severity": r.severity, "rows": int(r.rows),
                             "symbols": int(r.symbols), "top_symbol": t["symbol"], "top_symbol_rows": int(t["n"]),
                             "top_symbol_share": round(float(t["n"]) / max(int(agg.loc[agg["check"] == r.check, "rows"].sum()), 1), 4)})
    raw_cols = [c for c in ("symbol", "date", "open", "high", "low", "close", "volume") if c in bars]
    rec = F["recommendation"].value_counts().to_dict() if len(F) else {}
    return {
        "rows_raw": int(len(bars)), "rows_usable": int(len(U)), "symbols_with_bars": int(bars["symbol"].nunique()) if len(bars) else 0,
        "symbols_total": int(len(S)), "audit_status": S["audit_status"].value_counts().to_dict() if len(S) else {},
        "findings": int(len(F)), "by_severity": F["severity"].value_counts().to_dict() if len(F) else {},
        "by_recommendation": rec, "by_check": by_check,
        "rows_removed": int(X.loc[X["scope"] == "row_removed", "rows"].sum()) if len(X) else 0,
        "rows_signal_excluded": int(X.loc[X["scope"] == "signals_excluded", "rows"].sum()) if len(X) else 0,
        "corrections": int(len(C)), "rows_repaired": int(F["check"].eq("ohlc_rounding_repaired").sum()) if len(F) else 0,
        "corporate_action_candidates": K["confidence"].value_counts().to_dict() if len(K) else {},
        "config": asdict(cfg),
        "input_fingerprint": _hash_frame(bars[raw_cols].sort_values(["symbol", "date"], kind="stable")) if len(bars) else None,
        "findings_fingerprint": _hash_frame(F),
        "usable_fingerprint": bars_fingerprint(U),
    }


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
