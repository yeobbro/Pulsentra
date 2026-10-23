# Pulsentra v0.3 — 실제 데이터 전략 검증 결과
> 자동 생성 보고서. 이 문서의 수치는 아래 데이터·설정·코드 버전으로 재현됩니다. 과거 검증 결과이며 미래 수익을 보장하지 않습니다.
## 1. 데이터 출처와 표본
- 가격: Naver Finance daily chart (fchart.stock.naver.com, unofficial endpoint, no key)
- 상장/폐지 목록: FinanceDataReader StockListing('KRX'/'KRX-DELISTING') — KRX listing snapshots cached on GitHub
- 지수: FinanceDataReader DataReader('KS11'/'KQ11') — KRX index history cached on GitHub
- 수집 시각(UTC): ['2026-10-23T10:02:08+00:00', '2026-10-23T10:31:32+00:00']
- 데이터 지문: `de52ccfa16e847ee` · 코드: `4918af9`
- 기간: 2016-01-04 ~ 2026-10-23 · 종목 2,823개 · 일봉 5,799,758개 · 거래소 {'KOSDAQ': 1919, 'KOSPI': 904}
- 상장폐지 종목: 목록 274개 중 데이터 확보 273개 → 상장폐지 종목 포함 (목록 출처 기준). 목록에 없는 폐지 종목과 데이터가 없는 폐지 종목은 빠져 있음
- 출처 유형: 전부 REAL (합성 데이터 혼입 시 실행 중단)
**알려진 데이터 한계**: 가격은 분할·권리락 등 수정주가이나 거래량은 수정되지 않음(거래 정지 구간을 가로지르는 거래량 수준 급변을 탐지해 해당 40거래일 창의 신호를 제외). 거래대금은 제공되지 않아 `수정종가 × 거래량`으로 추정(유동성 필터에만 사용). 현금 배당은 가격에 반영되지 않아 수익률은 배당 제외 가격 수익률. 무상증자처럼 거래 정지가 없는 이벤트의 거래량 불연속은 탐지하지 못함.
## 2. 데이터 품질 검사
| 검사 | 구분 | 행 | 종목 |
|---|---|---|---|
| missing_trading_days | warning | 1,868 | 915 |
| non_positive_price | error | 1,461 | 1 |
| ohlc_inconsistent | error | 179 | 23 |
| ohlc_rounding_repaired | warning | 24,379 | 911 |
| price_jump | warning | 2,454 | 1,169 |
| volume_level_break | warning | 1,991 | 559 |
| zero_volume | warning | 4,025 | 84 |
| suspended_session_removed:zero_ohl_marker | info | 135,490 | 916 |
| suspended_session_removed:zero_volume_flat_bar | info | 63,761 | 539 |
| rows excluded: 40-bar volume window spans a volume_level_break | exclusion | 39,936 | 559 |

## 3. 전략 정의 (모두 같은 데이터·비용·체결 규칙)
- 공통: 신호는 t일 종가까지의 정보, 진입은 t+1일 시가 + 슬리피지. 같은 봉 손절·익절 동시 도달 시 손절. 갭은 시가 체결. 비용 편도 수수료 1.5bp, 매도세 20.0bp, 슬리피지 편도 10.0bp (가정값). 적격 조건: 20일 평균 추정 거래대금 ≥ 10억 원, 종가 ≥ 1,000원, 거래량 창 오염 없음.
- A `volume_quiet`: 20일 누적 거래량 ÷ 직전 20일 ≥ 3, 20일 등락 −3%~+3%, 거래대금 배수 ≥ 1.5, 신호 다음 날 진입.
- B1 `volume_quiet_breakout`: A 조건이 최근 10거래일 안에 있었고 오늘 종가가 직전 20일 고가를 돌파.
- B2 `breakout_volume`: 독립 돌파 — 직전 20일 고가 돌파 + 당일 거래량 ≥ 직전 20일 평균의 1.5배.
- C `pullback_rebound`: 20일선>60일선, 60일선 20일 기울기>0, 종가>60일선; 직전 5거래일 내 저가가 20일선+2% 이내로 조정되며 5일/20일 평균 거래량 ≤ 0.8; 오늘 종가 > 전일 고가 & 20일선.
- 기준 BASE: 120거래일 종가 신고가 + 120일 수익률 > 0 (단순 모멘텀), 무작위 진입(적격 행에서 무작위 추출).

## 4. 기간 분리와 선택 규칙

- 개발(dev) 2016-01-01~2021-12-31, 검증(validation) 2022-01-01~2023-12-31, 최종 평가(test) 2024-01-01~데이터 끝. 각 기간 끝 35일 이내 신호는 제외(다음 기간 데이터로 청산되는 것 방지).
- 청산 규칙 48개 조합(손절 4 × 익절 4 × 보유 3)을 전부 계산해 `strategy_exit_grid.csv`에 보존. 전략별로 **dev 기간 거래당 평균 순수익이 가장 높은 조합 하나**(dev 거래 ≥ 100)를 고르고, 그 조합을 validation·test에 그대로 적용.

