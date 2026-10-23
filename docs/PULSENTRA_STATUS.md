# Pulsentra 개발 현황

## v0.4 (진행 중, 2026-10-23) — 데이터 감사·재현성·교차 검증

| 항목 | 상태 |
|---|---|
| 종목 단위 데이터 감사 | **구현·합성 데이터로 검증** (`python -m app.audit`, `docs/DATA_AUDIT.md`). 실제 데이터로는 미실행: 개발 환경에 `data/real/` 없음 |
| 재현성 | manifest v2(커밋·패키지·시드·경고·산출물 해시), `requirements-lock.txt`, 같은 입력 두 번 실행 시 결과 해시 동일 확인 |
| 공식 출처 교차 검증 | **도구 구현, 실행 안 함** — KIS·DART 키 없음 (`docs/CROSS_VALIDATION.md`) |
| 기준선 | `main` = `v0.3.0` = `a57dcc1` 보존. v0.3 실행 커밋 `4918af9`는 저장소에 없음(설정은 `a57dcc1`과 일치, 코드 동일성은 증명 불가) |
| 연구 결론 | v0.3 결론 유지. 재실행하지 않음 |
| 코드 동일성 | 같은 TEST 데이터에서 v0.3.0 코드와 결과 표가 동일함(`scripts/compare_baseline.py`). 실제 데이터로는 미확인 |
| 실제 데이터 단계 | 절차서 `docs/REAL_DATA_RUNBOOK.md`. 감사·교차 검증·재실행 모두 미실행 |
| 테스트 | `python -m pytest -q` → 아래 v0.4 테스트 절 (인계 시점 168 passed, 2 skipped) |

### v0.4 테스트

명령: `python -m pytest -q -W error::DeprecationWarning` (2026-10-23, Python 3.13 / numpy 2.5.4 / pandas 2.3.3, 외부 네트워크 없이)

- v0.3의 77개는 그대로 통과합니다. 감사, manifest, 기간 경계, 결과 상태, 어댑터 재시도·스키마, 교차 검증, 저장소 위생 테스트를 추가했습니다.
- 실제 API 스모크 테스트 2개는 키가 없어 건너뜁니다.
- DeprecationWarning 173건 → 0건. 남은 경고 1건은 Starlette TestClient가 내는 서드파티 경고입니다.
- Python 3.11(범위 의존성)과 3.12(잠금 파일)에서도 통과를 확인했습니다.

## v0.3 (2026-10-23) — 실제 데이터 전략 검증

| 항목 | 상태 |
|---|---|
| 실제 데이터 | **확보함** — KOSPI·KOSDAQ 보통주 2,823개(상장폐지 273개 포함) 일봉 2016-01~2026-10, KOSPI·KOSDAQ 지수. 출처는 비공식 웹 엔드포인트·제3자 캐시 → [`DATA_SOURCES.md`](DATA_SOURCES.md) |
| 공식 API 실호출 | **안 함** — KRX 정보데이터시스템은 로그인 필요, KIS·OpenDART는 키 없음 |
| 전략 검증 | 완료 — A·B1·B2·C·모멘텀 기준 × 청산 48조합 × dev/validation/test, 워크포워드, 무작위·국면·비용·포트폴리오 → [`RESEARCH_V0_3.md`](RESEARCH_V0_3.md), `reports/v0.3/` |
| 결론 | 비용 차감 후 validation·test 양쪽에서 우위가 확인된 전략 없음. A는 dev에서만 우위(과적합으로 판단) |
| ML | 15개 비교 모두 validation 채택 기준 불통과 → 운영 신호로 쓰지 않음 |
| 발견·수정한 결함 | 낙폭 진입 중단의 영구 정지, 수정주가 반올림 오차 처리, 거래정지 평평한 행, 거래량 미수정 분할 → `RESEARCH_V0_3.md` §5 |
| 테스트 | `python -m pytest -q` → 아래 v0.3 테스트 절 참고 |

### v0.3 테스트

명령: `python -m pytest -q` (2026-10-23, 외부 네트워크 없이 실행)

v0.2 테스트 65개에 아래 항목을 추가했습니다.
- `tests/test_research.py`: 일괄 시뮬레이터와 단일 시뮬레이터가 신호별로 같은 결과를 내는지 6개 청산 규칙으로 확인, 반올림 보정, 분할 의심 탐지, REAL·합성 혼합 거부, 표준 CSV 로더의 합성 거부, 리서치 CLI 처음부터 끝까지 실행
- `tests/test_backtest_engine.py`: 낙폭 진입 중단 후 재개

---

# v0.2 기록 (참고)

작성 기준: 2026-10-23. 코드·테스트로 확인한 내용만 적었습니다.

