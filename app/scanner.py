"""Market Scanner: rule-based screens with explicit positive / negative evidence.

The core hypothesis screen is "volume expansion + quiet price": 20-session volume at least
`volume_multiple` times the previous 20 sessions while the 20-session return stays inside
[return_min, return_max]. Passing the screen is NOT evidence of accumulation; it marks an
instrument for further validation. `score` is a transparent sort key, not a probability.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.indicators import INDICATOR_SPECS, compute_indicators


@dataclass
class ScanConfig:
    volume_multiple: float = 3.0
    return_min: float = -0.03
    return_max: float = 0.03
    turnover_multiple: float = 1.5
    require_positive_clv: bool = False
    require_breakout: bool = False
    min_avg_turnover: float = 0.0          # KRW per day; 0 = no liquidity filter
    low_liquidity_warning: float = 500_000_000.0
    high_volatility_atr_pct: float = 0.08
    min_bars: int = 41                     # 40 bars are needed for the two 20-session windows

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ScanConfig":
        data = data or {}
        known = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.volume_multiple <= 0 or self.turnover_multiple < 0:
            raise ValueError("volume_multiple must be > 0 and turnover_multiple >= 0")
        if self.return_min > self.return_max:
            raise ValueError("return_min must be <= return_max")

    def to_dict(self) -> dict:
        return asdict(self)


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def evidence(r: pd.Series, cfg: ScanConfig, disclosures: list[dict] | None = None) -> tuple[list[str], list[str], list[str]]:
    """Positive signals, negative signals, and indicators that could not be computed."""
    pos: list[str] = []
    neg: list[str] = []
    unknown: list[str] = []

    def val(name: str) -> float | None:
        v = _num(r.get(name))
        if v is None:
            unknown.append(name)
        return v

    vr, tr_, ud, cmf = val("volume_ratio_20"), val("turnover_ratio_20"), val("up_down_volume_ratio_20"), val("cmf_20")
    ma20, ma60, close = val("ma20"), val("ma60"), _num(r.get("close"))
    rel20, rsi, dist60, atrp, avg_to = val("rel_ret_20"), val("rsi_14"), val("dist_high60"), val("atr_pct_14"), val("avg_turnover_20")
    brk = r.get("breakout_20")

    if vr is not None and vr >= cfg.volume_multiple:
        pos.append(f"20일 누적 거래량 {vr:.2f}배 (기준 {cfg.volume_multiple:g}배)")
    if tr_ is not None and tr_ >= cfg.turnover_multiple:
        pos.append(f"거래대금 {tr_:.2f}배 증가")
    if ud is not None:
        if ud >= 1.2:
            pos.append(f"상승일/하락일 거래량 {ud:.2f} — 상승일 거래 우위")
        elif ud <= 0.8:
            neg.append(f"상승일/하락일 거래량 {ud:.2f} — 하락일 거래 우위")
    if cmf is not None:
        (pos if cmf > 0 else neg).append(f"CMF(20) {cmf:+.3f} — 종가가 일중 범위의 {'상단' if cmf > 0 else '하단'}에 위치한 거래 비중")
    if close is not None and ma60 is not None:
        (pos if close > ma60 else neg).append(f"종가가 60일선 {'위' if close > ma60 else '아래'}")
    if ma20 is not None and ma60 is not None:
        if ma20 > ma60:
            pos.append("20일선 > 60일선 (중기 상승 배열)")
        else:
            neg.append("20일선 < 60일선")
    if brk is True or brk == 1.0:
        pos.append("직전 20거래일 고가 돌파")
    if rel20 is not None:
        (pos if rel20 > 0 else neg).append(f"벤치마크 대비 20일 {rel20:+.1%}")
    if rsi is not None and rsi >= 75:
        neg.append(f"RSI(14) {rsi:.0f} — 단기 과열")
    if dist60 is not None and dist60 <= -0.30:
        neg.append(f"60일 고점 대비 {dist60:.0%} — 깊은 하락 구간")
    if atrp is not None and atrp >= cfg.high_volatility_atr_pct:
        neg.append(f"ATR(14) {atrp:.1%} — 변동성 큼, 손절폭 확대 필요")
    if avg_to is not None and avg_to < cfg.low_liquidity_warning:
        neg.append(f"20일 평균 거래대금 {avg_to / 1e8:.1f}억 — 유동성 부족 가능")
    flags = str(r.get("quality_flags") or "")
    if flags:
        neg.append(f"데이터 품질 경고: {flags}")
    for d in disclosures or []:
        if d.get("risk_flags"):
            neg.append(f"공시 {d.get('rcept_dt', '')} {d.get('report_nm', '')} — {d['risk_flags']}")
    return pos, neg, sorted(set(unknown))


def rule_score(r: pd.Series) -> float:
    """The MVP's transparent sort score (0-100). Unknown inputs contribute 0. NOT a probability."""
    def f(name: str) -> float | None:
        return _num(r.get(name))
    score = 0.0
    vr, tr_, clv, c60, r60 = f("volume_ratio_20"), f("turnover_ratio_20"), f("clv_20"), f("close_to_ma60"), f("ret_60")
    if vr is not None:
        score += min(35, max(0, (vr - 1) * 8))
    if tr_ is not None:
        score += min(20, max(0, (tr_ - 1) * 10))
    score += 15 if clv is not None and clv > 0 else 0
    score += 15 if c60 is not None and c60 > 0 else 0
    score += 15 if r60 is not None and r60 > 0 else 0
    return round(score, 1)