## 5. 전략별 결과 (거래 단위, dev에서 고른 청산 규칙)
| 전략 | 청산 | 기간 | 거래 | 승률 | 평균 순수익 | 95% CI(월 군집) | 비용 전 | 평균 R | PF | 최대 연속손실 |
|---|---|---|---|---|---|---|---|---|---|---|
| A_volume_quiet | PCT7|3R|20d | dev | 18,064 | 38.71% | 0.63% | [-0.24%, 1.54%] | 1.06% | 0.09 | 1.16 | 263 |
| A_volume_quiet | PCT7|3R|20d | validation | 10,596 | 32.05% | -0.93% | [-2.29%, 0.68%] | -0.53% | -0.13 | 0.80 | 108 |
| A_volume_quiet | PCT7|3R|20d | test | 13,329 | 30.43% | -0.83% | [-1.74%, 0.07%] | -0.41% | -0.12 | 0.81 | 92 |
| └ 무작위 진입 (같은 청산: A) | PCT7|3R|20d | dev | 80,784 | 33.95% | -0.26% | [-0.86%, 0.37%] | 0.15% | -0.04 | 0.94 | 133 |
| └ 무작위 진입 (같은 청산: A) | PCT7|3R|20d | validation | 28,775 | 31.47% | -0.82% | [-1.89%, 0.31%] | -0.41% | -0.12 | 0.82 | 90 |
| └ 무작위 진입 (같은 청산: A) | PCT7|3R|20d | test | 37,363 | 29.93% | -0.54% | [-1.38%, 0.29%] | -0.13% | -0.08 | 0.89 | 143 |
| B1_quiet_then_breakout | PCT7|1.5R|20d | dev | 1,012 | 36.07% | -0.92% | [-1.57%, -0.30%] | -0.49% | -0.13 | 0.80 | 16 |
| B1_quiet_then_breakout | PCT7|1.5R|20d | validation | 498 | 36.75% | -0.64% | [-1.87%, 0.65%] | -0.28% | -0.09 | 0.86 | 11 |
| B1_quiet_then_breakout | PCT7|1.5R|20d | test | 696 | 34.48% | -1.07% | [-1.71%, -0.44%] | -0.74% | -0.15 | 0.77 | 21 |
| └ 무작위 진입 (같은 청산: B1) | PCT7|1.5R|20d | dev | 80,790 | 40.92% | -0.33% | [-0.77%, 0.14%] | 0.06% | -0.05 | 0.92 | 103 |
| └ 무작위 진입 (같은 청산: B1) | PCT7|1.5R|20d | validation | 28,776 | 38.79% | -0.72% | [-1.55%, 0.20%] | -0.32% | -0.10 | 0.83 | 44 |
| └ 무작위 진입 (같은 청산: B1) | PCT7|1.5R|20d | test | 37,409 | 38.96% | -0.54% | [-1.12%, 0.02%] | -0.14% | -0.08 | 0.87 | 55 |
| B2_breakout_with_volume | SWING10|3R|20d | dev | 51,024 | 38.49% | -0.35% | [-1.44%, 0.66%] | 0.07% | -0.02 | 0.95 | 127 |
| B2_breakout_with_volume | SWING10|3R|20d | validation | 16,063 | 33.42% | -2.32% | [-4.24%, -0.55%] | -1.91% | -0.13 | 0.72 | 48 |
| B2_breakout_with_volume | SWING10|3R|20d | test | 24,336 | 37.26% | -1.25% | [-3.39%, 0.76%] | -0.83% | -0.03 | 0.86 | 175 |
| └ 무작위 진입 (같은 청산: B2) | SWING10|3R|20d | dev | 78,376 | 31.91% | -0.14% | [-0.80%, 0.56%] | 0.28% | -0.16 | 0.97 | 90 |
| └ 무작위 진입 (같은 청산: B2) | SWING10|3R|20d | validation | 27,601 | 29.98% | -0.67% | [-1.54%, 0.23%] | -0.25% | -0.22 | 0.84 | 81 |
| └ 무작위 진입 (같은 청산: B2) | SWING10|3R|20d | test | 36,045 | 29.05% | -0.75% | [-1.68%, 0.21%] | -0.33% | -0.25 | 0.85 | 78 |
| C_pullback_rebound | SWING10|3R|20d | dev | 50,809 | 35.68% | -0.16% | [-1.21%, 0.83%] | 0.26% | -0.01 | 0.97 | 62 |
| C_pullback_rebound | SWING10|3R|20d | validation | 13,910 | 31.55% | -1.34% | [-2.93%, 0.04%] | -0.92% | -0.13 | 0.79 | 58 |
| C_pullback_rebound | SWING10|3R|20d | test | 22,521 | 32.52% | -0.97% | [-2.73%, 0.81%] | -0.56% | -0.07 | 0.87 | 81 |
| └ 무작위 진입 (같은 청산: C) | SWING10|3R|20d | dev | 78,376 | 31.91% | -0.14% | [-0.80%, 0.56%] | 0.28% | -0.16 | 0.97 | 90 |
| └ 무작위 진입 (같은 청산: C) | SWING10|3R|20d | validation | 27,601 | 29.98% | -0.67% | [-1.54%, 0.23%] | -0.25% | -0.22 | 0.84 | 81 |
| └ 무작위 진입 (같은 청산: C) | SWING10|3R|20d | test | 36,045 | 29.05% | -0.75% | [-1.68%, 0.21%] | -0.33% | -0.25 | 0.85 | 78 |
| BASE_momentum_120d_high | SWING10|2R|20d | dev | 62,797 | 39.17% | -0.10% | [-1.18%, 0.93%] | 0.33% | -0.01 | 0.99 | 51 |
| BASE_momentum_120d_high | SWING10|2R|20d | validation | 16,606 | 35.68% | -1.44% | [-3.49%, 0.39%] | -1.01% | -0.09 | 0.83 | 31 |
| BASE_momentum_120d_high | SWING10|2R|20d | test | 32,663 | 39.58% | -0.05% | [-2.33%, 2.08%] | 0.35% | 0.01 | 0.99 | 41 |
| └ 무작위 진입 (같은 청산: BASE) | SWING10|2R|20d | dev | 78,378 | 35.46% | -0.16% | [-0.75%, 0.49%] | 0.26% | -0.17 | 0.96 | 71 |
| └ 무작위 진입 (같은 청산: BASE) | SWING10|2R|20d | validation | 27,601 | 33.84% | -0.60% | [-1.38%, 0.24%] | -0.18% | -0.22 | 0.85 | 61 |
| └ 무작위 진입 (같은 청산: BASE) | SWING10|2R|20d | test | 36,076 | 32.68% | -0.72% | [-1.57%, 0.11%] | -0.31% | -0.26 | 0.85 | 71 |

