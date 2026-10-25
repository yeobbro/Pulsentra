"""Pulsentra API (FastAPI). The original Market Signal Lab endpoints are kept and extended."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import joblib
import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import alerts, db, paper_trading
from app.backtest import backtest_symbol
from app.backtesting.engine import ExitConfig
from app.backtesting.report import compare_exits, compare_strategies, run_report, signal_study, walk_forward
from app.data.freshness import all_series_status
from app.data.ingest import ingest_frame, parse_csv, record_failure
from app.data.schema import DataStatus
from app.db import init_db, list_symbols, load_candles
from app.demo import DEMO_SOURCE
from app.indicators import INDICATOR_SPECS, compute_indicators
from app.models.lab import MLConfig
from app.providers.dart import DartAPIError, DartClient
from app.providers.kis import KISClient, KisAPIError
from app.risk import CostModel, RiskConfig
from app.scanner import ScanConfig, evidence, run_scan
from app.services import (adopted_model, json_safe, load_demo, lookups, model_list, run_ml, save_backtest, universe)
from app.settings import (APP_NAME, APP_NAME_KO, APP_TAGLINE, APP_VERSION, BASE_DIR, DB_PATH, DEFAULT_ROUND_TRIP_COST_BPS,
                          KIS_PAPER)
from app.strategies import get_strategy, list_strategies


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, description=APP_TAGLINE, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
_json_safe = json_safe  # MVP name


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------------------ request models

class ExitModel(BaseModel):
    stop_type: str = "atr"
    atr_mult: float = Field(1.5, gt=0, le=10)
    stop_pct: float = Field(0.07, gt=0, lt=1)
    swing_buffer_pct: float = Field(0.0, ge=0, lt=0.5)
    target_r: Optional[float] = Field(2.0, gt=0, le=20)
    partial_fraction: float = Field(0.0, ge=0, lt=1)
    partial_r: float = Field(1.5, gt=0, le=20)
    trail_atr_mult: Optional[float] = Field(None, gt=0, le=20)
    ma_exit: Optional[str] = None
    max_hold_days: int = Field(10, ge=1, le=252)


class RiskModel(BaseModel):
    initial_cash: float = Field(100_000_000, gt=0)
    risk_per_trade: float = Field(0.005, gt=0, le=0.1)
    max_positions: int = Field(10, ge=1, le=200)
    max_position_pct: float = Field(0.2, gt=0, le=1)
    max_sector_positions: int = Field(3, ge=1, le=200)
    daily_loss_limit: float = Field(0.03, gt=0, le=1)
    max_drawdown_halt: float = Field(0.25, gt=0, le=1)
    drawdown_halt_days: int = Field(20, ge=0, le=500)
    pause_after_consecutive_losses: int = Field(0, ge=0, le=100)
    pause_days: int = Field(5, ge=1, le=60)
    lot_size: int = Field(1, ge=1)


class CostsModel(BaseModel):
    commission_bps: float = Field(CostModel().commission_bps, ge=0, le=500)
    sell_tax_bps: float = Field(CostModel().sell_tax_bps, ge=0, le=500)
    slippage_bps: float = Field(CostModel().slippage_bps, ge=0, le=1000)


class PortfolioBacktestRequest(BaseModel):
    market: str = "KR"
    symbols: Optional[list[str]] = None
    strategy: str = "volume_quiet_breakout"
    strategy_params: dict[str, Any] = {}
    exit: ExitModel = ExitModel()
    risk: RiskModel = RiskModel()
    costs: CostsModel = CostsModel()
    start: Optional[str] = None
    end: Optional[str] = None
    random_runs: int = Field(10, ge=0, le=50)


class CompareRequest(BaseModel):
    market: str = "KR"
    strategies: list[str] = ["volume_surge_only", "volume_quiet", "volume_quiet_breakout", "ma_trend", "random_entry"]
    exit: ExitModel = ExitModel()
    risk: RiskModel = RiskModel()
    costs: CostsModel = CostsModel()


class ExitCompareRequest(BaseModel):
    market: str = "KR"
    strategy: str = "volume_quiet_breakout"
    strategy_params: dict[str, Any] = {}
    variants: Optional[dict[str, dict[str, Any]]] = None
    costs: CostsModel = CostsModel()


class WalkForwardRequest(BaseModel):
    market: str = "KR"
    strategy: str = "volume_quiet_breakout"
    strategy_params: dict[str, Any] = {}
    exit: ExitModel = ExitModel()
    risk: RiskModel = RiskModel()
    costs: CostsModel = CostsModel()
    n_folds: int = Field(4, ge=2, le=10)
    min_train_trades: int = Field(10, ge=1, le=1000)
    grid: Optional[dict[str, list[Any]]] = None


class MLRequest(BaseModel):
    market: str = "KR"
    task: str = "triple_barrier"
    horizon: int = Field(10, ge=1, le=252)
    atr_mult: float = Field(1.5, gt=0, le=10)
    target_r: float = Field(2.0, gt=0, le=10)
    universe: str = "all"
    base_strategy: str = "volume_quiet"
    feature_set: Optional[str] = None
    n_folds: int = Field(4, ge=2, le=10)
    models: list[str] = ["logistic", "random_forest", "hist_gb"]
    allow_synthetic: bool = False
    costs: CostsModel = CostsModel()


class BacktestRequest(BaseModel):  # MVP single-symbol quick backtest (unchanged contract)
    market: str = "KR"
    symbol: str
    volume_multiple: float = Field(3.0, gt=0, le=50)
    return_min: float = Field(-0.03, ge=-1, le=1)
    return_max: float = Field(0.03, ge=-1, le=1)
    turnover_multiple: float = Field(1.5, ge=0, le=50)
    atr_stop: float = Field(1.5, gt=0, le=10)
    reward_risk: float = Field(2.0, gt=0, le=10)
    max_hold_days: int = Field(10, ge=1, le=252)
    round_trip_cost_bps: float = Field(DEFAULT_ROUND_TRIP_COST_BPS, ge=0, le=3000)
    require_positive_clv: bool = False
    require_above_ma60: bool = False
    initial_cash: float = Field(10_000_000, gt=0)


class SavedScan(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    params: dict[str, Any]


class StrategyConfigModel(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    strategy: str
    params: dict[str, Any] = {}
    exit: ExitModel = ExitModel()
    risk: RiskModel = RiskModel()


class PaperAccountRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    initial_cash: float = Field(100_000_000, gt=0)
    market: str = "KR"
    strategy: str = "volume_quiet_breakout"
    strategy_params: dict[str, Any] = {}
    exit: ExitModel = ExitModel()
    risk: RiskModel = RiskModel()
    costs: CostsModel = CostsModel()
    start_date: Optional[str] = None


class PaperOrderRequest(BaseModel):
    market: str = "KR"
    symbol: str
    qty: int = Field(..., ge=1)
    order_type: str = "market"
    limit_price: Optional[float] = Field(None, gt=0)
    valid_days: int = Field(1, ge=1, le=30)


class DartSyncRequest(BaseModel):
    symbols: list[str]
    days: int = Field(365, ge=1, le=3650)


def _exit(m: ExitModel) -> ExitConfig:
    try:
        return ExitConfig.from_dict(m.model_dump())
    except ValueError as exc:
        raise _bad_request(exc)


def _risk(m: RiskModel) -> RiskConfig:
    return RiskConfig.from_dict(m.model_dump())


def _costs(m: CostsModel) -> CostModel:
    return CostModel(**m.model_dump())


def _universe_or_404(market: str, symbols: list[str] | None = None):
    frame, bench, inst = universe(market, symbols)
    if frame.empty:
        raise HTTPException(status_code=404, detail=f"{market.upper()} 시장 데이터가 없습니다. CSV/KIS로 적재하거나 DEMO를 사용하세요.")
    return frame, bench, inst


# ------------------------------------------------------------------ pages & status

@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse((BASE_DIR / "static" / "index.html").read_text(encoding="utf-8"))


def _provider_status() -> dict:
    client = KISClient()
    dart = DartClient()
    return {
        "provider": "KIS Open API", "configured": client.configured,
        "mode": "paper" if KIS_PAPER else "live_quote_read_only", "real_orders_enabled": False, "data_is_live": False,
        "message": ("API 키가 설정되어 있습니다. 현재가/일봉 읽기 전용 어댑터입니다." if client.configured else
                    "KIS_APP_KEY와 KIS_APP_SECRET을 .env에 설정하면 국내 현재가/일봉 조회를 연결할 수 있습니다."),
        "providers": [
            {"name": "KIS Open API (일봉·현재가 REST)", "configured": client.configured, "implemented": True,
             "status": DataStatus.DELAYED.value if client.configured else "NOT_CONFIGURED"},
            {"name": "KIS WebSocket (실시간 체결)", "configured": client.configured, "implemented": False, "status": "NOT_IMPLEMENTED"},
            {"name": "OpenDART (공시 목록)", "configured": dart.configured, "implemented": True,
             "status": "READY" if dart.configured else "NOT_CONFIGURED"},
            {"name": "KRX OPEN API", "configured": False, "implemented": False, "status": "NOT_IMPLEMENTED",
             "note": "연구용 로컬 검증(python -m app.krxcheck)만 구현. 이용약관상 제3자 제공 금지·비상업적 이용 — "
                     "화면·API 제공은 라이선스 확인 전까지 미구현"},
            {"name": "CSV 업로드", "configured": True, "implemented": True, "status": DataStatus.HISTORICAL.value},
        ],
    }


@app.get("/api/health")
def health():
    symbols = list_symbols()
    bars = int(sum(int(s["bars"]) for s in symbols))
    demo_bars = db.fetch_all("SELECT COUNT(*) AS n FROM candles WHERE source=?", (DEMO_SOURCE,))[0]["n"]
    statuses = all_series_status()
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"status": "ok", "app": APP_NAME, "app_ko": APP_NAME_KO, "tagline": APP_TAGLINE, "version": APP_VERSION,
            "database": str(DB_PATH.name), "symbols": len(symbols), "bars": bars, "demo_bars": int(demo_bars),
            "data_status_counts": counts, "provider": _provider_status(), "generated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/provider-status")
def provider_status():
    return _provider_status()


@app.get("/api/symbols")
def symbols():
    return json_safe(list_symbols())


@app.get("/api/data/status")
def data_status():
    return json_safe(all_series_status())


@app.get("/api/data/quality")
def data_quality(market: Optional[str] = None, symbol: Optional[str] = None, limit: int = 300):
    sql, params = "SELECT * FROM quality_issues", []
    clauses = []
    if market:
        clauses.append("market=?"); params.append(market.upper())
    if symbol:
        clauses.append("symbol=?"); params.append(symbol.upper())
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 2000)))
    issues = db.fetch_all(sql, tuple(params))
    summary = db.fetch_all("SELECT check_name, severity, COUNT(*) AS n FROM quality_issues GROUP BY check_name, severity ORDER BY n DESC")
    return {"summary": summary, "issues": issues}


@app.get("/api/data/runs")
def data_runs(limit: int = 50):
    return db.fetch_all("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))


@app.get("/api/history/{symbol}")
def history(symbol: str, market: str = "KR", limit: int = 180):
    frame = load_candles(symbol=symbol.strip().upper(), market=market.strip().upper())
    if frame.empty:
        raise HTTPException(status_code=404, detail="해당 종목의 가격 데이터가 없습니다.")
    x = compute_indicators(frame).tail(max(20, min(limit, 1500)))
    cols = ["date", "open", "high", "low", "close", "volume", "turnover", "source", "ma20", "ma60", "atr_14", "bb_upper", "bb_lower", "quality_flags"]
    return {"symbol": symbol.upper(), "market": market.upper(), "bars": json_safe(x[[c for c in cols if c in x]].to_dict(orient="records")),
            "source": sorted(frame["source"].dropna().unique().tolist())}


# ------------------------------------------------------------------ data ingestion

@app.post("/api/data/import")
async def import_csv(file: UploadFile = File(...), market: str = Form("KR"), symbol: str = Form(""), adjusted: str = Form("")):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV 파일만 업로드할 수 있습니다.")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="파일 크기는 20MB 이하로 제한됩니다.")
    try:
        data = parse_csv(content, market, symbol)
        adj = {"true": True, "false": False}.get(adjusted.strip().lower())
        if adj is not None:
            data["adjusted"] = adj
        res = ingest_frame(data, "csv_import", market=market.upper(), symbol=symbol.upper() or None)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"CSV 가져오기 실패: {exc}") from exc
    return json_safe({"status": res["status"], "rows_received": res["rows_saved"], "rows_in_file": res["rows_received"], **res,
                      "symbols": list_symbols(),
                      "note": "CSV는 HISTORICAL(과거 데이터)로 저장됩니다. 오류 행은 저장하지 않았고, 경고 행은 품질 플래그와 함께 저장했습니다."})


@app.post("/api/instruments/import")
async def import_instruments(file: UploadFile = File(...)):
    """CSV: market,symbol[,name,sector,exchange,listed_date,delisted_date,is_benchmark]."""
    content = await file.read()
    try:
        import io
        data = pd.read_csv(io.BytesIO(content), dtype=str)
        data.columns = [c.strip().lower() for c in data.columns]
        if not {"market", "symbol"} <= set(data.columns):
            raise ValueError("market, symbol 열이 필요합니다.")
        rows = []
        for r in data.fillna("").to_dict("records"):
            rows.append({"market": r["market"], "symbol": r["symbol"].zfill(6) if r["symbol"].isdigit() else r["symbol"],
                         "name": r.get("name") or None, "sector": r.get("sector") or None, "exchange": r.get("exchange") or None,
                         "listed_date": r.get("listed_date") or None, "delisted_date": r.get("delisted_date") or None,
                         "is_benchmark": str(r.get("is_benchmark", "")).lower() in {"1", "true", "y", "yes"}})
        n = db.upsert_instruments(rows)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"종목 메타데이터 가져오기 실패: {exc}") from exc
    return {"status": "ok", "rows": n}


@app.post("/api/demo/load")
def demo_load():
    return json_safe({"status": "ok", "rows_received": None, **load_demo()})


@app.delete("/api/demo")
def demo_clear():
    n = db.clear_source(DEMO_SOURCE)
    db.execute("DELETE FROM instruments WHERE market='DEMO'")
    return {"status": "ok", "deleted_bars": n}


@app.post("/api/sync/{symbol}")
def sync_symbol(symbol: str, lookback_days: int = 420):
    client = KISClient()
    sym = symbol.strip().upper()
    if not client.configured:
        raise HTTPException(status_code=503, detail="KIS API 키가 설정되지 않았습니다. .env.example을 참고해 .env를 설정하세요.")
    try:
        bars = client.daily_bars(sym, lookback_calendar_days=lookback_days)
        if bars.empty:
            record_failure("kis_eod", "KR", sym, "API에서 조회된 일봉 없음")
            return {"status": "empty", "symbol": sym, "rows_received": 0, "message": "API에서 조회된 일봉이 없습니다."}
        res = ingest_frame(bars, "kis_eod", adjusted=True, market="KR", symbol=sym)
        quote = client.current_quote(sym)
        return json_safe({"status": res["status"], "symbol": sym, "rows_received": res["rows_saved"], "quality": res["quality"],
                          "quote": quote, "updated_at": datetime.now(timezone.utc).isoformat(), "source": "KIS Open API"})
    except KisAPIError as exc:
        record_failure("kis_eod", "KR", sym, str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        record_failure("kis_eod", "KR", sym, str(exc))
        raise HTTPException(status_code=502, detail=f"데이터 동기화 실패: {exc}") from exc


@app.get("/api/quote/{symbol}")
def quote(symbol: str):
    client = KISClient()
    if not client.configured:
        raise HTTPException(status_code=503, detail="KIS API 키가 설정되지 않았습니다.")
    try:
        return json_safe(client.current_quote(symbol))
    except KisAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/dart/sync")
def dart_sync(req: DartSyncRequest):
    client = DartClient()
    if not client.configured:
        raise HTTPException(status_code=503, detail="DART_API_KEY가 설정되지 않았습니다.")
    try:
        codes = client.corp_codes()
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - pd.Timedelta(req.days, unit="D")).strftime("%Y%m%d")
        saved, missing = 0, []
        for sym in req.symbols:
            info = codes.get(sym.strip())
            if not info:
                missing.append(sym)
                continue
            for d in client.disclosures(info["corp_code"], start, end):
                db.execute("""INSERT OR REPLACE INTO disclosures (rcept_no, market, symbol, corp_code, corp_name, report_nm, rcept_dt, risk_flags, source, fetched_at)
                              VALUES (?, 'KR', ?, ?, ?, ?, ?, ?, 'opendart', ?)""",
                           (d["rcept_no"], d["symbol"] or sym, d["corp_code"], d["corp_name"], d["report_nm"], d["rcept_dt"], d["risk_flags"], db.utc_now()))
                saved += 1
    except DartAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", "saved": saved, "symbols_without_corp_code": missing}


@app.get("/api/disclosures/{symbol}")
def disclosures(symbol: str, market: str = "KR"):
    return db.fetch_all("SELECT * FROM disclosures WHERE market=? AND symbol=? ORDER BY rcept_dt DESC LIMIT 100", (market.upper(), symbol.upper()))


# ------------------------------------------------------------------ scanner

@app.get("/api/signals")
def signals(market: str = "KR", volume_multiple: float = 3.0, return_min: float = -0.03,
            return_max: float = 0.03, turnover_multiple: float = 1.5, require_positive_clv: bool = False):
    """MVP endpoint, now backed by app.scanner."""
    frame, bench, _ = universe(market)
    if frame.empty:
        return {"market": market.upper(), "data_count": 0, "signals": [], "message": "데이터가 없습니다. CSV를 가져오거나 KIS API로 국내 종목을 동기화하세요."}
    try:
        cfg = ScanConfig.from_dict({"volume_multiple": volume_multiple, "return_min": return_min, "return_max": return_max,
                                    "turnover_multiple": turnover_multiple, "require_positive_clv": require_positive_clv})
    except ValueError as exc:
        raise _bad_request(exc)
    statuses, names, disc = lookups()
    res = run_scan(frame, cfg, bench, disc, statuses, names)
    return json_safe({"market": market.upper(), "data_count": int(len(frame)), "signals": res["rows"],
                      "message": ("조건 필터 결과입니다. score는 규칙 기반 정렬 점수이며 상승 확률이 아닙니다."
                                  if res["rows"] else "현재 데이터에서 조건을 만족하는 종목이 없습니다.")})


@app.post("/api/scan")
def scan(params: dict[str, Any], market: str = "KR", include_all: bool = False):
    try:
        cfg = ScanConfig.from_dict(params)
    except (ValueError, TypeError) as exc:
        raise _bad_request(exc)
    frame, bench, _ = universe(market)
    statuses, names, disc = lookups()
    res = run_scan(frame, cfg, bench, disc, statuses, names, include_all=include_all)
    res["benchmark_loaded"] = bench is not None
    return json_safe(res)


@app.get("/api/scans")
def saved_scans():
    rows = db.fetch_all("SELECT * FROM saved_scans ORDER BY name")
    for r in rows:
        r["params"] = json.loads(r["params"])
    return rows


@app.post("/api/scans")
def save_scan(req: SavedScan):
    try:
        ScanConfig.from_dict(req.params)
    except (ValueError, TypeError) as exc:
        raise _bad_request(exc)
    db.execute("INSERT INTO saved_scans (name, params) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET params=excluded.params",
               (req.name, db.dumps(req.params)))
    return {"status": "ok"}


@app.delete("/api/scans/{name}")
def delete_scan(name: str):
    db.execute("DELETE FROM saved_scans WHERE name=?", (name,))
    return {"status": "ok"}


@app.get("/api/stock/{market}/{symbol}")
def stock_detail(market: str, symbol: str, strategy: str = "volume_quiet_breakout", atr_mult: float = 1.5, target_r: float = 2.0):
    market, symbol = market.upper(), symbol.upper()
    frame, bench, _ = universe(market, [symbol])
    if frame.empty:
        raise HTTPException(status_code=404, detail="해당 종목 데이터가 없습니다.")
    x = compute_indicators(frame, bench)
    r = x.iloc[-1]
    statuses, names, disc = lookups()
    d = disc.get((market, symbol), [])
    pos, neg, unknown = evidence(r, ScanConfig(), d)
    close, atr = float(r["close"]), r.get("atr_14")
    levels = None
    if pd.notna(atr) and atr > 0:
        stop = close - atr_mult * float(atr)
        levels = {"reference_close": close, "stop": stop, "target": close + target_r * (close - stop), "atr_mult": atr_mult,
                  "target_r": target_r, "note": "참고용: 실제 진입은 다음 거래일 시가 기준이며 손절·익절가는 체결가로 다시 계산된다."}
    try:
        study = signal_study(frame, strategy, {}, CostModel(), bench, (market, symbol))["forward"]
    except ValueError as exc:
        study = {"error": str(exc)}
    issues = db.fetch_all("SELECT date, check_name, severity, detail FROM quality_issues WHERE market=? AND symbol=? ORDER BY date DESC LIMIT 30", (market, symbol))
    latest = {k: r.get(k) for k in ["date", "close", "ret_1", "ret_5", "ret_20", "ret_60", "ret_120", "ret_252", "volume_ratio_20", "turnover_ratio_20",
                                     "up_down_volume_ratio_20", "clv_20", "cmf_20", "atr_14", "atr_pct_14", "ma20", "ma60", "ma120", "rsi_14",
                                     "bb_width_20", "breakout_20", "prior_high_20", "dist_high60", "rel_ret_20", "rel_ret_60", "avg_turnover_20",
                                     "volatility_20", "quality_flags"]}
    return json_safe({"market": market, "symbol": symbol, "name": names.get((market, symbol)), "latest": latest,
                      "positives": pos, "negatives": neg, "unknown": unknown, "levels": levels, "data_status": statuses.get((market, symbol)),
                      "sources": sorted(frame["source"].dropna().unique()), "similar_signals": study, "signal_strategy": strategy,
                      "disclosures": d[:20], "quality_issues": issues, "indicator_specs": INDICATOR_SPECS})


# ------------------------------------------------------------------ strategies & backtests

@app.get("/api/strategies")
def strategies():
    return list_strategies()


@app.get("/api/strategy-configs")
def strategy_configs():
    rows = db.fetch_all("SELECT * FROM strategy_configs ORDER BY name")
    for r in rows:
        r["params"] = json.loads(r["params"])
    return rows


@app.post("/api/strategy-configs")
def save_strategy_config(req: StrategyConfigModel):
    try:
        get_strategy(req.strategy, **req.params)
        _exit(req.exit)
        _risk(req.risk)
    except ValueError as exc:
        raise _bad_request(exc)
    payload = {"params": req.params, "exit": req.exit.model_dump(), "risk": req.risk.model_dump()}
    db.execute("INSERT INTO strategy_configs (name, strategy, params) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET strategy=excluded.strategy, params=excluded.params",
               (req.name, req.strategy, db.dumps(payload)))
    return {"status": "ok"}


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    """MVP quick single-symbol backtest (trade-sequence equity). Use /api/backtests/run for the portfolio engine."""
    frame = load_candles(symbol=req.symbol.strip().upper(), market=req.market.strip().upper())
    if frame.empty:
        raise HTTPException(status_code=404, detail="해당 시장·종목의 데이터가 없습니다. 먼저 CSV 또는 KIS API로 데이터를 넣으세요.")
    source_types = frame["source"].dropna().unique().tolist()
    result = backtest_symbol(frame, params=req.model_dump(exclude={"market", "symbol", "initial_cash"}), initial_cash=req.initial_cash)
    result.update({"market": req.market.upper(), "symbol": req.symbol.upper(), "sources": source_types,
                   "synthetic_demo_warning": bool(set(source_types) == {DEMO_SOURCE})})
    if result["synthetic_demo_warning"]:
        result["warning"] = "합성 데모 데이터 결과입니다. 실제 시장의 수익 가능성을 의미하지 않습니다."
    return json_safe(result)


@app.post("/api/backtests/run")
def backtest_portfolio(req: PortfolioBacktestRequest):
    frame, bench, inst = _universe_or_404(req.market, req.symbols)
    try:
        report = run_report(frame, req.strategy, req.strategy_params, _exit(req.exit), _risk(req.risk), _costs(req.costs),
                            bench, inst, req.start, req.end, req.random_runs)
    except ValueError as exc:
        raise _bad_request(exc)
    report["run_id"] = save_backtest("portfolio", req.model_dump(), frame, report)
    return json_safe(report)


@app.post("/api/backtests/compare")
def backtest_compare(req: CompareRequest):
    frame, bench, inst = _universe_or_404(req.market)
    try:
        res = compare_strategies(frame, [(s, {}) for s in req.strategies], _exit(req.exit), _risk(req.risk), _costs(req.costs), bench, inst)
    except ValueError as exc:
        raise _bad_request(exc)
    res["run_id"] = save_backtest("compare", req.model_dump(), frame, res)
    return json_safe(res)


DEFAULT_EXIT_VARIANTS = {
    "1.5R 전량": {"target_r": 1.5}, "2R 전량": {"target_r": 2.0}, "3R 전량": {"target_r": 3.0},
    "1.5R 절반 + ATR 추적": {"target_r": None, "partial_fraction": 0.5, "partial_r": 1.5, "trail_atr_mult": 2.0, "max_hold_days": 40},
    "20일선 이탈 청산": {"target_r": None, "ma_exit": "ma20", "max_hold_days": 40},
    "ATR 2배 추적 손절": {"target_r": None, "trail_atr_mult": 2.0, "max_hold_days": 40},
    "10일 시간 청산만": {"target_r": None, "max_hold_days": 10},
}


@app.post("/api/backtests/exits")
def backtest_exits(req: ExitCompareRequest):
    frame, bench, _ = _universe_or_404(req.market)
    try:
        res = compare_exits(frame, req.strategy, req.strategy_params, req.variants or DEFAULT_EXIT_VARIANTS, _costs(req.costs), bench)
    except ValueError as exc:
        raise _bad_request(exc)
    res["run_id"] = save_backtest("exits", req.model_dump(), frame, res)
    return json_safe(res)


@app.post("/api/backtests/walk-forward")
def backtest_walk_forward(req: WalkForwardRequest):
    frame, bench, inst = _universe_or_404(req.market)
    try:
        res = walk_forward(frame, req.strategy, req.strategy_params, _exit(req.exit), _risk(req.risk), _costs(req.costs),
                           req.grid, req.n_folds, req.min_train_trades, bench, inst)
    except ValueError as exc:
        raise _bad_request(exc)
    res["run_id"] = save_backtest("walk_forward", req.model_dump(), frame, res)
    return json_safe(res)


@app.get("/api/backtests")
def backtest_history(limit: int = 30):
    rows = db.fetch_all("SELECT id, created_at, kind, request, sources, synthetic, summary FROM backtest_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))
    for r in rows:
        r["request"] = json.loads(r["request"])
        r["summary"] = json.loads(r["summary"] or "{}")
        r["sources"] = json.loads(r["sources"] or "[]")
    return rows


# ------------------------------------------------------------------ ML

@app.post("/api/ml/run")
def ml_run(req: MLRequest):
    try:
        cfg = MLConfig.from_dict({**req.model_dump(exclude={"market", "costs"}), "models": tuple(req.models)})
        report = run_ml(req.market, cfg, _costs(req.costs))
    except ValueError as exc:
        raise _bad_request(exc)
    return json_safe(report)


@app.post("/api/train")
def train_model(horizon: int = 10, atr_stop: float = 1.5, reward_risk: float = 2.0, market: str = "KR"):
    """MVP endpoint, now routed through the ML Lab (walk-forward + adoption gate). Demo data is refused."""
    try:
        cfg = MLConfig.from_dict({"horizon": horizon, "atr_mult": atr_stop, "target_r": reward_risk})
        return json_safe(run_ml(market, cfg, CostModel()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"모델 학습/검증 실패: {exc}") from exc


@app.get("/api/ml/models")
def ml_models():
    return json_safe(model_list())


@app.get("/api/ml/models/{version}")
def ml_model(version: str):
    rows = db.fetch_all("SELECT report FROM model_registry WHERE version=?", (version,))
    if not rows:
        raise HTTPException(status_code=404, detail="모델 버전이 없습니다.")
    return json.loads(rows[0]["report"])


@app.get("/api/model-status")
def model_status():
    rows = model_list()
    if not rows:
        return {"trained": False, "adopted": False, "message": "아직 모델 검증을 실행하지 않았습니다."}
    latest = rows[0]
    adopted = adopted_model()
    return json_safe({"trained": True, "latest": latest, "adopted": adopted is not None,
                      "adopted_version": adopted["version"] if adopted else None,
                      "message": "채택된 모델 없음 — 예측을 제공하지 않습니다." if adopted is None else "채택 기준을 통과한 모델이 있습니다."})


@app.get("/api/predict/{symbol}")
def predict_symbol(symbol: str, market: str = "KR"):
    model = adopted_model()
    if model is None or not model["model_path"]:
        raise HTTPException(status_code=409, detail="채택 기준을 통과한 모델이 없어 예측을 제공하지 않습니다. ML Lab의 검증 결과를 확인하세요.")
    frame, bench, _ = universe(market, [symbol])
    if frame.empty:
        raise HTTPException(status_code=404, detail="해당 종목의 일봉 데이터가 없습니다.")
    if (frame["source"] == DEMO_SOURCE).all():
        raise HTTPException(status_code=400, detail="합성 데모 데이터에는 예측을 제공하지 않습니다.")
    try:
        bundle = joblib.load(model["model_path"])
        x = compute_indicators(frame, bench)
        row = x.iloc[[-1]][bundle["features"]]
        p = float(bundle["model"].predict_proba(row)[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"모델 예측 실패: {exc}") from exc
    return {"market": market.upper(), "symbol": symbol.upper(), "based_on_date": pd.Timestamp(x.iloc[-1]["date"]).strftime("%Y-%m-%d"),
            "probability": p, "model_version": model["version"], "model": model["selected_model"], "task": model["task"],
            "warning": "워크포워드 표본 외 검증으로 보정된 과거 빈도 추정치이며, 실제 상승 확률이나 수익을 보장하지 않습니다."}


# ------------------------------------------------------------------ paper trading & alerts

@app.post("/api/paper/accounts")
def paper_create(req: PaperAccountRequest):
    try:
        acc_id = paper_trading.create_account(req.name, req.initial_cash, req.strategy, req.strategy_params, req.exit.model_dump(),
                                              req.risk.model_dump(), req.costs.model_dump(), req.market, req.start_date)
    except ValueError as exc:
        raise _bad_request(exc)
    return {"id": acc_id}


@app.get("/api/paper/accounts")
def paper_list():
    return paper_trading.list_accounts()


@app.get("/api/paper/accounts/{account_id}")
def paper_get(account_id: int):
    try:
        return json_safe(paper_trading.summary(account_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/paper/accounts/{account_id}/run")
def paper_run(account_id: int, end_date: Optional[str] = None):
    try:
        return json_safe(paper_trading.run_until(account_id, end_date))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/paper/accounts/{account_id}/orders")
def paper_order(account_id: int, req: PaperOrderRequest):
    try:
        oid = paper_trading.place_order(account_id, req.market, req.symbol, req.qty, req.order_type, req.limit_price, valid_days=req.valid_days)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    return {"id": oid}


@app.delete("/api/paper/accounts/{account_id}/orders/{order_id}")
def paper_cancel(account_id: int, order_id: int):
    return {"cancelled": paper_trading.cancel_order(account_id, order_id)}


@app.get("/api/paper/accounts/{account_id}/compare")
def paper_compare(account_id: int):
    """Backtest of the same strategy/rules over the dates the paper account has processed."""
    try:
        s = paper_trading.summary(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    acc, curve = s["account"], s["equity_curve"]
    if not curve:
        raise HTTPException(status_code=409, detail="아직 처리한 거래일이 없습니다.")
    p = acc["params"]
    frame, bench, inst = _universe_or_404(p.get("market", "KR"))
    rep = run_report(frame, acc["strategy"], p.get("strategy_params"), ExitConfig.from_dict(p.get("exit")),
                     RiskConfig.from_dict({"initial_cash": acc["initial_cash"], **p.get("risk", {})}),
                     CostModel(**p["costs"]) if p.get("costs") else CostModel(), bench, inst, curve[0]["date"], curve[-1]["date"], 0)
    paper_ret = curve[-1]["equity"] / acc["initial_cash"] - 1
    return json_safe({"period": [curve[0]["date"], curve[-1]["date"]],
                      "paper": {"total_return": paper_ret, "trades": len(s["trades"]), "max_drawdown": s["max_drawdown"]},
                      "backtest": {"total_return": rep["metrics"]["total_return"], "trades": rep["metrics"]["trades"],
                                   "max_drawdown": rep["metrics"]["max_drawdown"]},
                      "note": "두 결과가 다르면 주문 처리 시점·체결·보유 제약 차이를 확인한다. 종가 기준 모의투자는 실시간 체결이 아니다."})


@app.post("/api/alerts/evaluate")
def alerts_evaluate(params: Optional[dict[str, Any]] = None):
    return json_safe(alerts.evaluate(alerts.AlertConfig.from_dict(params)))


@app.get("/api/alerts")
def alerts_list(limit: int = 200):
    rows = alerts.list_alerts(limit)
    for r in rows:
        r["snapshot"] = json.loads(r["snapshot"] or "{}")
    return rows


# ------------------------------------------------------------------ dashboard

@app.get("/api/dashboard")
def dashboard(market: str = "KR"):
    statuses = all_series_status()
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    frame, bench, _ = universe(market)
    n_signals = None
    if not frame.empty:
        st, names, disc = lookups()
        n_signals = len(run_scan(frame, ScanConfig(), bench, disc, st, names)["rows"])
    last_bt = db.fetch_all("SELECT id, created_at, kind, synthetic, summary FROM backtest_runs ORDER BY id DESC LIMIT 1")
    if last_bt:
        last_bt[0]["summary"] = json.loads(last_bt[0]["summary"] or "{}")
    models = model_list()
    risk_warnings = []
    if counts.get("DEMO"):
        risk_warnings.append("DEMO 데이터가 적재되어 있습니다. 실제 시세가 아닙니다.")
    if counts.get("STALE") or counts.get("ERROR"):
        risk_warnings.append(f"STALE {counts.get('STALE', 0)}개, ERROR {counts.get('ERROR', 0)}개 종목의 데이터를 확인하세요.")
    if not KISClient().configured:
        risk_warnings.append("KIS API 미연결: 실시간·지연 시세가 없습니다.")
    recent_alerts = alerts.list_alerts(10)
    return json_safe({"market": market.upper(), "data_status_counts": counts, "instruments": len(statuses),
                      "detected_signals": n_signals, "benchmark_loaded": bench is not None,
                      "last_backtest": last_bt[0] if last_bt else None, "latest_model": models[0] if models else None,
                      "adopted_model": adopted_model() is not None, "risk_warnings": risk_warnings, "recent_alerts": recent_alerts,
                      "market_state": _market_state(bench, frame)})


def _market_state(bench: pd.DataFrame | None, frame: pd.DataFrame) -> dict | None:
    from app.validation import regime_series
    if bench is not None and not bench.empty:
        s = bench.assign(date=pd.to_datetime(bench["date"])).set_index("date")["close"].sort_index()
        label = "벤치마크 지수"
    elif not frame.empty:
        piv = frame.pivot_table(index="date", columns="symbol", values="close")
        s = (piv / piv.bfill().iloc[0]).mean(axis=1)
        label = "적재 종목 동일가중(대용)"
    else:
        return None
    reg = regime_series(s)
    return {"basis": label, "date": s.index[-1].strftime("%Y-%m-%d"), "regime": reg.iloc[-1],
            "ret_60": float(s.iloc[-1] / s.iloc[-61] - 1) if len(s) > 60 else None,
            "definition": "60거래일 수익률 > +5% 상승, < −5% 하락, 그 외 횡보"}



