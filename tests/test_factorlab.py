"""Factor Lab: factor values, timing/leakage rules, quantiles, rank IC, costs, exclusions and the CLI.

All price series here are hand-built test fixtures (deterministic drifts); they say nothing about real markets.
"""
import json

import numpy as np
import pandas as pd
import pytest

from app.research.factors import (FactorLabConfig, _one_way_turnover, build_panel, compute_factor, evaluate_factor,
                                  forward_returns, period_labels, run_factorlab)

CAL = pd.bdate_range("2022-01-03", periods=400)


def bars(n_sym=10, drifts=None, cal=CAL, volume=1e6, price=10_000.0):
    """Each symbol grows by a constant daily drift; open = previous close (no gaps)."""
    drifts = drifts if drifts is not None else np.linspace(-0.002, 0.002, n_sym)
    rows = []
    for i, g in enumerate(drifts):
        close = price * np.cumprod(np.full(len(cal), 1 + g))
        opn = np.r_[price, close[:-1]]
        for d, o, c in zip(cal, opn, close):
            rows.append({"symbol": f"S{i:02d}", "date": d, "open": o, "high": max(o, c), "low": min(o, c), "close": c, "volume": volume})
    return pd.DataFrame(rows)


def cfg(**kw):
    base = dict(horizon=5, quantiles=5, min_cross_section=10, min_avg_turnover=0.0, min_price=0.0,
                periods={"dev": ("2022-01-01", "2022-12-31"), "validation": ("2023-01-01", "2023-12-31")}, embargo_days=10,
                factors={"mom_20": {"kind": "momentum", "lookback": 20, "skip": 0}})
    base.update(kw)
    return FactorLabConfig(**base)


def test_momentum_and_other_factor_values():
    b = bars(3)
    p = build_panel(b, CAL)
    m = compute_factor(p, {"kind": "momentum", "lookback": 20, "skip": 0})
    t = CAL[50]
    s = b[b.symbol == "S02"].set_index("date")["close"]
    assert m.loc[t, "S02"] == pytest.approx(s[t] / s[CAL[30]] - 1)
    assert m.iloc[:20].isna().all().all()                                   # not enough history -> NaN
    sk = compute_factor(p, {"kind": "momentum", "lookback": 20, "skip": 5})
    assert sk.loc[t, "S02"] == pytest.approx(s[CAL[45]] / s[CAL[25]] - 1)
    rv = compute_factor(p, {"kind": "reversal", "lookback": 5})
    assert rv.loc[t, "S02"] == pytest.approx(-(s[t] / s[CAL[45]] - 1))
    vol = compute_factor(p, {"kind": "volatility", "window": 20})
    assert vol.loc[t, "S01"] == pytest.approx(0.0, abs=1e-12)               # constant (zero) drift
    to = compute_factor(p, {"kind": "log_turnover", "window": 20})
    assert to.loc[t, "S01"] == pytest.approx(np.log(10_000 * 1e6))
    tr = compute_factor(p, {"kind": "turnover_ratio", "short": 5, "long": 60})
    assert tr.loc[CAL[100], "S01"] == pytest.approx(1.0)
    il = compute_factor(p, {"kind": "amihud", "window": 20})
    assert il.loc[t, "S01"] == pytest.approx(0.0, abs=1e-15)


def test_no_lookahead_factor_unchanged_by_future_bars():
    b = bars(10)
    t = CAL[200]
    p1 = build_panel(b, CAL)
    b2 = b.copy()
    fut = b2["date"] > t
    b2.loc[fut, ["open", "close", "volume"]] *= 3.7                           # rewrite the future
    p2 = build_panel(b2, CAL)
    for spec in cfg().factors.values():
        assert compute_factor(p1, spec).loc[t].equals(compute_factor(p2, spec).loc[t])
    for spec in ({"kind": "volatility", "window": 20}, {"kind": "turnover_ratio", "short": 5, "long": 60}, {"kind": "amihud", "window": 20}):
        pd.testing.assert_series_equal(compute_factor(p1, spec).loc[t], compute_factor(p2, spec).loc[t])


