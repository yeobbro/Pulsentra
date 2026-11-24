"""PBO (CSCV) and Deflated Sharpe Ratio: properties on constructed return matrices, and their use in the gate."""
import math

import numpy as np
import pandas as pd
import pytest

from app.research.overfitting import (_block_edges, choose_splits, deflated_sharpe, expected_max_sharpe, pbo_cscv,
                                      sharpe)
from app.research.robustness import FAIL, INSUFFICIENT, PASS, evaluate
from tests.test_robustness import by_name, make_run


def noise(t, n, seed=0, scale=0.02):
    return pd.DataFrame(np.random.default_rng(seed).normal(0, scale, (t, n)), columns=[f"c{i}" for i in range(n)])


# ---------- building blocks

def test_blocks_cover_every_row_once_and_splits_shrink_to_fit():
    for t, s in [(96, 16), (100, 16), (17, 4)]:
        e = _block_edges(t, s)
        assert e[0][0] == 0 and e[-1][1] == t and all(a1 == b0 for (_, b0), (a1, _) in zip(e, e[1:]))
        assert max(b - a for a, b in e) - min(b - a for a, b in e) <= 1
    assert choose_splits(96) == 16 and choose_splits(20) == 10 and choose_splits(9) == 4 and choose_splits(7) is None
    assert choose_splits(100, wanted=15) == 14          # always even


def test_expected_max_sharpe_grows_with_trials_and_dispersion():
    assert expected_max_sharpe(1, 1.0) == 0.0 and expected_max_sharpe(50, 0.0) == 0.0
    vals = [expected_max_sharpe(n, 0.01) for n in (2, 10, 100, 1000)]
    assert all(a < b for a, b in zip(vals, vals[1:]))
    assert expected_max_sharpe(100, 0.04) == pytest.approx(2 * expected_max_sharpe(100, 0.01))
    # reference value of the formula's constant for N=1000, V=1 (about 3.26 standard deviations)
    # hand value: (1 - 0.5772) * Z(0.999) + 0.5772 * Z(1 - 1/(1000 e)) = 0.4228 * 3.0902 + 0.5772 * 3.3756
    assert expected_max_sharpe(1000, 1.0) == pytest.approx(0.4228 * 3.0902 + 0.5772 * 3.3756, abs=2e-3)


def test_dsr_with_one_trial_is_the_probabilistic_sharpe_ratio():
    x = np.random.default_rng(1).normal(0.002, 0.02, 500)
    d = deflated_sharpe(x, 1, 0.5)
    assert d["sr0_expected_max"] == 0.0
    z = (x - x.mean()) / x.std()
    sr = x.mean() / x.std(ddof=1)
    den = 1 - np.mean(z ** 3) * sr + (np.mean(z ** 4) - 1) / 4 * sr ** 2
    assert d["dsr"] == pytest.approx(0.5 * (1 + math.erf(sr * math.sqrt(499) / math.sqrt(den) / math.sqrt(2))))


def test_dsr_falls_as_more_trials_are_charged():
    x = np.random.default_rng(2).normal(0.003, 0.02, 120)
    d = [deflated_sharpe(x, n, 0.01)["dsr"] for n in (1, 10, 100, 1000)]
    assert all(a > b for a, b in zip(d, d[1:])) and d[0] > 0.9 and d[-1] < 0.5


def test_dsr_refuses_what_it_cannot_compute():
    assert deflated_sharpe([0.01, 0.01, 0.01, 0.01], 5, 0.1)["reason"]
    assert deflated_sharpe([0.01, 0.02], 5, 0.1)["reason"]
    assert math.isnan(sharpe([1.0])) and math.isnan(sharpe([2.0, 2.0]))


# ---------- PBO

def test_pbo_of_pure_noise_is_near_one_half():
    r = pbo_cscv(noise(240, 20, seed=3))
    assert r["n_splits"] == 16 and r["n_combinations"] == 12870
    assert 0.3 < r["pbo"] < 0.7 and r["reason"] is None


def test_pbo_is_low_when_one_trial_really_is_better():
    m = noise(240, 10, seed=4)
    m["c0"] += 0.01                                       # half a standard deviation every date
    r = pbo_cscv(m)
    assert r["pbo"] < 0.05 and r["is_winner_share"].get("c0", 0) > 0.95 and r["prob_oos_loss"] < 0.05


