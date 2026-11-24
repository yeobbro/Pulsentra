"""Selection-bias statistics for the Robustness Gate: PBO by CSCV and the Deflated Sharpe Ratio.

Both answer the same question from two sides: "the best-looking configuration was picked out of many; how much of
its performance is the picking?"

* PBO — Probability of Backtest Overfitting, estimated with Combinatorially Symmetric Cross-Validation
  (Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest Overfitting", J. Computational Finance, 2017).
  The T x N matrix of per-date returns (dates x trials) is cut into S contiguous blocks. For every way of taking
  S/2 blocks as in-sample (IS) and the rest as out-of-sample (OOS), the trial with the best IS Sharpe is selected and
  its OOS rank among all trials is recorded as the logit  lambda = ln(w / (1 - w)),  w = rank / (N + 1)
  (ranks ascending, ties get the average rank). PBO is the share of splits with lambda <= 0: the IS winner did no
  better than the OOS median. Ties at the median count as overfit (conservative).
* DSR — Deflated Sharpe Ratio (Bailey & López de Prado, "The Deflated Sharpe Ratio", J. Portfolio Management, 2014).
  The probability that the true Sharpe ratio exceeds the Sharpe ratio one would expect from the best of N
  unskilled trials,
      SR0 = sqrt(V[SR_n]) * ((1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N e))),  g = Euler-Mascheroni,
      DSR = Z((SR - SR0) * sqrt(T - 1) / sqrt(1 - skew * SR + (kurt - 1) / 4 * SR^2)),
  with SR, skew and (non-excess) kurtosis of the selected series per period (not annualised). With N = 1 or no
  cross-trial dispersion SR0 = 0 and DSR is the Probabilistic Sharpe Ratio against zero.

Nothing here chooses a parameter. Inputs are what the caller already evaluated; outputs are numbers plus the
reason when a number cannot be computed (never an estimate in its place). Pure numpy + standard library.
"""
from __future__ import annotations

import math
from itertools import combinations
from statistics import NormalDist

import numpy as np
import pandas as pd

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()
_EPS = 1e-12


