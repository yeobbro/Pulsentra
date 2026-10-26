"""Strategy Library: catalog integrity, engine linkage, parameter validation, API, comparison and ledger records.

Backtests here run on the DEMO (synthetic) data set; the numbers only show that the plumbing works.
"""
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.backtesting.engine import ExitConfig, prepare, run_portfolio
from app.risk import CostModel, RiskConfig
from app.strategies import STRATEGY_REGISTRY, get_strategy
from app.strategies.catalog import (AVAILABLE, ENTRIES, FAMILIES, LIMITED, PLANNED, STATUSES, get_entry, search,
                                    validate_params)

REQUIRED_TEXT = ("id", "name_ko", "name_en", "family", "description_ko", "description_en", "hypothesis", "suitable_conditions",
                 "status", "status_reason")


# ------------------------------------------------------------------ catalog

def test_ids_unique_and_metadata_complete():
    ids = [e.id for e in ENTRIES]
    assert len(ids) == len(set(ids))
    for e in ENTRIES:
        for f in REQUIRED_TEXT:
            assert str(getattr(e, f)).strip(), (e.id, f)
        assert e.family in FAMILIES and e.status in STATUSES
        assert e.entry_rules and e.exit_rules and e.data_requirements and e.key_risks and e.tests and e.docs
        for p in e.params:
            assert p.type in ("int", "float", "bool") and p.description
            if p.type != "bool":
                assert p.min is not None and p.max is not None and p.min <= p.default <= p.max, (e.id, p.name)
        text = " ".join(str(v) for v in e.as_dict().values()).lower()
        assert "가장 인기" not in text and "수익률 상위" not in text and "most popular" not in text


def test_runnable_entries_are_linked_to_registered_strategies_with_same_defaults():
    linked = set()
    for e in ENTRIES:
        if e.status == PLANNED:
            assert not e.runnable and e.as_dict()["engine"] is None
            continue
        assert e.strategy_key in STRATEGY_REGISTRY, e.id
        cls = STRATEGY_REGISTRY[e.strategy_key]
        assert cls.available, e.id
        assert {p.name: p.default for p in e.params} == cls.defaults, e.id
        assert e.runnable and "run_portfolio" in e.as_dict()["engine"]
        if e.default_exit:
            ExitConfig.from_dict(e.default_exit)
        linked.add(e.strategy_key)
    # every available registered strategy is in the catalog (no hidden duplicates, nothing left out)
    assert linked == {k for k, c in STRATEGY_REGISTRY.items() if c.available}
    assert get_entry("disclosure_event").status == PLANNED and not STRATEGY_REGISTRY["disclosure_event"].available


def test_requested_candidates_are_covered_with_honest_status():
    status = {e.id: e.status for e in ENTRIES}
    for i in ("trend_ma_crossover", "momentum_120d_high", "donchian_breakout", "rsi_mean_reversion", "bollinger_mean_reversion",
              "volume_breakout", "bollinger_squeeze_breakout"):
        assert status[i] == AVAILABLE
    assert status["momentum_relative_strength"] == LIMITED and status["atr_volatility_breakout"] == LIMITED
    assert status["pairs_trading"] == PLANNED and status["value_quality"] == PLANNED


def test_search_and_filters():
    assert {e.id for e in search(family="mean_reversion")} == {"rsi_mean_reversion", "bollinger_mean_reversion"}
    assert [e.id for e in search(q="donchian")] == ["donchian_breakout"]
    assert {e.id for e in search(q="볼린저")} >= {"bollinger_mean_reversion", "bollinger_squeeze_breakout"}
    assert all(e.status == PLANNED for e in search(status=PLANNED))
    assert search(q="no-such-strategy-xyz") == []


@pytest.mark.parametrize("entry_id,params,msg", [
    ("trend_ma_crossover", {"fast": 60, "slow": 20}, "smaller"),
    ("trend_ma_crossover", {"fast": 1}, "outside"),
    ("trend_ma_crossover", {"fast": 10.5}, "integer"),
    ("trend_ma_crossover", {"fast": "10"}, "number"),
    ("trend_ma_crossover", {"fast": True}, "number"),
    ("trend_ma_crossover", {"nope": 1}, "unknown"),
    ("rsi_mean_reversion", {"require_above_ma120": 1}, "true or false"),
    ("rsi_mean_reversion", {"oversold": 60, "exit_rsi": 55}, "outside"),
    ("donchian_breakout", {"entry_n": 10, "exit_n": 20}, "smaller"),
    ("bollinger_mean_reversion", {"k": float("nan")}, "number"),
])
def test_invalid_parameters_rejected(entry_id, params, msg):
    with pytest.raises(ValueError, match=msg):
        validate_params(get_entry(entry_id), params)


