"""Required tests 8-9 and engine behaviour: next-open entry, same-bar stop/target, gaps, costs, limits."""
import numpy as np
import pandas as pd
import pytest

from app.backtesting.engine import ExitConfig, prepare, run_portfolio
from app.backtesting.events import simulate_trade
from app.backtesting.metrics import period_returns, trade_metrics, wilson_interval
from app.risk import CostModel, RiskConfig
from app.strategies.base import Strategy


class SignalOn(Strategy):
    """Test strategy: fires on the given positional rows only."""
    key = ""
    defaults = {"rows": ()}

    def _signal(self, x):
        s = pd.Series(False, index=x.index)
        for i in self.params["rows"]:
            s.iloc[i] = True
        return s


def _flat(bars, n=40, **kw):
    # constant ATR of 4: high = close+2, low = close-2
    close = np.full(n, 100.0)
    return bars(close, highs=np.full(n, 102.0), lows=np.full(n, 98.0), **kw)


def _run(df, rows, exit_cfg=None, costs=None, risk=None):
    prepared = prepare(df)
    return run_portfolio(prepared, SignalOn(rows=rows), exit_cfg or ExitConfig(), risk or RiskConfig(max_position_pct=1.0),
                         costs or CostModel.zero())


# 8. next trading day entry price
def test_entry_is_next_open_plus_slippage(bars):
    df = _flat(bars)
    df.loc[21, "open"] = 101.0
    df.loc[21, "high"] = 103.0
    res = _run(df, [20], costs=CostModel(commission_bps=0, sell_tax_bps=0, slippage_bps=100))
    t = res["trades"][0]
    assert t["signal_date"] == df.loc[20, "date"].strftime("%Y-%m-%d")
    assert t["entry_date"] == df.loc[21, "date"].strftime("%Y-%m-%d")
    assert t["entry"] == pytest.approx(101.0 * 1.01)


def test_no_entry_when_signal_on_last_bar(bars):
    df = _flat(bars, n=30)
    assert _run(df, [29])["trades"] == []


# 9. stop and target touched in the same bar -> stop
def test_same_bar_stop_and_target_is_stop(bars):
    df = _flat(bars)
    # entry at open 100 on row 21; stop = 100 - 1.5*4 = 94; 2R target = 112
    df.loc[22, ["open", "high", "low", "close"]] = [100.0, 113.0, 93.0, 100.0]
    res = _run(df, [20])
    t = res["trades"][0]
    assert t["final_reason"] == "STOP_BOTH_TOUCHED_CONSERVATIVE"
    assert t["exit"] == pytest.approx(94.0)
    single = simulate_trade(prepare(df)[("KR", "T001")], 20, ExitConfig(), CostModel.zero())
    assert single["reason"] == "STOP_BOTH_TOUCHED_CONSERVATIVE"


def test_gap_below_stop_fills_at_open(bars):
    df = _flat(bars)
    df.loc[22, ["open", "high", "low", "close"]] = [90.0, 91.0, 89.0, 90.0]
    t = _run(df, [20])["trades"][0]
    assert t["final_reason"] == "STOP_GAP" and t["exit"] == pytest.approx(90.0)


def test_target_hit_and_r_multiple(bars):
    df = _flat(bars)
    df.loc[23, ["open", "high", "low", "close"]] = [101.0, 112.5, 100.5, 111.0]
    t = _run(df, [20])["trades"][0]
    assert t["final_reason"] == "TARGET" and t["exit"] == pytest.approx(112.0)
    assert t["r_multiple"] == pytest.approx(2.0)


def test_time_exit_at_close(bars):
    df = _flat(bars)
    t = _run(df, [20], ExitConfig(max_hold_days=3))["trades"][0]
    assert t["final_reason"] == "TIME"
    assert t["exit_date"] == df.loc[23, "date"].strftime("%Y-%m-%d")


def test_costs_reduce_pnl_exactly(bars):
    df = _flat(bars)
    cm = CostModel(commission_bps=10, sell_tax_bps=20, slippage_bps=0)
    t = _run(df, [20], ExitConfig(max_hold_days=2), costs=cm)["trades"][0]
    qty = t["qty"]
    expected = -(100.0 * qty * 0.001) - (100.0 * qty * 0.003)  # flat price: pnl = minus costs
    assert t["pnl"] == pytest.approx(expected)


