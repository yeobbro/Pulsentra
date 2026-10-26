# Strategy Library (전략 라이브러리)

대표적인 매매전략을 **템플릿**으로 정리한 카탈로그입니다. 원리와 규칙을 읽고, 파라미터를 바꿔 기존 백테스트 엔진으로 검증하고,
결과를 Experiment Ledger에 남기는 진입점입니다. 인기·성과 순위가 아니며, 어떤 항목도 수익을 보장하지 않습니다.

- 카탈로그: `app/strategies/catalog.py` (메타데이터·파라미터 범위·검증) · 전략 코드: `app/strategies/library.py`
- 실행·비교·원장 연결: `app/strategy_library.py` · API: `app/main.py` `/api/library/*` · 화면: `static/index.html` "Strategy Library" 탭
- 테스트: `tests/test_strategy_library.py`

## 상태

| 상태 | 뜻 |
|---|---|
| AVAILABLE | 앱에 저장된 일봉 OHLCV로, 여기에 적힌 규칙 그대로 실행 |
| LIMITED | 실행되지만 제약이 있음(빠질 수 있는 데이터, 단순화된 규칙). 제약은 화면과 결과에 표시 |
| PLANNED | 필요한 데이터나 코드가 없어 실행 버튼이 없음. API도 409로 거부 |

## 카탈로그 (코드에서 생성, 18개)

| ID | 이름 | 계열 | 상태 | 엔진 전략 | 파라미터 = 기본값 [범위] | 구현·제약 |
|---|---|---|---|---|---|---|
| `trend_ma_crossover` | 이동평균 교차 추세 추종 | 추세 추종 | **AVAILABLE** | `ma_trend` | `fast`=20 [2~200], `slow`=60 [5~250] | 기존 ma_trend 전략 + 이번에 추가한 데드크로스 청산 규칙 |
| `momentum_120d_high` | 120일 종가 신고가 모멘텀 | 모멘텀 | **AVAILABLE** | `momentum_simple` | `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 momentum_simple 전략 |
| `momentum_relative_strength` | 상대강도 중기 모멘텀 | 모멘텀 | **LIMITED** | `momentum_rs` | `min_rel_ret_60`=0.0 [-0.5~1.0], `max_dist_high60`=-0.1 [-0.9~0.0], `rebalance_every`=20 [1~120] | 시장지수가 적재되지 않으면 상대강도가 '미확인'이 되어 신호가 없음. 종목 간 순위(상위 N개) 방식이 아니라 종목별 절대 조건 필터임 |
| `donchian_breakout` | Donchian 채널 돌파 | 가격 돌파 | **AVAILABLE** | `donchian_breakout` | `entry_n`=20 [5~250], `exit_n`=10 [2~120], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 이번에 추가한 donchian_breakout 전략 |
| `rsi_mean_reversion` | RSI 과매도 반등 | 평균회귀 | **AVAILABLE** | `rsi_rebound` | `oversold`=30.0 [5.0~50.0], `require_above_ma120`=False, `exit_rsi`=50.0 [30.0~95.0] | 기존 rsi_rebound 전략 + 이번에 추가한 RSI 청산 규칙 |
| `bollinger_mean_reversion` | 볼린저밴드 하단 복귀 | 평균회귀 | **AVAILABLE** | `bb_reversion` | `window`=20 [5~120], `k`=2.0 [0.5~4.0], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 이번에 추가한 bb_reversion 전략 |
| `bollinger_squeeze_breakout` | 볼린저밴드 수축 후 상단 돌파 | 변동성 | **AVAILABLE** | `bb_squeeze` | `max_width_rank`=0.2 [0.01~1.0], `lookback`=5 [1~60] | 기존 bb_squeeze 전략 |
| `volume_breakout` | 거래량 동반 20일 고점 돌파 | 거래량 | **AVAILABLE** | `breakout_volume` | `volume_spike`=1.5 [1.0~20.0], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 breakout_volume 전략 |
| `atr_volatility_breakout` | ATR 변동성 돌파 (종가 확인형) | 변동성 | **LIMITED** | `atr_breakout` | `k`=1.5 [0.3~6.0], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 장중 돌파가격에 체결하는 원형(Larry Williams형)은 일봉·다음 날 시가 체결 엔진으로 검증할 수 없어 종가 확인형만 제공 |
| `volume_quiet_breakout` | 거래량 급증 + 횡보 후 20일 고점 돌파 | 거래량 | **AVAILABLE** | `volume_quiet_breakout` | `volume_multiple`=3.0 [1.0~20.0], `turnover_multiple`=1.5 [1.0~20.0], `return_min`=-0.03 [-0.5~0.0], `return_max`=0.03 [0.0~0.5], `setup_lookback`=10 [1~60], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 volume_quiet_breakout 전략 |
| `volume_quiet` | 거래량 급증 + 횡보 (즉시 진입) | 거래량 | **AVAILABLE** | `volume_quiet` | `volume_multiple`=3.0 [1.0~20.0], `turnover_multiple`=1.5 [1.0~20.0], `return_min`=-0.03 [-0.5~0.0], `return_max`=0.03 [0.0~0.5], `require_positive_clv`=False, `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 volume_quiet 전략 |
| `volume_surge_only` | 거래량 급증만 (비교 기준) | 거래량 | **AVAILABLE** | `volume_surge_only` | `volume_multiple`=3.0 [1.0~20.0], `turnover_multiple`=1.5 [1.0~20.0], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 volume_surge_only 전략 |
| `pullback_rebound` | 상승 추세 눌림목 + 반등 확인 | 추세 추종 | **AVAILABLE** | `pullback_rebound` | `pullback_lookback`=5 [1~30], `touch_band`=0.02 [0.0~0.2], `volume_dryup`=0.8 [0.1~2.0], `min_ma60_slope`=0.0 [-1.0~1.0], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 pullback_rebound 전략 |
| `pullback_uptrend` | 상승 추세 중 거래량 감소 눌림목 | 추세 추종 | **AVAILABLE** | `pullback_uptrend` | `near_ma20_low`=-0.03 [-0.3~0.0], `near_ma20_high`=0.01 [0.0~0.3], `volume_dryup`=0.8 [0.1~2.0], `min_avg_turnover`=0.0 [0.0~10000000000000.0] | 기존 pullback_uptrend 전략 |
| `random_entry` | 무작위 진입 (비교 기준) | 비교 기준 | **AVAILABLE** | `random_entry` | `probability`=0.02 [0.0001~0.5], `seed`=7 [0~1000000000] | 기존 random_entry 전략 |
| `disclosure_event` | 공시·실적 이벤트 | 이벤트 | **PLANNED** | — | — | 공시 수집 어댑터는 있으나 이벤트 분류·성과 검증이 구현되지 않음(기존 disclosure_event는 available=False) |
| `pairs_trading` | 페어 트레이딩 | 상대가치 | **PLANNED** | — | — | 엔진이 단일 종목 롱 포지션만 지원하고, 종목 간 시계열 정렬·관계 검증이 준비되지 않음 |
| `value_quality` | 가치·퀄리티 | 펀더멘털 | **PLANNED** | — | — | 신뢰할 수 있는 시점 기준 펀더멘털 데이터가 없음 |