def test_valid_parameters_merge_with_defaults():
    p = validate_params(get_entry("trend_ma_crossover"), {"fast": 10.0})
    assert p == {"fast": 10, "slow": 60} and isinstance(p["fast"], int)


# ------------------------------------------------------------------ new strategies and exit rules (no look-ahead)

def _frame(close, highs=None, lows=None):
    from tests.conftest import make_bars
    return make_bars(close, highs=highs, lows=lows)


def test_donchian_entry_uses_prior_channel_and_exit_rule():
    close = np.r_[np.full(30, 100.0), 103.0, 104.0, np.full(10, 104.0), 90.0]
    x = prepare(_frame(close, highs=np.r_[np.full(30, 101.0), 103.5, 104.5, np.full(10, 104.5), 91.0],
                       lows=np.r_[np.full(30, 99.0), 102.0, 103.0, np.full(10, 103.0), 89.0]))[("KR", "T001")]
    s = get_strategy("donchian_breakout", entry_n=20, exit_n=10)
    sig = s.signals(x)
    assert list(np.flatnonzero(sig)) == [30]                      # first close above the prior 20-day high only
    assert s.exit_signals(x).iloc[-1] and not s.exit_signals(x).iloc[35]


def test_bb_reversion_and_atr_breakout_signals():
    close = np.r_[np.full(25, 100.0) + np.tile([0.5, -0.5], 13)[:25], 90.0, 99.0, np.full(5, 100.0)]
    x = prepare(_frame(close))[("KR", "T001")]
    bb = get_strategy("bb_reversion")
    assert list(np.flatnonzero(bb.signals(x))) == [26]            # below the lower band at 25, back inside at 26
    assert bb.exit_signals(x).iloc[-1]
    close2 = np.r_[np.linspace(100, 101, 30), 115.0, np.full(5, 115.0)]
    x2 = prepare(_frame(close2))[("KR", "T001")]
    assert 30 in np.flatnonzero(get_strategy("atr_breakout", k=1.5).signals(x2))


def test_future_bars_do_not_change_new_signals():
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.02, 200))
    full = prepare(_frame(close))[("KR", "T001")]
    cut = prepare(_frame(close[:150]))[("KR", "T001")]
    for key, params in (("donchian_breakout", {}), ("bb_reversion", {}), ("atr_breakout", {}), ("ma_trend", {}), ("rsi_rebound", {})):
        s = get_strategy(key, **params)
        assert s.signals(full).iloc[:150].tolist() == s.signals(cut).tolist(), key
        if s.has_exit_rule:
            assert s.exit_signals(full).iloc[:150].tolist() == s.exit_signals(cut).tolist(), key


def test_strategy_exit_is_off_by_default_and_fills_next_open():
    close = np.r_[np.full(70, 100.0), np.linspace(100, 130, 30), np.linspace(130, 95, 30)]
    pr = prepare(_frame(close))
    s = get_strategy("ma_trend", fast=5, slow=20)
    risk, costs = RiskConfig(max_position_pct=1.0), CostModel.zero()
    base = run_portfolio(pr, s, ExitConfig(target_r=None, max_hold_days=200, atr_mult=10), risk, costs)
    own = run_portfolio(pr, s, ExitConfig(target_r=None, max_hold_days=200, atr_mult=10, strategy_exit=True), risk, costs)
    assert all(t["final_reason"] != "STRATEGY_EXIT" for t in base["trades"])
    ex = [t for t in own["trades"] if t["final_reason"] == "STRATEGY_EXIT"]
    assert ex
    x = pr[("KR", "T001")]
    fired = s.exit_signals(x)
    d = x["date"].dt.strftime("%Y-%m-%d").tolist()
    i = d.index(ex[0]["exit_date"])
    assert fired.iloc[i - 1] and ex[0]["exit"] == pytest.approx(x["open"].iloc[i])   # decided at close t-1, filled at open t


# ------------------------------------------------------------------ API, engine reuse, comparison, ledger

@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/demo/load").status_code == 200
        yield c


def test_list_detail_and_filters_api(client):
    r = client.get("/api/library/strategies").json()
    assert len(r["entries"]) == len(ENTRIES) and "순위가 아니며" in r["note"]
    assert client.get("/api/library/strategies?family=breakout").json()["entries"][0]["id"] == "donchian_breakout"
    assert client.get("/api/library/strategies?family=zzz").status_code == 400
    assert client.get("/api/library/strategies?status=WHATEVER").status_code == 400
    d = client.get("/api/library/strategies/pairs_trading").json()
    assert d["runnable"] is False and d["how_to_run"] is None
    assert client.get("/api/library/strategies/unknown_id").status_code == 404
    assert client.get("/api/library/strategies/trend_ma_crossover").json()["has_strategy_exit_rule"] is True


