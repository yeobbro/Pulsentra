"""Hosted (Render) deployment: single-user access guard, /healthz, startup DEMO autoload, persistent DB path.

Credentials are generated at run time (`secrets`); no password is stored in this file."""
import base64
import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db, security, services
from app.demo import DEMO_SOURCE
from app.main import app
from app.security import AccessConfig, AccessConfigError, AccessGuard

ROOT = Path(__file__).resolve().parents[1]
USER = "pulsentra-test"
PASSWORD = secrets.token_urlsafe(24)
PROTECTED_GET = ["/", "/static/index.html", "/api/health", "/api/symbols", "/docs", "/redoc", "/openapi.json", "/no-such-page"]


def _auth(user=USER, password=PASSWORD):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


CREATED_SCANS: list[str] = []      # scans these tests save in the shared session database; removed afterwards


@pytest.fixture(scope="module")
def guarded():
    with TestClient(AccessGuard(app, AccessConfig(username=USER, password=PASSWORD, production=True))) as c:
        yield c
        for name in CREATED_SCANS:     # leave the shared database as other test modules expect it
            c.delete(f"/api/scans/{name}", headers={**_auth(), "Origin": "http://testserver"})
        CREATED_SCANS.clear()


# ---------------------------------------------------------------- development mode keeps the old behaviour

def test_development_without_credentials_is_open():
    cfg = AccessConfig.from_env({})
    cfg.validate()                                           # no exception outside production
    assert not cfg.auth_enabled and not cfg.enforce_origin
    with TestClient(AccessGuard(app, cfg)) as c:
        assert c.get("/api/health").status_code == 200 and c.get("/").status_code == 200


def test_test_session_app_has_guard_disabled():
    assert app.user_middleware and app.user_middleware[0].cls is AccessGuard
    from app import main
    assert not main.ACCESS.auth_enabled and not main.ACCESS.production


# ---------------------------------------------------------------- authentication

@pytest.mark.parametrize("path", PROTECTED_GET)
def test_unauthenticated_requests_get_401(guarded, path):
    r = guarded.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith('Basic realm="Pulsentra"')
    assert PASSWORD not in r.text and USER not in r.text


@pytest.mark.parametrize("method,path", [("post", "/api/demo/load"), ("delete", "/api/demo"), ("post", "/api/backtests/run"),
                                         ("post", "/api/paper/accounts"), ("post", "/api/data/import")])
def test_state_changing_requests_need_auth(guarded, method, path):
    assert getattr(guarded, method)(path).status_code == 401


@pytest.mark.parametrize("header", [_auth(password=PASSWORD + "x"), _auth(user="other"), _auth(password=""),
                                    {"Authorization": "Basic not-base64!!"},
                                    {"Authorization": "Basic " + base64.b64encode(b"no-colon").decode()},
                                    {"Authorization": "Bearer " + PASSWORD}, {"Authorization": ""}])
def test_wrong_credentials_are_refused(guarded, header):
    r = guarded.get("/api/health", headers=header)
    assert r.status_code == 401 and PASSWORD not in r.text


@pytest.mark.parametrize("path", ["/", "/static/index.html", "/api/health", "/docs", "/redoc", "/openapi.json"])
def test_correct_credentials_reach_page_static_api_and_docs(guarded, path):
    r = guarded.get(path, headers=_auth())
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_static_file_and_page_content_served_after_login(guarded):
    page = guarded.get("/", headers=_auth())
    assert "Pulsentra" in page.text
    assert guarded.get("/static/index.html", headers=_auth()).text == page.text


def test_config_repr_and_errors_hide_values():
    cfg = AccessConfig(username=USER, password=PASSWORD, production=True)
    assert PASSWORD not in repr(cfg) and USER not in repr(cfg)


# ---------------------------------------------------------------- health check

