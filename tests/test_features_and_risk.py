"""Required tests 1-7: indicator arithmetic, stops, targets, sizing, costs."""
import math

import numpy as np
import pandas as pd
import pytest

from app.indicators import compute_indicators
from app.risk import CostModel, RiskConfig, atr_stop, percent_stop, position_size, r_target, swing_low_stop
from app.scanner import ScanConfig, run_scan


# 1. volume multiple
def test_volume_ratio_exact(bars):
    vol = np.r_[np.full(40, 1000.0), np.full(20, 1000.0), np.full(20, 3500.0)]
    x = compute_indicators(bars(np.full(80, 100.0), volume=vol))
    last = x.iloc[-1]
    assert last["volume_20_sum"] == 70_000
    assert last["volume_prev20_sum"] == 20_000
    assert last["volume_ratio_20"] == pytest.approx(3.5)


def test_volume_ratio_unknown_with_short_history(bars):
    x = compute_indicators(bars(np.full(39, 100.0)))
    assert math.isnan(x.iloc[-1]["volume_ratio_20"])  # needs 40 bars: unknown, never 0


# 2. 20-session price change
def test_ret_20(bars):
    close = np.r_[np.full(10, 100.0), np.linspace(100, 110, 21)]
    x = compute_indicators(bars(close))
    assert x.iloc[-1]["ret_20"] == pytest.approx(110 / 100 - 1)


# 3. turnover multiple
def test_turnover_ratio(bars):
    close = np.r_[np.full(20, 100.0), np.full(20, 200.0)]
    df = bars(close, volume=np.full(40, 10.0))
    x = compute_indicators(df)
    assert x.iloc[-1]["turnover_ratio_20"] == pytest.approx(2.0)  # same volume, price doubled
    assert x.iloc[-1]["volume_ratio_20"] == pytest.approx(1.0)


# 4. ATR and stop price
def test_atr_true_range_and_stop(bars):
    close = np.full(30, 100.0)
    df = bars(close, highs=np.full(30, 102.0), lows=np.full(30, 98.0))
    x = compute_indicators(df)
    assert x.iloc[-1]["atr_14"] == pytest.approx(4.0)
    assert atr_stop(101.0, 4.0, 1.5) == pytest.approx(95.0)
    assert percent_stop(100.0, 0.07) == pytest.approx(93.0)
    assert swing_low_stop(100.0, 96.0) == 96.0
    assert swing_low_stop(100.0, 101.0) is None  # swing low above entry is not a valid stop
    with pytest.raises(ValueError):
        atr_stop(100.0, 0.0)


def test_true_range_uses_previous_close_gap(bars):
    close = np.r_[np.full(14, 100.0), 120.0]
    opens = np.r_[np.full(14, 100.0), 119.0]
    df = bars(close, opens=opens, highs=np.r_[np.full(14, 101.0), 121.0], lows=np.r_[np.full(14, 99.0), 118.0])
    x = compute_indicators(df)
    assert x.iloc[-1]["true_range"] == pytest.approx(21.0)  # high - previous close, not high - low


# 5. R-multiple targets
@pytest.mark.parametrize("r,expected", [(1.5, 115.0), (2.0, 120.0), (3.0, 130.0)])
def test_r_targets(r, expected):
    assert r_target(100.0, 90.0, r) == pytest.approx(expected)


def test_r_target_rejects_stop_above_entry():
    with pytest.raises(ValueError):
        r_target(100.0, 101.0, 2.0)


# 6. position size
def test_position_size_by_risk():
    # 1% of 100,000,000 = 1,000,000 risk; 5 KRW per share -> 200,000 shares, capped by 20% notional (20,000,000 / 100)
    assert position_size(100_000_000, 0.01, 100.0, 95.0, max_position_pct=1.0) == 200_000
    assert position_size(100_000_000, 0.01, 100.0, 95.0, max_position_pct=0.1) == 100_000  # notional cap 10,000,000 / 100
    assert position_size(10_000_000, 0.01, 50_000.0, 48_000.0, max_position_pct=1.0) == 50
    assert position_size(10_000_000, 0.01, 50_000.0, 48_000.0, max_position_pct=1.0, lot_size=10) == 50
    assert position_size(10_000_000, 0.01, 50_000.0, 48_000.0, max_position_pct=0.1) == 20  # 1,000,000 / 50,000
    assert position_size(10_000_000, 0.01, 50_000.0, 48_000.0, available_cash=120_000) == 2
    assert position_size(10_000_000, 0.01, 50_000.0, 50_000.0) == 0


def test_risk_config_validation():
    with pytest.raises(ValueError):
        RiskConfig.from_dict({"risk_per_trade": 0.5})


# 7. costs and slippage
def test_cost_model_math():
    c = CostModel(commission_bps=10, sell_tax_bps=20, slippage_bps=50)
    assert c.buy_fill(100.0) == pytest.approx(100.5)
    assert c.sell_fill(100.0) == pytest.approx(99.5)
    assert c.sell_fill(100.0, market_order=False) == pytest.approx(100.0)  # resting limit at the target
    assert c.buy_cost(1_000_000) == pytest.approx(1_000)
    assert c.sell_cost(1_000_000) == pytest.approx(3_000)
    assert CostModel.zero().buy_fill(100.0) == 100.0


# Scanner: unknown inputs are reported, never treated as 0 or as passing
def test_scanner_unknown_not_zero(bars):
    df = bars(np.full(45, 100.0), volume=np.r_[np.full(25, 1000.0), np.full(20, 4000.0)])
    res = run_scan(df, ScanConfig(volume_multiple=1.0, turnover_multiple=0.0), include_all=True)
    row = res["rows"][0]
    assert row["ma60"] is None and "ma60" in row["unknown"]
    assert row["rel_ret_20"] is None and "rel_ret_20" in row["unknown"]


def test_scanner_filters_and_explains(bars):
    quiet = bars(np.full(80, 100.0), symbol="A", volume=np.r_[np.full(60, 1000.0), np.full(20, 4000.0)])
    noisy = bars(np.r_[np.full(60, 100.0), np.linspace(100, 130, 20)], symbol="B", volume=np.r_[np.full(60, 1000.0), np.full(20, 4000.0)])
    res = run_scan(pd.concat([quiet, noisy]), ScanConfig())
    syms = [r["symbol"] for r in res["rows"]]
    assert syms == ["A"]  # B rose 30% in 20 sessions: outside -3%..+3%
    assert any("4.00배" in p for p in res["rows"][0]["positives"])
