"""Factor Lab MVP: cross-sectional factor evaluation on daily bars with explicit timing rules.

Timing (no look-ahead):
  * factor value at date t uses bars dated <= t only (close, volume of t and earlier);
  * the position is entered at the OPEN of the next trading day t+1 and exited at the OPEN of t+1+h
    (h = horizon trading days on the market calendar), so forward return = open[t+1+h] / open[t+1] - 1;
  * eligibility (turnover, price) is decided with data <= t;
  * everything is placed on the market calendar (KOSPI index dates). A missing bar inside a factor window, at entry
    or at exit (halt, data gap, delisting) makes that value NaN; such rows are counted, never filled.
  * rebalance dates are every h-th calendar day, so holding windows do not overlap.

What is measured per rebalance date: Pearson IC, Spearman rank IC, quantile mean forward returns (equal weight),
top-minus-bottom spread, one-way turnover of the top and bottom legs, costs, and two benchmarks:
  * `ew_universe` — equal-weight mean forward return of all names used that date, same entry/exit timing (primary);
  * `kospi_cc` — KOSPI close[exit day] / close[entry day] - 1; close-to-close, so the timing differs from the
    open-to-open strategy return by up to one overnight gap (secondary, reported as such).

Costs (StudyConfig.costs, per side): buying costs commission + slippage, selling costs commission + slippage + sell
tax. A leg pays turnover × (buy + sell) at each rebalance, buy-side on formation (first date of a period) and
sell-side on liquidation (last date of a period). Short borrow fees and short-sale restrictions are NOT modeled;
the long-short spread is a research diagnostic, not an investable return.

Not handled: dividends (price return only), corporate actions beyond the loader's flags, survivorship of names whose
data stops before exit (dropped from that date and counted as `forward_missing`), market-impact beyond flat slippage.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from app.research.study import StudyConfig

FACTOR_KINDS = ("momentum", "reversal", "volatility", "log_turnover", "turnover_ratio", "amihud")
VOLUME_KINDS = {"log_turnover", "turnover_ratio", "amihud"}


def default_factors() -> dict[str, dict]:
    return {
        "mom_20": {"kind": "momentum", "lookback": 20, "skip": 0},
        "mom_60": {"kind": "momentum", "lookback": 60, "skip": 0},
        "mom_120": {"kind": "momentum", "lookback": 120, "skip": 0},
        "rev_5": {"kind": "reversal", "lookback": 5},
        "vol_20": {"kind": "volatility", "window": 20},
        "turnover_20": {"kind": "log_turnover", "window": 20},
        "turnover_ratio_5_60": {"kind": "turnover_ratio", "short": 5, "long": 60},
        "illiq_20": {"kind": "amihud", "window": 20},
    }


@dataclass
class FactorLabConfig:
    factors: dict[str, dict] = field(default_factory=default_factors)
    horizon: int = 20                          # trading days held (entry open t+1 -> exit open t+1+h)
    quantiles: int = 5
    min_cross_section: int = 30                # names with factor and forward return needed on a date
    min_avg_turnover: float = StudyConfig().min_avg_turnover
    min_price: float = StudyConfig().min_price
    periods: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(StudyConfig().periods))
    embargo_days: int = StudyConfig().embargo_days
    costs: dict = field(default_factory=lambda: dict(StudyConfig().costs))
    audit_dir: str | None = None
    audit_lookback_bars: int = StudyConfig().audit_lookback_bars

    def validate(self) -> None:
        if self.horizon < 1 or self.quantiles < 2 or self.min_cross_section < self.quantiles:
            raise ValueError("horizon >= 1, quantiles >= 2 and min_cross_section >= quantiles are required")
        need = int(np.ceil((self.horizon + 1) * 7 / 5))
        if self.embargo_days < need:
            raise ValueError(f"embargo_days={self.embargo_days} < {need}: a {self.horizon}-day hold could end in the next period")
        StudyConfig(periods=self.periods, embargo_days=max(self.embargo_days, 35)).validate()   # period order/overlap rules
        for name, spec in self.factors.items():
            if spec.get("kind") not in FACTOR_KINDS:
                raise ValueError(f"factor {name}: kind must be one of {FACTOR_KINDS}")
            for k in ("lookback", "window", "short", "long"):
                if k in spec and not (isinstance(spec[k], int) and spec[k] >= 1):
                    raise ValueError(f"factor {name}: {k} must be a positive integer")
            if spec.get("skip", 0) < 0:
                raise ValueError(f"factor {name}: skip must be >= 0")
            if spec["kind"] == "turnover_ratio" and spec["short"] >= spec["long"]:
                raise ValueError(f"factor {name}: short must be < long")

    def buy_rate(self) -> float:
        return (self.costs["commission_bps"] + self.costs["slippage_bps"]) / 1e4

    def sell_rate(self) -> float:
        return (self.costs["commission_bps"] + self.costs["slippage_bps"] + self.costs["sell_tax_bps"]) / 1e4

    def factor_window(self, name: str) -> int:
        s = self.factors[name]
        return int(s.get("lookback", 0) + s.get("skip", 0) or s.get("window") or s.get("long"))


# ------------------------------------------------------------------------------------------------ preparation

@dataclass
class Panel:
    calendar: pd.DatetimeIndex
    open: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    turnover: pd.DataFrame
    level_break: pd.DataFrame                  # bool: provider flagged a volume level break on that bar
    untrusted: pd.DataFrame                    # bool: audit says exclude_until_verified on that bar
    counts: dict


def build_panel(bars: pd.DataFrame, calendar: pd.DatetimeIndex, audit: dict | None = None) -> Panel:
    """Dates x symbols matrices on the market calendar. Counts every row that is dropped and why."""
    b = bars.copy()
    b["date"] = pd.to_datetime(b["date"])
    counts = {"rows_in": int(len(b))}
    exact = b.duplicated(["symbol", "date", "open", "close", "volume"])
    b = b[~exact]
    conflict = b.duplicated(["symbol", "date"], keep=False)
    counts.update(duplicate_rows_identical_dropped=int(exact.sum()), duplicate_rows_conflicting_dropped=int(conflict.sum()))
    b = b[~conflict]
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(calendar).unique()))
    off = ~b["date"].isin(cal)
    counts["off_calendar_rows_dropped"] = int(off.sum())
    b = b[~off]
    bad = ~((b["open"] > 0) & (b["close"] > 0) & (b["volume"] >= 0))
    counts["nonpositive_price_rows_dropped"] = int(bad.sum())
    b = b[~bad]
    if audit:
        drop = b["symbol"].isin(audit.get("drop", set()))
        counts["audit_symbols_dropped"] = int(b.loc[drop, "symbol"].nunique())
        b = b[~drop]
    counts["rows_used"] = int(len(b))
    if "turnover" not in b or b["turnover"].isna().all():
        b["turnover"] = b["close"] * b["volume"]
    else:
        b["turnover"] = pd.to_numeric(b["turnover"], errors="coerce").fillna(b["close"] * b["volume"])
    flags = b["quality_flags"].fillna("").astype(str) if "quality_flags" in b else pd.Series("", index=b.index)
    b["level_break"] = flags.str.contains("volume_level_break")
    b["untrusted"] = False
    if audit and audit.get("dates"):
        for s, dates in audit["dates"].items():
            m = (b["symbol"] == s) & b["date"].isin(dates)
            b.loc[m, "untrusted"] = True

    def wide(col, fill=None):
        w = b.pivot(index="date", columns="symbol", values=col).reindex(cal)
        return w.eq(True) if fill is not None else w.astype(float)   # missing bar -> False / NaN

    return Panel(cal, wide("open"), wide("close"), wide("volume"), wide("turnover"), wide("level_break", False),
                 wide("untrusted", False), counts)


def compute_factor(p: Panel, spec: dict) -> pd.DataFrame:
    """Factor values at each date t from bars <= t. Any missing bar in the window gives NaN (min_periods = window)."""
    k, c = spec["kind"], p.close
    if k == "momentum":
        n, s = spec["lookback"], spec.get("skip", 0)
        return c.shift(s) / c.shift(s + n) - 1
    if k == "reversal":
        return -(c / c.shift(spec["lookback"]) - 1)
    if k == "volatility":
        w = spec["window"]
        return np.log(c / c.shift(1)).rolling(w, min_periods=w).std()
    if k == "log_turnover":
        w = spec["window"]
        return np.log(p.turnover.rolling(w, min_periods=w).mean().where(lambda x: x > 0))
    if k == "turnover_ratio":
        a, b = spec["short"], spec["long"]
        return p.turnover.rolling(a, min_periods=a).mean() / p.turnover.rolling(b, min_periods=b).mean().where(lambda x: x > 0)
    if k == "amihud":
        w = spec["window"]
        r = (c / c.shift(1) - 1).abs()
        return (r / p.turnover.where(p.turnover > 0) * 1e9).rolling(w, min_periods=w).mean()   # |ret| per KRW 1bn traded
    raise ValueError(f"unknown factor kind {k}")


def forward_returns(p: Panel, h: int) -> pd.DataFrame:
    """open[t+1+h] / open[t+1] - 1 on the calendar; NaN when the entry or exit bar is missing."""
    return p.open.shift(-(1 + h)) / p.open.shift(-1) - 1


def _window_any(mask: pd.DataFrame, w: int) -> pd.DataFrame:
    return mask.astype(float).rolling(w, min_periods=1).max().fillna(0) > 0


def _forward_any(mask: pd.DataFrame, h: int) -> pd.DataFrame:
    """True at t when the mask is set on any bar from t+1 to t+1+h (the holding window)."""
    rev = mask.iloc[::-1].astype(float).rolling(h + 1, min_periods=1).max().iloc[::-1]
    return rev.shift(-1).fillna(0) > 0


# ------------------------------------------------------------------------------------------------ evaluation

def period_labels(dates: pd.DatetimeIndex, exit_dates: pd.DatetimeIndex, cfg: FactorLabConfig) -> pd.Series:
    """Period of each rebalance date: signal inside [start, end - embargo] AND exit on or before the period end."""
    out = pd.Series("", index=dates, dtype=object)
    for name, (a, b) in cfg.periods.items():
        a, b = pd.Timestamp(a), pd.Timestamp(b)
        inside = (dates >= a) & (dates <= b - pd.Timedelta(cfg.embargo_days, unit="D")) & (exit_dates <= b)
        out[inside] = name
    return out


def _ranks_corr(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    ic = float(np.corrcoef(x, y)[0, 1]) if np.std(x) > 0 and np.std(y) > 0 else np.nan
    rx, ry = pd.Series(x).rank().to_numpy(), pd.Series(y).rank().to_numpy()
    ric = float(np.corrcoef(rx, ry)[0, 1]) if np.std(rx) > 0 and np.std(ry) > 0 else np.nan
    return ic, ric


def _one_way_turnover(prev: set | None, cur: set) -> float:
    """0.5 * sum |w_new - w_old| for equal-weight legs (drift between rebalances ignored)."""
    if prev is None:
        return 1.0
    if not cur and not prev:
        return 0.0
    w_old = {s: 1 / len(prev) for s in prev} if prev else {}
    w_new = {s: 1 / len(cur) for s in cur} if cur else {}
    return 0.5 * sum(abs(w_new.get(s, 0) - w_old.get(s, 0)) for s in set(w_old) | set(w_new))


def evaluate_factor(name: str, p: Panel, cfg: FactorLabConfig, fwd: pd.DataFrame, kospi: pd.Series | None = None
                    ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Per-date table, per-date quantile table and exclusion counts for one factor."""
    spec, h, q = cfg.factors[name], cfg.horizon, cfg.quantiles
    f = compute_factor(p, spec)
    w = cfg.factor_window(name)
    avg_to = p.turnover.rolling(20, min_periods=20).mean()
    base = (avg_to >= cfg.min_avg_turnover) & (p.close >= cfg.min_price)            # known at t
    contam = _window_any(p.level_break, w + 1) if spec["kind"] in VOLUME_KINDS else pd.DataFrame(False, index=f.index, columns=f.columns)
    untrusted = _window_any(p.untrusted, max(w + 1, cfg.audit_lookback_bars)) | _forward_any(p.untrusted, h)
    cal = p.calendar
    pos = np.arange(0, len(cal) - (h + 1), h)                                          # every h-th day with an exit day
    dates, exits, entries = cal[pos], cal[pos + 1 + h], cal[pos + 1]
    labels = period_labels(dates, exits, cfg)
    rows, qrows = [], []
    cnt = {"dates_total": int(len(dates)), "dates_outside_periods": int((labels == "").sum()), "dates_too_small": 0,
           "names_eligible": 0, "names_factor_missing": 0, "names_volume_contaminated": 0, "names_audit_excluded": 0,
           "names_forward_missing": 0, "names_used": 0}
    prev_top: dict[str, set | None] = {}
    prev_bot: dict[str, set | None] = {}
    for d, entry, ex, per in zip(dates, entries, exits, labels):
        if not per:
            continue
        el = base.loc[d].fillna(False).to_numpy(bool)
        fv, rv = f.loc[d].to_numpy(float), fwd.loc[d].to_numpy(float)
        ct, ut = contam.loc[d].to_numpy(bool), untrusted.loc[d].to_numpy(bool)
        n_el = int(el.sum())
        m_f = el & ~np.isfinite(fv)
        m_c = el & np.isfinite(fv) & ct
        m_u = el & np.isfinite(fv) & ~ct & ut
        ok_pre = el & np.isfinite(fv) & ~ct & ~ut
        m_r = ok_pre & ~np.isfinite(rv)
        use = ok_pre & np.isfinite(rv)
        cnt["names_eligible"] += n_el; cnt["names_factor_missing"] += int(m_f.sum()); cnt["names_volume_contaminated"] += int(m_c.sum())
        cnt["names_audit_excluded"] += int(m_u.sum()); cnt["names_forward_missing"] += int(m_r.sum())
        n = int(use.sum())
        if n < cfg.min_cross_section:
            cnt["dates_too_small"] += 1
            continue
        cnt["names_used"] += n
        syms = f.columns.to_numpy()[use]
        x, y = fv[use], rv[use]
        ic, ric = _ranks_corr(x, y)
        bucket = pd.qcut(pd.Series(x).rank(method="first"), q, labels=False).to_numpy() + 1
        qmeans = [float(y[bucket == k].mean()) for k in range(1, q + 1)]
        top, bot = set(syms[bucket == q]), set(syms[bucket == 1])
        tt, tb = _one_way_turnover(prev_top.get(per), top), _one_way_turnover(prev_bot.get(per), bot)
        first = prev_top.get(per) is None
        prev_top[per], prev_bot[per] = top, bot
        br, sr = cfg.buy_rate(), cfg.sell_rate()
        # formation pays buy side on 100%; later rebalances pay (buy + sell) on the changed fraction.
        cost_top = br if first else tt * (br + sr)
        cost_bot = sr if first else tb * (br + sr)         # short leg opens with a sale
        ew = float(y.mean())
        k_ret = float(kospi.get(ex) / kospi.get(entry) - 1) if kospi is not None and entry in kospi.index and ex in kospi.index else np.nan
        rows.append({"factor": name, "date": d, "entry_date": entry, "exit_date": ex, "period": per, "year": d.year, "n": n,
                     "n_eligible": n_el, "ic": ic, "rank_ic": ric, **{f"q{k}": v for k, v in enumerate(qmeans, 1)},
                     "top": qmeans[-1], "bottom": qmeans[0], "ls_gross": qmeans[-1] - qmeans[0],
                     "turnover_top": tt, "turnover_bottom": tb, "cost_top": cost_top, "cost_bottom": cost_bot,
                     "ew_universe": ew, "kospi_cc": k_ret, "first_in_period": first})
        for k, v in enumerate(qmeans, 1):
            qrows.append({"factor": name, "date": d, "period": per, "quantile": k, "n": int((bucket == k).sum()), "mean_fwd": v})
    df = pd.DataFrame(rows)
    if not df.empty:
        # liquidation at the end of each period block: the last rebalance also pays the closing side on 100%
        last = df.groupby("period")["date"].transform("max") == df["date"]
        df["last_in_period"] = last
        df.loc[last, "cost_top"] += cfg.sell_rate()
        df.loc[last, "cost_bottom"] += cfg.buy_rate()
        df["top_net"] = df["top"] - df["cost_top"]
        df["ls_net"] = df["ls_gross"] - df["cost_top"] - df["cost_bottom"]
        df["top_excess_ew"] = df["top"] - df["ew_universe"]
        df["top_net_excess_ew"] = df["top_net"] - df["ew_universe"]
        df["top_excess_kospi_cc"] = df["top"] - df["kospi_cc"]
    return df, pd.DataFrame(qrows), cnt