def test_healthz_is_open_and_minimal(guarded):
    r = guarded.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"} and r.text == '{"status":"ok"}'
    assert guarded.head("/healthz").status_code in (200, 405)       # never 401
    assert guarded.post("/healthz").status_code == 401
    assert guarded.get("/healthz/").status_code == 401 and guarded.get("/healthz/../api/health").status_code == 401
    assert "/healthz" not in guarded.get("/openapi.json", headers=_auth()).json()["paths"]


# ---------------------------------------------------------------- production startup

@pytest.mark.parametrize("env", [{"PULSENTRA_ENV": "production"},
                                 {"PULSENTRA_ENV": "production", "PULSENTRA_ACCESS_USERNAME": USER},
                                 {"PULSENTRA_ENV": "production", "PULSENTRA_ACCESS_PASSWORD": PASSWORD}])
def test_production_without_credentials_refuses_to_start(env):
    cfg = AccessConfig.from_env(env)
    with pytest.raises(AccessConfigError) as e:
        cfg.validate()
    assert PASSWORD not in str(e.value) and USER not in str(e.value)
    with pytest.raises(AccessConfigError):
        AccessGuard(app, cfg)


def _child_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PULSENTRA_", "MSL_"))}
    env.update({"PULSENTRA_DATA_DIR": str(tmp_path / "data"), "PULSENTRA_DB_PATH": str(tmp_path / "data" / "market_signal_lab.sqlite3"),
                "PULSENTRA_MODEL_DIR": str(tmp_path / "data" / "models"), "PULSENTRA_MODEL_PATH": str(tmp_path / "data" / "model.joblib"),
                "PULSENTRA_MODEL_META_PATH": str(tmp_path / "data" / "model_meta.json"), "PYTHONDONTWRITEBYTECODE": "1",
                "KIS_APP_KEY": "", "KIS_APP_SECRET": "", "DART_API_KEY": "", "KRX_API_KEY": ""})
    env.update(extra)
    return env


def test_production_app_import_fails_without_credentials(tmp_path):
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, capture_output=True, text=True,
                       env=_child_env(tmp_path, PULSENTRA_ENV="production", PULSENTRA_ACCESS_USERNAME=USER), timeout=120)
    assert r.returncode != 0 and "AccessConfigError" in r.stderr and "PULSENTRA_ACCESS_PASSWORD" in r.stderr
    assert USER not in r.stderr


STARTUP_SCRIPT = r"""
import base64, json, os, sys
from fastapi.testclient import TestClient
from app import db, settings
from app.main import app
auth = {"Authorization": "Basic " + base64.b64encode((os.environ["PULSENTRA_ACCESS_USERNAME"] + ":" + os.environ["PULSENTRA_ACCESS_PASSWORD"]).encode()).decode()}
with TestClient(app, base_url="https://pulsentra-web.onrender.com") as c:
    out = {"anon_root": c.get("/").status_code, "anon_static": c.get("/static/index.html").status_code,
           "healthz": c.get("/healthz").json(), "root": c.get("/", headers=auth).status_code,
           "health": c.get("/api/health", headers=auth).json(),
           "cross": c.post("/api/scans", headers={**auth, "Origin": "https://evil.example"}, json={"name": "x", "params": {}}).status_code,
           "db_path": str(settings.DB_PATH), "model_dir": str(settings.MODEL_DIR), "env": settings.APP_ENV,
           "runs": db.fetch_all("SELECT COUNT(*) AS n FROM ingestion_runs")[0]["n"]}
print(json.dumps(out))
"""