## 1. 실제 데이터 연결 상태

| 소스 | 코드 | 이번 작업에서 실제 호출 | 비고 |
|---|---|---|---|
| CSV 업로드 | 구현 | — (사용자 파일 필요) | HISTORICAL로 저장 |
| KIS 일봉·현재가 REST | 구현 (MVP 어댑터 개선) | **안 함** — API 키 없음 | 필드·TR ID·`FID_ORG_ADJ_PRC=0`(수정주가)·1회 최대 100건은 KIS 예제 문서를 참고했다. 첫 실제 호출 결과를 확인해야 한다 |
| KIS WebSocket 실시간 | **미구현** | — | 승인키 발급·구독·재연결·장 운영시간 처리가 필요하다. 공식 명세를 확인하지 못해 엔드포인트를 만들지 않았다 |
| OpenDART | 구현 (`corpCode.xml`, `list.json`) | **안 함** — 키 없음, 모의 응답 테스트만 | 요청·응답 필드는 OpenDART 공식 개발가이드 기준 |
| KRX OPEN API | **미구현** | — | 이용 계약·제공 필드 확인 필요 |
| 해외 주식 | CSV만 | — | 제공자 인터페이스(`app/providers`)는 분리되어 있음 |

**LIVE 상태는 현재 어떤 데이터에도 표시되지 않습니다.** 실시간 스트림이 없기 때문입니다. KIS 일봉과 현재가 조회는 DELAYED로 표시합니다.

## 2. 실제 데이터 기반 성능 검증 여부

**하지 않았습니다.** 이 환경에는 실제 시세 데이터와 API 키가 없습니다. 아래 수치는 모두 합성 데이터로 코드가 동작하는지 확인한 결과이며, 시장에 대해 아무것도 말해 주지 않습니다.

합성 데이터 실행 예시 (`make_demo_data(days=800, n_symbols=12, end="2026-10-16", include_index=True)`):

| 항목 | 결과 (합성) | 해석 |
|---|---|---|
| `volume_quiet_breakout` 포트폴리오 백테스트 | 거래 18건, 총수익 +0.16%, MDD −2.4%, 승률 95% CI [24.6%, 66.3%], 기대값 +0.02R (95% CI [−0.54, +0.58]) | 표본이 작고 신뢰구간이 0을 포함한다. 무작위 진입 10회 중 백분위 50% → 우위 없음 |
| ML 삼중 장벽(10일, 2R) 워크포워드 4폴드 | 표본 외 AUC: 로지스틱 0.586, RF 0.540, HistGB 0.547 / Brier skill 모두 음수 | 채택 게이트 **불통과**. 합성 데이터 모델은 원칙적으로 채택하지 않는다 |
| 모의투자 vs 백테스트 (같은 규칙·기간) | 거래 수·총수익 일치 | 두 엔진의 체결 규칙이 일치한다는 구현 검증일 뿐, 성과 검증이 아님 |

모델 채택 기준(전부 충족해야 함): 실데이터, 누수 검사 통과, 표본 외 300행 이상, 평균 AUC 0.55 이상이면서 75% 이상의 폴드에서 0.5 초과, Brier skill 0 초과, 모델이 고른 거래 30건 이상 + 비용 차감 평균의 95% CI 하한 0 초과, 단순 규칙 평균보다 우수.

## 3. 테스트

명령: `python -m pytest -q` → **65 passed** (2026-10-23, Python 3.13 / pandas 2.3 / scikit-learn 1.9, 외부 네트워크 없음)

| 요구 테스트 | 위치 |
|---|---|
| 1 거래량 배수 | `test_features_and_risk.py::test_volume_ratio_exact`, `…_unknown_with_short_history` |
| 2 주가 등락률 | `test_ret_20` |
| 3 거래대금 증가율 | `test_turnover_ratio` |
| 4 ATR·손절가 | `test_atr_true_range_and_stop`, `test_true_range_uses_previous_close_gap` |
| 5 R 목표가 | `test_r_targets` |
| 6 매수 수량 | `test_position_size_by_risk` |
| 7 수수료·슬리피지 | `test_cost_model_math`, `test_backtest_engine.py::test_costs_reduce_pnl_exactly` |
| 8 다음 거래일 진입가 | `test_entry_is_next_open_plus_slippage`, `test_no_entry_when_signal_on_last_bar` |
| 9 손절·익절 동시 도달 | `test_same_bar_stop_and_target_is_stop` (+ 갭·익절·시간·일부 익절·보유 한도·상장폐지) |
| 10 데이터 누수 방지 | `test_validation_and_ml.py::test_every_indicator_is_causal`, `test_features_do_not_use_future_bars`, `test_signals_unchanged_by_future_shock`, `test_labels_look_forward_and_record_end` |
| 11 시간순 분할 | `test_time_folds_are_ordered_and_gapped`, `test_walk_forward_purges_overlapping_labels` |
| 12 모델 성능 계산 | `test_classification_metrics_against_manual`, `test_ml_lab_refuses_synthetic_by_default_and_never_adopts` |
| 13 CSV 적재·품질 검사 | `test_data_center.py` (0 채움, 오류/경고 분류, 중복, 분할 의심, 수집 기록, MVP DB 마이그레이션) |
| 14 API 오류·데이터 지연 상태 | `test_status_rules`, `test_failed_collection_shows_error`, `test_feed_delay_status`, `test_api.py::test_kis_not_configured_and_errors` |
| 15 핵심 API 통합 | `test_api.py` 11개 (임시 DB, TestClient) |

