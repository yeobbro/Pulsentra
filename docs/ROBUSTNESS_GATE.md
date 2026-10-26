# Robustness Gate (Alpha Research Lab v1 · Phase 3 MVP)

`python -m app.robustness evaluate --factorlab <Factor Lab 출력> --factor <요인> --out <새 폴더> [--ledger-id <실험 ID>]`

Factor Lab 실행 1건의 한 요인을 고정된 검사로 판정합니다. 파라미터를 고르거나 다시 계산하지 않습니다.
코드: `app/research/robustness.py`, CLI: `app/robustness/__main__.py`, 테스트: `tests/test_robustness.py`.

## 상태와 종합 규칙

| 상태 | 뜻 |
|---|---|
| PASS | 구현된 검사를 이 데이터에서 통과. **지속적인 우위의 증거가 아님** |
| WATCH | 통과했지만 근거가 약하거나, 검증 조건(사전 등록·감사·데이터 상태·재현성)이 갖춰지지 않음 |
| INSUFFICIENT_DATA | 판단할 표본이 부족 |
| FAIL | 규칙 위반(기간 겹침·사후 등록·결과 파일 변경 등) 또는 효과가 없거나 반대 |

종합 상태 = 가장 나쁜 검사 상태 (FAIL > INSUFFICIENT_DATA > WATCH > PASS). 모든 검사가 PASS일 때만 PASS입니다.
현재 실제 데이터의 결과 상태가 `PROVISIONAL`이므로(`app/research/validity.py`), 실제 데이터 실행은 `data_validity` 검사 때문에 PASS가 될 수 없습니다.

## 방향과 기간

- 요인의 방향(+/−)은 **dev 기간의 평균 Rank IC 부호로만** 정합니다. validation·test는 그 방향으로 평가만 합니다.
- test 검사는 다른 모든 검사 뒤에 계산하며, 방향·요인·임계값 선택에 쓰지 않습니다.
- 워크포워드는 dev+validation 연도만 씁니다: 각 연도의 부호를 그 이전 연도들의 평균으로 예측해 맞은 비율.

## 임계값 (기본값, `GateConfig`)

| 이름 | 기본값 | 쓰이는 검사 |
|---|---|---|
| `min_dates_dev` / `min_dates_validation` / `min_dates_test` | 24 / 8 / 8 | sample_size (겹치지 않는 보유 구간 수) |
| `min_names_mean` | 50 | sample_size |
| `min_coverage` | 0.5 | sample_size (사용 종목 / 적격 종목) |
| `dev_rank_ic_t_min` | 2.0 | dev_effect (\|t\| 미만이면 FAIL) |
| `validation_rank_ic_t_watch` | 1.0 | validation/test (방향·순초과수익은 맞지만 t 미만이면 WATCH) |
| `walk_forward_min_hit` / `walk_forward_fail_below` | 0.6 / 0.4 | walk_forward |
| `cost_stress_multipliers` | (2.0, 3.0) | cost_stress (첫 배수에서 순초과 ≤ 0이면 FAIL, 이후 배수만이면 WATCH) |
| `max_audit_review_share` | 0.5 | audit_applied |

사전 등록 명세의 `gate` 항목에 임계값을 적으면 그 값이 기준이 됩니다. 평가 시 `--threshold name=value`로 바꾸면
결과는 계산하지만 `thresholds` 검사가 WATCH가 됩니다(결과를 보고 기준을 바꾼 것과 구별할 수 없기 때문).
**결과를 본 뒤 이 문서의 기본값을 바꾸지 않습니다.** 바꿀 때는 이유와 날짜를 이 문서에 남기고 이전 판정은 다시 하지 않습니다.

## 검사 목록