def test_forward_return_uses_next_open_not_close_t():
    b = bars(10)
    t, i = CAL[200], 200
    p = build_panel(b, CAL)
    f1 = forward_returns(p, 5).loc[t]
    b2 = b.copy()
    b2.loc[b2["date"] == t, ["open", "close"]] *= 1.5                        # change bar t itself
    f2 = forward_returns(build_panel(b2, CAL), 5).loc[t]
    pd.testing.assert_series_equal(f1, f2)
    s = b[b.symbol == "S03"].set_index("date")["open"]
    assert f1["S03"] == pytest.approx(s[CAL[i + 6]] / s[CAL[i + 1]] - 1)
    b3 = b.copy()
    b3.loc[b3["date"] == CAL[i + 1], "open"] *= 0.5                           # entry bar does matter
    assert forward_returns(build_panel(b3, CAL), 5).loc[t, "S03"] != pytest.approx(f1["S03"])


def test_missing_entry_or_exit_bar_gives_nan_and_is_counted():
    b = bars(12)
    i = 100
    b = b[~((b.symbol == "S00") & (b.date == CAL[i + 1]))]                    # halt on the entry day
    b = b[~((b.symbol == "S01") & (b.date > CAL[i + 3]))]                     # data stops (delisting) before exit
    p = build_panel(b, CAL)
    f = forward_returns(p, 5)
    assert np.isnan(f.loc[CAL[i], "S00"]) and np.isnan(f.loc[CAL[i], "S01"]) and np.isfinite(f.loc[CAL[i], "S02"])
    c = cfg(min_cross_section=5)
    df, _, cnt = evaluate_factor("mom_20", p, c, f)
    assert cnt["names_forward_missing"] > 0
    assert set(df["n"]) <= set(range(5, 13))


def test_perfect_ranking_quantiles_rank_ic_and_costs_counted_once():
    b = bars(10)
    c = cfg()
    p = build_panel(b, CAL)
    df, qd, cnt = evaluate_factor("mom_20", p, c, forward_returns(p, 5))
    assert len(df) > 10 and df["rank_ic"].min() == pytest.approx(1.0)
    assert (df["q5"] > df["q4"]).all() and (df["q2"] > df["q1"]).all() and (df["ls_gross"] > 0).all()
    assert set(qd["n"]) == {2}
    # same names every date -> turnover 0 after formation; costs = buy once + sell once per period block, nothing else
    for per, g in df.groupby("period"):
        g = g.sort_values("date")
        assert g["turnover_top"].iloc[0] == 1.0 and (g["turnover_top"].iloc[1:] == 0).all()
        assert g["cost_top"].sum() == pytest.approx(c.buy_rate() + c.sell_rate())
        assert g["cost_bottom"].sum() == pytest.approx(c.buy_rate() + c.sell_rate())
    assert c.buy_rate() + c.sell_rate() == pytest.approx(43 / 1e4)            # 1.5+10 per side + 20 tax = 43bp round trip
    assert (df["top_net"] == df["top"] - df["cost_top"]).all()
    assert np.allclose(df["ls_net"], df["ls_gross"] - df["cost_top"] - df["cost_bottom"])


def test_reversed_ranking_has_negative_rank_ic():
    b = bars(10, drifts=np.linspace(0.002, -0.002, 10))
    p = build_panel(b, CAL)
    df, _, _ = evaluate_factor("rev_5", p, cfg(factors={"rev_5": {"kind": "reversal", "lookback": 5}}), forward_returns(p, 5))
    assert df["rank_ic"].max() == pytest.approx(-1.0)                        # high recent return persists here


