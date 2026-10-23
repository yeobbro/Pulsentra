"""Alerts & watchlist rules on the latest stored bar (end-of-day today; real-time later).

Every alert stores the inputs it was computed from (`snapshot`) and a dedupe key
(rule:market:symbol:bar_date), so re-running the evaluation never creates a duplicate.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from app import db
from app.data.freshness import all_series_status
from app.scanner import _num, latest_rows


@dataclass
class AlertConfig:
    volume_multiple: float = 3.0
    return_min: float = -0.03
    return_max: float = 0.03
    stop_proximity: float = 0.02      # close within 2% above the paper stop
    target_proximity: float = 0.02
    watchlist: tuple[str, ...] = ()   # "MARKET:SYMBOL"; empty = every stored instrument

    @classmethod
    def from_dict(cls, d: dict | None) -> "AlertConfig":
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in (d or {}).items() if k in known})
        cfg.watchlist = tuple(str(w).upper() for w in cfg.watchlist)
        return cfg


def _insert(rule: str, market: str, symbol: str, bar_date: str, message: str, snapshot: dict, db_path) -> bool:
    key = f"{rule}:{market}:{symbol}:{bar_date}"
    try:
        db.execute("""INSERT INTO alerts (rule, market, symbol, bar_date, dedupe_key, message, snapshot, created_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (rule, market, symbol, bar_date, key, message, db.dumps(snapshot), db.utc_now()), db_path)
        return True
    except sqlite3.IntegrityError:
        return False  # already raised for this bar


def evaluate(cfg: AlertConfig | None = None, db_path: Path | str | None = None) -> dict:
    cfg = cfg or AlertConfig()
    frame = db.load_candles(db_path=db_path)
    created, duplicates = [], 0
    if not frame.empty:
        if cfg.watchlist:
            keys = {tuple(w.split(":", 1)) for w in cfg.watchlist if ":" in w}
            frame = frame[[(m, s) in keys for m, s in zip(frame["market"], frame["symbol"])]]
        rows = latest_rows(frame)
        for (market, symbol), r in rows.items():
            bar_date = pd.Timestamp(r["date"]).strftime("%Y-%m-%d")
            snap = {k: _num(r.get(k)) for k in ("close", "volume_ratio_20", "ret_20", "turnover_ratio_20", "prior_high_20", "ma60", "atr_14")}
            snap["sources"] = str(r.get("source"))
            checks = []
            vr, ret20 = _num(r.get("volume_ratio_20")), _num(r.get("ret_20"))
            if vr is not None and vr >= cfg.volume_multiple:
                checks.append(("volume_surge", f"{symbol} 20일 거래량 {vr:.2f}배 (기준 {cfg.volume_multiple:g}배)"))
                if ret20 is not None and cfg.return_min <= ret20 <= cfg.return_max:
                    checks.append(("volume_quiet", f"{symbol} 거래량 급증 + 20일 등락 {ret20:+.1%} (횡보 조건 충족)"))
            if r.get("breakout_20") is True or r.get("breakout_20") == 1.0:
                checks.append(("breakout_20", f"{symbol} 직전 20일 고가 돌파"))
            close, ma60 = _num(r.get("close")), _num(r.get("ma60"))
            x_prev_above = r.get("_prev_above_ma60")
            if close is not None and ma60 is not None and close < ma60 and x_prev_above:
                checks.append(("trend_break", f"{symbol} 종가가 60일선 아래로 이탈"))
            for rule, msg in checks:
                if _insert(rule, market, symbol, bar_date, msg, snap, db_path):
                    created.append({"rule": rule, "symbol": symbol, "message": msg})
                else:
                    duplicates += 1
        # paper positions near stop / target
        for pos in db.fetch_all("SELECT * FROM paper_positions", (), db_path):
            r = rows.get((pos["market"], pos["symbol"]))
            if r is None:
                continue
            close = float(r["close"])
            bar_date = pd.Timestamp(r["date"]).strftime("%Y-%m-%d")
            snap = {"close": close, "stop": pos["stop_price"], "target": pos["target_price"], "account_id": pos["account_id"]}
            if pos["stop_price"] and close <= pos["stop_price"] * (1 + cfg.stop_proximity):
                msg = f"모의계좌 {pos['account_id']} {pos['symbol']} 종가 {close:,.0f} — 손절가 {pos['stop_price']:,.0f} 근접"
                (created.append({"rule": "near_stop", "symbol": pos["symbol"], "message": msg})
                 if _insert("near_stop", pos["market"], pos["symbol"], bar_date, msg, snap, db_path) else None)
            if pos["target_price"] and close >= pos["target_price"] * (1 - cfg.target_proximity):
                msg = f"모의계좌 {pos['account_id']} {pos['symbol']} 종가 {close:,.0f} — 익절가 {pos['target_price']:,.0f} 근접"
                (created.append({"rule": "near_target", "symbol": pos["symbol"], "message": msg})
                 if _insert("near_target", pos["market"], pos["symbol"], bar_date, msg, snap, db_path) else None)
    # data freshness / collection failures
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    for st in all_series_status(db_path):
        if st["status"] in {"STALE", "ERROR"}:
            msg = f"{st['symbol']} 데이터 상태 {st['status']}: {st['reason']}"
            if _insert(f"data_{st['status'].lower()}", st["market"], st["symbol"], today, msg, {"status": st}, db_path):
                created.append({"rule": f"data_{st['status'].lower()}", "symbol": st["symbol"], "message": msg})
            else:
                duplicates += 1
    # disclosure risk (only what was actually collected from OpenDART)
    for d in db.fetch_all("SELECT * FROM disclosures WHERE risk_flags IS NOT NULL AND risk_flags != ''", (), db_path):
        msg = f"{d['symbol'] or d['corp_name']} 공시 위험: {d['report_nm']} ({d['risk_flags']})"
        if _insert("disclosure_risk", d["market"] or "KR", d["symbol"] or d["corp_code"], d["rcept_dt"], msg, {"rcept_no": d["rcept_no"]}, db_path):
            created.append({"rule": "disclosure_risk", "symbol": d["symbol"], "message": msg})
        else:
            duplicates += 1
    return {"created": created, "duplicates_skipped": duplicates, "config": asdict(cfg)}


def list_alerts(limit: int = 200, db_path=None) -> list[dict]:
    return db.fetch_all("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,), db_path)
