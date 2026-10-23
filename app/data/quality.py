"""Data-quality checks for daily OHLCV bars.

Errors make a row unusable (it is rejected and never written). Warnings keep the row but attach a
flag to it (`quality_flags`), so a backtest can report which flagged bars it used. Nothing is
silently "repaired": prices are never interpolated and missing values are never replaced by 0.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

ERROR = "error"
WARNING = "warning"

# KRX daily price limit is +/-30%; a larger close-to-close move on an unadjusted series usually
# means a corporate action (split/merger) or a bad record. Configurable per call.
DEFAULT_JUMP_THRESHOLD = 0.30
DEFAULT_GAP_BUSINESS_DAYS = 5
_SPLIT_RATIOS = (2, 3, 4, 5, 10, 20, 25, 50)


@dataclass
class QualityReport:
    valid: pd.DataFrame
    rejected: pd.DataFrame
    issues: list[dict] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i["severity"] == ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i["severity"] == WARNING)

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue["check"]] = counts.get(issue["check"], 0) + 1
        return {"rows_valid": int(len(self.valid)), "rows_rejected": int(len(self.rejected)),
                "errors": self.error_count, "warnings": self.warning_count, "by_check": counts}


def _issue(row: pd.Series | dict, check: str, severity: str, detail: str) -> dict:
    date = row.get("date")
    return {"market": row.get("market"), "symbol": row.get("symbol"),
            "date": pd.Timestamp(date).strftime("%Y-%m-%d") if date is not None and not pd.isna(date) else None,
            "check": check, "severity": severity, "detail": detail}


def check_frame(df: pd.DataFrame, jump_threshold: float = DEFAULT_JUMP_THRESHOLD,
                gap_business_days: int = DEFAULT_GAP_BUSINESS_DAYS, calendar: pd.DatetimeIndex | None = None,
                detect_volume_breaks: bool = True, rounding_tolerance: float = 0.002) -> QualityReport:
    """Run row-level and series-level checks. `df` needs market, symbol, date, OHLC, volume."""
    if df.empty:
        return QualityReport(df.copy(), df.copy(), [])
    work = df.copy().reset_index(drop=True)
    issues: list[dict] = []
    reject = pd.Series(False, index=work.index)
    flags: dict[int, list[str]] = {}

    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        work[col] = pd.to_numeric(work[col], errors="coerce") if col in work else np.nan
    if "turnover" in work:
        work["turnover"] = pd.to_numeric(work["turnover"], errors="coerce")

    # 1. missing values / unparsable dates
    missing = work[["date", "open", "high", "low", "close", "volume"]].isna().any(axis=1)
    for idx in work.index[missing]:
        cols = [c for c in ["date", "open", "high", "low", "close", "volume"] if pd.isna(work.at[idx, c])]
        issues.append(_issue(work.loc[idx], "missing_value", ERROR, f"missing or unparsable: {', '.join(cols)}"))
    reject |= missing

    # 2. non-positive prices
    nonpos = (~missing) & (work[["open", "high", "low", "close"]] <= 0).any(axis=1)
    for idx in work.index[nonpos]:
        issues.append(_issue(work.loc[idx], "non_positive_price", ERROR, "OHLC must be > 0"))
    reject |= nonpos

    # 3. negative volume
    negvol = (~missing) & (work["volume"] < 0)
    for idx in work.index[negvol]:
        issues.append(_issue(work.loc[idx], "negative_volume", ERROR, f"volume={work.at[idx, 'volume']}"))
    reject |= negvol

    # 4. inconsistent OHLC (high below low, or open/close outside the high-low range)
    ok = ~missing
    inverted = ok & ~nonpos & ((work["high"] < work["low"]) | (work["high"] < work[["open", "close"]].max(axis=1))
                     | (work["low"] > work[["open", "close"]].min(axis=1)))
    # Adjusted series round each price separately, so close can exceed high by a unit (e.g. C=32160, H=32159).
    # Within `rounding_tolerance` on an ADJUSTED row this is repaired by widening high/low, and the row is flagged —
    # never silently. Anything larger stays an error.
    if rounding_tolerance > 0 and "adjusted" in work and inverted.any():
        adj = work["adjusted"].map(lambda v: v is True or v == 1)
        hi_need = work[["open", "close", "low"]].max(axis=1)
        lo_need = work[["open", "close", "high"]].min(axis=1)
        small = ((hi_need - work["high"]).clip(lower=0) <= work["high"].abs() * rounding_tolerance) & \
                ((work["low"] - lo_need).clip(lower=0) <= work["low"].abs() * rounding_tolerance)
        fix = inverted & adj & small & (work["high"] >= work["low"] * (1 - rounding_tolerance))
        if fix.any():
            work.loc[fix, "high"] = np.maximum(work.loc[fix, "high"], hi_need[fix])
            work.loc[fix, "low"] = np.minimum(work.loc[fix, "low"], lo_need[fix])
            if "quality_flags" not in work:
                work["quality_flags"] = ""
            qf = work["quality_flags"].fillna("").astype(str)
            work.loc[fix, "quality_flags"] = [",".join(filter(None, [f, "ohlc_rounding_repaired"])) for f in qf[fix]]
            for idx in work.index[fix]:
                issues.append(_issue(work.loc[idx], "ohlc_rounding_repaired", WARNING,
                                     f"adjusted prices off by rounding; high/low widened to H={work.at[idx, 'high']} L={work.at[idx, 'low']}"))
            inverted = inverted & ~fix
    for idx in work.index[inverted]:
        r = work.loc[idx]
        issues.append(_issue(r, "ohlc_inconsistent", ERROR, f"O={r['open']} H={r['high']} L={r['low']} C={r['close']}"))
    reject |= inverted

    # 5. duplicates: identical copies -> keep one (warning); conflicting copies -> reject all (error)
    key = ["market", "symbol", "date"]
    dup_mask = work.duplicated(subset=key, keep=False) & ~work["date"].isna()
    if dup_mask.any():
        value_cols = ["open", "high", "low", "close", "volume"]
        for _, grp in work[dup_mask].groupby(key):
            if len(grp[value_cols].drop_duplicates()) == 1:
                for idx in grp.index[1:]:
                    reject.at[idx] = True
                issues.append(_issue(grp.iloc[0], "duplicate_identical", WARNING, f"{len(grp)} identical rows; one kept"))
            else:
                reject.loc[grp.index] = True
                issues.append(_issue(grp.iloc[0], "duplicate_conflict", ERROR, f"{len(grp)} rows with different values; all rejected"))

    valid = work[~reject].copy()
    rejected = work[reject].copy()

    # Series-level warnings on the rows that survive (vectorised: millions of rows are expected).
    if not valid.empty:
        valid = valid.sort_values(key).reset_index(drop=True)
        group_cols = [c for c in ["market", "symbol"] if c in valid]
        g = valid.groupby(group_cols, sort=False) if group_cols else None
        first = (valid.index == 0) if g is None else (g.cumcount() == 0).to_numpy()
        prev_close = valid["close"].shift(1) if g is None else g["close"].shift(1)
        prev_date = valid["date"].shift(1) if g is None else g["date"].shift(1)
        change = valid["close"] / prev_close - 1
        adjusted = valid["adjusted"] if "adjusted" in valid else pd.Series([None] * len(valid), index=valid.index)
        adj_true = adjusted.map(lambda v: v is True or v == 1 or v == 1.0)
        ratio = valid["close"] / prev_close
        split_like = pd.Series(False, index=valid.index)
        for k in _SPLIT_RATIOS:
            split_like |= ((ratio * k - 1).abs() < 0.03) | ((ratio / k - 1).abs() < 0.03)
        jump = (~pd.Series(first, index=valid.index)) & change.notna() & (change.abs() > jump_threshold)
        corp = jump & split_like & ~adj_true
        pjump = jump & ~corp
        zero = valid["volume"] == 0
        gaps = missing_sessions(valid["date"], prev_date, calendar)
        gap_flag = (~pd.Series(first, index=valid.index)) & (gaps >= gap_business_days)
        unit = "trading days (market calendar)" if calendar is not None else "business days (holidays included)"
        rules = [(zero, "zero_volume", lambda i: "volume is 0 (halt or missing data)"),
                 (corp, "possible_corporate_action", lambda i: f"close ratio {ratio.at[i]:.3f} looks like a split/merge on a series not marked as adjusted"),
                 (pjump, "price_jump", lambda i: f"close-to-close change {change.at[i]:+.1%} exceeds {jump_threshold:.0%}"),
                 (gap_flag, "missing_trading_days", lambda i: f"{int(gaps.at[i])} {unit} without a bar before this date")]
        for mask, name, detail in rules:
            for idx in valid.index[mask.to_numpy(dtype=bool)]:
                flags.setdefault(idx, []).append(name)
                issues.append(_issue(valid.loc[idx], name, WARNING, detail(idx)))
        if detect_volume_breaks:
            vb = volume_level_breaks(valid, gaps, group_cols)
            for idx, r in vb.items():
                flags.setdefault(idx, []).append("volume_level_break")
                issues.append(_issue(valid.loc[idx], "volume_level_break", WARNING,
                                     f"median volume changed x{r:.2f} across a trading gap — volume is probably not adjusted for a split/merge"))
        existing = valid["quality_flags"].fillna("") if "quality_flags" in valid else pd.Series("", index=valid.index)
        if flags:
            valid["quality_flags"] = [",".join(filter(None, [existing.at[i]] + flags.get(i, []))) for i in valid.index]
        else:
            valid["quality_flags"] = existing
    return QualityReport(valid=valid, rejected=rejected, issues=issues)


def missing_sessions(dates: pd.Series, prev_dates: pd.Series, calendar: pd.DatetimeIndex | None = None) -> pd.Series:
    """Number of sessions strictly between the previous bar and this bar (0 = consecutive)."""
    out = pd.Series(0, index=dates.index, dtype="int64")
    ok = prev_dates.notna()
    if not ok.any():
        return out
    d = pd.to_datetime(dates[ok]).to_numpy(dtype="datetime64[D]")
    p = pd.to_datetime(prev_dates[ok]).to_numpy(dtype="datetime64[D]")
    if calendar is not None:
        cal = np.asarray(pd.DatetimeIndex(calendar).normalize().unique().sort_values(), dtype="datetime64[D]")
        n = np.searchsorted(cal, d, side="left") - np.searchsorted(cal, p, side="right")
    else:
        n = np.busday_count(p, d) - 1
    out[ok] = np.maximum(n, 0)
    return out


def volume_level_breaks(valid: pd.DataFrame, gaps: pd.Series, group_cols: list[str], factor: float = 3.0,
                        window: int = 60, min_gap: int = 1) -> dict[int, float]:
    """Rows right after a trading gap where the typical volume level jumps by >= factor (or drops to <= 1/factor).

    Some providers adjust PRICES for splits/merges but not VOLUME; across such an event the 20-session volume
    ratio becomes meaningless. The test is deliberately narrow (needs a gap, as Korean splits/merges suspend
    trading) — bonus issues without a suspension are NOT detected.
    """
    out: dict[int, float] = {}
    cand = gaps >= min_gap
    if not cand.any():
        return out
    groups = valid.groupby(group_cols, sort=False) if group_cols else [(None, valid)]
    for _, grp in groups:
        idx = grp.index[cand.loc[grp.index].to_numpy()]
        if not len(idx):
            continue
        v = grp["volume"].astype(float)
        before = v.shift(1).rolling(window, min_periods=20).median()
        after = v[::-1].rolling(window, min_periods=20).median()[::-1]
        for i in idx:
            b, a = before.at[i], after.at[i]
            if pd.notna(b) and pd.notna(a) and b > 0 and a > 0:
                r = a / b
                if r >= factor or r <= 1 / factor:
                    out[int(i)] = float(r)
    return out


def flagged_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if "quality_flags" not in frame:
        return frame.iloc[0:0]
    return frame[frame["quality_flags"].fillna("") != ""]