def test_turnover_formula():
    assert _one_way_turnover(None, {"a"}) == 1.0
    assert _one_way_turnover({"a", "b"}, {"a", "b"}) == 0.0
    assert _one_way_turnover({"a", "b"}, {"b", "c"}) == pytest.approx(0.5)
    assert _one_way_turnover({"a", "b"}, {"c", "d"}) == pytest.approx(1.0)


def test_duplicates_offcalendar_and_bad_prices_counted():
    b = bars(3)
    extra = b.iloc[[5]].copy()
    conflict = b.iloc[[7]].copy(); conflict["close"] *= 1.1
    off = b.iloc[[9]].copy(); off["date"] = pd.Timestamp("2022-01-08").as_unit("ns")     # a Saturday, not in the calendar
    bad = b.iloc[[11]].copy(); bad["date"] = CAL[-1]; bad["symbol"] = "S99"; bad["open"] = 0
    p = build_panel(pd.concat([b, extra, conflict, off, bad]), CAL)
    c = p.counts
    assert c["duplicate_rows_identical_dropped"] == 1 and c["duplicate_rows_conflicting_dropped"] == 2
    assert c["off_calendar_rows_dropped"] == 1 and c["nonpositive_price_rows_dropped"] == 1
    assert np.isnan(p.close.loc[b.iloc[7]["date"], b.iloc[7]["symbol"]])     # conflicting day is a gap, not a guess


def test_small_cross_section_yields_no_dates():
    b = bars(6)
    p = build_panel(b, CAL)
    df, _, cnt = evaluate_factor("mom_20", p, cfg(min_cross_section=10), forward_returns(p, 5))
    assert df.empty and cnt["dates_too_small"] > 0
    res = run_factorlab(b, CAL, cfg(min_cross_section=10))
    assert res["summary"].empty


def test_period_labels_and_embargo():
    c = cfg()
    d = pd.DatetimeIndex(["2022-12-10", "2022-12-21", "2023-01-02"])
    ex = pd.DatetimeIndex(["2022-12-19", "2023-01-02", "2023-01-09"])
    lab = period_labels(d, ex, c)
    assert list(lab) == ["dev", "", "validation"]                            # 12-21 is inside the embargo
    with pytest.raises(ValueError, match="embargo"):
        cfg(horizon=10, embargo_days=10).validate()
    with pytest.raises(ValueError, match="overlap"):
        cfg(periods={"dev": ("2022-01-01", "2022-12-31"), "validation": ("2022-06-01", "2023-12-31")}).validate()
    with pytest.raises(ValueError):
        cfg(factors={"x": {"kind": "astrology"}}).validate()


def test_volume_level_break_masks_volume_factor_only():
    b = bars(12)
    b["quality_flags"] = ""
    i = 150
    b.loc[(b.symbol == "S05") & (b.date == CAL[i]), "quality_flags"] = "volume_level_break"
    p = build_panel(b, CAL)
    f = forward_returns(p, 5)
    c = cfg(min_cross_section=5, factors={"mom_20": {"kind": "momentum", "lookback": 20}, "turnover_20": {"kind": "log_turnover", "window": 20}})
    _, _, cm = evaluate_factor("mom_20", p, c, f)
    _, _, cv = evaluate_factor("turnover_20", p, c, f)
    assert cm["names_volume_contaminated"] == 0 and cv["names_volume_contaminated"] > 0