def _start(tmp_path, env):
    r = subprocess.run([sys.executable, "-c", STARTUP_SCRIPT], cwd=ROOT, capture_output=True, text=True, env=env, timeout=170)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_production_startup_autoloads_demo_once_on_persistent_path(tmp_path):
    env = _child_env(tmp_path, PULSENTRA_ENV="production", PULSENTRA_ACCESS_USERNAME=USER,
                     PULSENTRA_ACCESS_PASSWORD=PASSWORD, PULSENTRA_AUTOLOAD_DEMO="true")
    first = _start(tmp_path, env)
    db_file = tmp_path / "data" / "market_signal_lab.sqlite3"
    assert first["db_path"] == str(db_file) and db_file.exists() and first["env"] == "production"
    assert first["model_dir"] == str(tmp_path / "data" / "models")
    assert first["anon_root"] == 401 and first["anon_static"] == 401 and first["root"] == 200 and first["cross"] == 403
    assert first["healthz"] == {"status": "ok"}
    assert first["health"]["demo_bars"] > 0 and first["health"]["data_status_counts"].get("DEMO", 0) >= 1
    # restart (new process, same disk): data kept, DEMO not loaded again
    second = _start(tmp_path, env)
    assert second["health"]["demo_bars"] == first["health"]["demo_bars"] and second["runs"] == first["runs"]


# ---------------------------------------------------------------- same-origin rule for state-changing requests

def _scan(guarded, headers):
    name = f"origin-{uuid.uuid4().hex}"
    CREATED_SCANS.append(name)
    return guarded.post("/api/scans", headers={**_auth(), **headers}, json={"name": name, "params": {}})