무작위 비교 (같은 기간·같은 청산 규칙·같은 거래 수로 무작위 추출 1,000회):

| 전략 | 기간 | 거래 | 전략 평균 | 무작위 평균 | 무작위 같은 n 5~95% | 이긴 비율 | 단측 p |
|---|---|---|---|---|---|---|---|
| A_volume_quiet | dev | 18,064 | 0.63% | -0.26% | [-0.37%, -0.15%] | 100.0% | 0.000 |
| A_volume_quiet | validation | 10,596 | -0.93% | -0.82% | [-0.95%, -0.70%] | 8.0% | 0.920 |
| A_volume_quiet | test | 13,329 | -0.83% | -0.54% | [-0.67%, -0.43%] | 0.0% | 1.000 |
| B1_quiet_then_breakout | dev | 1,012 | -0.92% | -0.33% | [-0.75%, 0.09%] | 1.0% | 0.990 |
| B1_quiet_then_breakout | validation | 498 | -0.64% | -0.72% | [-1.30%, -0.11%] | 59.7% | 0.403 |
| B1_quiet_then_breakout | test | 696 | -1.07% | -0.54% | [-1.07%, -0.07%] | 4.5% | 0.955 |
| B2_breakout_with_volume | dev | 51,024 | -0.35% | -0.14% | [-0.19%, -0.09%] | 0.0% | 1.000 |
| B2_breakout_with_volume | validation | 16,063 | -2.32% | -0.67% | [-0.77%, -0.58%] | 0.0% | 1.000 |
| B2_breakout_with_volume | test | 24,336 | -1.25% | -0.75% | [-0.83%, -0.66%] | 0.0% | 1.000 |
| C_pullback_rebound | dev | 50,809 | -0.16% | -0.14% | [-0.20%, -0.09%] | 31.3% | 0.687 |
| C_pullback_rebound | validation | 13,910 | -1.34% | -0.67% | [-0.78%, -0.56%] | 0.0% | 1.000 |
| C_pullback_rebound | test | 22,521 | -0.97% | -0.75% | [-0.84%, -0.66%] | 0.0% | 1.000 |
| BASE_momentum_120d_high | dev | 62,797 | -0.10% | -0.16% | [-0.19%, -0.12%] | 100.0% | 0.000 |
| BASE_momentum_120d_high | validation | 16,606 | -1.44% | -0.60% | [-0.68%, -0.51%] | 0.0% | 1.000 |
| BASE_momentum_120d_high | test | 32,663 | -0.05% | -0.72% | [-0.76%, -0.69%] | 100.0% | 0.000 |

## 6. 연도별 워크포워드 (해당 연도 이전 데이터로만 청산 규칙 선택)
| 전략 | 연도 수 | 표본 외 거래 | 평균>0 연도 | 연도 평균의 평균 |
|---|---|---|---|---|
| A_volume_quiet | 8 | 35,458 | 4 | -0.05% |
| B1_quiet_then_breakout | 8 | 1,908 | 0 | -0.92% |
| B2_breakout_with_volume | 8 | 72,650 | 0 | -1.02% |
| BASE_momentum_120d_high | 8 | 90,215 | 1 | -0.90% |
| C_pullback_rebound | 8 | 70,491 | 1 | -0.81% |