| 검사 | FAIL / INSUFFICIENT 조건 | WATCH 조건 |
|---|---|---|
| thresholds | — | 등록값·기본값과 다른 임계값 |
| period_separation | 기간 겹침, 엠바고 < ⌈(h+1)·7/5⌉일, 보유 구간이 기간 밖으로 나감, t < 진입 < 청산 위반 | — |
| preregistration | 실행 시작 후 등록, manifest의 실험 ID 불일치 | 등록 없음, 결과 후 수정, 이전 명세 버전으로 실행, 원장을 거치지 않은 실행 |
| experiment_count | — | 실제 평가 수 > 등록한 계획 수 |
| data_fingerprint_matches_spec | 등록 지문 ≠ manifest 지문 | — |
| periods_match_spec | 등록 기간 ≠ 실행 기간 | — |
| sample_size | 날짜·종목·커버리지 부족 → INSUFFICIENT | — |
| dev_effect | \|t\| < 2 → FAIL (dev 날짜 부족이면 INSUFFICIENT) | — |
| validation_out_of_sample, test_out_of_sample | 방향 반대 또는 비용 후 동일가중 대비 초과수익 ≤ 0 | t < 1 |
| walk_forward | 적중률 < 0.4 (연도 3개 미만이면 INSUFFICIENT) | 0.4 ≤ 적중률 < 0.6 |
| cost_stress | 2배 비용에서 순초과 ≤ 0 | 3배에서만 ≤ 0 |
| parameter_neighbourhood | — | 같은 종류 다른 기간 요인이 반대 부호, 또는 이웃 없음 |
| benchmark | validation 비용 후 동일가중 대비 ≤ 0 | — (KOSPI 종가 기준 비교는 참고값만) |
| audit_applied | — | `--audit` 없이 실행, review 종목 비율 > 0.5 |
| data_validity | INELIGIBLE | PROVISIONAL |
| reproducibility | 산출물 SHA-256이 manifest와 다름 | 실행 시 커밋되지 않은 코드 |

## 구현하지 않은 것 (추정값을 만들지 않음)

PBO(CSCV), Deflated Sharpe Ratio, White Reality Check / Hansen SPA, 시장 국면별 판정, 공매도 대차비용·제한.
다중 실험 보정은 현재 "계획 수 대비 실제 평가 수" 경고와 실험 수 기록뿐입니다.

## Strategy Library 실행용 게이트 (별도 어댑터)

`POST /api/library/runs/{run_id}/robustness` · 화면: Strategy Library 결과의 "Robustness Gate 평가" 버튼, 저장 실행 표의 "평가" 버튼.
코드 `app/research/robustness_strategy.py`(판정 규칙), `app/strategy_library.py` `evidence_for_run`·`gate_run`(근거 수집·저장), 테스트 `tests/test_strategy_gate.py`.

- 위의 Factor Lab 게이트(`app/research/robustness.py`)는 **바꾸지 않았습니다.** 상태 이름과 "가장 나쁜 검사가 종합 상태" 규칙만 같이 씁니다.
  포트폴리오 백테스트를 Factor Lab 출력처럼 바꾸지 않으며, Rank IC 같은 지표를 만들지 않습니다. 두 게이트의 판정은 검사 항목이 달라 같은 척도가 아닙니다.
- 근거는 실행이 저장할 때 남긴 값뿐입니다(`library.evidence_version` = 1): 기간별 비용 전·후 수익률, 같은 날짜의 벤치마크 수익률과 초과수익,
  기간별 거래 기대수익과 부트스트랩 95% 구간, 수수료·세금·슬리피지를 2배·3배로 올려 엔진을 다시 돌린 결과, 연도별 결과, 벤치마크 종류, 데이터 출처, 기간 설정.
  이전 버전 실행처럼 기록이 없는 항목은 INSUFFICIENT_DATA와 빠진 필드 이름으로 돌려줍니다.
- 평가 결과는 `data/robustness/library/run-<id>/gate_<시각>.json`에 매번 새 파일로 남고(덮어쓰지 않음), 원장에 연결된 실행이면 `robustness_gate` 산출물로 연결됩니다.

