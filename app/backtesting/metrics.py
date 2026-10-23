"""Performance metrics with explicit sample-size uncertainty."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_mean_ci(values: np.ndarray, n_boot: int = 2000, alpha: float = 0.05, seed: int = 11) -> tuple[float, float] | None:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return None
    rng = np.random.default_rng(seed)
    means = rng.choice(values, size=(n_boot, len(values)), replace=True).mean(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def max_consecutive(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def sample_warning(n: int) -> str | None:
    if n == 0:
        return "거래가 없습니다. 성과를 판단할 수 없습니다."
    if n < 30:
        return f"거래 {n}건: 표본이 매우 작아 승률·평균 수익의 불확실성이 큽니다. 신뢰구간을 함께 보세요."
    if n < 100:
        return f"거래 {n}건: 표본이 제한적입니다. 기간·종목을 늘려 재검증이 필요합니다."
    return None


def equity_metrics(equity: pd.DataFrame, initial_cash: float, rf_annual: float = 0.0) -> dict[str, Any]:
    if equity.empty:
        return {"total_return": 0.0, "cagr": None, "volatility": None, "sharpe": None, "max_drawdown": 0.0}
    e = equity.set_index("date")["equity"].astype(float)
    total = float(e.iloc[-1] / initial_cash - 1)
    days = (e.index[-1] - e.index[0]).days
    cagr = float((e.iloc[-1] / initial_cash) ** (365.25 / days) - 1) if days >= 180 and e.iloc[-1] > 0 else None
    daily = e.pct_change().dropna()
    vol = float(daily.std() * math.sqrt(TRADING_DAYS)) if len(daily) > 1 else None
    excess = daily - rf_annual / TRADING_DAYS
    sharpe = float(excess.mean() / daily.std() * math.sqrt(TRADING_DAYS)) if len(daily) > 1 and daily.std() > 0 else None
    dd = e / e.cummax() - 1
    # longest time under water, in trading days
    under = (dd < 0).astype(int).tolist()
    return {"total_return": total, "cagr": cagr, "volatility": vol, "sharpe": sharpe,
            "max_drawdown": float(dd.min()), "max_drawdown_duration_days": max_consecutive([u == 1 for u in under]),
            "start": e.index[0].strftime("%Y-%m-%d"), "end": e.index[-1].strftime("%Y-%m-%d"),
            "final_equity": float(e.iloc[-1]), "cagr_note": None if cagr is not None else "기간 180일 미만: 연환산하지 않음"}


def trade_metrics(trades: list[dict]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": None, "win_rate_ci95": None, "avg_win": None, "avg_loss": None,
                "expectancy_return": None, "expectancy_return_ci95": None, "median_return": None, "expectancy_r": None,
                "expectancy_r_ci95": None, "profit_factor": None, "profit_factor_note": None, "total_costs": 0.0,
                "exit_reasons": {}, "trades_with_quality_flags": 0,
                "max_consecutive_losses": 0, "avg_holding_days": None, "sample_warning": sample_warning(0)}
    t = pd.DataFrame(trades)
    rets = t["net_return"].astype(float).to_numpy()
    wins = rets[rets > 0]
    losses = rets[rets <= 0]
    pnl = t["pnl"].astype(float)
    gross_win, gross_loss = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    reasons = t["final_reason"].value_counts().to_dict() if "final_reason" in t else {}
    return {
        "trades": n,
        "win_rate": float(len(wins) / n),
        "win_rate_ci95": wilson_interval(len(wins), n),
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(losses.mean()) if len(losses) else None,
        "expectancy_return": float(rets.mean()),
        "expectancy_return_ci95": bootstrap_mean_ci(rets),
        "median_return": float(np.median(rets)),
        "expectancy_r": float(t["r_multiple"].mean()),
        "expectancy_r_ci95": bootstrap_mean_ci(t["r_multiple"].to_numpy()),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else None,
        "profit_factor_note": None if gross_loss > 0 else "손실 거래가 없어 계산하지 않음",
        "max_consecutive_losses": max_consecutive([r <= 0 for r in rets]),
        "avg_holding_days": float(t["holding_days"].mean()),
        "total_costs": float(t["costs"].sum()),
        "exit_reasons": {str(k): int(v) for k, v in reasons.items()},
        "trades_with_quality_flags": int(sum(1 for f in t.get("quality_flags", []) if f)),
        "sample_warning": sample_warning(n),
    }


def period_returns(equity: pd.DataFrame, freq: str) -> list[dict]:
    """freq: 'M' monthly or 'Y' yearly. Return of each period from its last equity value."""
    if equity.empty:
        return []
    e = equity.set_index("date")["equity"].astype(float)
    rule = "ME" if freq == "M" else "YE"
    last = e.resample(rule).last().dropna()
    first_value = e.iloc[0]
    seed = pd.Series([first_value], index=pd.DatetimeIndex([e.index[0] - pd.Timedelta(days=1)]))
    prev = pd.concat([seed, last]).shift(1).iloc[1:]
    out = []
    for d, v in last.items():
        out.append({"period": d.strftime("%Y-%m" if freq == "M" else "%Y"), "return": float(v / prev.loc[d] - 1)})
    return out


def buy_and_hold_curve(prices: pd.DataFrame, dates: pd.Series, initial_cash: float) -> pd.DataFrame:
    """prices: date, close. Equity of holding it from the first to the last backtest date (no costs)."""
    p = prices[["date", "close"]].copy()
    p["date"] = pd.to_datetime(p["date"])
    p = p.drop_duplicates("date").set_index("date")["close"].sort_index()
    idx = pd.DatetimeIndex(sorted(pd.to_datetime(dates)))
    p = p.reindex(idx).ffill()
    if p.dropna().empty:
        return pd.DataFrame(columns=["date", "equity"])
    first = p.dropna().iloc[0]
    eq = initial_cash * p / first
    return pd.DataFrame({"date": idx, "equity": eq.to_numpy()}).dropna()


def equal_weight_curve(prepared: dict, dates: pd.Series, initial_cash: float) -> pd.DataFrame:
    """Equal-weight buy-and-hold of every instrument in the test universe (used when no index is loaded)."""
    idx = pd.DatetimeIndex(sorted(pd.to_datetime(dates)))
    rel = []
    for x in prepared.values():
        s = x.set_index("date")["close"].reindex(idx).ffill()
        first = s.dropna()
        if first.empty:
            continue
        rel.append(s / first.iloc[0])
    if not rel:
        return pd.DataFrame(columns=["date", "equity"])
    avg = pd.concat(rel, axis=1).mean(axis=1, skipna=True)
    return pd.DataFrame({"date": idx, "equity": (initial_cash * avg).to_numpy()}).dropna()
