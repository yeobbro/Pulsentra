"""v0.3 research study on REAL data: strategies A/B/C, exit grid, chronological splits, walk-forward,
random / momentum baselines, regimes, costs, portfolio backtests and an ML filter comparison.

Everything is written to an output folder (CSV + REPORT.md + run_manifest.json) so the run is reproducible.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import time
import warnings
from dataclasses import asdict, dataclass, field
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.backtesting.batch import REASONS, simulate_batch
from app.backtesting.engine import ExitConfig, run_portfolio
from app.backtesting.metrics import buy_and_hold_curve, equity_metrics, max_consecutive, period_returns, trade_metrics, wilson_interval
from app.indicators import ML_FEATURES, compute_indicators
from app.research.data import RealDataset, sample_description
from app.risk import CostModel, RiskConfig
from app.strategies import get_strategy
from app.strategies.base import Strategy
from app.validation import regime_series

SLIM_COLS = ["date", "open", "high", "low", "close", "volume", "turnover", "quality_flags", "ret_1", "ret_5", "ret_20", "ret_60",
             "ret_120", "ret_252", "volume_ratio_20", "turnover_ratio_20", "avg_turnover_20", "log_avg_turnover_20", "volume_spike_1d",
             "up_down_volume_ratio_20", "atr_14", "atr_pct_14", "clv_20", "cmf_20", "ma20", "ma60", "close_to_ma20", "close_to_ma60",
             "ma20_over_ma60", "ma60_slope_20", "high20_distance", "prior_high_20", "breakout_20", "dist_high60", "swing_low_10",
             "volatility_20", "volatility_compression", "range_pct_20", "rsi_14", "bb_width_20", "bb_upper", "bb_width_rank_120",
             "prior_high_close_120", "prev_high", "rel_ret_20", "rel_ret_60"]
SIM_COLS = ["open", "high", "low", "close", "atr_14", "swing_low_10", "ma20", "ma60"]


@dataclass
class StudyConfig:
    periods: dict[str, tuple[str, str]] = field(default_factory=lambda: {
        "dev": ("2016-01-01", "2021-12-31"), "validation": ("2022-01-01", "2023-12-31"), "test": ("2024-01-01", "2099-12-31")})
    embargo_days: int = 35                     # calendar days dropped before each period end (trades must close inside)
    strategies: dict[str, tuple[str, dict]] = field(default_factory=lambda: {
        "A_volume_quiet": ("volume_quiet", {}),
        "B1_quiet_then_breakout": ("volume_quiet_breakout", {}),
        "B2_breakout_with_volume": ("breakout_volume", {}),
        "C_pullback_rebound": ("pullback_rebound", {}),
        "BASE_momentum_120d_high": ("momentum_simple", {}),
    })
    stops: dict[str, dict] = field(default_factory=lambda: {
        "ATR1.5": {"stop_type": "atr", "atr_mult": 1.5}, "ATR2.0": {"stop_type": "atr", "atr_mult": 2.0},
        "PCT7": {"stop_type": "percent", "stop_pct": 0.07}, "SWING10": {"stop_type": "swing_low"}})
    targets: dict[str, dict] = field(default_factory=lambda: {
        "1.5R": {"target_r": 1.5}, "2R": {"target_r": 2.0}, "3R": {"target_r": 3.0},
        "PART50@1.5R+TRAIL2ATR": {"target_r": None, "partial_fraction": 0.5, "partial_r": 1.5, "trail_atr_mult": 2.0}})
    holds: tuple[int, ...] = (5, 10, 20)
    default_exit: str = "ATR1.5|2R|10d"
    min_avg_turnover: float = 1_000_000_000.0  # KRW/day (estimated turnover)
    min_price: float = 1_000.0
    min_dev_trades: int = 100
    costs: dict = field(default_factory=lambda: {"commission_bps": 1.5, "sell_tax_bps": 20.0, "slippage_bps": 10.0})
    random_pool: int = 150_000
    random_draws: int = 1_000
    portfolio_random_seeds: int = 5
    ml_exit: str = "ATR1.5|2R|10d"
    seed: int = 7

    def exit_grid(self) -> dict[str, ExitConfig]:
        out = {}
        for (sn, s), (tn, t), h in product(self.stops.items(), self.targets.items(), self.holds):
            out[f"{sn}|{tn}|{h}d"] = ExitConfig.from_dict({**s, **t, "max_hold_days": h})
        return out


class Precomputed(Strategy):
    """Wraps signals computed once (strategy & eligibility) so portfolio runs do not recompute them."""
    key = ""
    defaults = {}

    def __init__(self, table: dict, name: str = "") -> None:
        super().__init__()
        self.table, self.label = table, name

    def _signal(self, x):
        return self.table[(x["market"].iloc[0], x["symbol"].iloc[0])] if "symbol" in x else pd.Series(False, index=x.index)


# ---------------------------------------------------------------------------------------------------- statistics

def cluster_ci(values: np.ndarray, clusters: np.ndarray, n_boot: int = 2000, seed: int = 11) -> tuple[float, float] | None:
    """95% CI of the mean, resampling whole clusters (signal months) — trades in the same month are correlated."""
    v = np.asarray(values, float)
    ok = np.isfinite(v)
    v, c = v[ok], np.asarray(clusters)[ok]
    if len(v) < 2:
        return None
    uniq, inv = np.unique(c, return_inverse=True)
    if len(uniq) < 2:
        return None
    sums = np.bincount(inv, weights=v)
    cnts = np.bincount(inv)
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))
    means = sums[pick].sum(axis=1) / cnts[pick].sum(axis=1)
    return (float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975)))


def summarize(df: pd.DataFrame) -> dict:
    n = len(df)
    if n == 0:
        return {"n": 0}
    r = df["net_return"].to_numpy(float)
    g = df["gross_return"].to_numpy(float) if "gross_return" in df else np.full(n, np.nan)
    rm = df["r_multiple"].to_numpy(float)
    months = df["signal_date"].dt.strftime("%Y-%m").to_numpy()
    wins = int((r > 0).sum())
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    order = np.argsort(df["signal_date"].to_numpy(), kind="stable")
    ci = cluster_ci(r, months)
    return {"n": n, "months": int(len(np.unique(months))), "win_rate": wins / n, "win_rate_ci95_wilson": wilson_interval(wins, n),
            "mean_net": float(r.mean()), "mean_net_ci95_cluster": ci, "median_net": float(np.median(r)),
            "mean_gross": float(np.nanmean(g)) if np.isfinite(g).any() else None,
            "mean_r": float(rm.mean()), "mean_r_ci95_cluster": cluster_ci(rm, months),
            "profit_factor": float(gains / losses) if losses > 0 else None,
            "avg_win": float(r[r > 0].mean()) if wins else None, "avg_loss": float(r[r <= 0].mean()) if wins < n else None,
            "max_consecutive_losses": max_consecutive(list(r[order] <= 0)),
            "avg_holding_days": float(df["holding_days"].mean()),
            "ci_excludes_zero": bool(ci is not None and (ci[0] > 0 or ci[1] < 0))}


# ---------------------------------------------------------------------------------------------------- the study

class Study:
    def __init__(self, ds: RealDataset, cfg: StudyConfig, out_dir: Path, log=print) -> None:
        self.ds, self.cfg, self.out, self.log = ds, cfg, Path(out_dir), log
        self.out.mkdir(parents=True, exist_ok=True)
        self.costs = CostModel(**cfg.costs)
        self.grid = cfg.exit_grid()
        self.timing: dict[str, float] = {}

    # ---- preparation
    def prepare(self) -> None:
        t = time.time()
        idx = self.ds.indices
        exch = self.ds.instruments.set_index("symbol")["exchange"].to_dict() if "exchange" in self.ds.instruments else {}
        self.prepared: dict[tuple[str, str], pd.DataFrame] = {}
        keys, offsets, arrays = [], [], {c: [] for c in SIM_COLS}
        dates, elig, sym_of_row = [], [], []
        off = 0
        for (m, s), raw in self.ds.bars.groupby(["market", "symbol"], sort=True):
            bench_name = "KOSDAQ" if exch.get(s) == "KOSDAQ" else "KOSPI"
            bench = idx.get(bench_name)
            x = compute_indicators(raw, bench)
            x = x[[c for c in SLIM_COLS if c in x]].copy()
            for c in x.columns:  # memory: thousands of instruments x ~50 columns
                if c not in ("date", "quality_flags", "open", "high", "low", "close", "atr_14", "swing_low_10", "ma20", "ma60") and x[c].dtype == "float64":
                    x[c] = x[c].astype("float32")
            x["market"], x["symbol"] = m, s
            flags = x["quality_flags"].fillna("").astype(str)
            brk = flags.str.contains("volume_level_break").to_numpy()
            # a 40-bar volume window that contains a level break is contaminated (volume not adjusted for a split/merge)
            contam = pd.Series(brk.astype(float)).rolling(40, min_periods=1).max().to_numpy() > 0
            nxt = np.r_[np.ones(len(x) - 1, dtype=bool), False]
            e = ((x["avg_turnover_20"] >= self.cfg.min_avg_turnover) & (x["close"] >= self.cfg.min_price)
                 & (x["atr_14"] > 0)).to_numpy() & ~contam & nxt
            x["eligible"] = e
            x["contaminated"] = contam
            self.prepared[(m, s)] = x.reset_index(drop=True)
            keys.append((m, s))
            offsets.append(off)
            for c in SIM_COLS:
                arrays[c].append(x[c].to_numpy(float))
            dates.append(x["date"].to_numpy())
            elig.append(e)
            sym_of_row.append(np.full(len(x), len(keys) - 1))
            off += len(x)
        self.keys = keys
        self.offsets = np.array(offsets + [off])
        self.G = {c: np.concatenate(v) for c, v in arrays.items()}
        self.G_dates = np.concatenate(dates)
        self.G_elig = np.concatenate(elig)
        self.G_sym = np.concatenate(sym_of_row)
        self.G_end = self.offsets[self.G_sym + 1]
        kospi = idx.get("KOSPI")
        self.regime = regime_series(kospi.set_index("date")["close"]) if kospi is not None else pd.Series(dtype=object)
        self.G_regime = pd.Series(pd.to_datetime(self.G_dates)).map(self.regime).fillna("unknown").to_numpy() if len(self.regime) else np.full(off, "unknown", dtype=object)
        self.timing["prepare_s"] = round(time.time() - t, 1)
        self.log(f"prepared {len(keys)} instruments, {off:,} bars, eligible rows {int(self.G_elig.sum()):,} ({self.timing['prepare_s']}s)")

    def period_of(self, d: pd.Series) -> pd.Series:
        out = pd.Series("", index=d.index, dtype=object)
        for name, (a, b) in self.cfg.periods.items():
            end = pd.Timestamp(b) - pd.Timedelta(self.cfg.embargo_days, unit="D")
            out[(d >= pd.Timestamp(a)) & (d <= end)] = name
        return out

    # ---- signals
    def build_signals(self) -> None:
        t = time.time()
        self.signal_rows: dict[str, np.ndarray] = {}
        self.signal_tables: dict[str, dict] = {}
        for label, (key, params) in self.cfg.strategies.items():
            strat = get_strategy(key, **params)
            table, glob = {}, []
            for i, k in enumerate(self.keys):
                x = self.prepared[k]
                s = strat.signals(x) & x["eligible"]
                table[k] = s
                glob.append(np.flatnonzero(s.to_numpy()) + self.offsets[i])
            self.signal_rows[label] = np.concatenate(glob) if glob else np.array([], dtype=np.int64)
            self.signal_tables[label] = table
            self.log(f"signals {label}: {len(self.signal_rows[label]):,}")
        rng = np.random.default_rng(self.cfg.seed)
        pool = np.flatnonzero(self.G_elig)
        self.random_pool = np.sort(rng.choice(pool, size=min(self.cfg.random_pool, len(pool)), replace=False))
        self.timing["signals_s"] = round(time.time() - t, 1)

    def simulate(self, rows: np.ndarray, ec: ExitConfig, costs: CostModel) -> dict:
        return simulate_batch(self.G, rows, ec, costs, ends=self.G_end[rows])

    def outcomes(self, rows: np.ndarray, exit_name: str, with_gross: bool = True) -> pd.DataFrame:
        ec = self.grid[exit_name]
        net = self.simulate(rows, ec, self.costs)
        df = pd.DataFrame({"row": rows, "signal_date": pd.to_datetime(self.G_dates[rows]),
                           "symbol": [self.keys[i][1] for i in self.G_sym[rows]],
                           "net_return": net["net_return"], "r_multiple": net["r_multiple"], "reason": REASONS[net["reason"]],
                           "holding_days": net["exit_offset"] + 1, "entry_price": net["fill"], "initial_stop": net["stop"]})
        if with_gross:
            df["gross_return"] = self.simulate(rows, ec, CostModel.zero())["net_return"]
        df = df[df["net_return"].notna()].copy()
        df["period"] = self.period_of(df["signal_date"])
        df["regime"] = self.G_regime[df["row"].to_numpy()]
        return df

    # ---- exit grid, selection, walk-forward
    def run_grid(self) -> pd.DataFrame:
        t = time.time()
        recs = []
        self.net_matrix: dict[str, np.ndarray] = {}      # label -> (signals x exits) net returns, float32
        self.meta: dict[str, pd.DataFrame] = {}
        self.exit_names = list(self.grid)
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}
        labels = list(self.cfg.strategies) + ["RANDOM_pool"]
        for label in labels:
            rows = self.random_pool if label == "RANDOM_pool" else self.signal_rows[label]
            meta = pd.DataFrame({"row": rows, "signal_date": pd.to_datetime(self.G_dates[rows])})
            meta["period"] = self.period_of(meta["signal_date"]).to_numpy()
            self.meta[label] = meta
            mat = np.full((len(rows), len(self.exit_names)), np.nan, dtype=np.float32)
            for j, ex in enumerate(self.exit_names):
                o = self.outcomes(rows, ex)
                mat[o.index.to_numpy(), j] = o["net_return"].to_numpy(np.float32)
                for per in list(self.cfg.periods) + ["all"]:
                    sub = o[o["period"] == per] if per != "all" else o[o["period"] != ""]
                    recs.append({"strategy": label, "exit": ex, "period": per, **summarize(sub)})
            self.net_matrix[label] = mat
            self.log(f"grid {label}: {len(self.grid)} exits ({round(time.time() - t)}s)")
        grid = pd.DataFrame(recs)
        self.timing["grid_s"] = round(time.time() - t, 1)
        return grid

    def outcome_df(self, label: str, ex: str) -> pd.DataFrame:
        if (label, ex) not in self._cache:
            rows = self.random_pool if label == "RANDOM_pool" else self.signal_rows[label]
            self._cache[(label, ex)] = self.outcomes(rows, ex)
        return self._cache[(label, ex)]

    def select(self, grid: pd.DataFrame) -> dict[str, str]:
        chosen = {}
        for label in self.cfg.strategies:
            g = grid[(grid["strategy"] == label) & (grid["period"] == "dev") & (grid["n"] >= self.cfg.min_dev_trades)]
            chosen[label] = g.sort_values("mean_net", ascending=False)["exit"].iloc[0] if len(g) else self.cfg.default_exit
        return chosen

    def walk_forward(self) -> pd.DataFrame:
        recs = []
        years = sorted(set(pd.to_datetime(self.G_dates).year))
        for label in self.cfg.strategies:
            mat, meta = self.net_matrix[label], self.meta[label]
            d = meta["signal_date"]
            for y in years:
                if y < 2019:
                    continue
                cut = pd.Timestamp(f"{y}-01-01") - pd.Timedelta(self.cfg.embargo_days, unit="D")
                tr = (d < cut).to_numpy()
                counts = np.isfinite(mat[tr]).sum(axis=0)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    means = np.nanmean(mat[tr], axis=0) if tr.any() else np.full(mat.shape[1], np.nan)
                means = np.where(counts >= self.cfg.min_dev_trades, means, -np.inf)
                if not np.isfinite(means).any():
                    continue
                j = int(np.argmax(means))
                best = self.exit_names[j]
                o = self.outcome_df(label, best)
                te = o[(o["signal_date"] >= pd.Timestamp(f"{y}-01-01")) & (o["signal_date"] <= pd.Timestamp(f"{y}-12-31"))]
                recs.append({"strategy": label, "year": y, "selected_exit": best, "in_sample_mean_net": float(means[j]),
                             **{f"oos_{k}": v for k, v in summarize(te).items()}})
        return pd.DataFrame(recs)

    def random_comparison(self, chosen: dict[str, str]) -> pd.DataFrame:
        recs = []
        rng = np.random.default_rng(self.cfg.seed + 1)
        for label, ex in chosen.items():
            so = self.outcome_df(label, ex)
            ro = self.outcome_df("RANDOM_pool", ex)
            for per in self.cfg.periods:
                s = so[so["period"] == per]["net_return"].to_numpy()
                r = ro[ro["period"] == per]["net_return"].to_numpy()
                if len(s) < 5 or len(r) < len(s):
                    continue
                draws = np.array([r[rng.choice(len(r), size=len(s), replace=False)].mean() for _ in range(self.cfg.random_draws)])
                recs.append({"strategy": label, "exit": ex, "period": per, "n": len(s), "strategy_mean_net": float(s.mean()),
                             "random_mean_net": float(r.mean()), "random_same_n_p05": float(np.quantile(draws, 0.05)),
                             "random_same_n_p95": float(np.quantile(draws, 0.95)),
                             "share_of_random_draws_beaten": float((draws < s.mean()).mean()),
                             "one_sided_p_value": float((draws >= s.mean()).mean())})
        return pd.DataFrame(recs)

    def regime_table(self, chosen: dict[str, str]) -> pd.DataFrame:
        recs = []
        for label, ex in list(chosen.items()) + [("RANDOM_pool", self.cfg.default_exit)]:
            o = self.outcome_df(label, ex)
            for (per, reg), g in o[o["period"] != ""].groupby(["period", "regime"]):
                recs.append({"strategy": label, "exit": ex, "period": per, "regime": reg, **summarize(g)})
        return pd.DataFrame(recs)

    def cost_sensitivity(self, chosen: dict[str, str]) -> pd.DataFrame:
        recs = []
        variants = {"gross(0)": CostModel.zero(), "base": self.costs,
                    "slippage x3": CostModel(self.costs.commission_bps, self.costs.sell_tax_bps, self.costs.slippage_bps * 3)}
        for label, ex in chosen.items():
            rows = self.signal_rows[label]
            for vn, cm in variants.items():
                res = self.simulate(rows, self.grid[ex], cm)
                df = pd.DataFrame({"signal_date": pd.to_datetime(self.G_dates[rows]), "net_return": res["net_return"]}).dropna()
                df["period"] = self.period_of(df["signal_date"])
                for per in self.cfg.periods:
                    v = df[df["period"] == per]["net_return"]
                    recs.append({"strategy": label, "exit": ex, "cost_model": vn, "period": per, "n": len(v),
                                 "mean_net": float(v.mean()) if len(v) else None})
        return pd.DataFrame(recs)

    # ---- portfolio backtests
    def portfolio(self, chosen: dict[str, str]) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
        t = time.time()
        risk = RiskConfig()
        recs, trades_out = [], {}
        windows = {p: (a, min(pd.Timestamp(b), pd.Timestamp(self.G_dates.max())).strftime("%Y-%m-%d")) for p, (a, b) in self.cfg.periods.items()}
        bench = {n: i for n, i in self.ds.indices.items()}
        elig_tables = {k: self.prepared[k]["eligible"] for k in self.keys}
        runs: list[tuple[str, Strategy, str]] = [(lab, Precomputed(self.signal_tables[lab], lab), chosen[lab]) for lab in self.cfg.strategies]
        n_elig = int(self.G_elig.sum())
        a_rate = len(self.signal_rows[next(iter(self.cfg.strategies))]) / max(1, n_elig)
        for seed in range(1, self.cfg.portfolio_random_seeds + 1):
            rnd = get_strategy("random_entry", probability=max(1e-4, a_rate), seed=seed)
            tbl = {k: rnd.signals(self.prepared[k]) & elig_tables[k] for k in self.keys}
            runs.append((f"RANDOM_seed{seed}", Precomputed(tbl), chosen[next(iter(self.cfg.strategies))]))
        for per, (a, b) in windows.items():
            dates = pd.Series(pd.to_datetime(self.G_dates)).drop_duplicates()
            dates = dates[(dates >= pd.Timestamp(a)) & (dates <= pd.Timestamp(b))]
            bm = {}
            for name, idxf in bench.items():
                curve = buy_and_hold_curve(idxf, dates, risk.initial_cash)
                em = equity_metrics(curve, risk.initial_cash) if not curve.empty else {}
                bm[name] = em
                recs.append({"run": f"BENCH_{name}_buy_hold", "period": per, "exit": None, **{k: em.get(k) for k in ("total_return", "cagr", "max_drawdown", "volatility", "sharpe")}})
            for label, strat, ex in runs:
                net = run_portfolio(self.prepared, strat, self.grid[ex], risk, self.costs, None, self.ds.instruments, a, b)
                gross = run_portfolio(self.prepared, strat, self.grid[ex], risk, CostModel.zero(), None, self.ds.instruments, a, b)
                em, tm = equity_metrics(net["equity"], risk.initial_cash), trade_metrics(net["trades"])
                gm = equity_metrics(gross["equity"], risk.initial_cash)
                recs.append({"run": label, "period": per, "exit": ex, "signals": net["signals"], **{k: em.get(k) for k in ("total_return", "cagr", "max_drawdown", "volatility", "sharpe")},
                             "gross_total_return": gm.get("total_return"),
                             "excess_vs_KOSPI": (em["total_return"] - bm["KOSPI"]["total_return"]) if bm.get("KOSPI") else None,
                             "excess_vs_KOSDAQ": (em["total_return"] - bm["KOSDAQ"]["total_return"]) if bm.get("KOSDAQ") else None,
                             **{k: tm.get(k) for k in ("trades", "win_rate", "expectancy_return", "expectancy_r", "profit_factor",
                                                       "max_consecutive_losses", "avg_holding_days", "sample_warning")},
                             "halted_days": net["halted_days"], "drawdown_halts": net.get("drawdown_halts"),
                             "gross_trades": len(gross["trades"]), "skipped": json.dumps(net["skipped"], ensure_ascii=False)})
                if not label.startswith("RANDOM") or label == "RANDOM_seed1":
                    trades_out[f"{label}__{per}"] = pd.DataFrame(net["trades"])
                    self._last_equity = net["equity"]
                    if label == next(iter(self.cfg.strategies)):
                        pr = period_returns(net["equity"], "Y")
                        for r in pr:
                            recs.append({"run": f"{label} yearly {r['period']}", "period": per, "exit": ex, "total_return": r["return"]})
            self.log(f"portfolio {per} done ({round(time.time() - t)}s)")
        self.timing["portfolio_s"] = round(time.time() - t, 1)
        return pd.DataFrame(recs), trades_out

    # ---- ML filter vs rule
    def ml(self) -> tuple[pd.DataFrame, dict]:
        from sklearn.metrics import roc_auc_score  # noqa: F401
        from app.models.lab import _Calibrated, _clf_metrics, _make_model, _reliability
        recs, detail = [], {}
        ex = self.cfg.ml_exit
        feats = [f for f in ML_FEATURES if f in SLIM_COLS]
        for label in self.cfg.strategies:
            o = self.outcome_df(label, ex).copy()
            o = o[o["period"] != ""]
            if (o["period"] == "dev").sum() < 300:
                recs.append({"strategy": label, "model": "—", "period": "—", "rejection_reasons": f"dev 표본 {(o['period'] == 'dev').sum()}건 < 300: ML 비교 생략"})
                continue
            X = np.vstack([self._features_at(int(r), feats) for r in o["row"]])
            o[feats] = X
            o["y"] = o["reason"].isin(["TARGET", "TARGET_GAP"]).astype(int)
            dev = o[o["period"] == "dev"].sort_values("signal_date")
            cut = dev["signal_date"].quantile(0.8)
            fit = dev[dev["signal_date"] < cut - pd.Timedelta(self.cfg.embargo_days, unit="D")]
            cal = dev[dev["signal_date"] >= cut]
            base_rate = float(dev["y"].mean())
            for name in ("logistic", "random_forest", "hist_gb"):
                model = _Calibrated(_make_model(name, self.cfg.seed), "isotonic" if len(cal) >= 500 else "sigmoid")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    model.fit(fit[feats], fit["y"], cal[feats], cal["y"])
                for per in ("validation", "test"):
                    ev = o[o["period"] == per]
                    if len(ev) < 30:
                        continue
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        p = model.predict_proba(ev[feats])
                    m = _clf_metrics(ev["y"].to_numpy(), p, base_rate)
                    sel = ev[p >= base_rate + 0.05]
                    rule = summarize(ev)
                    s = summarize(sel) if len(sel) else {"n": 0}
                    months = ev["signal_date"].dt.strftime("%Y-%m").to_numpy()
                    rec = {"strategy": label, "model": name, "period": per, "label": f"TARGET before STOP ({ex})",
                           "dev_rows_fit": len(fit), "dev_rows_calibration": len(cal), "base_rate_dev": base_rate,
                           "auc": m.get("roc_auc"), "brier": m.get("brier"), "brier_skill": m.get("brier_skill"),
                           "rule_n": rule["n"], "rule_mean_net": rule["mean_net"], "rule_mean_r": rule["mean_r"],
                           "ml_n": s["n"], "ml_mean_net": s.get("mean_net"), "ml_mean_net_ci95": s.get("mean_net_ci95_cluster"),
                           "ml_mean_r": s.get("mean_r"), "ml_mean_r_ci95": s.get("mean_r_ci95_cluster")}
                    if per == "validation":
                        reasons = []
                        if (m.get("roc_auc") or 0) < 0.55:
                            reasons.append("AUC < 0.55")
                        if (m.get("brier_skill") or -1) <= 0:
                            reasons.append("Brier skill <= 0 (확률이 기준 비율보다 낫지 않음)")
                        if s["n"] < 30:
                            reasons.append("선택 거래 < 30")
                        elif not s.get("mean_net_ci95_cluster") or s["mean_net_ci95_cluster"][0] <= 0:
                            reasons.append("선택 거래 순수익 95% CI 하한 <= 0")
                        if s.get("mean_net") is None or s["mean_net"] <= rule["mean_net"]:
                            reasons.append("선택 거래 평균이 규칙 전체 평균보다 낫지 않음")
                        rec["adopted_on_validation"] = not reasons
                        rec["rejection_reasons"] = "; ".join(reasons)
                    recs.append(rec)
                    detail[f"{label}|{name}|{per}"] = _reliability(ev["y"].to_numpy(), p)
                    _ = months
        return pd.DataFrame(recs), detail

    def _features_at(self, grow: int, feats: list[str]) -> np.ndarray:
        si = int(self.G_sym[grow])
        key = self.keys[si]
        i = grow - self.offsets[si]
        return self.prepared[key].loc[i, feats].to_numpy(dtype=float)

    # ---- data quality report
    def quality_tables(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        iss = self.ds.issues
        summ = (iss.groupby(["check", "severity"]).agg(rows=("symbol", "size"), symbols=("symbol", "nunique")).reset_index()
                if len(iss) else pd.DataFrame(columns=["check", "severity", "rows", "symbols"]))
        h = self.ds.halts
        extra = pd.DataFrame([{"check": f"suspended_session_removed:{k}", "severity": "info", "rows": int(len(g)), "symbols": int(g["symbol"].nunique())}
                              for k, g in h.groupby("kind")]) if len(h) and "kind" in h else pd.DataFrame(
            [{"check": "suspended_session_removed", "severity": "info", "rows": int(len(h)), "symbols": int(h["symbol"].nunique()) if len(h) else 0}])
        contam = sum(int(self.prepared[k]["contaminated"].sum()) for k in self.keys)
        extra2 = pd.DataFrame([{"check": "rows excluded: 40-bar volume window spans a volume_level_break", "severity": "exclusion",
                                "rows": contam, "symbols": sum(1 for k in self.keys if self.prepared[k]["contaminated"].any())}])
        return pd.concat([summ, extra, extra2], ignore_index=True), iss

    # ---- everything
    def run(self, command: str = "") -> dict:
        t0 = time.time()
        self.prepare()
        self.build_signals()
        grid = self.run_grid()
        chosen = self.select(grid)
        wf = self.walk_forward()
        rnd = self.random_comparison(chosen)
        reg = self.regime_table(chosen)
        cost = self.cost_sensitivity(chosen)
        port, trades = self.portfolio(chosen)
        mlr, mld = self.ml()
        qsum, qiss = self.quality_tables()
        desc = sample_description(self.ds)
        o = self.out
        grid.to_csv(o / "strategy_exit_grid.csv", index=False)
        wf.to_csv(o / "walk_forward_by_year.csv", index=False)
        rnd.to_csv(o / "vs_random_entry.csv", index=False)
        reg.to_csv(o / "by_regime.csv", index=False)
        cost.to_csv(o / "cost_sensitivity.csv", index=False)
        port.to_csv(o / "portfolio_results.csv", index=False)
        mlr.to_csv(o / "ml_vs_rule.csv", index=False)
        qsum.to_csv(o / "data_quality_summary.csv", index=False)
        qiss.to_csv(o / "data_quality_issues.csv", index=False)
        self.ds.halts.to_csv(o / "provider_halt_rows.csv", index=False)
        tdir = o / "trades"
        tdir.mkdir(exist_ok=True)
        for name, df in trades.items():
            df.to_csv(tdir / f"portfolio_{name}.csv", index=False)
        for label, ex in chosen.items():
            oc = self.outcome_df(label, ex)
            oc.drop(columns=["row"]).to_csv(tdir / f"signals_{label}__{ex.replace('|', '_')}.csv", index=False)
        manifest = {
            "command": command, "started_utc": pd.Timestamp.utcnow().isoformat(), "seconds": round(time.time() - t0, 1),
            "git_commit": _git_commit(), "python": platform.python_version(), "config": _jsonable(asdict(self.cfg)),
            "chosen_exit_on_dev": chosen, "sample": desc, "provenance": {k: v for k, v in self.ds.provenance.items() if k != "manifest_sha256"},
            "data_fingerprint": _fingerprint(self.ds.bars), "timing": self.timing, "configs_tested_per_strategy": len(self.grid),
        }
        (o / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (o / "ml_reliability.json").write_text(json.dumps(mld, ensure_ascii=False, default=str), encoding="utf-8")
        return {"grid": grid, "chosen": chosen, "wf": wf, "random": rnd, "regime": reg, "cost": cost, "portfolio": port,
                "ml": mlr, "quality": qsum, "sample": desc, "manifest": manifest}


def _git_commit() -> str | None:
    root = Path(__file__).resolve().parents[2]
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--", "app", "scripts"], cwd=root,
                                        stderr=subprocess.DEVNULL).decode().strip()
        return sha + ("+uncommitted-changes" if dirty else "")
    except Exception:
        return None


def _fingerprint(bars: pd.DataFrame) -> str:
    h = hashlib.sha256(pd.util.hash_pandas_object(bars[["symbol", "date", "close", "volume"]], index=False).values.tobytes())
    return h.hexdigest()[:16]


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x