## 실행 방식

- 모든 실행은 `app.backtesting.report.run_report` → `app.backtesting.engine.run_portfolio`(Strategy Lab과 같은 함수)를 씁니다.
  신호는 t일 종가까지의 데이터로 계산, 진입은 t+1 시가, 비용은 `CostModel`(수수료·슬리피지 편도, 거래세 매도만)을 한 번만 적용합니다.
  같은 설정이면 Strategy Lab(`/api/backtests/run`)과 같은 결과가 나오는지 테스트합니다(`test_library_run_reuses_engine_and_costs`).
- **전략 자체 청산 규칙**(이번에 추가): `ExitConfig.strategy_exit=True`일 때만 `Strategy.exit_signals`가 t일 종가에 판단되고 t+1 시가에 청산합니다.
  기본값은 False라 기존 실행은 바뀌지 않습니다. 보호 손절과 최대 보유일은 항상 적용됩니다(엔진은 손절이 없는 진입을 받지 않음).
  청산 규칙이 있는 전략: `ma_trend`(데드크로스), `donchian_breakout`(M일 저가 이탈), `rsi_rebound`(RSI ≥ exit_rsi), `bb_reversion`(중심선 회복).
- 파라미터는 카탈로그의 형식·범위·상호 조건(예: fast < slow)으로 검증하고, 맞지 않으면 400으로 거부합니다.
- 결과에는 누적·연환산 수익률, MDD, Sharpe, 거래 수, 비용 전후 수익률, 연 회전율, 데이터 커버리지, 구간(dev/validation/test)별 결과가 들어갑니다.
  구간별 결과는 **같은 파라미터의 한 번 실행을 기간별로 나눈 값**이며, 기간마다 파라미터를 고르지 않습니다. 기간 끝을 넘는 거래 수도 셉니다.
  계산하지 않은 지표(예: 기간 180일 미만의 연환산)는 "미확인/미계산"으로 표시하고 값을 만들지 않습니다.

## 비교

- `POST /api/library/compare`: 한 요청 안의 전략을 같은 유니버스·기간·초기 자본·포지션 규칙·비용·청산 설정·벤치마크로 실행합니다.
  PLANNED 항목, 벤치마크가 없어 신호를 낼 수 없는 LIMITED 항목은 `comparable: false`와 사유로 표시합니다.
  `strategy_exit=True`이면 전략마다 청산 규칙이 달라지므로 `exit_rules_identical: false`와 경고를 붙입니다. 비교한 설정 수 경고(다중 비교)도 항상 붙습니다.