## 7. 포트폴리오 백테스트 (자금·보유 수 제약, dev에서 고른 청산 규칙)
| 실행 | 기간 | 총수익 | 연환산 | MDD | 샤프 | 비용 전 총수익 | KOSPI 대비 | 거래 | 승률 | PF | 진입 중단일 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BENCH_KOSPI_buy_hold | dev | 55.19% | 7.62% | -43.90% | 0.53 | — | — | — | — | — | — |
| BENCH_KOSDAQ_buy_hold | dev | 52.55% | 7.31% | -53.79% | 0.43 | — | — | — | — | — | — |
| A_volume_quiet | dev | 133.77% | 15.24% | -30.47% | 1.00 | 271.45% | 78.58% | 1,229 | 41.25% | 1.24 | 30 |
| B1_quiet_then_breakout | dev | -43.74% | -9.16% | -44.90% | -1.13 | -26.54% | -98.93% | 874 | 36.27% | 0.80 | 42 |
| B2_breakout_with_volume | dev | -15.01% | -2.68% | -21.76% | -0.38 | -13.71% | -70.20% | 776 | 35.18% | 0.88 | 0 |
| C_pullback_rebound | dev | -25.94% | -4.89% | -39.44% | -0.40 | -14.69% | -81.12% | 1,028 | 33.27% | 0.88 | 19 |
| BASE_momentum_120d_high | dev | -21.44% | -3.95% | -30.27% | -0.56 | -8.51% | -76.63% | 761 | 33.51% | 0.82 | 20 |
| RANDOM_seed1 | dev | -48.01% | -10.35% | -64.80% | -0.57 | 13.15% | -103.20% | 1,475 | 29.83% | 0.86 | 96 |
| RANDOM_seed2 | dev | 4.52% | 0.74% | -52.28% | 0.13 | 0.38% | -50.67% | 1,531 | 33.31% | 1.01 | 55 |
| RANDOM_seed3 | dev | -31.61% | -6.15% | -58.33% | -0.32 | 25.17% | -86.80% | 1,511 | 31.77% | 0.93 | 76 |
| RANDOM_seed4 | dev | -20.59% | -3.78% | -55.96% | -0.15 | 47.38% | -75.78% | 1,504 | 31.85% | 0.95 | 78 |
| RANDOM_seed5 | dev | -56.62% | -13.02% | -67.77% | -0.75 | -20.56% | -111.81% | 1,511 | 29.72% | 0.82 | 95 |
| BENCH_KOSPI_buy_hold | validation | -11.16% | -5.79% | -27.89% | -0.27 | — | — | — | — | — | — |
| BENCH_KOSDAQ_buy_hold | validation | -16.50% | -8.70% | -37.22% | -0.24 | — | — | — | — | — | — |
| A_volume_quiet | validation | -6.07% | -3.11% | -21.73% | -0.09 | 1.69% | 5.09% | 463 | 34.13% | 0.96 | 3 |
| B1_quiet_then_breakout | validation | -20.36% | -10.85% | -23.67% | -1.08 | -11.60% | -9.20% | 447 | 36.69% | 0.84 | 1 |
| B2_breakout_with_volume | validation | -24.47% | -13.20% | -25.09% | -1.99 | -23.46% | -13.32% | 271 | 27.68% | 0.52 | 8 |
| C_pullback_rebound | validation | -33.53% | -18.62% | -34.20% | -1.78 | -32.30% | -22.37% | 356 | 28.09% | 0.62 | 20 |
| BASE_momentum_120d_high | validation | -20.53% | -10.95% | -22.81% | -1.66 | -18.87% | -9.38% | 291 | 29.55% | 0.63 | 0 |
| RANDOM_seed1 | validation | -37.74% | -21.27% | -39.28% | -1.16 | -14.70% | -26.59% | 523 | 26.58% | 0.74 | 50 |
| RANDOM_seed2 | validation | -34.86% | -19.44% | -43.78% | -1.12 | -17.81% | -23.70% | 522 | 31.23% | 0.77 | 48 |
| RANDOM_seed3 | validation | -33.28% | -18.47% | -35.75% | -1.00 | -14.96% | -22.13% | 538 | 30.67% | 0.79 | 24 |
| RANDOM_seed4 | validation | -18.85% | -10.00% | -25.95% | -0.46 | 6.29% | -7.70% | 542 | 30.63% | 0.88 | 25 |
| RANDOM_seed5 | validation | -43.14% | -24.79% | -45.61% | -1.52 | -33.07% | -31.98% | 497 | 28.57% | 0.65 | 46 |
| BENCH_KOSPI_buy_hold | test | 107.89% | 29.80% | -39.29% | 0.92 | — | — | — | — | — | — |
| BENCH_KOSDAQ_buy_hold | test | -14.48% | -5.42% | -47.42% | 0.01 | — | — | — | — | — | — |
| A_volume_quiet | test | -18.00% | -6.83% | -27.98% | -0.34 | -15.15% | -125.89% | 619 | 30.37% | 0.90 | 27 |
| B1_quiet_then_breakout | test | -39.09% | -16.19% | -40.35% | -1.79 | -30.07% | -146.98% | 623 | 34.35% | 0.78 | 20 |
| B2_breakout_with_volume | test | -19.04% | -7.25% | -22.48% | -1.07 | -15.63% | -126.93% | 361 | 32.41% | 0.71 | 0 |
| C_pullback_rebound | test | -45.11% | -19.24% | -45.38% | -2.05 | -36.91% | -153.00% | 490 | 25.31% | 0.60 | 39 |
| BASE_momentum_120d_high | test | -18.60% | -7.07% | -20.47% | -1.07 | -16.28% | -126.49% | 393 | 29.52% | 0.75 | 0 |
| RANDOM_seed1 | test | -42.86% | -18.08% | -46.17% | -0.91 | -30.89% | -150.75% | 820 | 27.20% | 0.81 | 58 |
| RANDOM_seed2 | test | -45.71% | -19.56% | -50.68% | -0.94 | -33.87% | -153.60% | 863 | 26.19% | 0.82 | 54 |
| RANDOM_seed3 | test | -28.36% | -11.20% | -41.03% | -0.51 | -24.86% | -136.25% | 781 | 28.81% | 0.88 | 50 |
| RANDOM_seed4 | test | -46.13% | -19.78% | -46.44% | -1.10 | -14.73% | -154.02% | 798 | 26.44% | 0.79 | 53 |
| RANDOM_seed5 | test | -26.78% | -10.51% | -35.92% | -0.44 | 0.80% | -134.67% | 844 | 27.73% | 0.91 | 31 |