def sharpe(x) -> float:
    """Per-period Sharpe ratio (mean / sample std), nan when it cannot be computed."""
    a = np.asarray(x, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) < 2:
        return float("nan")
    sd = a.std(ddof=1)
    return float(a.mean() / sd) if sd > _EPS else float("nan")


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum Sharpe ratio of n_trials unskilled trials whose Sharpe ratios have the given variance."""
    if n_trials <= 1 or not np.isfinite(sr_variance) or sr_variance <= 0:
        return 0.0
    z1 = _N.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sr_variance) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(returns, n_trials: int, sr_variance: float) -> dict:
    """Deflated Sharpe Ratio of one selected return series. Returns the inputs used and `dsr` (nan + `reason`)."""
    a = np.asarray(returns, dtype=float)
    a = a[np.isfinite(a)]
    t = len(a)
    out = {"n_obs": t, "n_trials": int(n_trials), "sr_variance_across_trials": float(sr_variance) if np.isfinite(sr_variance) else None,
           "sharpe": float("nan"), "sr0_expected_max": float("nan"), "skew": float("nan"), "kurtosis": float("nan"),
           "dsr": float("nan"), "reason": None}
    if t < 3:
        out["reason"] = f"{t} observations (3 needed)"
        return out
    sd = a.std(ddof=1)
    if sd <= _EPS:
        out["reason"] = "returns have no variation; Sharpe ratio undefined"
        return out
    sr = float(a.mean() / sd)
    z = (a - a.mean()) / a.std(ddof=0)
    skew, kurt = float(np.mean(z ** 3)), float(np.mean(z ** 4))
    sr0 = expected_max_sharpe(int(n_trials), sr_variance)
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    out.update(sharpe=sr, sr0_expected_max=sr0, skew=skew, kurtosis=kurt)
    if not denom > 0:
        out["reason"] = "non-normality term is not positive (skew too large for this Sharpe ratio); DSR undefined"
        return out
    out["dsr"] = float(_N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)))
    return out


def _block_edges(t: int, s: int) -> list[tuple[int, int]]:
    """S contiguous blocks covering all T rows; sizes differ by at most one (earlier blocks get the extra row)."""
    base, extra = divmod(t, s)
    edges, start = [], 0
    for i in range(s):
        end = start + base + (1 if i < extra else 0)
        edges.append((start, end))
        start = end
    return edges


def choose_splits(t: int, wanted: int = 16, min_block: int = 2, min_splits: int = 4) -> int | None:
    """Largest even S <= wanted with at least `min_block` rows per block; None if even `min_splits` does not fit."""
    s = wanted - (wanted % 2)
    while s >= min_splits:
        if t // s >= min_block:
            return s
        s -= 2
    return None


def pbo_cscv(matrix: pd.DataFrame, n_splits: int = 16, min_block: int = 2) -> dict:
    """PBO by CSCV on a dates x trials matrix of per-date returns (rows in time order, no missing values).

    Returns pbo, the logit distribution summary, how often each trial was the IS winner, the probability that the
    IS winner lost money OOS, and the slope of OOS on IS Sharpe for the winner (performance degradation).
    """
    m = matrix.to_numpy(dtype=float)
    t, n = m.shape
    res = {"n_obs": int(t), "n_trials": int(n), "n_splits": None, "n_combinations": 0, "pbo": float("nan"), "reason": None}
    if n < 2:
        res["reason"] = f"{n} trial(s); at least 2 needed to measure selection"
        return res
    if not np.isfinite(m).all():
        res["reason"] = "matrix has missing values"
        return res
    s = choose_splits(t, n_splits, min_block)
    if s is None:
        res["reason"] = f"{t} dates: too few for 4 blocks of {min_block}"
        return res
    edges = _block_edges(t, s)
    bsum = np.array([m[a:b].sum(axis=0) for a, b in edges])           # S x N
    bsq = np.array([(m[a:b] ** 2).sum(axis=0) for a, b in edges])
    bcnt = np.array([b - a for a, b in edges], dtype=float)           # S
    combos = np.array(list(combinations(range(s), s // 2)))
    mask = np.zeros((len(combos), s))
    mask[np.arange(len(combos))[:, None], combos] = 1.0

    def sr(w):                                                        # w: C x S block weights -> C x N Sharpe
        cnt = w @ bcnt
        mean = (w @ bsum) / cnt[:, None]
        var = ((w @ bsq) - cnt[:, None] * mean ** 2) / (cnt[:, None] - 1)
        return mean / np.sqrt(np.maximum(var, _EPS ** 2))
    is_sr, oos_sr = sr(mask), sr(1.0 - mask)
    best = np.argmax(is_sr, axis=1)                                   # ties: first trial (deterministic)
    rows = np.arange(len(combos))
    sel_oos = oos_sr[rows, best]
    below = (oos_sr < sel_oos[:, None]).sum(axis=1)
    ties = (oos_sr == sel_oos[:, None]).sum(axis=1)                   # includes the selected trial itself
    rank = below + (ties + 1) / 2.0                                   # average rank, 1..N ascending
    w = rank / (n + 1.0)
    logit = np.log(w / (1.0 - w))
    sel_is = is_sr[rows, best]
    slope = float(np.polyfit(sel_is, sel_oos, 1)[0]) if np.ptp(sel_is) > _EPS else float("nan")
    names = list(matrix.columns)
    counts = pd.Series(best).value_counts()
    res.update(n_splits=int(s), n_combinations=int(len(combos)), pbo=float(np.mean(logit <= 0)),
               logit_median=float(np.median(logit)), logit_mean=float(np.mean(logit)),
               prob_oos_loss=float(np.mean(sel_oos < 0)), degradation_slope=slope,
               is_winner_share={str(names[i]): float(c / len(combos)) for i, c in counts.items()})
    return res