def passes(r: pd.Series, cfg: ScanConfig) -> bool:
    vr, ret20, tr_ = _num(r.get("volume_ratio_20")), _num(r.get("ret_20")), _num(r.get("turnover_ratio_20"))
    if vr is None or ret20 is None or tr_ is None:
        return False  # unknown is not "passing"
    if vr < cfg.volume_multiple or not (cfg.return_min <= ret20 <= cfg.return_max) or tr_ < cfg.turnover_multiple:
        return False
    if cfg.require_positive_clv and not ((_num(r.get("clv_20")) or 0) > 0):
        return False
    if cfg.require_breakout and not (r.get("breakout_20") is True or r.get("breakout_20") == 1.0):
        return False
    avg_to = _num(r.get("avg_turnover_20"))
    if cfg.min_avg_turnover and (avg_to is None or avg_to < cfg.min_avg_turnover):
        return False
    return True


SCAN_METHOD = (
    "기준 시점 = 각 종목의 마지막 저장 일봉 종가. 거래량 배수 = 최근 20거래일 누적 ÷ 직전 20거래일 누적, "
    "등락률 = 20거래일 종가 변화율, 거래대금 배수 = 최근 20거래일 ÷ 직전 20거래일 거래대금. "
    "조건 통과는 '추가 검증 대상'이라는 뜻이며 매집이나 상승을 의미하지 않는다."
)


def run_scan(frame: pd.DataFrame, cfg: ScanConfig, benchmark: pd.DataFrame | None = None,
             disclosures: dict[tuple[str, str], list[dict]] | None = None,
             statuses: dict[tuple[str, str], dict] | None = None, names: dict[tuple[str, str], str] | None = None,
             include_all: bool = False) -> dict:
    rows: list[dict] = []
    evaluated = 0
    skipped_short: list[str] = []
    if frame.empty:
        return {"rows": [], "evaluated": 0, "skipped_insufficient_history": [], "config": cfg.to_dict(), "method": SCAN_METHOD}
    for (market, symbol), raw in frame.groupby(["market", "symbol"], sort=False):
        if len(raw) < cfg.min_bars:
            skipped_short.append(f"{market}/{symbol}")
            continue
        x = compute_indicators(raw, benchmark)
        r = x.iloc[-1]
        evaluated += 1
        ok = passes(r, cfg)
        if not ok and not include_all:
            continue
        key = (market, symbol)
        disc = (disclosures or {}).get(key, [])
        pos, neg, unknown = evidence(r, cfg, disc)
        sources = sorted(raw["source"].dropna().unique().tolist()) if "source" in raw else []
        row = {
            "market": market, "symbol": symbol, "name": (names or {}).get(key),
            "date": pd.Timestamp(r["date"]).strftime("%Y-%m-%d"), "close": _num(r["close"]),
            "ret_20": _num(r.get("ret_20")), "volume_20_sum": _num(r.get("volume_20_sum")),
            "volume_prev20_sum": _num(r.get("volume_prev20_sum")), "volume_ratio_20": _num(r.get("volume_ratio_20")),
            "turnover_ratio_20": _num(r.get("turnover_ratio_20")), "rel_ret_20": _num(r.get("rel_ret_20")),
            "atr_14": _num(r.get("atr_14")), "atr_pct_14": _num(r.get("atr_pct_14")), "clv_20": _num(r.get("clv_20")),
            "cmf_20": _num(r.get("cmf_20")), "up_down_volume_ratio_20": _num(r.get("up_down_volume_ratio_20")),
            "ma20": _num(r.get("ma20")), "ma60": _num(r.get("ma60")), "close_to_ma60": _num(r.get("close_to_ma60")),
            "breakout_20": None if pd.isna(r.get("breakout_20")) else bool(r.get("breakout_20")),
            "dist_high60": _num(r.get("dist_high60")), "rsi_14": _num(r.get("rsi_14")),
            "avg_turnover_20": _num(r.get("avg_turnover_20")),
            "passes": ok, "score": rule_score(r), "positives": pos, "negatives": neg, "unknown": unknown,
            "reasons": pos[:3],  # MVP field
            "disclosures": disc[:5], "source": sources[0] if len(sources) == 1 else ",".join(sources),
            "sources": sources, "data_status": (statuses or {}).get(key),
        }
        rows.append(row)
    rows.sort(key=lambda d: (d["passes"], d["score"], d["volume_ratio_20"] or 0), reverse=True)
    return {"rows": rows, "evaluated": evaluated, "skipped_insufficient_history": skipped_short,
            "config": cfg.to_dict(), "method": SCAN_METHOD, "indicator_specs": INDICATOR_SPECS}


def latest_rows(frame: pd.DataFrame, benchmark: pd.DataFrame | None = None) -> dict[tuple[str, str], pd.Series]:
    out = {}
    for key, raw in frame.groupby(["market", "symbol"], sort=False):
        x = compute_indicators(raw, benchmark)
        if not x.empty:
            r = x.iloc[-1].copy()
            if len(x) > 1:
                prev = x.iloc[-2]
                r["_prev_above_ma60"] = bool(pd.notna(prev["ma60"]) and prev["close"] > prev["ma60"])
            out[key] = r
    return out