포트폴리오 규칙: 초기 자본 1억 원, 거래당 위험 0.5%, 최대 10종목, 종목당 20%, 일일 손실 3% 시 다음 날 진입 중단, 고점 대비 25% 낙폭 시 20영업일 신규 진입 중단 후 고점 재설정. 포트폴리오 결과는 자금·보유 수 제약 때문에 경로 의존적이다 — 비용 0 실행은 다른 거래 순서를 따르므로 '비용 전 총수익'과 '총수익'의 차이를 비용 효과로 읽으면 안 된다(비용 효과는 9절 거래 단위 표 참고).

## 8. 시장 국면별 (KOSPI 60거래일 수익률 ±5%)
| 전략 | 기간 | 국면 | 거래 | 평균 순수익 | 95% CI | 승률 |
|---|---|---|---|---|---|---|
| A_volume_quiet | dev | bear | 3,126 | 0.60% | [-1.00%, 2.44%] | 36.34% |
| A_volume_quiet | dev | bull | 5,687 | 1.57% | [0.27%, 2.87%] | 43.96% |
| A_volume_quiet | dev | sideways | 9,170 | 0.01% | [-1.11%, 1.23%] | 36.12% |
| A_volume_quiet | dev | unknown | 81 | 6.23% | — | 55.56% |
| A_volume_quiet | test | bear | 2,045 | -0.34% | [-1.60%, 1.46%] | 30.90% |
| A_volume_quiet | test | bull | 7,174 | -0.82% | [-2.08%, 0.36%] | 30.89% |
| A_volume_quiet | test | sideways | 4,110 | -1.09% | [-2.37%, 0.68%] | 29.39% |
| A_volume_quiet | validation | bear | 3,937 | 0.32% | [-1.29%, 1.92%] | 38.74% |
| A_volume_quiet | validation | bull | 1,004 | 0.41% | [-2.49%, 3.32%] | 39.44% |
| A_volume_quiet | validation | sideways | 5,655 | -2.04% | [-3.76%, 0.01%] | 26.08% |
| B1_quiet_then_breakout | dev | bear | 213 | -0.94% | [-2.94%, 0.23%] | 36.15% |
| B1_quiet_then_breakout | dev | bull | 333 | -0.39% | [-1.40%, 0.54%] | 39.04% |
| B1_quiet_then_breakout | dev | sideways | 463 | -1.32% | [-2.25%, -0.28%] | 33.69% |
| B1_quiet_then_breakout | dev | unknown | 3 | 4.40% | — | 66.67% |
| B1_quiet_then_breakout | test | bear | 121 | -2.09% | [-3.92%, -0.46%] | 29.75% |
| B1_quiet_then_breakout | test | bull | 349 | -0.95% | [-1.74%, -0.19%] | 34.10% |
| B1_quiet_then_breakout | test | sideways | 226 | -0.71% | [-2.54%, 1.10%] | 37.61% |
| B1_quiet_then_breakout | validation | bear | 142 | -1.56% | [-2.74%, -0.15%] | 31.69% |
| B1_quiet_then_breakout | validation | bull | 91 | 1.09% | [-1.60%, 3.51%] | 46.15% |
| B1_quiet_then_breakout | validation | sideways | 265 | -0.75% | [-2.33%, 0.96%] | 36.23% |
| B2_breakout_with_volume | dev | bear | 6,142 | 0.45% | [-2.85%, 2.60%] | 41.08% |
| B2_breakout_with_volume | dev | bull | 20,681 | 0.88% | [-0.57%, 2.42%] | 41.08% |
| B2_breakout_with_volume | dev | sideways | 23,072 | -1.72% | [-3.13%, -0.44%] | 35.25% |
| B2_breakout_with_volume | dev | unknown | 1,129 | 0.85% | [0.48%, 1.04%] | 43.05% |
| B2_breakout_with_volume | test | bear | 2,749 | -5.91% | [-9.60%, -2.46%] | 28.92% |
| B2_breakout_with_volume | test | bull | 15,474 | -0.19% | [-3.02%, 2.21%] | 38.64% |
| B2_breakout_with_volume | test | sideways | 6,113 | -1.85% | [-4.95%, 1.39%] | 37.53% |
| B2_breakout_with_volume | validation | bear | 4,257 | -2.29% | [-3.72%, -1.10%] | 34.25% |
| B2_breakout_with_volume | validation | bull | 3,076 | 0.04% | [-3.07%, 2.86%] | 37.42% |
| B2_breakout_with_volume | validation | sideways | 8,730 | -3.17% | [-5.88%, -0.54%] | 31.62% |
| C_pullback_rebound | dev | bear | 3,450 | -1.17% | [-3.27%, 0.79%] | 33.54% |
| C_pullback_rebound | dev | bull | 24,390 | 0.76% | [-0.79%, 2.30%] | 38.31% |
| C_pullback_rebound | dev | sideways | 22,969 | -0.98% | [-2.23%, 0.19%] | 33.20% |
| C_pullback_rebound | test | bear | 1,830 | -2.52% | [-5.19%, 0.57%] | 30.49% |
| C_pullback_rebound | test | bull | 15,440 | -0.67% | [-2.89%, 1.55%] | 32.58% |
| C_pullback_rebound | test | sideways | 5,251 | -1.32% | [-3.06%, 0.41%] | 33.04% |
| C_pullback_rebound | validation | bear | 3,199 | 0.18% | [-2.00%, 1.60%] | 37.70% |
| C_pullback_rebound | validation | bull | 2,887 | -0.16% | [-2.46%, 1.70%] | 34.50% |
| C_pullback_rebound | validation | sideways | 7,824 | -2.39% | [-4.41%, -0.46%] | 27.95% |
| BASE_momentum_120d_high | dev | bear | 5,266 | 0.70% | [-2.26%, 2.66%] | 40.24% |
| BASE_momentum_120d_high | dev | bull | 29,865 | 1.05% | [-0.36%, 2.45%] | 41.96% |
| BASE_momentum_120d_high | dev | sideways | 27,666 | -1.48% | [-2.93%, -0.13%] | 35.97% |
| BASE_momentum_120d_high | test | bear | 1,968 | -4.50% | [-8.42%, -0.70%] | 31.86% |
| BASE_momentum_120d_high | test | bull | 23,557 | 0.64% | [-2.09%, 3.07%] | 40.13% |
| BASE_momentum_120d_high | test | sideways | 7,138 | -1.12% | [-4.85%, 2.65%] | 39.90% |
| BASE_momentum_120d_high | validation | bear | 3,123 | -1.73% | [-3.94%, -0.09%] | 36.25% |
| BASE_momentum_120d_high | validation | bull | 4,155 | 0.55% | [-3.03%, 3.97%] | 38.63% |
| BASE_momentum_120d_high | validation | sideways | 9,328 | -2.23% | [-4.88%, 0.22%] | 34.18% |
| RANDOM_pool | dev | bear | 13,562 | 0.74% | [-0.88%, 2.50%] | 43.33% |
| RANDOM_pool | dev | bull | 27,896 | -0.12% | [-0.74%, 0.45%] | 39.73% |
| RANDOM_pool | dev | sideways | 37,524 | -0.88% | [-1.45%, -0.33%] | 36.56% |
| RANDOM_pool | dev | unknown | 1,801 | 0.27% | [-6.29%, 0.70%] | 45.25% |
| RANDOM_pool | test | bear | 5,615 | 0.55% | [-1.01%, 2.51%] | 42.33% |
| RANDOM_pool | test | bull | 21,913 | -1.16% | [-2.62%, 0.07%] | 35.76% |
| RANDOM_pool | test | sideways | 9,778 | -0.90% | [-2.03%, 0.04%] | 36.66% |
| RANDOM_pool | validation | bear | 9,977 | 0.23% | [-0.94%, 1.36%] | 44.84% |
| RANDOM_pool | validation | bull | 3,814 | -0.40% | [-1.31%, 0.73%] | 38.20% |
| RANDOM_pool | validation | sideways | 14,982 | -1.56% | [-2.51%, -0.70%] | 31.66% |

