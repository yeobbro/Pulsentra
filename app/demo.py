"""Deterministic SYNTHETIC data for UI and tests — never real market data.

Every row is tagged `source = "synthetic_demo"` and market "DEMO". The generator plants volume
bursts with quiet prices, trends and drawdowns so every screen and strategy has something to
show; any performance measured on it says nothing about real markets.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEMO_SOURCE = "synthetic_demo"
DEMO_INDEX_SYMBOL = "DEMOIDX"


def make_demo_data(days: int = 420, n_symbols: int = 3, end: str | pd.Timestamp | None = None,
                   seed: int = 20261022, include_index: bool = False) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end_ts = pd.Timestamp(end).normalize() if end is not None else pd.Timestamp.today().normalize()
    dates = pd.bdate_range(end=end_ts, periods=days)
    market_ret = rng.normal(0.0003, 0.010, len(dates))
    # A synthetic market with a down leg and a sideways leg so regime tables are populated.
    if len(dates) > 300:
        market_ret[int(len(dates) * 0.35):int(len(dates) * 0.5)] -= 0.0025
        market_ret[int(len(dates) * 0.6):int(len(dates) * 0.7)] *= 0.3
    rows: list[dict] = []
    bases = [10000.0, 45000.0, 7800.0, 23000.0, 3100.0, 128000.0, 56000.0, 15400.0, 9200.0, 71000.0, 4400.0, 33000.0]
    for k in range(n_symbols):
        symbol = f"DEMO{k + 1:03d}"
        base = bases[k % len(bases)]
        beta = 0.6 + 0.1 * (k % 6)
        daily_ret = beta * market_ret + rng.normal(0.0002, 0.016, len(dates))
        volume = rng.lognormal(mean=12.4, sigma=0.40, size=len(dates))
        starts = sorted(rng.choice(np.arange(80, max(81, len(dates) - 60)), size=min(4, max(1, len(dates) // 120)), replace=False))
        for st in starts:
            # quiet price + heavy volume for 20 sessions, then a drift that may or may not follow
            daily_ret[st:st + 20] = rng.normal(0.0, 0.004, len(daily_ret[st:st + 20]))
            volume[st:st + 20] *= rng.uniform(3.0, 4.5)
            drift = rng.choice([0.006, 0.003, -0.002, -0.004])
            daily_ret[st + 20:st + 40] += drift
        daily_ret[-20:] = rng.normal(0.0001, 0.0012, 20)
        volume[-20:] *= 3.2
        close = base * np.cumprod(1 + daily_ret)
        open_ = np.r_[close[0], close[:-1] * (1 + rng.normal(0, 0.006, len(close) - 1))]
        high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.025, len(close)))
        low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.02, len(close)))
        for i, dt in enumerate(dates):
            rows.append({"market": "DEMO", "symbol": symbol, "date": dt, "open": round(float(open_[i]), 2),
                         "high": round(float(high[i]), 2), "low": round(float(low[i]), 2), "close": round(float(close[i]), 2),
                         "volume": int(volume[i]), "turnover": round(float(volume[i] * close[i]), 2),
                         "source": DEMO_SOURCE, "adjusted": True})
    if include_index:
        idx = 2500 * np.cumprod(1 + market_ret)
        for i, dt in enumerate(dates):
            o = idx[i - 1] if i else idx[0]
            rows.append({"market": "DEMO", "symbol": DEMO_INDEX_SYMBOL, "date": dt, "open": round(float(o), 2),
                         "high": round(float(max(o, idx[i]) * 1.004), 2), "low": round(float(min(o, idx[i]) * 0.996), 2),
                         "close": round(float(idx[i]), 2), "volume": 1_000_000, "turnover": round(float(idx[i]) * 1_000_000, 2), "source": DEMO_SOURCE, "adjusted": True})
    return pd.DataFrame(rows)


def demo_instruments(n_symbols: int = 3, include_index: bool = False) -> list[dict]:
    sectors = ["DEMO-소재", "DEMO-IT", "DEMO-산업재", "DEMO-소비재"]
    out = [{"market": "DEMO", "symbol": f"DEMO{k + 1:03d}", "name": f"데모 합성종목 {k + 1}", "sector": sectors[k % 4],
            "exchange": "DEMO", "listed_date": None, "delisted_date": None, "is_benchmark": False} for k in range(n_symbols)]
    if include_index:
        out.append({"market": "DEMO", "symbol": DEMO_INDEX_SYMBOL, "name": "데모 합성 지수", "sector": None, "exchange": "DEMO",
                    "listed_date": None, "delisted_date": None, "is_benchmark": True})
    return out
