import unittest
import numpy as np
import pandas as pd
from app.indicators import compute_indicators, screen_candidates
from app.backtest import backtest_symbol


def sample_bars(n=140, vol_burst_at=100):
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = np.full(n, 100.0)
    # deterministic slight drift
    close += np.arange(n) * 0.01
    open_ = close.copy()
    high = close + 1.0
    low = close - 1.0
    volume = np.full(n, 1000.0)
    if vol_burst_at + 20 <= n:
        volume[vol_burst_at:vol_burst_at + 20] = 3000
    turnover = close * volume
    return pd.DataFrame({"market":"KR", "symbol":"TEST01", "date":dates, "open":open_, "high":high, "low":low,
                         "close":close, "volume":volume, "turnover":turnover, "source":"test"})


class IndicatorTests(unittest.TestCase):
    def test_volume_ratio_compares_latest_20_to_previous_20(self):
        df = sample_bars(n=140, vol_burst_at=120)
        out = compute_indicators(df)
        self.assertAlmostEqual(out.iloc[-1]["volume_ratio_20"], 3.0, places=6)

    def test_features_are_sorted_by_date(self):
        df = sample_bars().sample(frac=1, random_state=4)
        out = compute_indicators(df)
        self.assertTrue(out["date"].is_monotonic_increasing)

    def test_screen_candidate_requires_volume_and_return_range(self):
        df = sample_bars(n=160, vol_burst_at=140)
        result = screen_candidates(df, volume_multiple=2.5, return_min=-0.03, return_max=0.03, min_turnover_multiple=1.5)
        self.assertIsInstance(result, pd.DataFrame)
        if not result.empty:
            self.assertGreaterEqual(result.iloc[0]["volume_ratio_20"], 2.5)
            self.assertLessEqual(result.iloc[0]["ret_20"], 0.03)
            self.assertGreaterEqual(result.iloc[0]["ret_20"], -0.03)

    def test_backtest_returns_metrics_structure(self):
        df = sample_bars(n=260, vol_burst_at=200)
        result = backtest_symbol(df, {"volume_multiple":2.0, "return_min":-0.03, "return_max":0.03,
                                      "turnover_multiple":1.0, "atr_stop":1.5, "reward_risk":2,
                                      "max_hold_days":10, "round_trip_cost_bps":40})
        self.assertIn("metrics", result)
        self.assertIn("win_rate", result["metrics"])
        self.assertGreaterEqual(result["metrics"]["trades"], 0)

if __name__ == "__main__":
    unittest.main()