## 9. 비용 민감도 (거래 단위 평균 순수익)
| 전략 | 기간 | 비용 0 | 기본 | 슬리피지 3배 |
|---|---|---|---|---|
| A_volume_quiet | dev | 1.06% | 0.63% | 0.26% |
| A_volume_quiet | test | -0.41% | -0.83% | -1.21% |
| A_volume_quiet | validation | -0.53% | -0.93% | -1.25% |
| B1_quiet_then_breakout | dev | -0.49% | -0.92% | -1.23% |
| B1_quiet_then_breakout | test | -0.74% | -1.07% | -1.31% |
| B1_quiet_then_breakout | validation | -0.28% | -0.64% | -1.06% |
| B2_breakout_with_volume | dev | 0.07% | -0.35% | -0.73% |
| B2_breakout_with_volume | test | -0.83% | -1.25% | -1.63% |
| B2_breakout_with_volume | validation | -1.91% | -2.32% | -2.70% |
| BASE_momentum_120d_high | dev | 0.33% | -0.10% | -0.46% |
| BASE_momentum_120d_high | test | 0.35% | -0.05% | -0.44% |
| BASE_momentum_120d_high | validation | -1.01% | -1.44% | -1.80% |
| C_pullback_rebound | dev | 0.26% | -0.16% | -0.52% |
| C_pullback_rebound | test | -0.56% | -0.97% | -1.33% |
| C_pullback_rebound | validation | -0.92% | -1.34% | -1.72% |