테스트가 실제로 결함을 잡는지 확인하려고 결함을 일부러 넣어 봤습니다(변이 확인). 6개 모두 테스트 실패로 이어졌고, 확인 후 원래 코드로 되돌렸습니다.

- 같은 봉에서 손절 우선 규칙 제거
- 거래량 창을 한 칸 어긋나게 계산
- 20일 고점에 다음 날 고가 사용 (미래 참조)
- 워크포워드 퍼지 제거
- 진입가를 신호일 종가로 변경
- 매도 슬리피지 제거

처음에는 미래 참조와 퍼지 제거 두 가지를 테스트가 잡지 못했습니다. 그래서 모든 지표의 인과성 테스트와 학습 라벨 종료일 검사를 추가했습니다.

브라우저 확인: Playwright(Chromium)로 데스크톱 1440px와 모바일 390px에서 7개 탭의 주요 동작을 실행했습니다. 콘솔 오류 0건, 모바일 가로 넘침 없음.

## 4. 아직 구현하지 않은 기능

- KIS WebSocket 실시간 수집, 장중 분봉 저장, 장중 신호 갱신 (분봉 집계 함수만 있음)
- KRX OPEN API, 해외 주식 API
- 기업행동(분할·병합·배당) 자동 조정. 현재는 수정주가 여부 기록과 분할 의심 경고까지만 함
- 시점 정보가 있는 재무 데이터 특징 (공개 시점을 확인할 수 없는 재무 데이터는 의도적으로 쓰지 않음)
- 공시 이벤트 기반 전략 검증 (공시 수집과 위험 분류만 있음)
- LightGBM, LSTM (HistGradientBoosting으로 대체. LSTM은 충분한 실데이터가 쌓인 뒤 검토)
- 알림 외부 발송(이메일·메신저), 사용자 계정, 운영 모니터링, 스케줄러
- 실제 주문 — 범위 밖. 검토하려면 별도 승인, 보안 검토, 금융 규제 검토가 필요함

## 5. 알려진 한계

- 포트폴리오 엔진은 종목 수 × 거래일 루프 방식입니다. 수천 종목 × 수년 규모에서는 느릴 수 있습니다.
- 신호 단위 연구(과거 유사 신호)에서 겹치는 신호는 독립 표본이 아닙니다. 그래서 신뢰구간이 실제보다 좁을 수 있고, 화면에도 이 점을 표시합니다.
- 국면 분류는 지수의 60일 수익률 기준 단순 규칙입니다.
- 거래일 누락 검사는 주말만 제외하고 공휴일은 반영하지 않으므로 경고 기준을 5영업일로 두었습니다.
- 비용 기본값(수수료 1.5bp, 매도세 20bp, 슬리피지 10bp)은 가정값입니다. 실제 증권사 요율과 해당 연도의 세율을 확인해야 합니다.

## 6. 다음 개발 우선순위

1. **실데이터 확보:** KOSPI·KOSDAQ 전 종목 일봉(상장폐지 종목 포함)과 지수, 종목 메타데이터를 적재합니다. 그 다음 기본 전략·청산 비교·워크포워드를 실데이터로 처음 실행합니다.
2. **KIS 실호출 검증:** 키를 설정한 뒤 일봉 동기화 결과를 CSV·다른 출처와 대조하고, 수정주가 여부를 확인합니다.
3. **기업행동 조정:** 분할 의심 경고를 실제 조정으로 연결합니다.
4. **실데이터 ML 검증:** ML Lab을 실데이터로 실행합니다. 채택 게이트 결과와 무관하게 결과를 기록합니다.
5. **실시간 수집:** KIS WebSocket과 분봉 저장, 수신 지연 경보.
6. **운영 기능:** 모의투자 일별 자동 처리(스케줄러), 알림 발송.
