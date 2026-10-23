"""Causal (known at the close of bar t) indicators for one instrument.

Every value at row t uses rows <= t only. Missing inputs give NaN — never 0 — and the scanner
shows NaN as "미확인". `INDICATOR_SPECS` documents each formula and its look-back for the UI.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# MVP feature list (kept: a model trained by the MVP still loads with these columns).
FEATURE_COLUMNS = [
    "ret_1", "ret_5", "ret_20", "ret_60", "ret_120",
    "volume_ratio_20", "turnover_ratio_20", "atr_pct_14",
    "clv_20", "close_to_ma20", "close_to_ma60", "high20_distance",
    "volatility_20", "volatility_compression", "range_pct_20",
]

# Pulsentra ML feature set. Relative-strength columns are NaN when no benchmark is loaded.
ML_FEATURES = FEATURE_COLUMNS + [
    "ret_252", "up_down_volume_ratio_20", "rsi_14", "bb_width_20", "dist_high60",
    "ma20_over_ma60", "rel_ret_20", "rel_ret_60", "log_avg_turnover_20", "volume_spike_1d",
]

INDICATOR_SPECS: dict[str, str] = {
    "ret_N": "close[t] / close[t-N] - 1 (N = 1, 5, 20, 60, 120, 252 거래일)",
    "volume_ratio_20": "sum(volume[t-19..t]) / sum(volume[t-39..t-20]) — 최근 20거래일 누적 ÷ 직전 20거래일 누적",
    "turnover_ratio_20": "sum(turnover[t-19..t]) / sum(turnover[t-39..t-20]); 20일 평균 비율과 같음",
    "up_down_volume_ratio_20": "최근 20거래일 중 상승일(close>전일 close) 거래량 합 ÷ 하락일 거래량 합. 보합일 제외",
    "clv": "(2·close − high − low) / (high − low); high=low이면 0",
    "clv_20 / cmf_20": "Σ(clv·volume) / Σ(volume), 20거래일. 정의상 Chaikin Money Flow(20)와 같음",
    "atr_14": "True Range 14거래일 단순평균 (MVP 정의 유지)",
    "atr_14_wilder": "True Range의 Wilder 평활(α=1/14)",
    "ma20 / ma60 / ma120": "종가 단순이동평균",
    "breakout_20": "close[t] > max(high[t-20..t-1]) — 당일 고가는 기준에서 제외",
    "dist_high60": "close[t] / max(high[t-59..t]) − 1",
    "rsi_14": "Wilder RSI(14)",
    "bb_width_20": "(상단 − 하단) / ma20, 밴드 = ma20 ± 2·표준편차(20)",
    "bb_width_rank_120": "오늘 bb_width_20의 직전 120거래일 내 백분위 (낮을수록 수축)",
    "rel_ret_N": "종목 ret_N − 벤치마크 ret_N (벤치마크 미적재 시 미확인)",
    "avg_turnover_20": "20거래일 평균 거래대금",
    "swing_low_10": "min(low[t-9..t])",
}


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def compute_indicators(raw: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> pd.DataFrame:
    """`benchmark` (optional): DataFrame with date, close of a market index for relative strength."""
    if raw.empty:
        return raw.copy()
    df = raw.copy().sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "turnover" not in df or df["turnover"].isna().all():
        df["turnover"] = df["close"] * df["volume"]
    else:
        df["turnover"] = pd.to_numeric(df["turnover"], errors="coerce").fillna(df["close"] * df["volume"])

    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    prev_close = close.shift(1)
    for n in (1, 5, 20, 60, 120, 252):
        df[f"ret_{n}"] = close / close.shift(n) - 1

    recent_vol = volume.rolling(20, min_periods=20).sum()
    prior_vol = volume.shift(20).rolling(20, min_periods=20).sum()
    df["volume_20_sum"] = recent_vol
    df["volume_prev20_sum"] = prior_vol
    df["volume_ratio_20"] = recent_vol / prior_vol.replace(0, np.nan)
    recent_turnover = df["turnover"].rolling(20, min_periods=20).sum()
    prior_turnover = df["turnover"].shift(20).rolling(20, min_periods=20).sum()
    df["turnover_ratio_20"] = recent_turnover / prior_turnover.replace(0, np.nan)
    df["avg_turnover_20"] = df["turnover"].rolling(20, min_periods=20).mean()
    df["log_avg_turnover_20"] = np.log10(df["avg_turnover_20"].where(df["avg_turnover_20"] > 0))
    df["volume_spike_1d"] = volume / volume.shift(1).rolling(20, min_periods=20).mean().replace(0, np.nan)

    up = (close > prev_close).astype(float).where(prev_close.notna())
    down = (close < prev_close).astype(float).where(prev_close.notna())
    up_vol = (volume * up).rolling(20, min_periods=20).sum()
    down_vol = (volume * down).rolling(20, min_periods=20).sum()
    df["up_down_volume_ratio_20"] = up_vol / down_vol.replace(0, np.nan)

    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    tr = tr.where(high.notna() & low.notna())
    df["true_range"] = tr
    df["atr_14"] = tr.rolling(14, min_periods=14).mean()
    df["atr_14_wilder"] = _wilder(tr, 14)
    df["atr_pct_14"] = df["atr_14"] / close

    daily_range = high - low
    clv = (2 * close - high - low) / daily_range.replace(0, np.nan)
    df["clv"] = clv.clip(-1, 1).where(daily_range != 0, 0.0).where(daily_range.notna())
    weighted_num = (df["clv"] * volume).rolling(20, min_periods=20).sum()
    weighted_den = volume.rolling(20, min_periods=20).sum().replace(0, np.nan)
    df["clv_20"] = weighted_num / weighted_den
    df["cmf_20"] = df["clv_20"]

    df["ma20"] = close.rolling(20, min_periods=20).mean()
    df["ma60"] = close.rolling(60, min_periods=60).mean()
    df["ma120"] = close.rolling(120, min_periods=120).mean()
    df["close_to_ma20"] = close / df["ma20"] - 1
    df["close_to_ma60"] = close / df["ma60"] - 1
    df["ma20_over_ma60"] = df["ma20"] / df["ma60"] - 1
    df["ma60_slope_20"] = df["ma60"] / df["ma60"].shift(20) - 1
    high20 = high.rolling(20, min_periods=20).max()
    df["high20"] = high20
    df["high20_distance"] = close / high20 - 1
    df["prior_high_20"] = high.shift(1).rolling(20, min_periods=20).max()
    df["breakout_20"] = (close > df["prior_high_20"]).where(df["prior_high_20"].notna())
    df["high60"] = high.rolling(60, min_periods=60).max()
    df["dist_high60"] = close / df["high60"] - 1
    df["swing_low_10"] = low.rolling(10, min_periods=10).min()
    df["prior_high_close_120"] = close.shift(1).rolling(120, min_periods=120).max()
    df["prev_high"] = high.shift(1)

    daily_ret = close.pct_change()
    vol20 = daily_ret.rolling(20, min_periods=20).std()
    vol60 = daily_ret.rolling(60, min_periods=60).std()
    df["volatility_20"] = vol20 * np.sqrt(252)
    df["volatility_compression"] = vol20 / vol60.replace(0, np.nan)
    df["range_pct_20"] = (high.rolling(20, min_periods=20).max() - low.rolling(20, min_periods=20).min()) / close

    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), 14)
    loss = _wilder(-delta.clip(upper=0), 14)
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = (100 - 100 / (1 + rs)).where(loss != 0, 100.0).where(gain.notna())

    std20 = close.rolling(20, min_periods=20).std()
    df["bb_upper"] = df["ma20"] + 2 * std20
    df["bb_lower"] = df["ma20"] - 2 * std20
    df["bb_width_20"] = (df["bb_upper"] - df["bb_lower"]) / df["ma20"]
    # share of the previous values in a 120-bar window that are strictly below today's width (vectorised:
    # rolling "min" rank − 1 counts the strictly smaller values; divide by the other values in the window)
    bw = df["bb_width_20"]
    roll = bw.rolling(120, min_periods=60)
    below = roll.rank(method="min") - 1
    others = roll.count() - 1
    df["bb_width_rank_120"] = (below / others.where(others > 0)).where(bw.notna())

    if benchmark is not None and not benchmark.empty:
        b = benchmark[["date", "close"]].copy()
        b["date"] = pd.to_datetime(b["date"])
        b = b.sort_values("date").drop_duplicates("date").rename(columns={"close": "bench_close"})
        df = df.merge(b, on="date", how="left")
        bc = df["bench_close"]  # no forward-fill: a missing benchmark day stays unknown
        for n in (20, 60):
            df[f"bench_ret_{n}"] = bc / bc.shift(n) - 1
            df[f"rel_ret_{n}"] = df[f"ret_{n}"] - df[f"bench_ret_{n}"]
    else:
        for n in (20, 60):
            df[f"bench_ret_{n}"] = np.nan
            df[f"rel_ret_{n}"] = np.nan
    return df


def screen_candidates(frame: pd.DataFrame, volume_multiple: float = 3.0, return_min: float = -0.03,
                      return_max: float = 0.03, min_turnover_multiple: float = 1.5,
                      require_positive_clv: bool = False) -> pd.DataFrame:
    """MVP screener (kept for backward compatibility). New code uses app.scanner."""
    from app.scanner import ScanConfig, run_scan
    cfg = ScanConfig(volume_multiple=volume_multiple, return_min=return_min, return_max=return_max,
                     turnover_multiple=min_turnover_multiple, require_positive_clv=require_positive_clv)
    rows = run_scan(frame, cfg)["rows"]
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)
