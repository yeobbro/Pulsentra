"""Walk-forward validation, regimes and time-ordered splits shared by strategies and ML models."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def to_dict(self) -> dict:
        return {k: (v.strftime("%Y-%m-%d") if isinstance(v, pd.Timestamp) else v) for k, v in self.__dict__.items()}


def time_folds(dates: list | pd.Series | np.ndarray, n_folds: int = 4, min_train_fraction: float = 0.4,
               gap: int = 0, expanding: bool = True) -> list[Fold]:
    """Chronological folds over UNIQUE dates.

    The first `min_train_fraction` of dates is the initial training window; the rest is split
    into `n_folds` consecutive test windows. `gap` dates are dropped between train and test
    (purge/embargo) so labels that look `gap` bars ahead cannot overlap the test window.
    """
    u = pd.DatetimeIndex(sorted(set(pd.to_datetime(list(dates)))))
    if len(u) < 10 or n_folds < 1:
        return []
    first_test = int(len(u) * min_train_fraction)
    edges = np.linspace(first_test, len(u), n_folds + 1).astype(int)
    folds = []
    for k in range(n_folds):
        ts, te = edges[k], edges[k + 1] - 1
        tr_end = ts - 1 - gap
        tr_start = 0 if expanding else max(0, ts - first_test - gap)
        if tr_end <= tr_start or te < ts:
            continue
        folds.append(Fold(k, u[tr_start], u[tr_end], u[ts], u[te]))
    return folds


def regime_series(index_close: pd.Series, lookback: int = 60, band: float = 0.05) -> pd.Series:
    """Market regime per date from an index (or equal-weight proxy), known at that date's close.

    bull: lookback return > +band; bear: < -band; otherwise sideways.
    """
    ret = index_close / index_close.shift(lookback) - 1
    reg = pd.Series("unknown", index=index_close.index, dtype=object)
    reg[ret > band] = "bull"
    reg[ret < -band] = "bear"
    reg[(ret >= -band) & (ret <= band)] = "sideways"
    return reg


def param_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in product(*(grid[k] for k in keys))]
