"""Labels. Features at row i use bars <= i; labels look FORWARD from i and record the last bar
they used (`label_end`) so training rows can be purged before a test window."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting.engine import ExitConfig
from app.backtesting.events import simulate_trade
from app.risk import CostModel

TB_CLASSES = ("TARGET", "STOP", "TIMEOUT")


def triple_barrier(x: pd.DataFrame, atr_mult: float = 1.5, target_r: float = 2.0, horizon: int = 10,
                   costs: CostModel | None = None) -> pd.DataFrame:
    """Per row: outcome (TARGET / STOP / TIMEOUT), realised net R, label_end date.

    Entry = next open; stop = entry − atr_mult·ATR14(i); target = entry + target_r·risk; same-bar
    stop+target = STOP (conservative); timeout = close of bar i+horizon. Rows without enough
    future bars get no label (NaN), they are never filled.
    """
    costs = costs or CostModel()
    cfg = ExitConfig(stop_type="atr", atr_mult=atr_mult, target_r=target_r, max_hold_days=horizon)
    outcome = np.full(len(x), None, dtype=object)
    r_mult = np.full(len(x), np.nan)
    net = np.full(len(x), np.nan)
    end = np.full(len(x), None, dtype=object)
    for i in range(len(x) - 1):
        res = simulate_trade(x, i, cfg, costs)
        if res is None:
            continue
        reason = res["reason"]
        outcome[i] = "TARGET" if reason.startswith("TARGET") else ("STOP" if reason.startswith("STOP") else "TIMEOUT")
        r_mult[i] = res["r_multiple"]
        net[i] = res["net_return"]
        end[i] = pd.Timestamp(res["exit_date"])
    return pd.DataFrame({"tb_outcome": outcome, "tb_r": r_mult, "tb_net": net, "label_end": end}, index=x.index)


def forward_direction(x: pd.DataFrame, horizon: int, costs: CostModel | None = None) -> pd.DataFrame:
    """1 when the net return from the next open to the close of bar i+horizon is > 0."""
    costs = costs or CostModel()
    n = len(x)
    fwd = np.full(n, np.nan)
    end = np.full(n, None, dtype=object)
    opens, closes, dates = x["open"].to_numpy(float), x["close"].to_numpy(float), x["date"].to_numpy()
    for i in range(n):
        j = i + horizon
        if j >= n or i + 1 >= n:
            continue
        entry = costs.buy_fill(opens[i + 1])
        exit_px = costs.sell_fill(closes[j])
        fwd[i] = (exit_px - costs.sell_cost(exit_px) - costs.buy_cost(entry)) / entry - 1
        end[i] = pd.Timestamp(dates[j])
    label = np.where(np.isnan(fwd), np.nan, (fwd > 0).astype(float))
    return pd.DataFrame({"fwd_net": fwd, "y": label, "label_end": end}, index=x.index)