def test_audit_untrusted_bar_excludes_lookback_and_holding(tmp_path):
    b = bars(12)
    d = tmp_path / "audit"; d.mkdir()
    pd.DataFrame({"symbol": [f"S{i:02d}" for i in range(12)], "audit_status": ["exclude"] + ["ok"] * 11}).to_csv(d / "symbol_summary.csv", index=False)
    pd.DataFrame({"symbol": ["S03"], "date": [CAL[150].date()], "recommendation": ["exclude_until_verified"]}).to_csv(d / "findings.csv", index=False)
    (d / "audit_summary.json").write_text(json.dumps({"usable_fingerprint": "x"}))
    res = run_factorlab(b, CAL, cfg(min_cross_section=5, audit_dir=str(d), audit_lookback_bars=30))
    assert res["panel_counts"]["audit_symbols_dropped"] == 1
    assert res["exclusions"]["mom_20"]["names_audit_excluded"] > 0
    used = res["by_date"]
    # holding window reaches the bar (t in 144..149) or the 30-bar lookback contains it (t in 150..179: 30 bars incl. t)
    near = used[(used["date"] >= CAL[144]) & (used["date"] <= CAL[179])]
    far = used[(used["date"] < CAL[144]) | (used["date"] > CAL[179])]
    assert len(near) and (near["n"] == 10).all() and (far["n"] == 11).all()  # S00 dropped everywhere, S03 only near


def _standard_dir(path):
    from tests.test_research import _write_standard_dir
    _write_standard_dir(path)


def test_cli_end_to_end_with_ledger(tmp_path):
    from app.factorlab.__main__ import main
    from app.research.ledger import Ledger, verify
    from tests.test_ledger import spec
    data = tmp_path / "data"; data.mkdir()
    _standard_dir(data)
    L = tmp_path / "ledger.jsonl"
    Ledger(L).register(spec(eid="EXP-fl-test", family="momentum"))
    out = tmp_path / "out"
    args = ["run", "--data", str(data), "--format", "standard", "--out", str(out), "--start", "2016-01-01", "--factors", "mom_20,rev_5",
            "--horizon", "5", "--quantiles", "2", "--min-names", "4", "--min-turnover", "0", "--ledger", str(L), "--ledger-id", "EXP-fl-test", "--run-id", "r1"]
    assert main(args) == 0
    for f in ("factor_ic_by_date.csv", "factor_quantiles.csv", "factor_summary.csv", "factor_yearly.csv", "run_manifest.json", "REPORT.md"):
        assert (out / f).exists(), f
    m = json.loads((out / "run_manifest.json").read_text())
    assert m["kind"] == "factorlab" and m["experiments_total"] == 2 and m["ledger_experiment"]["id"] == "EXP-fl-test"
    assert m["cost_assumptions"]["round_trip_bps"] == pytest.approx(43.0) and m["data_fingerprint"]
    assert {o["path"] for o in m["outputs"]} == {"factor_ic_by_date.csv", "factor_quantiles.csv", "factor_summary.csv", "factor_yearly.csv"}
    assert "Alpha 검증 결과가 아닙니다" in (out / "REPORT.md").read_text()
    st = Ledger(L).experiment("EXP-fl-test")
    assert st["runs"]["r1"]["status"] == "success" and st["manifests"][0]["facts"]["data_fingerprint"] == m["data_fingerprint"]
    assert len(st["artifacts"]) == 5 and verify(L).ok
    assert main(args) == 2                                                   # non-empty out folder refused
    # a failing run is recorded as a failure (empty data folder)
    empty = tmp_path / "empty"; empty.mkdir()
    (empty / "index_KOSPI.csv").write_text("date,close\n")
    a2 = [x if x != str(data) else str(empty) for x in args]
    a2[a2.index(str(out))] = str(tmp_path / "out2"); a2[a2.index("r1")] = "r2"
    assert main(a2) == 1
    st = Ledger(L).experiment("EXP-fl-test")
    assert st["runs"]["r2"]["status"] == "failure" and st["runs"]["r2"]["error"]
    assert main(a2[:-4] + ["--ledger", str(L), "--ledger-id", "EXP-unregistered"]) == 2


def test_cli_rejects_unknown_factor(tmp_path, capsys):
    from app.factorlab.__main__ import main
    assert main(["run", "--data", str(tmp_path), "--out", str(tmp_path / "o"), "--factors", "magic"]) == 2
    assert main(["factors"]) == 0