def test_planned_entries_cannot_run(client):
    for i in ("pairs_trading", "value_quality", "disclosure_event"):
        r = client.post(f"/api/library/strategies/{i}/backtest", json={"market": "DEMO"})
        assert r.status_code == 409 and "PLANNED" in r.json()["detail"]


def test_bad_parameters_are_400(client):
    r = client.post("/api/library/strategies/trend_ma_crossover/backtest", json={"market": "DEMO", "params": {"fast": 80, "slow": 30}})
    assert r.status_code == 400 and "smaller" in r.json()["detail"]


def test_library_run_reuses_engine_and_costs(client):
    """Same result as the Strategy Lab endpoint for the same settings: one engine, one cost model."""
    exit_ = {"target_r": 2.0, "max_hold_days": 10}
    costs = {"commission_bps": 5.0, "sell_tax_bps": 20.0, "slippage_bps": 10.0}
    lib = client.post("/api/library/strategies/volume_breakout/backtest",
                      json={"market": "DEMO", "exit": exit_, "costs": costs, "random_runs": 0}).json()
    lab = client.post("/api/backtests/run", json={"market": "DEMO", "strategy": "breakout_volume", "exit": exit_, "costs": costs,
                                                  "random_runs": 0}).json()
    for k in ("total_return", "trades", "max_drawdown", "total_costs"):
        assert lib["metrics"][k] == pytest.approx(lab["metrics"][k]), k
    assert lib["costs"] == costs and lib["cost_impact"]["gross_total_return"] >= lib["metrics"]["total_return"]
    # costs: each trade pays commission on both sides plus sell tax, counted once
    t = lib["trades"][0]
    buy, sell = t["qty"] * t["entry"], t["qty"] * t["exit"]
    assert t["costs"] == pytest.approx(buy * 5e-4 + sell * (5e-4 + 20e-4), rel=1e-6)
    L = lib["library"]
    assert {r["period"] for r in L["by_period"]} == {"dev", "validation", "test"} and L["coverage"]["instruments"] == 12
    assert L["ledger"] is None


def test_default_exit_uses_strategy_rule(client):
    r = client.post("/api/library/strategies/rsi_mean_reversion/backtest", json={"market": "DEMO", "random_runs": 0}).json()
    assert r["exit"]["strategy_exit"] is True and r["library"]["uses_strategy_exit_rule"] is True
    assert "STRATEGY_EXIT" in r["metrics"]["exit_reasons"]


def test_ledger_registration_and_history_preserved(client):
    from app.research.ledger import Ledger, verify
    from app.strategy_library import default_ledger_path
    body = {"market": "DEMO", "random_runs": 0, "ledger": True, "params": {"fast": 10, "slow": 40}}
    a = client.post("/api/library/strategies/trend_ma_crossover/backtest", json=body).json()["library"]["ledger"]
    b = client.post("/api/library/strategies/trend_ma_crossover/backtest", json=body).json()["library"]["ledger"]
    c = client.post("/api/library/strategies/trend_ma_crossover/backtest", json={**body, "params": {"fast": 20, "slow": 60}}).json()["library"]["ledger"]
    assert a["registered_now"] and not b["registered_now"] and a["experiment_id"] == b["experiment_id"] and b["runs_before"] == 1
    assert c["registered_now"] and c["experiment_id"] != a["experiment_id"] and c["family_id"] == a["family_id"]
    path = default_ledger_path()
    assert verify(path).ok
    led = Ledger(path)
    first = led.experiment(a["experiment_id"])
    assert first["version"] == 1 and first["spec"]["search_space"]["params"] == {"fast": 10, "slow": 40}
    assert [r["status"] for r in first["runs"].values()] == ["success", "success"] and len(first["manifests"]) == 2
    facts = first["manifests"][0]["facts"]
    assert facts["kind"] == "strategy_library" and facts["experiments_total"] == 1 and facts["data_fingerprint"]
    assert first["spec"]["data_fingerprint"] == facts["data_fingerprint"]
    assert led.experiment(c["experiment_id"])["spec"]["search_space"]["params"] == {"fast": 20, "slow": 60}


