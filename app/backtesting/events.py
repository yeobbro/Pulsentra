"""Signal-level studies (no portfolio constraints).

* `simulate_trade`: one independent trade from one signal, same fill rules as the portfolio engine.
  Used to compare exit rules on IDENTICAL entry signals (1.5R vs 2R vs 3R, trailing, MA, time).
* `forward_returns`: what happened after past signals ("similar historical cases").
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting.metrics import bootstrap_mean_ci, sample_warning, wilson_interval
from app.risk import CostModel, r_target, swing_low_stop

Key = tuple[str, str]


def _stop_for(cfg, fill: float, sig_row: pd.Series) -> float | None:
    if cfg.stop_type == "atr":
        atr = sig_row.get("atr_14")
        return fill - cfg.atr_mult * float(atr) if pd.notna(atr) and atr > 0 else None
    if cfg.stop_type == "swing_low":
        sl = sig_row.get("swing_low_10")
        return swing_low_stop(fill, float(sl), cfg.swing_buffer_pct) if pd.notna(sl) else None
    return fill * (1 - cfg.stop_pct)


def simulate_trade(x: pd.DataFrame, sig_i: int, cfg, costs: CostModel) -> dict | None:
    """1-unit long trade: signal at close sig_i, entry at open sig_i+1. Returns None if not tradable."""
    if sig_i + 1 >= len(x):
        return None
    fill = costs.buy_fill(float(x.at[sig_i + 1, "open"]))
    stop = _stop_for(cfg, fill, x.iloc[sig_i])
    if stop is None or not (0 < stop < fill):
        return None
    risk = fill - stop
    target = r_target(fill, stop, cfg.target_r) if cfg.target_r else None
    partial = r_target(fill, stop, cfg.partial_r) if cfg.partial_fraction > 0 else None
    remaining, proceeds, fees = 1.0, 0.0, costs.buy_cost(fill)
    highest, partial_done, pending_open_exit = fill, False, None
    reason, exit_i = "OPEN_AT_DATA_END", len(x) - 1

    def sell(qty: float, ref: float, market: bool) -> None:
        nonlocal remaining, proceeds, fees
        px = costs.sell_fill(ref, market)
        proceeds += qty * px
        fees += costs.sell_cost(qty * px)
        remaining -= qty

    last = min(len(x) - 1, sig_i + cfg.max_hold_days)
    for j in range(sig_i + 1, last + 1):
        o, h, l, c = (float(x.at[j, k]) for k in ("open", "high", "low", "close"))
        if pending_open_exit:
            sell(remaining, o, True)
            reason, exit_i = pending_open_exit, j
            break
        if j > sig_i + 1:
            if o <= stop:
                sell(remaining, o, True)
                reason, exit_i = "STOP_GAP", j
                break
            if target is not None and o >= target:
                sell(remaining, o, False)
                reason, exit_i = "TARGET_GAP", j
                break
            if partial is not None and not partial_done and o >= partial:
                sell(remaining * cfg.partial_fraction, o, False)
                partial_done = True
                stop = max(stop, fill)
        hit_stop = l <= stop
        hit_partial = partial is not None and not partial_done and h >= partial
        hit_target = target is not None and h >= target
        if hit_stop and (hit_partial or hit_target):
            sell(remaining, stop, True)
            reason, exit_i = "STOP_BOTH_TOUCHED_CONSERVATIVE", j
            break
        if hit_stop:
            sell(remaining, stop, True)
            reason, exit_i = ("STOP" if stop < fill else "STOP_TRAIL_OR_BE"), j
            break
        if hit_partial:
            sell(remaining * cfg.partial_fraction, partial, False)
            partial_done = True
            stop = max(stop, fill)
        if hit_target:
            sell(remaining, target, False)
            reason, exit_i = "TARGET", j
            break
        highest = max(highest, c)
        if j == last:
            if j - sig_i >= cfg.max_hold_days:
                sell(remaining, c, True)
                reason, exit_i = "TIME", j
            break  # otherwise the data ended first: the trade stays open and is excluded below
        if cfg.trail_atr_mult and pd.notna(x.at[j, "atr_14"]):
            stop = max(stop, highest - cfg.trail_atr_mult * float(x.at[j, "atr_14"]))
        if cfg.ma_exit and pd.notna(x.at[j, cfg.ma_exit]) and c < float(x.at[j, cfg.ma_exit]):
            pending_open_exit = f"{cfg.ma_exit.upper()}_EXIT"
    if remaining > 1e-9:
        return None  # ran out of data with an open position: excluded rather than guessed
    net = (proceeds - fees) / fill - 1
    return {"signal_date": pd.Timestamp(x.at[sig_i, "date"]).strftime("%Y-%m-%d"),
            "exit_date": pd.Timestamp(x.at[exit_i, "date"]).strftime("%Y-%m-%d"),
            "net_return": net, "r_multiple": net * fill / risk, "reason": reason, "holding_days": exit_i - sig_i}


def summarize(outcomes: list[dict]) -> dict:
    n = len(outcomes)
    if not n:
        return {"n": 0, "sample_warning": sample_warning(0)}
    r = np.array([o["net_return"] for o in outcomes])
    rm = np.array([o["r_multiple"] for o in outcomes])
    wins = int((r > 0).sum())
    return {"n": n, "win_rate": wins / n, "win_rate_ci95": wilson_interval(wins, n),
            "mean_return": float(r.mean()), "mean_return_ci95": bootstrap_mean_ci(r), "median_return": float(np.median(r)),
            "mean_r": float(rm.mean()), "mean_r_ci95": bootstrap_mean_ci(rm),
            "avg_holding_days": float(np.mean([o["holding_days"] for o in outcomes])), "sample_warning": sample_warning(n)}


def signal_outcomes(prepared: dict[Key, pd.DataFrame], signals: dict[Key, pd.Series], cfg, costs: CostModel) -> list[dict]:
    out = []
    for key, x in prepared.items():
        sig = signals.get(key)
        if sig is None:
            continue
        for i in np.flatnonzero(sig.to_numpy()):
            res = simulate_trade(x, int(i), cfg, costs)
            if res:
                out.append({"market": key[0], "symbol": key[1], **res})
    return out


def forward_returns(prepared: dict[Key, pd.DataFrame], signals: dict[Key, pd.Series], horizons=(1, 5, 20, 60),
                    costs: CostModel | None = None) -> dict:
    """Return from the next open (entry) to the close h bars after the signal, net of round-trip costs."""
    costs = costs or CostModel.zero()
    result: dict = {}
    cases: list[dict] = []
    for h in horizons:
        vals = []
        for key, x in prepared.items():
            sig = signals.get(key)
            if sig is None:
                continue
            for i in np.flatnonzero(sig.to_numpy()):
                if i + h >= len(x) or i + 1 >= len(x):
                    continue
                entry = costs.buy_fill(float(x.at[i + 1, "open"]))
                exit_px = costs.sell_fill(float(x.at[i + h, "close"]))
                net = (exit_px - costs.sell_cost(exit_px) - costs.buy_cost(entry)) / entry - 1
                vals.append(net)
        arr = np.array(vals)
        result[str(h)] = {"n": len(vals), "mean": float(arr.mean()) if len(arr) else None,
                          "median": float(np.median(arr)) if len(arr) else None,
                          "positive_share": float((arr > 0).mean()) if len(arr) else None,
                          "mean_ci95": bootstrap_mean_ci(arr), "sample_warning": sample_warning(len(vals))}
    for key, x in prepared.items():
        sig = signals.get(key)
        if sig is None:
            continue
        for i in np.flatnonzero(sig.to_numpy())[-20:]:
            row = {"market": key[0], "symbol": key[1], "signal_date": pd.Timestamp(x.at[i, "date"]).strftime("%Y-%m-%d")}
            for h in horizons:
                if i + h < len(x) and i + 1 < len(x):
                    row[f"ret_{h}"] = float(x.at[i + h, "close"]) / float(x.at[i + 1, "open"]) - 1
                else:
                    row[f"ret_{h}"] = None
            cases.append(row)
    result["recent_cases"] = sorted(cases, key=lambda r: r["signal_date"], reverse=True)[:30]
    result["definition"] = "다음 거래일 시가 진입 → h거래일 뒤 종가. 비용 모델 적용 시 왕복 비용 차감. 겹치는 신호는 독립 표본이 아니므로 신뢰구간이 실제보다 좁을 수 있음."
    return result

