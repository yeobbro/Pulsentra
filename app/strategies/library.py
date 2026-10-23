"""Built-in strategies. Thresholds are parameters (UI-editable), not conclusions."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.base import Strategy, col


def _true(s: pd.Series) -> pd.Series:
    return s.astype("float").fillna(0.0) > 0


def _volume_surge(x: pd.DataFrame, p: dict) -> pd.Series:
    return (col(x, "volume_ratio_20") >= p["volume_multiple"]) & (col(x, "turnover_ratio_20") >= p["turnover_multiple"])


def _liquid(x: pd.DataFrame, p: dict) -> pd.Series:
    if not p.get("min_avg_turnover"):
        return pd.Series(True, index=x.index)
    return col(x, "avg_turnover_20") >= p["min_avg_turnover"]


class VolumeQuiet(Strategy):
    key = "volume_quiet"
    name = "거래량 급증 + 주가 횡보 (즉시 진입)"
    hypothesis = "20일 거래량이 직전 20일의 N배 이상인데 20일 등락률이 좁은 범위면 이후 수익률이 기준보다 높은가? 돌파 확인 없이 다음 날 진입하는 변형."
    defaults = {"volume_multiple": 3.0, "turnover_multiple": 1.5, "return_min": -0.03, "return_max": 0.03,
                "require_positive_clv": False, "min_avg_turnover": 0.0}

    def _signal(self, x):
        p = self.params
        s = _volume_surge(x, p) & col(x, "ret_20").between(p["return_min"], p["return_max"]) & _liquid(x, p)
        if p["require_positive_clv"]:
            s &= col(x, "clv_20") > 0
        return s


class VolumeQuietBreakout(Strategy):
    key = "volume_quiet_breakout"
    name = "거래량 급증 + 횡보 후 20일 고점 돌파 확인"
    hypothesis = "거래량 급증·횡보 조건이 최근 N거래일 안에 있었고, 오늘 종가가 직전 20일 고가를 돌파했을 때만 진입하면 즉시 진입보다 나은가?"
    defaults = {"volume_multiple": 3.0, "turnover_multiple": 1.5, "return_min": -0.03, "return_max": 0.03,
                "setup_lookback": 10, "min_avg_turnover": 0.0}

    def _signal(self, x):
        p = self.params
        setup = _volume_surge(x, p) & col(x, "ret_20").between(p["return_min"], p["return_max"])
        recent_setup = setup.astype(float).rolling(int(p["setup_lookback"]), min_periods=1).max() > 0
        return recent_setup & _true(col(x, "breakout_20")) & _liquid(x, p)


class VolumeSurgeOnly(Strategy):
    key = "volume_surge_only"
    name = "거래량 급증만 (가격 조건 없음, 비교 기준)"
    hypothesis = "가격 횡보 조건이 실제로 성과에 기여하는지 확인하기 위한 비교 전략."
    defaults = {"volume_multiple": 3.0, "turnover_multiple": 1.5, "min_avg_turnover": 0.0}

    def _signal(self, x):
        return _volume_surge(x, self.params) & _liquid(x, self.params)


class BreakoutVolume(Strategy):
    key = "breakout_volume"
    name = "거래량 동반 20일 고점 돌파"
    hypothesis = "직전 20일 고가를 돌파한 날 거래량이 20일 평균의 N배 이상이면 추세가 이어지는가?"
    defaults = {"volume_spike": 1.5, "min_avg_turnover": 0.0}

    def _signal(self, x):
        p = self.params
        return _true(col(x, "breakout_20")) & (col(x, "volume_spike_1d") >= p["volume_spike"]) & _liquid(x, p)


class PullbackUptrend(Strategy):
    key = "pullback_uptrend"
    name = "상승 추세 중 거래량 감소 눌림목"
    hypothesis = "20일선>60일선 상승 배열에서 종가가 20일선 근처로 내려오고 최근 5일 거래량이 줄면 반등하는가?"
    defaults = {"near_ma20_low": -0.03, "near_ma20_high": 0.01, "volume_dryup": 0.8, "min_avg_turnover": 0.0}

    def _signal(self, x):
        p = self.params
        vol5 = x["volume"].rolling(5, min_periods=5).mean()
        vol20 = x["volume"].rolling(20, min_periods=20).mean()
        return ((col(x, "ma20") > col(x, "ma60")) & (x["close"] > col(x, "ma60"))
                & col(x, "close_to_ma20").between(p["near_ma20_low"], p["near_ma20_high"])
                & (vol5 / vol20 <= p["volume_dryup"]) & (col(x, "ret_60") > 0) & _liquid(x, p))


class MATrend(Strategy):
    key = "ma_trend"
    name = "20일·60일 이동평균 골든크로스 (단순 기준 전략)"
    hypothesis = "단순 이동평균 교차만으로 얻는 성과 — 복잡한 전략이 이 기준을 넘는지 비교하기 위한 기준선."
    defaults = {"fast": 20, "slow": 60}

    def _signal(self, x):
        fast = x["close"].rolling(int(self.params["fast"]), min_periods=int(self.params["fast"])).mean()
        slow = x["close"].rolling(int(self.params["slow"]), min_periods=int(self.params["slow"])).mean()
        above = fast > slow
        return above & ~above.shift(1, fill_value=True) & slow.shift(1).notna()


class RSIRebound(Strategy):
    key = "rsi_rebound"
    name = "RSI 과매도 반등"
    hypothesis = "RSI(14)가 과매도 기준 아래에서 위로 다시 올라오면 단기 반등하는가? (장기 추세 필터 선택)"
    defaults = {"oversold": 30.0, "require_above_ma120": False}

    def _signal(self, x):
        rsi = col(x, "rsi_14")
        s = (rsi >= self.params["oversold"]) & (rsi.shift(1) < self.params["oversold"])
        if self.params["require_above_ma120"]:
            s &= x["close"] > col(x, "ma120")
        return s


class BollingerSqueeze(Strategy):
    key = "bb_squeeze"
    name = "볼린저밴드 변동성 수축 후 상단 돌파"
    hypothesis = "밴드 폭이 최근 120일 중 하위 X%로 수축한 뒤 종가가 상단을 넘으면 변동성 확장이 상승으로 이어지는가?"
    defaults = {"max_width_rank": 0.2, "lookback": 5}

    def _signal(self, x):
        squeezed = (col(x, "bb_width_rank_120") <= self.params["max_width_rank"]).astype(float)
        recent = squeezed.rolling(int(self.params["lookback"]), min_periods=1).max() > 0
        return recent & (x["close"] > col(x, "bb_upper"))


class MomentumRS(Strategy):
    key = "momentum_rs"
    name = "상대강도 상위 중기 모멘텀"
    hypothesis = "120일 수익률이 양수이고 벤치마크 대비 60일 초과수익이 있으며 60일 고점 근처인 종목이 계속 강한가?"
    defaults = {"min_rel_ret_60": 0.0, "max_dist_high60": -0.10, "rebalance_every": 20}
    requires = ("benchmark",)

    def _signal(self, x):
        p = self.params
        cond = ((col(x, "ret_120") > 0) & (col(x, "rel_ret_60") > p["min_rel_ret_60"])
                & (col(x, "dist_high60") >= p["max_dist_high60"]) & (x["close"] > col(x, "ma60")))
        # Evaluate only every N bars so the strategy behaves like a periodic rebalance, not a daily re-entry.
        step = max(1, int(p["rebalance_every"]))
        on_day = pd.Series(np.arange(len(x)) % step == 0, index=x.index)
        return cond & on_day


class DisclosureEvent(Strategy):
    key = "disclosure_event"
    name = "공시·실적 이벤트 기반 탐색"
    hypothesis = "특정 공시 유형 이후 수익률 패턴을 검증한다."
    defaults = {}
    requires = ("disclosures",)
    available = False
    unavailable_reason = ("OpenDART 공시 이력(접수일 기준, 시점 정보 포함)이 적재되어야 검증할 수 있다. "
                          "공시 수집 어댑터는 있으나 이벤트 분류·성과 검증은 아직 구현하지 않았다.")

    def _signal(self, x):
        return pd.Series(False, index=x.index)


class RandomEntry(Strategy):
    key = "random_entry"
    name = "무작위 진입 (비교 기준)"
    hypothesis = "같은 빈도로 무작위로 진입했을 때의 성과 분포 — 전략 성과가 운으로 설명되는지 확인한다."
    defaults = {"probability": 0.02, "seed": 7}

    def _signal(self, x):
        # Seed mixes in the instrument so different symbols get different (but reproducible) draws.
        sym = str(x["symbol"].iloc[0]) if "symbol" in x and len(x) else ""
        rng = np.random.default_rng([int(self.params["seed"]), sum(map(ord, sym))])
        valid = col(x, "atr_14").notna()
        return pd.Series(rng.random(len(x)) < float(self.params["probability"]), index=x.index) & valid


class PullbackRebound(Strategy):
    key = "pullback_rebound"
    name = "상승 추세 눌림목 + 반등 확인 (전략 C)"
    hypothesis = ("중기 상승 추세(20일선>60일선, 60일선 상승, 종가>60일선)에서 최근 N거래일 안에 20일선 부근까지 조정되며 "
                  "거래량이 줄었고, 오늘 종가가 전일 고가와 20일선을 넘어 반등이 확인되면 이후 수익률이 기준보다 높은가?")
    defaults = {"pullback_lookback": 5, "touch_band": 0.02, "volume_dryup": 0.8, "min_ma60_slope": 0.0, "min_avg_turnover": 0.0}

    def _signal(self, x):
        p = self.params
        trend = (col(x, "ma20") > col(x, "ma60")) & (col(x, "ma60_slope_20") > p["min_ma60_slope"]) & (x["close"] > col(x, "ma60"))
        touched = (x["low"] <= col(x, "ma20") * (1 + p["touch_band"]))
        vol5 = x["volume"].rolling(5, min_periods=5).mean()
        vol20 = x["volume"].rolling(20, min_periods=20).mean()
        dry = (vol5 / vol20) <= p["volume_dryup"]
        lb = int(p["pullback_lookback"])
        # the pullback (touch + drying volume) happened in the previous `lb` bars, not today
        setup = (touched & dry).astype(float).shift(1).rolling(lb, min_periods=1).max() > 0
        confirm = (x["close"] > col(x, "prev_high")) & (x["close"] > col(x, "ma20"))
        return trend & setup & confirm & _liquid(x, p)


class MomentumSimple(Strategy):
    key = "momentum_simple"
    name = "단순 모멘텀: 120일 종가 신고가 (비교 기준)"
    hypothesis = "직전 120거래일 최고 종가를 넘는 날 진입하는 가장 단순한 추세 추종 기준선. 복잡한 전략이 이 기준을 넘는지 비교한다."
    defaults = {"min_avg_turnover": 0.0}

    def _signal(self, x):
        return (x["close"] > col(x, "prior_high_close_120")) & (col(x, "ret_120") > 0) & _liquid(x, self.params)