| 검사 | FAIL / INSUFFICIENT | WATCH |
|---|---|---|
| evidence_recorded | 게이트용 기록 없음 → INSUFFICIENT | — |
| thresholds | — | 등록값(원장 명세 `gate`)·기본값과 다른 임계값 |
| data_source | 합성(DEMO)·혼합 데이터 → FAIL, 출처 미기록 → INSUFFICIENT | — |
| data_validity | INELIGIBLE → FAIL | 결과 상태 미판정(현재 앱 DB 실행 전부), PROVISIONAL |
| audit_applied | — | 감사 미연결(현재 앱 DB 실행 전부), review 비율 > 0.5 |
| data_fingerprint | 원장 등록 지문과 다름 → FAIL, 지문 없음 → INSUFFICIENT | — |
| settings_integrity | 저장 설정으로 다시 계산한 설정 해시 불일치, 원장·manifest의 비용·파라미터 불일치 → FAIL | — |
| period_separation | 기간 겹침·누락 → FAIL | 기간 끝을 넘는 거래 |
| preregistration | 원장에 실행 시작 기록이 없거나 등록보다 먼저 시작 → FAIL | 등록 없음, 결과 후 수정, 실행 후 명세 변경 |
| selection_count | 시도 수를 알 수 없음 → INSUFFICIENT | 같은 전략에서 설정을 2개 이상 실행(각 설정의 test 구간을 이미 봄) |
| sample_size | 기간별 거래 수 < 30/15/15 → INSUFFICIENT | — |
| dev_effect | 비용 후 벤치마크 대비 초과 ≤ 0, 기대수익 ≤ 0, 95% 구간이 0 포함 → FAIL | — |
| validation_out_of_sample, test_out_of_sample | 초과 ≤ 0 또는 기대수익 ≤ 0 → FAIL | 95% 구간이 0 포함 |
| yearly_consistency | 초과 > 0인 연도 비율 < 0.4 → FAIL, dev+validation 연도 < 3 → INSUFFICIENT | 0.4 ≤ 비율 < 0.6 |
| cost_stress | 2배 비용에서 validation 초과 ≤ 0 → FAIL, validation 거래 부족·재실행 없음 → INSUFFICIENT | 3배에서만 ≤ 0 |
| parameter_neighbourhood | — | 같은 조건의 다른 파라미터 실행 없음, 또는 그중 validation 초과 ≤ 0 |
| benchmark | 종류 미기록 → INSUFFICIENT | 동일가중 대용(실제 지수 아님) |
| reproducibility | 산출물 해시가 manifest와 다름 → FAIL | manifest 없음(원장 없이 실행), 미커밋 코드 |

- 기본 임계값(`StrategyGateConfig`): 거래 수 dev 30 · validation 15 · test 15, 연도 3개, 연도 적중 0.6/0.4, 비용 배수 (2, 3).
  원장 등록 시 명세의 `gate`에 이 값이 함께 등록되며, 평가 시 바꾸면 thresholds 검사가 WATCH가 됩니다.
- 결과에는 `data_scope`(SYNTHETIC_DEMO / MIXED / REAL / UNKNOWN)와 `real_data_verified`(종합 PASS이면서 REAL일 때만 true)가 들어갑니다.
- **현재 한계:** 앱 DB 실행에는 감사와 결과 상태 판정이 연결되지 않아 data_validity·audit_applied가 WATCH이므로 실제 데이터 실행도 지금은 PASS가 될 수 없습니다.
  이것은 판정을 일부러 막은 것이 아니라 근거가 없다는 표시이며, 근거가 모두 있는 입력에서는 PASS가 나오는지 테스트합니다(`test_pass_is_reachable_only_with_full_evidence`).
  미구현: PBO, Deflated Sharpe, Reality Check/SPA, 파라미터 재최적화 워크포워드(`/api/backtests/walk-forward`는 게이트와 연결되지 않음), 국면별 판정,
  비용·배당을 반영한 벤치마크. 벤치마크는 비용을 내지 않으므로 초과수익은 전략에 불리하게 계산됩니다.
