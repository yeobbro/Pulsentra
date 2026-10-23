"""v0.3 research layer: batch simulator equivalence, provenance separation, corporate-action checks, pipeline."""
import itertools

import numpy as np
import pandas as pd
import pytest

from app.backtesting.batch import REASONS, simulate_batch
from app.backtesting.engine import ExitConfig, prepare
from app.backtesting.events import simulate_trade
from app.demo import make_demo_data
from app.risk import CostModel

EXITS = [
    {"stop_type": "atr", "atr_mult": 1.5, "target_r": 2.0, "max_hold_days": 10},
    {"stop_type": "atr", "atr_mult": 2.0, "target_r": 1.5, "max_hold_days": 5},
    {"stop_type": "percent", "stop_pct": 0.07, "target_r": 3.0, "max_hold_days": 20},
    {"stop_type": "swing_low", "target_r": 2.0, "max_hold_days": 10},
    {"stop_type": "atr", "atr_mult": 1.5, "target_r": None, "partial_fraction": 0.5, "partial_r": 1.5, "trail_atr_mult": 2.0, "max_hold_days": 20},
    {"stop_type": "atr", "atr_mult": 1.5, "target_r": None, "ma_exit": "ma20", "max_hold_days": 20},
]


@pytest.fixture(scope="module")
def prepared():
    return prepare(make_demo_data(days=500, n_symbols=4, end="2026-06-30", seed=3))


@pytest.mark.parametrize("cfg", EXITS)
def test_batch_matches_scalar_simulator(prepared, cfg):
    ec = ExitConfig.from_dict(cfg)
    costs = CostModel(commission_bps=1.5, sell_tax_bps=20, slippage_bps=10)
    checked = 0
    for key, x in prepared.items():
        arr = {c: x[c].to_numpy(float) for c in ("open", "high", "low", "close", "atr_14", "swing_low_10", "ma20", "ma60")}
        idx = np.arange(20, len(x), 7)  # many entries, including ones whose data ends early
        res = simulate_batch(arr, idx, ec, costs)
        for j, i in enumerate(idx):
            s = simulate_trade(x, int(i), ec, costs)
            if s is None:
                assert np.isnan(res["net_return"][j])
            else:
                assert res["net_return"][j] == pytest.approx(s["net_return"], rel=1e-9, abs=1e-12)
                assert REASONS[res["reason"][j]] == s["reason"]
                checked += 1
    assert checked > 50


def test_rounding_repair_only_for_adjusted_small_errors(bars):
    from app.data.quality import check_frame
    df = bars(np.full(6, 32160.0))
    df.loc[2, ["open", "high", "low", "close"]] = [31860, 32159, 31800, 32160]   # off by 1 KRW
    df.loc[4, ["open", "high", "low", "close"]] = [31860, 30000, 29000, 32160]   # real error
    raw = check_frame(df)                                    # not marked adjusted -> both rejected
    assert len(raw.rejected) == 2
    df["adjusted"] = True
    rep = check_frame(df)
    assert len(rep.rejected) == 1
    row = rep.valid[rep.valid["date"] == df.loc[2, "date"]].iloc[0]
    assert row["high"] == 32160 and "ohlc_rounding_repaired" in row["quality_flags"]


def test_volume_level_break_detected_across_gap(bars):
    from app.data.quality import check_frame
    n = 160
    close = np.full(n, 100.0)
    vol = np.r_[np.full(80, 1000.0), np.full(80, 50000.0)]   # x50 volume after a split, price already adjusted
    df = bars(close, volume=vol)
    df = df.drop(index=[80, 81, 82]).reset_index(drop=True)   # 3-session suspension
    df.loc[80:, "date"] = df.loc[80:, "date"]
    rep = check_frame(df)
    assert any(i["check"] == "volume_level_break" for i in rep.issues)
    no_gap = check_frame(bars(close, volume=vol))             # same jump without a suspension: not flagged
    assert not any(i["check"] == "volume_level_break" for i in no_gap.issues)


def test_real_and_synthetic_never_share_a_series(tmp_db, bars):
    from app.data.ingest import ingest_frame
    df = bars(np.full(30, 100.0), symbol="005930", market="KR")
    ingest_frame(df.drop(columns=["source"]), "csv_import", db_path=tmp_db)
    with pytest.raises(ValueError):
        ingest_frame(df.drop(columns=["source"]), "synthetic_demo", db_path=tmp_db)
    from app import db
    types = db.fetch_all("SELECT DISTINCT source_type FROM candles", (), tmp_db)
    assert types == [{"source_type": "REAL"}]


def _write_standard_dir(path, source="csv_import"):
    """Pipeline plumbing test only: demo-generator bars written as a CSV folder. The numbers mean nothing."""
    df = make_demo_data(days=900, n_symbols=5, end="2025-12-31", seed=9, include_index=True)
    idx = df[df["symbol"] == "DEMOIDX"][["date", "close"]]
    idx.to_csv(path / "index_KOSPI.csv", index=False)
    for sym, g in df[df["symbol"] != "DEMOIDX"].groupby("symbol"):
        g = g.drop(columns=["source"]).assign(market="KR", source=source)
        g.to_csv(path / f"{sym}.csv", index=False)


def test_standard_loader_refuses_synthetic(tmp_path):
    from app.research.data import load_standard_csv
    _write_standard_dir(tmp_path, source="synthetic_demo")
    with pytest.raises(ValueError):
        load_standard_csv(tmp_path)


def test_research_pipeline_end_to_end(tmp_path):
    from app.research.__main__ import main
    data = tmp_path / "data"
    data.mkdir()
    _write_standard_dir(data)
    out = tmp_path / "out"
    import app.research.study as st
    orig = st.StudyConfig

    def small():
        c = orig()
        c.periods = {"dev": ("2022-01-01", "2023-12-31"), "validation": ("2024-01-01", "2024-12-31"), "test": ("2025-01-01", "2099-12-31")}
        c.stops = {"ATR1.5": {"stop_type": "atr", "atr_mult": 1.5}}
        c.targets = {"2R": {"target_r": 2.0}}
        c.holds = (5, 10)
        c.min_avg_turnover = 0
        c.min_dev_trades = 1
        c.random_pool = 2000
        c.random_draws = 50
        c.portfolio_random_seeds = 1
        return c
    st.StudyConfig = small
    import app.research.__main__ as cli
    cli.StudyConfig = small
    try:
        assert main(["--data", str(data), "--format", "standard", "--out", str(out), "--start", "2022-01-01"]) == 0
    finally:
        st.StudyConfig = orig
        cli.StudyConfig = orig
    for f in ("REPORT.md", "strategy_exit_grid.csv", "portfolio_results.csv", "walk_forward_by_year.csv", "vs_random_entry.csv",
              "run_manifest.json", "data_quality_summary.csv", "ml_vs_rule.csv"):
        assert (out / f).exists(), f
    grid = pd.read_csv(out / "strategy_exit_grid.csv")
    assert set(grid["exit"]) == {"ATR1.5|2R|5d", "ATR1.5|2R|10d"}
    trades = list((out / "trades").glob("portfolio_*.csv"))
    assert trades
    t = pd.read_csv(trades[0]) if trades[0].stat().st_size > 5 else pd.DataFrame()
    if len(t):
        assert (pd.to_datetime(t["entry_date"]) > pd.to_datetime(t["signal_date"])).all()  # entry strictly after the signal