## 10. ML 필터 vs 단순 규칙
dev 기간으로 학습(마지막 20%로 확률 보정), validation에서 채택 여부 판정, test는 최종 보고만. 라벨: ATR1.5|2R|10d 기준 익절이 손절보다 먼저(시간 청산은 음성). 선택 = 예측확률 ≥ dev 양성비율 + 5%p.

| 전략 | 모델 | 기간 | AUC | Brier skill | 규칙 거래 | 규칙 평균 | ML 거래 | ML 평균 | ML 95% CI | 채택(검증) | 불채택 사유 / 비고 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A_volume_quiet | logistic | validation | 0.568 | -0.005 | 10,595 | -0.99% | 1,123 | -1.47% | [-2.45%, -0.28%] | 아니오 | Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0; 선택 거래 평균이 규칙 전체 평균보다 낫지 않음 |
| A_volume_quiet | logistic | test | 0.583 | 0.003 | 13,325 | -0.71% | 1,592 | -0.27% | [-1.12%, 0.52%] | — |  |
| A_volume_quiet | random_forest | validation | 0.570 | 0.002 | 10,595 | -0.99% | 1,177 | -0.89% | [-1.84%, 0.19%] | 아니오 | 선택 거래 순수익 95% CI 하한 <= 0 |
| A_volume_quiet | random_forest | test | 0.586 | 0.007 | 13,325 | -0.71% | 1,658 | -0.22% | [-0.94%, 0.51%] | — |  |
| A_volume_quiet | hist_gb | validation | 0.549 | 0.000 | 10,595 | -0.99% | 519 | -1.43% | [-2.46%, -0.21%] | 아니오 | AUC < 0.55; 선택 거래 순수익 95% CI 하한 <= 0; 선택 거래 평균이 규칙 전체 평균보다 낫지 않음 |
| A_volume_quiet | hist_gb | test | 0.574 | 0.006 | 13,325 | -0.71% | 626 | -0.68% | [-1.68%, 0.47%] | — |  |
| B1_quiet_then_breakout | logistic | validation | 0.457 | 0.003 | 498 | -1.17% | 2 | -8.62% | [-9.38%, -7.87%] | 아니오 | AUC < 0.55; 선택 거래 < 30; 선택 거래 평균이 규칙 전체 평균보다 낫지 않음 |
| B1_quiet_then_breakout | logistic | test | 0.473 | 0.009 | 694 | -1.94% | 6 | -8.67% | [-17.74%, 4.93%] | — |  |
| B1_quiet_then_breakout | random_forest | validation | 0.480 | -0.000 | 498 | -1.17% | 78 | -3.08% | [-5.79%, -0.09%] | 아니오 | AUC < 0.55; Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0; 선택 거래 평균이 규칙 전체 평균보다 낫지 않음 |
| B1_quiet_then_breakout | random_forest | test | 0.491 | 0.007 | 694 | -1.94% | 119 | -3.07% | [-4.96%, -1.07%] | — |  |
| B1_quiet_then_breakout | hist_gb | validation | 0.445 | -0.007 | 498 | -1.17% | 90 | -2.49% | [-4.32%, -0.93%] | 아니오 | AUC < 0.55; Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0; 선택 거래 평균이 규칙 전체 평균보다 낫지 않음 |
| B1_quiet_then_breakout | hist_gb | test | 0.485 | 0.002 | 694 | -1.94% | 159 | -2.14% | [-4.32%, 0.15%] | — |  |
| B2_breakout_with_volume | logistic | validation | 0.560 | -0.002 | 16,065 | -1.07% | 5,794 | -0.78% | [-1.36%, -0.21%] | 아니오 | Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0 |
| B2_breakout_with_volume | logistic | test | 0.528 | -0.004 | 24,628 | -0.86% | 12,850 | -0.81% | [-1.53%, -0.23%] | — |  |
| B2_breakout_with_volume | random_forest | validation | 0.573 | 0.000 | 16,065 | -1.07% | 4,081 | -0.50% | [-1.28%, 0.20%] | 아니오 | 선택 거래 순수익 95% CI 하한 <= 0 |
| B2_breakout_with_volume | random_forest | test | 0.533 | -0.002 | 24,628 | -0.86% | 7,903 | -0.58% | [-1.23%, -0.02%] | — |  |
| B2_breakout_with_volume | hist_gb | validation | 0.564 | -0.001 | 16,065 | -1.07% | 4,055 | -0.70% | [-1.40%, -0.07%] | 아니오 | Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0 |
| B2_breakout_with_volume | hist_gb | test | 0.532 | -0.002 | 24,628 | -0.86% | 7,996 | -0.58% | [-1.34%, 0.01%] | — |  |
| C_pullback_rebound | logistic | validation | 0.553 | -0.005 | 13,916 | -1.12% | 3,966 | -1.08% | [-1.72%, -0.54%] | 아니오 | Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0 |
| C_pullback_rebound | logistic | test | 0.549 | -0.008 | 22,635 | -0.84% | 10,027 | -0.60% | [-1.42%, 0.28%] | — |  |
| C_pullback_rebound | random_forest | validation | 0.561 | -0.004 | 13,916 | -1.12% | 4,756 | -0.82% | [-1.54%, -0.15%] | 아니오 | Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0 |
| C_pullback_rebound | random_forest | test | 0.560 | 0.001 | 22,635 | -0.84% | 8,908 | -0.56% | [-1.26%, 0.10%] | — |  |
| C_pullback_rebound | hist_gb | validation | 0.558 | -0.008 | 13,916 | -1.12% | 3,060 | -0.62% | [-1.39%, 0.10%] | 아니오 | Brier skill <= 0 (확률이 기준 비율보다 낫지 않음); 선택 거래 순수익 95% CI 하한 <= 0 |
| C_pullback_rebound | hist_gb | test | 0.550 | -0.008 | 22,635 | -0.84% | 6,641 | -0.64% | [-1.44%, 0.17%] | — |  |
| BASE_momentum_120d_high | logistic | validation | 0.589 | 0.004 | 16,612 | -1.19% | 5,495 | -0.41% | [-1.24%, 0.32%] | 아니오 | 선택 거래 순수익 95% CI 하한 <= 0 |
| BASE_momentum_120d_high | logistic | test | 0.536 | 0.000 | 32,782 | -0.56% | 15,306 | -0.64% | [-1.56%, 0.19%] | — |  |
| BASE_momentum_120d_high | random_forest | validation | 0.592 | 0.004 | 16,612 | -1.19% | 4,283 | 0.10% | [-0.94%, 1.03%] | 아니오 | 선택 거래 순수익 95% CI 하한 <= 0 |
| BASE_momentum_120d_high | random_forest | test | 0.540 | 0.001 | 32,782 | -0.56% | 10,546 | -0.35% | [-1.28%, 0.45%] | — |  |
| BASE_momentum_120d_high | hist_gb | validation | 0.588 | 0.005 | 16,612 | -1.19% | 3,263 | 0.06% | [-0.92%, 0.90%] | 아니오 | 선택 거래 순수익 95% CI 하한 <= 0 |
| BASE_momentum_120d_high | hist_gb | test | 0.534 | -0.003 | 32,782 | -0.56% | 9,091 | -0.50% | [-1.38%, 0.27%] | — |  |

## 11. 재현

```bash
python scripts/collect_kr_daily.py --out data/real --start 2016-01-01   # 데이터 수집 (재실행 시 이어받기)
python -m app.research --data /home/claude/realdata --format naver_raw --out /home/claude/realdata/reports/v0.3
```

실행 시간 771.5초 · 단계별 {'prepare_s': 147.1, 'signals_s': 15.0, 'grid_s': 324.1, 'portfolio_s': 130.0}

## 12. 파일

`strategy_exit_grid.csv`(전 조합×기간), `walk_forward_by_year.csv`, `vs_random_entry.csv`, `by_regime.csv`, `cost_sensitivity.csv`, `portfolio_results.csv`, `ml_vs_rule.csv`, `ml_reliability.json`, `data_quality_summary.csv`, `data_quality_issues.csv`, `provider_halt_rows.csv`, `trades/portfolio_*.csv`(포트폴리오 거래별 진입·청산), `trades/signals_*.csv`(신호별 결과), `run_manifest.json`.
