"""Strategy Library: a catalog of representative strategy templates linked to the existing backtest engine.

Each entry describes a strategy in words (hypothesis, entry/exit rules, risks), lists its parameters with defaults
and valid ranges, and says how it runs. `strategy_key` points at a class in app.strategies (STRATEGY_REGISTRY);
nothing here re-implements a signal. Entries with status PLANNED have no runnable code.

Status:
  AVAILABLE  runs on the daily OHLCV data the app stores, with the rules as written here.
  LIMITED    runs, but a stated constraint applies (data that may be missing, or a rule simplified).
  PLANNED    not runnable: the data or code it needs is not there yet.

These are templates, not recommendations. No entry is labelled "popular" or "best": no popularity or performance
ranking has been measured.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.strategies.base import STRATEGY_REGISTRY

AVAILABLE, LIMITED, PLANNED = "AVAILABLE", "LIMITED", "PLANNED"
STATUSES = (AVAILABLE, LIMITED, PLANNED)
FAMILIES = {
    "trend": "추세 추종 · Trend following", "momentum": "모멘텀 · Momentum", "breakout": "가격 돌파 · Breakout",
    "mean_reversion": "평균회귀 · Mean reversion", "volume": "거래량 · Volume", "volatility": "변동성 · Volatility",
    "baseline": "비교 기준 · Baseline", "event": "이벤트 · Event", "relative_value": "상대가치 · Relative value",
    "fundamental": "펀더멘털 · Fundamental",
}
ENGINE = "app.backtesting.engine.run_portfolio (app.backtesting.report.run_report 경유)"
RUN_HOW = "화면: Strategy Library 탭 → 전략 선택 → 백테스트 실행 · API: POST /api/library/strategies/{id}/backtest"
DAILY = ["일봉 OHLCV(시가·고가·저가·종가·거래량)"]
COMMON_RISKS = ["거래비용·슬리피지 가정이 실제와 다를 수 있음", "다수 전략·파라미터 비교 시 우연히 좋아 보이는 결과(과최적화)"]


@dataclass(frozen=True)
class Param:
    name: str
    type: str                       # "int" | "float" | "bool"
    default: Any
    min: float | None = None
    max: float | None = None
    description: str = ""


@dataclass(frozen=True)
class Entry:
    id: str
    name_ko: str
    name_en: str
    family: str
    description_ko: str
    description_en: str
    hypothesis: str
    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    params: tuple[Param, ...]
    data_requirements: tuple[str, ...]
    suitable_conditions: str
    key_risks: tuple[str, ...]
    status: str
    status_reason: str
    strategy_key: str | None
    default_exit: dict = field(default_factory=dict)
    tests: tuple[str, ...] = ("tests/test_strategy_library.py",)
    docs: tuple[str, ...] = ("docs/STRATEGY_LIBRARY.md",)
    check: Callable[[dict], str | None] | None = None      # cross-parameter rule, returns an error message

    @property
    def runnable(self) -> bool:
        return self.status in (AVAILABLE, LIMITED) and self.strategy_key is not None

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("check")
        d["family_label"] = FAMILIES[self.family]
        d["runnable"] = self.runnable
        d["engine"] = ENGINE if self.runnable else None
        d["how_to_run"] = RUN_HOW.replace("{id}", self.id) if self.runnable else None
        d["has_strategy_exit_rule"] = bool(self.strategy_key and STRATEGY_REGISTRY[self.strategy_key]().has_exit_rule)
        return d


TURNOVER = Param("min_avg_turnover", "float", 0.0, 0.0, 1e13, "20일 평균 거래대금 하한(원). 0이면 미적용")
VOL = (Param("volume_multiple", "float", 3.0, 1.0, 20.0, "20일 누적 거래량 ÷ 직전 20일 하한"),
       Param("turnover_multiple", "float", 1.5, 1.0, 20.0, "20일 누적 거래대금 ÷ 직전 20일 하한"))
STRATEGY_EXIT = {"strategy_exit": True, "target_r": None, "max_hold_days": 60}


def _lt(a: str, b: str) -> Callable[[dict], str | None]:
    return lambda p: None if p[a] < p[b] else f"{a}({p[a]}) must be smaller than {b}({p[b]})"


ENTRIES: tuple[Entry, ...] = (
    Entry("trend_ma_crossover", "이동평균 교차 추세 추종", "Moving average crossover", "trend",
          "짧은 이동평균이 긴 이동평균을 위로 교차한 다음 날 시가에 진입하고, 아래로 교차하면 청산합니다.",
          "Enter at the next open after the fast moving average crosses above the slow one; exit after it crosses back below.",
          "가격 추세는 한동안 지속되는 경향이 있어, 교차 이후 같은 방향의 움직임이 평균보다 크다.",
          ("종가 기준 fast일 이동평균 > slow일 이동평균으로 바뀐 날(전일은 아님)", "진입: 다음 거래일 시가"),
          ("전략 청산(strategy_exit): fast일 이동평균 < slow일 이동평균인 종가 → 다음 날 시가",
           "보호 손절(ATR 배수)과 최대 보유일은 항상 적용"),
          (Param("fast", "int", 20, 2, 200, "짧은 이동평균 기간"), Param("slow", "int", 60, 5, 250, "긴 이동평균 기간")),
          (*DAILY,), "뚜렷한 상승 추세가 이어지는 구간", ("횡보장에서 잦은 교차로 손실 누적(whipsaw)", "신호가 늦음", *COMMON_RISKS),
          AVAILABLE, "기존 ma_trend 전략 + 이번에 추가한 데드크로스 청산 규칙", "ma_trend", STRATEGY_EXIT, check=_lt("fast", "slow")),
    Entry("momentum_120d_high", "120일 종가 신고가 모멘텀", "120-day closing-high momentum", "momentum",
          "직전 120거래일 최고 종가를 넘고 120일 수익률이 양수인 날 진입합니다. 가장 단순한 추세 추종 기준선입니다.",
          "Enter when the close exceeds the prior 120-day closing high with a positive 120-day return.",
          "최근 강했던 종목이 이후에도 강한 경향(시계열 모멘텀)이 있다.",
          ("종가 > 직전 120거래일 최고 종가", "120일 수익률 > 0", "20일 평균 거래대금 ≥ 하한(선택)"),
          ("공통 청산 설정(ATR 손절·R 목표·최대 보유일·이동평균 이탈)",),
          (TURNOVER,), (*DAILY, "최소 121거래일 이력"), "강세장·주도주 장세",
          ("급락 반전 시 손실", "고점 매수", *COMMON_RISKS), AVAILABLE, "기존 momentum_simple 전략", "momentum_simple"),
    Entry("momentum_relative_strength", "상대강도 중기 모멘텀", "Relative-strength momentum", "momentum",
          "120일 수익률이 양수이고 시장지수 대비 60일 초과수익이 있으며 60일 고점 근처인 종목에 N거래일마다 진입을 검토합니다.",
          "Every N bars, enter names with positive 120-day return, positive 60-day return relative to the index, near their 60-day high.",
          "시장보다 강한 종목이 한동안 계속 강하다(상대 모멘텀).",
          ("120일 수익률 > 0", "시장지수 대비 60일 초과수익 > min_rel_ret_60", "60일 고점 대비 거리 ≥ max_dist_high60",
           "종가 > 60일 이동평균", "rebalance_every 거래일마다만 평가"),
          ("공통 청산 설정",),
          (Param("min_rel_ret_60", "float", 0.0, -0.5, 1.0, "지수 대비 60일 초과수익 하한"),
           Param("max_dist_high60", "float", -0.10, -0.9, 0.0, "60일 고점 대비 최소 거리(음수)"),
           Param("rebalance_every", "int", 20, 1, 120, "평가 간격(거래일)")),
          (*DAILY, "시장지수(벤치마크) 일봉"), "추세가 분명하고 주도 종목이 이어지는 장세",
          ("모멘텀 붕괴(급반전)", *COMMON_RISKS), LIMITED,
          "시장지수가 적재되지 않으면 상대강도가 '미확인'이 되어 신호가 없음. 종목 간 순위(상위 N개) 방식이 아니라 종목별 절대 조건 필터임",
          "momentum_rs"),
    Entry("donchian_breakout", "Donchian 채널 돌파", "Donchian channel breakout", "breakout",
          "종가가 직전 N거래일 최고가를 처음 넘으면 다음 날 시가에 진입하고, 직전 M거래일 최저가 아래로 마감하면 청산합니다.",
          "Enter after the close first exceeds the prior N-day high; exit after a close below the prior M-day low.",
          "가격이 일정 기간의 범위를 벗어나면 새로운 추세가 시작될 수 있다.",
          ("종가 > 직전 entry_n거래일 최고가(당일 제외), 전일은 조건 불충족", "진입: 다음 거래일 시가"),
          ("전략 청산(strategy_exit): 종가 < 직전 exit_n거래일 최저가 → 다음 날 시가", "보호 손절·최대 보유일 항상 적용"),
          (Param("entry_n", "int", 20, 5, 250, "돌파 기준 기간"), Param("exit_n", "int", 10, 2, 120, "청산 기준 기간"), TURNOVER),
          (*DAILY,), "변동성이 확대되며 추세가 형성되는 구간", ("거짓 돌파", "갭 하락 시 손절가보다 불리한 체결", *COMMON_RISKS),
          AVAILABLE, "이번에 추가한 donchian_breakout 전략", "donchian_breakout", STRATEGY_EXIT, check=_lt("exit_n", "entry_n")),
    Entry("rsi_mean_reversion", "RSI 과매도 반등", "RSI mean reversion", "mean_reversion",
          "RSI(14)가 과매도 기준 아래에서 다시 위로 올라온 날 진입하고, RSI가 청산 기준 이상이면 청산합니다.",
          "Enter when RSI(14) crosses back above the oversold level; exit when RSI reaches the exit level.",
          "단기 과매도는 일부 되돌림이 따른다.",
          ("RSI(14) ≥ oversold, 전일 RSI < oversold", "선택: 종가 > 120일 이동평균(장기 상승 추세 필터)"),
          ("전략 청산(strategy_exit): RSI(14) ≥ exit_rsi → 다음 날 시가", "보호 손절·최대 보유일 항상 적용"),
          (Param("oversold", "float", 30.0, 5.0, 50.0, "과매도 기준"), Param("require_above_ma120", "bool", False, description="120일선 위 종목만"),
           Param("exit_rsi", "float", 50.0, 30.0, 95.0, "청산 RSI")),
          (*DAILY, "RSI 기간은 14일로 고정(지표 계산 모듈)"), "횡보·박스권", ("하락 추세에서 계속 하락(떨어지는 칼날)", *COMMON_RISKS),
          AVAILABLE, "기존 rsi_rebound 전략 + 이번에 추가한 RSI 청산 규칙", "rsi_rebound", STRATEGY_EXIT,
          check=_lt("oversold", "exit_rsi")),
    Entry("bollinger_mean_reversion", "볼린저밴드 하단 복귀", "Bollinger band mean reversion", "mean_reversion",
          "종가가 하단 밴드 아래에 있다가 다시 밴드 안으로 들어온 날 진입하고, 중심선(이동평균) 이상에서 청산합니다.",
          "Enter when the close moves back inside the lower band after closing below it; exit at the middle band.",
          "통계적으로 크게 벗어난 가격은 평균 쪽으로 되돌아오는 경향이 있다.",
          ("전일 종가 < 하단 밴드(window일 평균 − k·표준편차), 오늘 종가 ≥ 하단 밴드",),
          ("전략 청산(strategy_exit): 종가 ≥ window일 이동평균 → 다음 날 시가", "보호 손절·최대 보유일 항상 적용"),
          (Param("window", "int", 20, 5, 120, "밴드 기간"), Param("k", "float", 2.0, 0.5, 4.0, "표준편차 배수"), TURNOVER),
          (*DAILY,), "횡보·박스권", ("추세 하락 중 반복 진입", *COMMON_RISKS),
          AVAILABLE, "이번에 추가한 bb_reversion 전략", "bb_reversion", STRATEGY_EXIT),
    Entry("bollinger_squeeze_breakout", "볼린저밴드 수축 후 상단 돌파", "Bollinger squeeze breakout", "volatility",
          "밴드 폭이 최근 120일 중 하위 구간으로 줄어든 뒤 종가가 상단 밴드를 넘으면 진입합니다.",
          "Enter when the close breaks above the upper band shortly after the band width was in the lowest part of its 120-day range.",
          "변동성 수축 뒤에는 확장이 오고, 상단 돌파는 상승 방향 확장의 신호일 수 있다.",
          ("최근 lookback일 안에 밴드 폭 순위 ≤ max_width_rank(120일 기준)", "종가 > 상단 밴드(20일, 2σ 고정)"),
          ("공통 청산 설정",),
          (Param("max_width_rank", "float", 0.2, 0.01, 1.0, "밴드 폭 백분위 상한"), Param("lookback", "int", 5, 1, 60, "수축 확인 기간")),
          (*DAILY, "밴드는 20일·2σ로 고정(지표 계산 모듈)"), "변동성 수축 후 방향성 장세", ("거짓 돌파", *COMMON_RISKS),
          AVAILABLE, "기존 bb_squeeze 전략", "bb_squeeze"),
    Entry("volume_breakout", "거래량 동반 20일 고점 돌파", "Volume-confirmed breakout", "volume",
          "직전 20일 고가를 돌파한 날 거래량이 20일 평균의 N배 이상이면 진입합니다.",
          "Enter after a close above the prior 20-day high on volume at least N times its 20-day average.",
          "거래량이 동반된 돌파는 참여자가 늘었다는 뜻이라 지속 가능성이 더 높다.",
          ("종가 > 직전 20일 최고가", "당일 거래량 ÷ 20일 평균 ≥ volume_spike"), ("공통 청산 설정",),
          (Param("volume_spike", "float", 1.5, 1.0, 20.0, "당일 거래량 배수"), TURNOVER),
          (*DAILY, "거래량은 분할·병합 시 수정되지 않을 수 있음(감사의 volume_level_break)"), "거래 증가를 동반한 상승 초기",
          ("거래량 급증 후 급락", "수정되지 않은 거래량에 의한 오신호", *COMMON_RISKS), AVAILABLE, "기존 breakout_volume 전략", "breakout_volume"),
    Entry("atr_volatility_breakout", "ATR 변동성 돌파 (종가 확인형)", "ATR volatility breakout (close-confirmed)", "volatility",
          "전일 종가 대비 오늘 상승폭이 k × ATR(14, 전일까지) 이상이면 다음 날 시가에 진입합니다.",
          "Enter at the next open after a close-to-close gain of at least k × the prior day's ATR(14).",
          "평소 변동성보다 큰 움직임은 새로운 정보가 반영되는 중이라 같은 방향으로 이어질 수 있다.",
          ("종가 − 전일 종가 ≥ k × ATR(14)(전일 값)", "진입: 다음 거래일 시가"), ("공통 청산 설정",),
          (Param("k", "float", 1.5, 0.3, 6.0, "ATR 배수"), TURNOVER),
          (*DAILY,), "변동성 확대 초기", ("급등 다음 날 되돌림", "장중 돌파가 체결형(open + k·range)과 결과가 다름", *COMMON_RISKS),
          LIMITED, "장중 돌파가격에 체결하는 원형(Larry Williams형)은 일봉·다음 날 시가 체결 엔진으로 검증할 수 없어 종가 확인형만 제공",
          "atr_breakout"),
    Entry("volume_quiet_breakout", "거래량 급증 + 횡보 후 20일 고점 돌파", "Volume surge in a range, then breakout", "volume",
          "Pulsentra 기본 가설: 거래량은 늘었지만 가격은 횡보한 뒤, 20일 고점을 돌파할 때 진입합니다.",
          "Pulsentra's base hypothesis: a volume surge while price is flat, followed by a 20-day-high breakout.",
          "가격이 움직이기 전에 거래량이 먼저 늘면 정보를 가진 매수가 쌓이는 중일 수 있다.",
          ("최근 setup_lookback일 안에: 20일 거래량·거래대금 배수 충족 + 20일 등락률이 [return_min, return_max]", "오늘 종가 > 직전 20일 최고가"),
          ("공통 청산 설정",),
          (*VOL, Param("return_min", "float", -0.03, -0.5, 0.0, "20일 등락률 하한"), Param("return_max", "float", 0.03, 0.0, 0.5, "20일 등락률 상한"),
           Param("setup_lookback", "int", 10, 1, 60, "설정 조건 유효 기간"), TURNOVER),
          (*DAILY,), "거래 증가가 선행하는 종목", ("v0.3 실제 데이터 검증에서 비용 후 일관된 우위 없음(reports/v0.3)", *COMMON_RISKS),
          AVAILABLE, "기존 volume_quiet_breakout 전략", "volume_quiet_breakout", check=_lt("return_min", "return_max")),
    Entry("volume_quiet", "거래량 급증 + 횡보 (즉시 진입)", "Volume surge in a range (immediate)", "volume",
          "거래량 급증·횡보 조건이 충족되면 돌파 확인 없이 다음 날 진입합니다.",
          "Enter the next day once the volume-surge-in-a-range condition holds, without waiting for a breakout.",
          "돌파 확인이 없는 진입이 확인형보다 나은지 비교한다.",
          ("20일 거래량·거래대금 배수 충족", "20일 등락률이 [return_min, return_max]", "선택: CLV(20) > 0"), ("공통 청산 설정",),
          (*VOL, Param("return_min", "float", -0.03, -0.5, 0.0, "20일 등락률 하한"), Param("return_max", "float", 0.03, 0.0, 0.5, "20일 등락률 상한"),
           Param("require_positive_clv", "bool", False, description="종가 위치(CLV) 양수만"), TURNOVER),
          (*DAILY,), "거래 증가가 선행하는 종목", ("v0.3 실제 데이터 검증에서 비용 후 일관된 우위 없음", *COMMON_RISKS),
          AVAILABLE, "기존 volume_quiet 전략", "volume_quiet", check=_lt("return_min", "return_max")),
    Entry("volume_surge_only", "거래량 급증만 (비교 기준)", "Volume surge only (control)", "volume",
          "가격 조건 없이 거래량 급증만으로 진입합니다. 횡보 조건의 기여를 확인하기 위한 비교군입니다.",
          "Volume surge only; a control to see whether the price condition adds anything.",
          "가격 횡보 조건이 실제로 성과에 기여하는지 확인한다.", ("20일 거래량·거래대금 배수 충족",), ("공통 청산 설정",),
          (*VOL, TURNOVER), (*DAILY,), "비교용", COMMON_RISKS, AVAILABLE, "기존 volume_surge_only 전략", "volume_surge_only"),
    Entry("pullback_rebound", "상승 추세 눌림목 + 반등 확인", "Uptrend pullback with rebound", "trend",
          "중기 상승 추세에서 20일선 부근까지 조정되고 거래량이 줄었다가, 종가가 전일 고가와 20일선을 넘으면 진입합니다.",
          "In an uptrend, after a pullback to the 20-day average on lower volume, enter when the close clears the prior high and the 20-day average.",
          "추세 안의 일시적 조정은 추세 방향으로 회복되는 경향이 있다.",
          ("20일선 > 60일선, 60일선 기울기 > min_ma60_slope, 종가 > 60일선", "직전 pullback_lookback일 안에 저가가 20일선 × (1 + touch_band) 이하이고 5일/20일 거래량 ≤ volume_dryup",
           "오늘 종가 > 전일 고가 그리고 > 20일선"), ("공통 청산 설정",),
          (Param("pullback_lookback", "int", 5, 1, 30, "조정 확인 기간"), Param("touch_band", "float", 0.02, 0.0, 0.2, "20일선 근접 허용폭"),
           Param("volume_dryup", "float", 0.8, 0.1, 2.0, "5일/20일 거래량 상한"), Param("min_ma60_slope", "float", 0.0, -1.0, 1.0, "60일선 20일 기울기 하한"), TURNOVER),
          (*DAILY,), "완만한 상승 추세", ("추세 종료 시 조정이 하락으로 이어짐", *COMMON_RISKS), AVAILABLE, "기존 pullback_rebound 전략", "pullback_rebound"),
    Entry("pullback_uptrend", "상승 추세 중 거래량 감소 눌림목", "Low-volume pullback in an uptrend", "trend",
          "20일선 > 60일선인 상승 배열에서 종가가 20일선 근처이고 최근 5일 거래량이 줄면 진입합니다(반등 확인 없음).",
          "In an uptrend, enter when the close sits near the 20-day average on drying volume (no rebound confirmation).",
          "상승 추세 중 거래량이 줄어드는 조정은 매도 압력이 약하다는 뜻일 수 있다.",
          ("20일선 > 60일선, 종가 > 60일선, 60일 수익률 > 0", "종가/20일선 − 1 ∈ [near_ma20_low, near_ma20_high]", "5일/20일 거래량 ≤ volume_dryup"),
          ("공통 청산 설정",),
          (Param("near_ma20_low", "float", -0.03, -0.3, 0.0, "20일선 대비 하한"), Param("near_ma20_high", "float", 0.01, 0.0, 0.3, "20일선 대비 상한"),
           Param("volume_dryup", "float", 0.8, 0.1, 2.0, "5일/20일 거래량 상한"), TURNOVER),
          (*DAILY,), "완만한 상승 추세", ("추세 종료", *COMMON_RISKS), AVAILABLE, "기존 pullback_uptrend 전략", "pullback_uptrend",
          check=_lt("near_ma20_low", "near_ma20_high")),
    Entry("random_entry", "무작위 진입 (비교 기준)", "Random entry (baseline)", "baseline",
          "같은 청산·비용 규칙에서 일정 확률로 무작위 진입합니다. 전략 성과가 운으로 설명되는지 보는 기준입니다.",
          "Random entries with the same exits and costs; a baseline for luck.",
          "전략이 무작위보다 낫지 않다면 우위가 확인되지 않은 것이다.", ("매 봉 probability 확률로 신호(종목·seed별 재현 가능)",), ("공통 청산 설정",),
          (Param("probability", "float", 0.02, 0.0001, 0.5, "봉당 진입 확률"), Param("seed", "int", 7, 0, 10**9, "난수 seed")),
          (*DAILY,), "비교용", ("seed에 따라 결과가 달라짐",), AVAILABLE, "기존 random_entry 전략", "random_entry"),
    Entry("disclosure_event", "공시·실적 이벤트", "Disclosure / earnings events", "event",
          "특정 공시 유형 이후의 수익률 패턴을 검증합니다.", "Returns after specific disclosure types.",
          "공시 정보가 가격에 천천히 반영된다면 공시 이후 수익률에 패턴이 있다.", ("미구현",), ("미구현",), (),
          (*DAILY, "OpenDART 공시 이력(접수 시각 기준)", "공시 유형 분류"), "—", ("공시 시각과 체결 시점 정렬 오류 시 미래 정보 누출",),
          PLANNED, "공시 수집 어댑터는 있으나 이벤트 분류·성과 검증이 구현되지 않음(기존 disclosure_event는 available=False)", None),
    Entry("pairs_trading", "페어 트레이딩", "Pairs trading", "relative_value",
          "두 종목 가격 관계(스프레드)가 평소보다 벌어지면 수렴에 베팅합니다.", "Bet on convergence when the spread between two related stocks widens.",
          "경제적으로 연결된 두 종목의 가격 관계는 장기적으로 유지된다.", ("미구현",), ("미구현",), (),
          (*DAILY, "종목 간 날짜 정렬된 시계열", "공적분 등 관계 검증", "공매도 또는 대체 수단"), "—",
          ("관계 붕괴", "한국 시장 공매도 제한·대차비용", "표본 내 페어 선택 편향"),
          PLANNED, "엔진이 단일 종목 롱 포지션만 지원하고, 종목 간 시계열 정렬·관계 검증이 준비되지 않음", None),
    Entry("value_quality", "가치·퀄리티", "Value / quality", "fundamental",
          "저평가·고수익성 종목을 고릅니다.", "Select cheap and/or high-quality companies.",
          "저평가되었거나 수익성이 높은 기업은 장기적으로 초과수익을 낸다는 가설.", ("미구현",), ("미구현",), (),
          ("시점 기준(공시일) 재무제표", "수정 재무제표 이력"), "—", ("재무 데이터 시점 오류에 의한 미래 정보 누출",),
          PLANNED, "신뢰할 수 있는 시점 기준 펀더멘털 데이터가 없음", None),
)
BY_ID = {e.id: e for e in ENTRIES}


def get_entry(entry_id: str) -> Entry:
    if entry_id not in BY_ID:
        raise KeyError(f"unknown strategy library id {entry_id!r}")
    return BY_ID[entry_id]


def search(family: str | None = None, q: str | None = None, status: str | None = None) -> list[Entry]:
    out = []
    ql = (q or "").strip().lower()
    for e in ENTRIES:
        if family and e.family != family:
            continue
        if status and e.status != status:
            continue
        if ql:
            text = " ".join([e.id, e.name_ko, e.name_en, e.family, FAMILIES[e.family], e.description_ko, e.description_en,
                             e.hypothesis, *e.entry_rules, *e.exit_rules]).lower()
            if not all(tok in text for tok in ql.split()):
                continue
        out.append(e)
    return out


def validate_params(entry: Entry, params: dict | None) -> dict:
    """Defaults merged with `params`, type- and range-checked. Raises ValueError listing every problem."""
    params = dict(params or {})
    specs = {p.name: p for p in entry.params}
    problems = [f"unknown parameter {k!r}" for k in sorted(set(params) - set(specs))]
    out = {p.name: p.default for p in entry.params}
    for k, v in params.items():
        p = specs.get(k)
        if p is None:
            continue
        if p.type == "bool":
            if not isinstance(v, bool):
                problems.append(f"{k} must be true or false"); continue
        elif isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            problems.append(f"{k} must be a number"); continue
        elif p.type == "int":
            if float(v) != int(v):
                problems.append(f"{k} must be an integer"); continue
            v = int(v)
        else:
            v = float(v)
        if p.type != "bool" and ((p.min is not None and v < p.min) or (p.max is not None and v > p.max)):
            problems.append(f"{k}={v} outside [{p.min}, {p.max}]"); continue
        out[k] = v
    if not problems and entry.check:
        msg = entry.check(out)
        if msg:
            problems.append(msg)
    if problems:
        raise ValueError(f"{entry.id}: " + "; ".join(problems))
    return out
