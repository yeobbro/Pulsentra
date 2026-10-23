"""MVP single-symbol quick backtest, kept for the `/api/backtest` endpoint.

It compounds each trade on the full capital (no position sizing, cash or concurrent positions).
Use app.backtesting (portfolio engine) for any evaluation.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from app.indicators import compute_indicators


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def _validate_params(params: dict) -> dict:
    p = {
        "volume_multiple": float(params.get("volume_multiple", 3.0)),
        "return_min": float(params.get("return_min", -0.03)),
        "return_max": float(params.get("return_max", 0.03)),
        "turnover_multiple": float(params.get("turnover_multiple", 1.5)),
        "atr_stop": float(params.get("atr_stop", 1.5)),
        "reward_risk": float(params.get("reward_risk", 2.0)),
        "max_hold_days": int(params.get("max_hold_days", 10)),
        "round_trip_cost_bps": float(params.get("round_trip_cost_bps", 40.0)),
        "require_positive_clv": bool(params.get("require_positive_clv", False)),
        "require_above_ma60": bool(params.get("require_above_ma60", False)),
    }
    if p["volume_multiple"] <= 0 or p["atr_stop"] <= 0 or p["reward_risk"] <= 0 or p["max_hold_days"] < 1:
        raise ValueError("volume_multiple, atr_stop, reward_risk must be positive and max_hold_days must be >= 1")
    if p["return_min"] > p["return_max"]:
        raise ValueError("return_min must be <= return_max")
    if not 0 <= p["round_trip_cost_bps"] <= 3000:
        raise ValueError("round_trip_cost_bps must be between 0 and 3000")
    return p


def backtest_symbol(raw: pd.DataFrame, params: dict | None = None, initial_cash: float = 10_000_000) -> dict:
    """Long-only, next-open entry, ATR stop/target, conservative OHLC fill model.

    A signal is calculated using close t. Entry is open t+1. If both target and
    stop appear inside the same candle, stop is assumed to occur first. No short
    selling, dividends, market impact, split adjustment, or portfolio correlation
    model is included in this MVP.
    """
    p = _validate_params(params or {})
    if raw.empty:
        return {"trades": [], "metrics": _empty_metrics(initial_cash)}
    x = compute_indicators(raw).reset_index(drop=True)
    if len(x) < 80:
        return {"trades": [], "metrics": _empty_metrics(initial_cash), "warning": "최소 80개 거래일 권장: 지표 워밍업과 신호 표본이 부족합니다."}
    trades: list[dict] = []
    i = 0
    cost_rate = p["round_trip_cost_bps"] / 10000.0
    while i < len(x) - 1:
        r = x.iloc[i]
        valid = not any(pd.isna(r.get(k, np.nan)) for k in ["ret_20", "volume_ratio_20", "turnover_ratio_20", "atr_14", "clv_20"])
        if not valid:
            i += 1
            continue
        signal = (
            r["volume_ratio_20"] >= p["volume_multiple"]
            and p["return_min"] <= r["ret_20"] <= p["return_max"]
            and r["turnover_ratio_20"] >= p["turnover_multiple"]
            and (not p["require_positive_clv"] or r["clv_20"] > 0)
            and (not p["require_above_ma60"] or r["close"] > r["ma60"])
        )
        if not signal:
            i += 1
            continue
        entry_idx = i + 1
        entry_row = x.iloc[entry_idx]
        entry = float(entry_row["open"])
        atr = float(r["atr_14"])
        if not math.isfinite(entry) or not math.isfinite(atr) or entry <= 0 or atr <= 0:
            i += 1
            continue
        risk = p["atr_stop"] * atr
        stop = entry - risk
        target = entry + p["reward_risk"] * risk
        exit_idx = min(entry_idx + p["max_hold_days"] - 1, len(x) - 1)
        exit_price = float(x.iloc[exit_idx]["close"])
        exit_reason = "TIME"
        final_idx = exit_idx
        for j in range(entry_idx, exit_idx + 1):
            bar = x.iloc[j]
            o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
            # Gaps are filled at open when worse than the stop. Target gap uses open for a better fill.
            if o <= stop:
                exit_price, exit_reason, final_idx = o, "STOP_GAP", j
                break
            if o >= target:
                exit_price, exit_reason, final_idx = o, "TARGET_GAP", j
                break
            hit_stop = l <= stop
            hit_target = h >= target
            if hit_stop and hit_target:
                exit_price, exit_reason, final_idx = stop, "STOP_BOTH_TOUCHED_CONSERVATIVE", j
                break
            if hit_stop:
                exit_price, exit_reason, final_idx = stop, "STOP", j
                break
            if hit_target:
                exit_price, exit_reason, final_idx = target, "TARGET", j
                break
        gross_return = exit_price / entry - 1
        net_return = gross_return - cost_rate
        trades.append({
            "signal_date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"),
            "entry_date": pd.Timestamp(entry_row["date"]).strftime("%Y-%m-%d"),
            "exit_date": pd.Timestamp(x.iloc[final_idx]["date"]).strftime("%Y-%m-%d"),
            "entry": round(entry, 4), "stop": round(stop, 4), "target": round(target, 4),
            "exit": round(exit_price, 4), "gross_return": gross_return, "net_return": net_return,
            "r_multiple_net": net_return / (risk / entry) if risk > 0 else 0,
            "reason": exit_reason, "holding_days": final_idx - entry_idx + 1,
        })
        i = final_idx + 1  # avoid overlapping positions in this single-symbol backtest

    if not trades:
        return {"trades": [], "metrics": _empty_metrics(initial_cash), "warning": "설정한 조건으로 발생한 거래가 없습니다. 임계값을 완화하거나 더 긴 이력을 제공하세요."}
    t = pd.DataFrame(trades)
    returns = t["net_return"].astype(float)
    equity = initial_cash * (1 + returns).cumprod()
    gross_wins = returns[returns > 0].sum()
    gross_losses = -returns[returns < 0].sum()
    elapsed_days = max(1, (pd.to_datetime(t["exit_date"]).max() - pd.to_datetime(t["entry_date"]).min()).days)
    total_return = float(equity.iloc[-1] / initial_cash - 1)
    cagr = (equity.iloc[-1] / initial_cash) ** (365.25 / elapsed_days) - 1 if elapsed_days > 180 and equity.iloc[-1] > 0 else None
    metrics = {
        "trades": int(len(t)),
        "win_rate": float((returns > 0).mean()),
        "avg_net_return": float(returns.mean()),
        "median_net_return": float(returns.median()),
        "profit_factor": float(gross_wins / gross_losses) if gross_losses > 0 else (None if gross_wins == 0 else float("inf")),
        "total_return": total_return,
        "cagr": float(cagr) if cagr is not None else None,
        "max_drawdown_trade_equity": max_drawdown(equity),
        "avg_holding_days": float(t["holding_days"].mean()),
        "target_exit_share": float(t["reason"].str.startswith("TARGET").mean()),
        "stop_exit_share": float(t["reason"].str.startswith("STOP").mean()),
        "initial_cash": float(initial_cash),
        "final_equity": float(equity.iloc[-1]),
        "cost_rate_round_trip": cost_rate,
        "warning": "MVP estimate: trade-equity curve uses sequential returns, not a full cash/position portfolio simulator. Gaps and order fills are approximated.",
    }
    return {"trades": trades, "metrics": metrics}


def _empty_metrics(initial_cash: float) -> dict:
    return {"trades": 0, "win_rate": 0.0, "avg_net_return": 0.0, "median_net_return": 0.0,
            "profit_factor": None, "total_return": 0.0, "cagr": None, "max_drawdown_trade_equity": 0.0,
            "avg_holding_days": 0.0, "target_exit_share": 0.0, "stop_exit_share": 0.0,
            "initial_cash": float(initial_cash), "final_equity": float(initial_cash)}
