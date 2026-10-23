"""Required test 15: API integration (FastAPI TestClient, isolated temporary database, no network)."""
import io

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post("/api/demo/load")
        assert r.status_code == 200
        yield c


def test_health_and_provider_status_are_honest(client):
    h = client.get("/api/health").json()
    assert h["app"] == "Pulsentra" and h["data_status_counts"].get("DEMO", 0) >= 1
    names = {p["name"]: p for p in h["provider"]["providers"]}
    assert names["KIS WebSocket (실시간 체결)"]["implemented"] is False
    assert h["provider"]["real_orders_enabled"] is False and h["provider"]["data_is_live"] is False


def test_scan_and_saved_scans(client):
    r = client.post("/api/scan?market=DEMO", json={"volume_multiple": 3.0})
    assert r.status_code == 200 and r.json()["evaluated"] == 12  # benchmark row excluded from the universe
    for row in r.json()["rows"]:
        assert row["data_status"]["status"] == "DEMO" and row["volume_ratio_20"] >= 3.0
    assert client.post("/api/scan?market=DEMO", json={"return_min": 0.1, "return_max": 0.0}).status_code == 400
    assert client.post("/api/scans", json={"name": "기본", "params": {"volume_multiple": 2.5}}).status_code == 200
    assert client.get("/api/scans").json()[0]["params"]["volume_multiple"] == 2.5


def test_stock_detail(client):
    j = client.get("/api/stock/DEMO/DEMO001").json()
    assert j["data_status"]["status"] == "DEMO" and j["levels"]["stop"] < j["levels"]["reference_close"]
    assert "5" in j["similar_signals"] and j["indicator_specs"]


def test_portfolio_backtest_report(client):
    r = client.post("/api/backtests/run", json={"market": "DEMO", "random_runs": 3})
    assert r.status_code == 200
    j = r.json()
    assert j["data"]["synthetic"] is True and "합성" in j["data"]["warning"]
    for key in ("total_return", "cagr", "win_rate", "expectancy_r", "profit_factor", "max_drawdown", "sharpe", "max_consecutive_losses",
                "avg_holding_days", "trades", "win_rate_ci95"):
        assert key in j["metrics"]
    assert j["cost_impact"]["gross_total_return"] >= j["cost_impact"]["net_total_return"] - 1e-9 or j["cost_impact"]["gross_trades"] != j["metrics"]["trades"]
    assert j["monthly"] and j["equity_curve"] and j["assumptions"]
    assert j["benchmark"]["label"].startswith("시장지수")


def test_compare_exits_walkforward(client):
    assert client.post("/api/backtests/compare", json={"market": "DEMO", "strategies": ["volume_quiet", "disclosure_event"]}).status_code == 200
    ex = client.post("/api/backtests/exits", json={"market": "DEMO"}).json()
    ns = {v["n"] for v in ex["variants"].values()}
    assert len(ns) == 1  # identical signals for every exit variant (fair comparison)
    wf = client.post("/api/backtests/walk-forward", json={"market": "DEMO", "n_folds": 3, "min_train_trades": 3})
    assert wf.status_code == 200 and wf.json()["configurations_tested_per_fold"] == 9
    assert client.get("/api/backtests").json()


def test_ml_refuses_demo_and_predict_needs_adopted_model(client):
    r = client.post("/api/ml/run", json={"market": "DEMO"})
    assert r.status_code == 400 and "합성" in r.json()["detail"]
    assert client.get("/api/predict/DEMO001?market=DEMO").status_code == 409
    assert client.post("/api/train?market=DEMO").status_code == 400  # MVP endpoint now goes through the gate


def test_csv_import_reports_quality(client):
    dates = pd.bdate_range("2025-01-02", periods=70)
    df = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000})
    df.loc[5, "high"] = 50.0  # invalid row
    buf = io.BytesIO(df.to_csv(index=False).encode())
    r = client.post("/api/data/import", files={"file": ("x.csv", buf, "text/csv")}, data={"market": "KR", "symbol": "123"})
    j = r.json()
    assert r.status_code == 200 and j["rows_saved"] == 69 and j["quality"]["rows_rejected"] == 1
    st = {(s["market"], s["symbol"]): s for s in client.get("/api/data/status").json()}
    assert st[("KR", "000123")]["status"] == "HISTORICAL"
    assert client.get("/api/data/quality?symbol=000123").json()["issues"][0]["check_name"] == "ohlc_inconsistent"
    bad = client.post("/api/data/import", files={"file": ("y.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")}, data={"symbol": "1"})
    assert bad.status_code == 400


def test_kis_not_configured_and_errors(client, monkeypatch):
    assert client.post("/api/sync/005930").status_code == 503
    assert client.get("/api/quote/005930").status_code == 503
    from app import main as m
    from app.providers.kis import KisAPIError

    class Broken:
        configured = True

        def daily_bars(self, *a, **k):
            raise KisAPIError("KIS API request failed (HTTP 500)")

    monkeypatch.setattr(m, "KISClient", lambda: Broken())
    assert client.post("/api/sync/005930").status_code == 502
    st = {s["symbol"]: s for s in client.get("/api/data/status").json()}
    assert st["005930"]["status"] == "ERROR"


def test_paper_trading_matches_backtest(client):
    acc = client.post("/api/paper/accounts", json={"name": "t", "market": "DEMO"}).json()["id"]
    run = client.post(f"/api/paper/accounts/{acc}/run").json()
    assert run["processed_days"] > 100
    cmp = client.get(f"/api/paper/accounts/{acc}/compare").json()
    assert cmp["paper"]["trades"] == cmp["backtest"]["trades"]
    assert cmp["paper"]["total_return"] == pytest.approx(cmp["backtest"]["total_return"], abs=1e-9)
    assert client.post(f"/api/paper/accounts/{acc}/orders", json={"symbol": "DEMO001", "market": "DEMO", "qty": 0}).status_code == 422


def test_alerts_deduplicate(client):
    first = client.post("/api/alerts/evaluate").json()
    second = client.post("/api/alerts/evaluate").json()
    assert first["created"] and second["created"] == []
    assert second["duplicates_skipped"] >= len(first["created"])
    assert client.get("/api/alerts").json()[0]["snapshot"]


def test_dashboard_and_legacy_endpoints(client):
    d = client.get("/api/dashboard?market=DEMO").json()
    assert d["market_state"]["regime"] in {"bull", "bear", "sideways", "unknown"}
    assert any("DEMO" in w for w in d["risk_warnings"])
    assert client.get("/api/signals?market=DEMO").status_code == 200
    legacy = client.post("/api/backtest", json={"market": "DEMO", "symbol": "DEMO001"}).json()
    assert legacy["synthetic_demo_warning"] is True
    assert client.get("/").status_code == 200