- `POST /api/library/runs/compare`: 저장된 실행끼리 비교합니다. 시장·종목·기간·청산·위험·비용·데이터 지문 중 다른 것이 있으면 `comparable: false`와 사유를 돌려줍니다.

## Experiment Ledger 연결

- 실행 요청에 `ledger: true`(화면: "Experiment Ledger에 등록")를 주면 **실행 전에** 가설·규칙·파라미터·비용·주요 지표·데이터 지문을 사전 등록합니다.
- 실험 ID = `LIB-<카탈로그 ID>-<설정 해시 10자>`. 설정 해시에는 파라미터·청산·위험·비용·유니버스·기간·데이터 지문이 들어갑니다.
  같은 설정을 다시 실행하면 같은 실험에 실행이 추가되고, 파라미터를 바꾸면 **새 실험**이 생깁니다(계열 ID `LIB-<카탈로그 ID>`로 묶임). 이전 등록·실행·결과는 그대로 남습니다.
- 실행 시작·성공/실패, `data/library/<run_id>/run_manifest.json`·`report.json`(Git 제외)을 원장에 연결합니다. 실패한 실행도 기록합니다.
- 원장 명세에는 Robustness Gate 임계값(`gate`)도 실행 전에 함께 등록됩니다.
- 채택 결정은 여전히 `python -m app.ledger decide`로 사람이 남깁니다.

## Robustness Gate 평가

- 저장된 실행마다 `POST /api/library/runs/{run_id}/robustness`(화면: 결과의 "Robustness Gate 평가", 저장 실행 표의 "평가")로 판정합니다.
  Factor Lab 게이트와 다른 별도 어댑터이며, 검사·한계는 [`ROBUSTNESS_GATE.md`](ROBUSTNESS_GATE.md) "Strategy Library 실행용 게이트"에 있습니다.
- 이를 위해 실행마다 기간별 벤치마크·초과수익, 거래 기대수익의 95% 구간, 비용 2배·3배 재실행, 연도별 결과, 벤치마크 종류, 데이터 출처를 함께 저장합니다
  (`library.evidence_version` = 1). 이 기능 이전에 저장된 실행은 해당 검사가 INSUFFICIENT_DATA로 나오므로 다시 실행해야 합니다.
- DEMO(합성) 데이터 실행은 data_source 검사에서 FAIL이고 `real_data_verified`는 항상 false입니다.

## 벤치마크

- 앱 DB에 해당 시장의 벤치마크 종목(지수)이 있으면 그 종가로 매수 후 보유(`kind: index`, 종목 기록). 없으면 유니버스 종목 동일가중 매수 후 보유(`kind: equal_weight_proxy`)를 쓰고,
  라벨과 방법에 "지수 미적재 대용 · 실제 시장지수가 아님"을 붙입니다. 두 경우 모두 비용·배당을 반영하지 않습니다.
- 실행마다 `library.benchmark`(종류·종목·방법)를 저장하고, 같은 조건 비교의 `conditions.benchmark_info`에도 표시합니다.
- 저장 실행 비교(`/api/library/runs/compare`)는 벤치마크 종류·종목이 다르거나 기록이 없는 실행이 섞이면 `comparable: false`와 사유를 돌려줍니다.

## 점검한 위험

| 위험 | 처리 |
|---|---|
| 미래 정보 누출 | 새 전략의 신호·청산이 미래 봉을 바꿔도 변하지 않는지 테스트, 채널·ATR은 전일까지 값 사용 |
| 생존자 편향 | 앱 DB에 상장폐지 종목·메타데이터가 없으면 `data.universe_metadata` 경고(기존 동작). 라이브러리가 해결하지 않음 |
| 가격 수정 | 앱 DB 일봉의 수정 여부는 `adjusted_counts`로만 표시. 자동 보정 없음 |
| 데이터 결측 | 커버리지(종목 수, 종목당 봉 수 최소·중앙값, 거래 종목 수) 표시 |
| 비용 중복 | 엔진 비용 한 번 적용을 거래 단위로 테스트 |
| 과최적화 | 다중 비교 경고, 파라미터 변경마다 별도 실험 기록, 구간별 결과는 고정 파라미터 |

## 보류한 것

- 페어 트레이딩: 엔진이 단일 종목 롱만 지원, 종목 간 정렬·관계 검증 없음.
- 가치·퀄리티: 시점 기준 재무 데이터 없음.
- 공시 이벤트: 이벤트 분류·성과 검증 미구현.
- ATR 변동성 돌파의 장중 체결형(open + k·range), 순위 기반 상대강도(상위 N 종목 보유), 공매도 전략.