def test_partial_take_profit_then_breakeven(bars):
    df = _flat(bars)
    df.loc[22, ["open", "high", "low", "close"]] = [100.0, 109.5, 99.0, 108.0]   # 1.5R = 109
    df.loc[23, ["open", "high", "low", "close"]] = [100.5, 101.0, 99.5, 100.0]   # back to breakeven
    t = _run(df, [20], ExitConfig(target_r=None, partial_fraction=0.5, partial_r=1.5, max_hold_days=10))["trades"][0]
    assert t["partial"] is True
    assert "PARTIAL_TARGET" in t["reason"] and t["final_reason"] == "STOP_TRAIL_OR_BE"


def test_max_positions_and_cash_limit(bars):
    a, b = _flat(bars, symbol="A"), _flat(bars, symbol="B")
    res = run_portfolio(prepare(pd.concat([a, b])), SignalOn(rows=[20]), ExitConfig(), RiskConfig(max_positions=1, max_position_pct=1.0), CostModel.zero())
    assert len(res["trades"]) == 1 and res["skipped"].get("max_positions") == 1


def test_delisted_position_closed_at_last_bar(bars):
    a = _flat(bars, n=25, symbol="A")
    b = _flat(bars, n=40, symbol="B")
    inst = pd.DataFrame([{"market": "KR", "symbol": "A", "listed_date": None, "delisted_date": a["date"].iloc[-1].strftime("%Y-%m-%d"), "sector": None}])
    res = run_portfolio(prepare(pd.concat([a, b])), SignalOn(rows=[20]), ExitConfig(max_hold_days=30), RiskConfig(max_position_pct=0.4), CostModel.zero(), instruments=inst)
    reasons = {t["symbol"]: t["final_reason"] for t in res["trades"]}
    assert reasons["A"] == "DELISTED_LAST_CLOSE"


def test_metrics_uncertainty():
    lo, hi = wilson_interval(6, 10)
    assert 0.29 < lo < 0.32 and 0.82 < hi < 0.85
    m = trade_metrics([{"net_return": 0.1, "r_multiple": 1.0, "pnl": 10, "holding_days": 3, "costs": 0, "final_reason": "TARGET"}])
    assert m["sample_warning"] and m["profit_factor"] is None


def test_period_returns():
    eq = pd.DataFrame({"date": pd.to_datetime(["2025-01-30", "2025-01-31", "2025-02-27", "2025-02-28"]), "equity": [100.0, 110.0, 99.0, 121.0]})
    months = period_returns(eq, "M")
    assert months[0] == {"period": "2025-01", "return": pytest.approx(0.10)}
    assert months[1]["return"] == pytest.approx(0.10)


def test_drawdown_halt_pauses_then_resumes(bars):
    # a long fall stops new entries; after the pause the engine trades again (v0.2 halted forever)
    n = 140
    close = np.r_[np.full(30, 100.0), np.linspace(100, 60, 40), np.full(70, 60.0)]
    df = bars(close, highs=close + 2, lows=close - 2)
    rows = list(range(20, 135, 3))
    risk = RiskConfig(max_position_pct=1.0, risk_per_trade=0.05, max_drawdown_halt=0.05, drawdown_halt_days=5)
    res = run_portfolio(prepare(df), SignalOn(rows=rows), ExitConfig(max_hold_days=2), risk, CostModel.zero())
    assert res["drawdown_halts"] >= 1 and res["skipped"].get("blocked_drawdown_halt", 0) > 0
    last_entry = max(pd.Timestamp(t["entry_date"]) for t in res["trades"])
    assert last_entry > df["date"].iloc[100]  # trading resumed after the pause
    forever = run_portfolio(prepare(df), SignalOn(rows=rows), ExitConfig(max_hold_days=2),
                            RiskConfig(max_position_pct=1.0, risk_per_trade=0.05, max_drawdown_halt=0.05, drawdown_halt_days=0), CostModel.zero())
    assert len(forever["trades"]) < len(res["trades"])