@pytest.mark.parametrize("headers", [{"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "https://testserver"},
                                     {"Origin": "http://testserver.evil.example"}, {"Sec-Fetch-Site": "cross-site"},
                                     {"Sec-Fetch-Site": "same-site"},
                                     {"Host": "pulsentra-web.onrender.com", "X-Forwarded-Proto": "https", "Origin": "http://pulsentra-web.onrender.com"}])
def test_cross_origin_state_change_refused(guarded, headers):
    r = _scan(guarded, headers)
    assert r.status_code == 403 and r.json() == {"detail": "cross-origin request refused"}


@pytest.mark.parametrize("headers", [{"Origin": "http://testserver"}, {"Sec-Fetch-Site": "same-origin"}, {},
                                     {"Host": "pulsentra-web.onrender.com", "X-Forwarded-Proto": "https", "Origin": "https://pulsentra-web.onrender.com"}])
def test_same_origin_state_change_allowed(guarded, headers):
    assert _scan(guarded, headers).status_code == 200


def test_cross_origin_read_is_not_blocked_but_still_needs_auth(guarded):
    assert guarded.get("/api/health", headers={"Origin": "http://evil.example"}).status_code == 401
    assert guarded.get("/api/health", headers={**_auth(), "Origin": "http://evil.example"}).status_code == 200


def test_origin_rule_off_outside_production():
    with TestClient(AccessGuard(app, AccessConfig(username=USER, password=PASSWORD, production=False))) as c:
        name = f"dev-{uuid.uuid4().hex}"
        r = c.post("/api/scans", headers={**_auth(), "Origin": "http://evil.example"}, json={"name": name, "params": {}})
        assert r.status_code == 200
        assert c.delete(f"/api/scans/{name}", headers=_auth()).status_code == 200


# ---------------------------------------------------------------- DEMO autoload

def _counts(path):
    return {k: db.fetch_all(sql, (), path)[0]["n"] for k, sql in {
        "demo_bars": f"SELECT COUNT(*) AS n FROM candles WHERE source='{DEMO_SOURCE}'",
        "bars": "SELECT COUNT(*) AS n FROM candles", "instruments": "SELECT COUNT(*) AS n FROM instruments",
        "runs": "SELECT COUNT(*) AS n FROM ingestion_runs"}.items()}


def test_autoload_once_into_empty_db(tmp_db):
    first = services.autoload_demo_if_empty(tmp_db)
    assert first["loaded"] is True and first["market"] == "DEMO" and "합성" in first["message"]
    c1 = _counts(tmp_db)
    assert c1["demo_bars"] > 0 and services.demo_autoload_marker(tmp_db).exists()
    assert services.autoload_demo_if_empty(tmp_db) == {"loaded": False, "reason": "already_autoloaded"}
    assert _counts(tmp_db) == c1


def test_autoload_never_touches_existing_data(tmp_db):
    row = pd.DataFrame([{"market": "KR", "symbol": "USER01", "date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10.5,
                         "volume": 100, "turnover": 1050, "source": "csv"}])
    db.upsert_candles(row, tmp_db)
    before = _counts(tmp_db)
    assert services.autoload_demo_if_empty(tmp_db) == {"loaded": False, "reason": "database_not_empty"}
    assert _counts(tmp_db) == before and before["demo_bars"] == 0
    assert db.load_candles(symbol="USER01", market="KR", db_path=tmp_db)["close"].tolist() == [10.5]


def test_autoload_skips_db_with_instruments_only(tmp_db):
    db.upsert_instruments([{"market": "KR", "symbol": "USER02", "name": "user"}], tmp_db)
    assert services.autoload_demo_if_empty(tmp_db)["reason"] == "database_not_empty" and _counts(tmp_db)["bars"] == 0


def test_demo_deleted_on_purpose_is_not_reloaded(tmp_db):
    assert services.autoload_demo_if_empty(tmp_db)["loaded"] is True
    db.clear_source(DEMO_SOURCE, tmp_db)
    db.execute("DELETE FROM instruments WHERE market='DEMO'", (), tmp_db)
    assert services.autoload_demo_if_empty(tmp_db)["reason"] == "already_autoloaded" and _counts(tmp_db)["bars"] == 0


def test_security_module_never_logs():
    src = Path(security.__file__).read_text()
    assert "print(" not in src and "logging" not in src and "logger" not in src


# ---------------------------------------------------------------- render.yaml

def test_render_blueprint_matches_the_deployment_contract():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((ROOT / "render.yaml").read_text())
    assert set(spec) == {"services"} and len(spec["services"]) == 1
    svc = spec["services"][0]
    allowed = {"type", "name", "runtime", "branch", "region", "plan", "buildCommand", "startCommand", "healthCheckPath",
               "autoDeployTrigger", "disk", "envVars"}
    assert set(svc) <= allowed
    assert (svc["type"], svc["name"], svc["runtime"], svc["branch"], svc["region"], svc["plan"]) == \
        ("web", "pulsentra-web", "python", "research/v0.4-data-audit", "singapore", "0.5c-512mb")
    assert svc["buildCommand"] == "pip install -r requirements.txt"
    assert svc["startCommand"] == "uvicorn app.main:app --host 0.0.0.0 --port $PORT"
    assert svc["healthCheckPath"] == "/healthz" and svc["autoDeployTrigger"] in {"commit", "checksPass", "off"}
    assert svc["disk"] == {"name": "pulsentra-data", "mountPath": "/var/data", "sizeGB": 1}
    env = {e["key"]: e for e in svc["envVars"]}
    assert {k: env[k]["value"] for k in env if "value" in env[k]} == {
        "PULSENTRA_ENV": "production", "PULSENTRA_DATA_DIR": "/var/data",
        "PULSENTRA_DB_PATH": "/var/data/market_signal_lab.sqlite3", "PULSENTRA_MODEL_DIR": "/var/data/models",
        "PULSENTRA_MODEL_PATH": "/var/data/model.joblib", "PULSENTRA_MODEL_META_PATH": "/var/data/model_meta.json",
        "PULSENTRA_AUTOLOAD_DEMO": "true"}
    for key in ("PULSENTRA_ACCESS_USERNAME", "PULSENTRA_ACCESS_PASSWORD"):
        assert env[key] == {"key": key, "sync": False}            # value only in the Render Dashboard
    for path in (v for k, v in ((k, env[k].get("value", "")) for k in env) if k.endswith(("_DIR", "_PATH"))):
        assert path.startswith(svc["disk"]["mountPath"] + "/") or path == svc["disk"]["mountPath"]
    assert not any(k.startswith(("KIS_", "DART_", "KRX_")) for k in env)    # no market-data keys shipped