def test_pbo_is_high_when_in_sample_winners_lose_out_of_sample():
    # every trial is +a in some blocks and -a in the others (zero overall): the trial that fits the IS blocks
    # best is, by construction, the one that is worst on the complementary OOS blocks
    rng = np.random.default_rng(5)
    s, per = 16, 6
    signs = rng.choice([-1.0, 1.0], size=(s, 30))
    signs = np.vstack([signs[: s // 2], -signs[: s // 2]])  # balanced: each trial sums to zero over blocks
    rng.shuffle(signs)
    m = pd.DataFrame(np.repeat(signs, per, axis=0) * 0.01 + rng.normal(0, 0.002, (s * per, 30)))
    r = pbo_cscv(m, n_splits=s)
    assert r["pbo"] > 0.8 and r["degradation_slope"] < 0 and r["prob_oos_loss"] > 0.8


def test_identical_trials_count_as_overfit():
    one = noise(96, 1, seed=6)["c0"]
    r = pbo_cscv(pd.DataFrame({"a": one, "b": one, "c": one, "d": one}))
    assert r["pbo"] == 1.0                                # winner sits exactly at the median: not better than it


def test_pbo_reports_why_it_cannot_run():
    assert "2 needed" in pbo_cscv(noise(96, 1))["reason"]
    m = noise(96, 4); m.iloc[3, 1] = np.nan
    assert "missing" in pbo_cscv(m)["reason"]
    assert "too few" in pbo_cscv(noise(7, 4))["reason"]


# ---------- gate

def test_gate_passes_selection_checks_when_the_factor_dominates(tmp_path):
    c = by_name(evaluate(make_run(tmp_path), "mom_60"))
    assert c["selection_overfitting"]["status"] == PASS and c["selection_overfitting"]["values"]["pbo"] == 0.0
    assert c["deflated_sharpe"]["status"] == PASS
    assert c["selection_overfitting"]["values"]["n_trials"] == 6          # 3 factors x 2 legs


def test_gate_fails_selection_when_no_trial_beats_another(tmp_path):
    res = evaluate(make_run(tmp_path, edge=(0.0, 0.0, 0.0)), "mom_60")     # every factor and leg pays the same
    c = by_name(res)["selection_overfitting"]
    assert c["status"] == FAIL and c["values"]["pbo"] == 1.0 and res["status"] == FAIL


def test_pbo_judges_the_selection_procedure_not_one_factor(tmp_path):
    # identical top legs: picking "a top leg" is not overfit even though the three factors cannot be told apart;
    # PBO answers for the procedure, the winner shares show which factor won
    c = by_name(evaluate(make_run(tmp_path, neighbour_edge=1.0), "mom_60"))["selection_overfitting"]
    assert c["status"] == PASS and set(c["values"]["is_winner_share"]) <= {"mom_20:top", "mom_60:top", "mom_120:top"}


def test_gate_deflates_by_the_number_of_experiments_run(tmp_path):
    lo = by_name(evaluate(make_run(tmp_path, name="a", total=3), "mom_60"))["deflated_sharpe"]
    hi = by_name(evaluate(make_run(tmp_path, name="b", total=100000), "mom_60"))["deflated_sharpe"]
    assert hi["values"]["n_trials"] == 100000 and hi["values"]["dsr"] < lo["values"]["dsr"]
    assert hi["status"] != PASS


def test_selection_checks_never_read_the_test_period(tmp_path):
    a = by_name(evaluate(make_run(tmp_path, name="a"), "mom_60"))
    b = by_name(evaluate(make_run(tmp_path, name="b", ric=(0.05, 0.05, -0.9), edge=(0.01, 0.01, -0.5)), "mom_60"))
    for k in ("selection_overfitting", "deflated_sharpe"):
        va, vb = a[k]["values"], b[k]["values"]
        assert {x: va[x] for x in va if x != "is_winner_share"} == {x: vb[x] for x in vb if x != "is_winner_share"}


def test_gate_selection_is_insufficient_without_enough_dates(tmp_path):
    d = make_run(tmp_path, ends=("2016-03-01", "2022-02-01", "2025-11-15"))
    c = by_name(evaluate(d, "mom_60"))
    assert c["selection_overfitting"]["status"] == INSUFFICIENT