def _t(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else np.nan


def summarize_factor(df: pd.DataFrame, cfg: FactorLabConfig, group: str = "period") -> pd.DataFrame:
    """One row per (factor, period). Means are per holding window (h days), not annualized."""
    out = []
    if df.empty:
        return pd.DataFrame()
    for (fac, g_key), g in df.groupby(["factor", group], sort=False):
        qcols = [f"q{k}" for k in range(1, cfg.quantiles + 1)]
        qm = g[qcols].mean().to_numpy()
        mono = float(pd.Series(qm).corr(pd.Series(np.arange(len(qm))), method="spearman")) if len(qm) > 1 else np.nan
        out.append({"factor": fac, group: g_key, "dates": int(len(g)), "names_mean": float(g["n"].mean()),
                    "names_min": int(g["n"].min()), "coverage_mean": float((g["n"] / g["n_eligible"]).mean()),
                    "ic_mean": float(g["ic"].mean()), "ic_std": float(g["ic"].std(ddof=1)) if len(g) > 1 else np.nan, "ic_t": _t(g["ic"]),
                    "rank_ic_mean": float(g["rank_ic"].mean()), "rank_ic_std": float(g["rank_ic"].std(ddof=1)) if len(g) > 1 else np.nan,
                    "rank_ic_t": _t(g["rank_ic"]), "rank_ic_pos_share": float((g["rank_ic"] > 0).mean()),
                    **{f"{c}_mean": float(v) for c, v in zip(qcols, qm)}, "quantile_monotonicity": mono,
                    "ls_gross_mean": float(g["ls_gross"].mean()), "ls_net_mean": float(g["ls_net"].mean()), "ls_net_t": _t(g["ls_net"]),
                    "top_gross_mean": float(g["top"].mean()), "top_net_mean": float(g["top_net"].mean()),
                    "ew_universe_mean": float(g["ew_universe"].mean()), "top_net_excess_ew_mean": float(g["top_net_excess_ew"].mean()),
                    "top_net_excess_ew_t": _t(g["top_net_excess_ew"]), "kospi_cc_mean": float(g["kospi_cc"].mean()),
                    "turnover_top_mean": float(g["turnover_top"].mean()), "turnover_bottom_mean": float(g["turnover_bottom"].mean()),
                    "cost_top_mean": float(g["cost_top"].mean())})
    return pd.DataFrame(out)


def load_audit(audit_dir: str | Path) -> dict:
    """Same rules as Study.load_audit: drop `exclude` instruments, distrust `exclude_until_verified` bars."""
    d = Path(audit_dir)
    sym = pd.read_csv(d / "symbol_summary.csv", dtype={"symbol": str})
    f = pd.read_csv(d / "findings.csv", dtype={"symbol": str}, parse_dates=["date"])
    flagged = f[(f["recommendation"] == "exclude_until_verified") & f["date"].notna()]
    summary = json.loads((d / "audit_summary.json").read_text(encoding="utf-8"))
    return {"dir": str(d), "drop": set(sym.loc[sym["audit_status"] == "exclude", "symbol"]),
            "dates": {s: pd.DatetimeIndex(g["date"].unique()) for s, g in flagged.groupby("symbol")},
            "review_share": float((sym["audit_status"] == "review").mean()) if len(sym) else None,
            "usable_fingerprint": summary.get("usable_fingerprint"), "input_fingerprint": summary.get("input_fingerprint")}


def run_factorlab(bars: pd.DataFrame, calendar: pd.DatetimeIndex, cfg: FactorLabConfig, kospi: pd.DataFrame | None = None
                  ) -> dict:
    cfg.validate()
    audit = load_audit(cfg.audit_dir) if cfg.audit_dir else None
    p = build_panel(bars, calendar, audit)
    fwd = forward_returns(p, cfg.horizon)
    ks = kospi.assign(date=pd.to_datetime(kospi["date"])).set_index("date")["close"] if kospi is not None and len(kospi) else None
    per_date, per_q, excl = [], [], {}
    for name in cfg.factors:
        d, qd, c = evaluate_factor(name, p, cfg, fwd, ks)
        per_date.append(d); per_q.append(qd); excl[name] = c
    by_date = pd.concat([x for x in per_date if not x.empty], ignore_index=True) if any(not x.empty for x in per_date) else pd.DataFrame()
    return {"by_date": by_date, "quantiles": pd.concat([x for x in per_q if not x.empty], ignore_index=True) if any(not x.empty for x in per_q) else pd.DataFrame(),
            "summary": summarize_factor(by_date, cfg), "yearly": summarize_factor(by_date, cfg, "year"),
            "exclusions": excl, "panel_counts": p.counts, "audit": audit, "config": asdict(cfg)}
