# Pulsentra 개발 현황

## v0.4 (진행 중) — 데이터 감사·재현성·교차 검증

갱신 2026-10-25, 브랜치 `research/v0.4-data-audit`(`2292168`, PR #1·#2·#3 병합 후). 2026-10-23 판에서 "실제 데이터로 미실행"이라고 적은 항목 중 일부를 실제로 실행했습니다. **구현**(코드와 자동 테스트가 있음)과 **실행**(실제 데이터·API로 돌려 산출물이 있음)을 나눠 적습니다.

| 항목 | 구현 | 실제 실행 | 근거(로컬 산출물, Git 제외) |
|---|---|---|---|
| 종목 단위 데이터 감사 | ✅ `python -m app.audit` | ✅ 2026-10-24, 같은 데이터로 2회. 원본 6,000,649행 → 사용 가능 5,799,758행, 2,824종목. `audit_status`: ok 597 / review 2,225 / exclude 2. 발견 사항 243,978건(오류 2,088). 두 실행의 산출물 7개와 입력·발견·사용 가능 지문(`a752efeb7f79240e`, `8c12036ff5b87a2d`, `1ea20b72f9562ede`)이 같고, 실행 시각·출력 경로가 들어간 `audit_manifest.json`만 다름. v0.3 보고서의 품질 수치도 재현됨 | `data/audit/full_20261024T060804Z_a`, `_b` |
| 감사 결과의 의미 | — | 이상을 **찾아 분류한 것**이지 해결한 것이 아님. 검토 필요 2,225종목은 검토하지 않았고, 감사 결과를 반영한 전략 연구도 다시 실행하지 않음 | — |
| v0.3 코드와 v0.4 코드의 동일성 | ✅ `scripts/compare_baseline.py` | ✅ 2026-10-24, 같은 실제 데이터(지문 `de52ccfa16e847ee`)에 `a57dcc1`(v0.3.0)과 `2edfc8b` 실행 → `same_results: true`. v0.3.0 재실행 보고서는 저장소의 `reports/v0.3/REPORT.md`와 전략 표가 같음. ML 표 2행만 조금 다름(AUC 0.491→0.490, 선택 거래 4,756→4,757건, CI 하한 −0.15%→−0.16%). 원인 미확인. validation·test 480개 조합 중 비용 차감 평균 양수 0개를 CSV로 다시 셈 | `reports/v0.4-compare/` |
| v0.3 실행 커밋 `4918af9` | — | 여전히 저장소에 없음. 위 재실행은 `a57dcc1` 코드의 재현이며, `4918af9`와의 동일성은 증명하지 않음 | — |
| KRX 시세 대조 | ✅ `python -m app.krxcheck`, `.fullmarket`, `.history` | ✅ 유가증권 일별매매정보를 날짜별 1회(총 4회, 2026-10-24) 요청해 저장하고, 비교는 저장된 응답과 로컬 네이버 일봉으로 오프라인 실행. 결과는 아래 표 | `data/krxcheck/full_market_*`, `history_mac_validation_20261024/` |
| OpenDART 공시 대조 | ✅ `python -m app.crosscheck` | 🟡 삼성전자 1건만(2026-10-25). 나머지 후보 8,072건 미대조 | `data/crosscheck/dart_005930_*` |
| KIS 가격 대조 | ✅ 코드·모의 테스트 | ⬜ 인증정보 없음 | — |
| 감사·교차 검증 반영 재실행(`--audit --crosscheck`) | ✅ 옵션 구현 | ⬜ 미실행 | — |
| 연구 결론 | — | v0.3 결론 유지(재확인). 새 Alpha 결론 없음 | — |
| 테스트 | — | `272 passed, 2 skipped, 1 warning` (2026-10-25) | 아래 v0.4 테스트 절 |

KRX 대조 요약(유가증권, 분모는 양쪽에 거래가 있는 비교 가능 종목):

| 거래일 | KRX 행 | 비교 가능 | OHLCV 완전 일치 | 해석 |
|---|---:|---:|---:|---|
| 2025-02-27 | 961 | 826 | 711 (86.1%) | 차이 115행 중 114행이 수정주가 기준 차이 |
| 2025-06-12 | 962 | 823 | 720 (87.5%) | 차이 103행 모두 수정주가 기준 차이 |
| 2026-10-22 | 940 | 803 | 79 (9.8%) | 원인 미확정. 거래량은 모두 같고 네이버 범위가 같거나 넓음 |
| 2026-10-23 | 940 | 803 | 51 (6.4%) | 원인 미확정. 네이버 고가가 높음 148, 저가가 낮음 88, 거래량이 작음 700 |

자세한 해석·권한 제한은 [`DATA_SOURCES.md`](DATA_SOURCES.md), 실행 범위와 수치는 [`RESEARCH_V0_4.md`](RESEARCH_V0_4.md) §6에 있습니다.

### v0.4 테스트

- **최신(2026-10-25):** `python -m pytest -q` → **272 passed, 2 skipped, 1 warning**. Mac Python 3.14.6(사용자 실행)과 Linux Python 3.10에서 같습니다. 건너뛴 2개는 `PULSENTRA_RUN_INTEGRATION=1`과 키가 필요한 실제 API 테스트입니다(`tests/test_crosscheck.py`). 경고 1건은 `starlette.testclient`의 `httpx` 사용 방식에 대한 `StarletteDeprecationWarning`입니다.
- **기록(2026-10-23 인계 시점):** `python -m pytest -q -W error::DeprecationWarning` → 168 passed, 2 skipped (Python 3.13). 그 뒤 KRX 대조, 라이선스 게이트, 다중 거래일 리포트, 공시 묶음 매칭 테스트가 추가되었습니다.
- v0.3의 77개는 그대로 통과합니다. DeprecationWarning 173건은 v0.4에서 원인을 고쳐 0건이 됐습니다. Python 3.11(범위 의존성)과 3.12(잠금 파일)는 2026-10-23에 확인했습니다.
- 테스트 통과는 코드 동작의 확인입니다. 실제 데이터로 전략이 검증됐다는 뜻이 아닙니다.

## v0.3 (2026-10-23) — 실제 데이터 전략 검증

| 항목 | 상태 |
|---|---|
| 실제 데이터 | **확보함** — KOSPI·KOSDAQ 보통주 2,823개(상장폐지 273개 포함) 일봉 2016-01~2026-10, KOSPI·KOSDAQ 지수. 출처는 비공식 웹 엔드포인트·제3자 캐시 → [`DATA_SOURCES.md`](DATA_SOURCES.md) |
| 공식 API 실호출 | v0.3 시점에는 **안 함** — KRX 정보데이터시스템은 로그인 필요, KIS·OpenDART는 키 없음. v0.4에서 KRX OPEN API·OpenDART 일부 실행(위 v0.4 표) |
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