def test_ledger_records_failed_run(client, monkeypatch):
    import app.strategy_library as sl
    from app.research.ledger import Ledger
    from app.risk import RiskConfig

    def boom(*a, **k):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(sl, "run_report", boom)
    with pytest.raises(RuntimeError, match="exploded"):
        sl.run_entry("donchian_breakout", {}, None, RiskConfig(), CostModel(), "DEMO", random_runs=0, ledger=True)
    st = Ledger(sl.default_ledger_path()).state()
    exp = [s for e, s in st.items() if e.startswith("LIB-donchian_breakout")]
    runs = [r for s in exp for r in s["runs"].values()]
    assert runs and runs[-1]["status"] == "failure" and "engine exploded" in runs[-1]["error"]
    assert all(s["open_run"] is None for s in exp)                  # nothing left hanging; the next run can start
    monkeypatch.undo()
    ok = sl.run_entry("donchian_breakout", {}, None, RiskConfig(), CostModel(), "DEMO", random_runs=0, ledger=True)
    st2 = Ledger(sl.default_ledger_path()).experiment(ok["library"]["ledger"]["experiment_id"])
    assert [r["status"] for r in st2["runs"].values()] == ["failure", "success"]   # the failure is kept


def test_compare_same_conditions_and_not_comparable_reasons(client):
    items = [{"id": "trend_ma_crossover"}, {"id": "donchian_breakout", "params": {"entry_n": 55, "exit_n": 20}}, {"id": "pairs_trading"},
             {"id": "momentum_relative_strength"}]
    r = client.post("/api/library/compare", json={"market": "DEMO", "items": items, "costs": {"commission_bps": 2, "sell_tax_bps": 20, "slippage_bps": 5}})
    assert r.status_code == 200
    j = r.json()
    rows = {x["label"]: x for x in j["rows"]}
    assert rows["pairs_trading"]["comparable"] is False and "PLANNED" in rows["pairs_trading"]["reason"]
    ok = [x for x in j["rows"] if x["comparable"]]
    assert len(ok) == 3 and len({json.dumps(x["period"]) for x in ok}) == 1
    for x in ok:
        for k in ("total_return_net", "total_return_gross", "max_drawdown", "trades", "turnover", "by_period", "coverage"):
            assert k in x
        assert x["total_return_gross"] >= x["total_return_net"] - 1e-9 or x["trades"] != x["gross_trades"]
    c = j["conditions"]
    assert c["costs"] == {"commission_bps": 2, "sell_tax_bps": 20, "slippage_bps": 5} and c["exit_rules_identical"] is True and c["data_fingerprint"]
    assert j["benchmark"]["label"] and any("합성" in w for w in j["warnings"])
    r2 = client.post("/api/library/compare", json={"market": "DEMO", "items": items[:2], "exit": {"strategy_exit": True}}).json()
    assert r2["conditions"]["exit_rules_identical"] is False
    assert client.post("/api/library/compare", json={"market": "DEMO", "items": [{"id": "nope"}]}).status_code == 404
    assert client.post("/api/library/compare", json={"market": "DEMO", "items": [{"id": "trend_ma_crossover", "params": {"fast": 0}}]}).status_code == 400


def test_compare_saved_runs_checks_settings(client):
    base = {"market": "DEMO", "random_runs": 0, "exit": {"max_hold_days": 10}}
    a = client.post("/api/library/strategies/volume_breakout/backtest", json=base).json()["run_id"]
    b = client.post("/api/library/strategies/rsi_mean_reversion/backtest", json=base).json()["run_id"]
    c = client.post("/api/library/strategies/volume_breakout/backtest", json={**base, "costs": {"commission_bps": 9, "sell_tax_bps": 20, "slippage_bps": 10}}).json()["run_id"]
    same = client.post("/api/library/runs/compare", json={"run_ids": [a, b]}).json()
    assert same["comparable"] is True and len(same["rows"]) == 2
    diff = client.post("/api/library/runs/compare", json={"run_ids": [a, c]}).json()
    assert diff["comparable"] is False and any("costs" in x for x in diff["reasons"])
    assert client.post("/api/library/runs/compare", json={"run_ids": [a, 999999]}).status_code == 404
    runs = client.get("/api/library/runs").json()
    assert {a, b, c} <= {x["run_id"] for x in runs}


def test_existing_strategy_lab_endpoints_unchanged(client):
    keys = {s["key"] for s in client.get("/api/strategies").json()}
    assert {"donchian_breakout", "bb_reversion", "atr_breakout", "ma_trend"} <= keys
    r = client.post("/api/backtests/run", json={"market": "DEMO", "strategy": "volume_quiet_breakout", "random_runs": 0})
    assert r.status_code == 200 and r.json()["exit"]["strategy_exit"] is False


def test_ui_has_library_tab_wired_to_api(client):
    html = client.get("/").text
    assert 'data-tab="library"' in html and 'id="tab-library"' in html
    for needle in ("/api/library/strategies", "/api/library/compare", "/api/library/runs/compare", "libRunBtn", "e.runnable?", "libToLab"):
        assert needle in html, needle
    assert 'data-tab="lab"' in html                                  # Strategy Lab still there
