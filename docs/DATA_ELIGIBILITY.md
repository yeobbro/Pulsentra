# 연구 데이터 적격성과 감사 출처 (`eligibility-v1`)

코드 `app/research/eligibility.py` · 테스트 `tests/test_data_eligibility.py` · 브랜치 `feat/research-data-eligibility`(미커밋)

**데이터 감사(audit)와 연구 적격성(eligibility)은 다른 판단입니다.**
- 감사(`python -m app.audit`): 데이터 묶음에 어떤 문제가 있는지 기록합니다.
- 적격성: 연구 실행 하나가 쓴 행을 "감사된 실제 데이터"로 볼 수 있는지 판정합니다.

적격성은 실행할 때 한 번 계산해 그 실행 기록에 저장합니다. 나중에 감사가 바뀌어도 과거 기록은 다시 쓰지 않습니다.

## 상태 (Robustness Gate와 같은 이름)

| 상태 | 라벨 | 뜻 |
|---|---|---|
| PASS | USABLE | 모든 검사 통과. `real_data_verified`는 이 경우 + 실제 데이터일 때만 true |
| WATCH | REVIEW_REQUIRED | 표시된 항목을 검토하기 전에는 사용 가능으로 보지 않음 |
| FAIL | UNUSABLE | 반대 근거: 합성·혼합 데이터, 같은 기간인데 지문 불일치, 제외 대상 사용, 감사 산출물 손상·변경, 감사 범위 밖 데이터 |
| INSUFFICIENT_DATA | INSUFFICIENT_EVIDENCE | 판정에 필요한 근거가 없음 |

종합 상태는 가장 나쁜 검사 결과입니다(FAIL > INSUFFICIENT_DATA > WATCH > PASS).

## 검사

| 검사 | PASS | WATCH | FAIL | INSUFFICIENT_DATA |
|---|---|---|---|---|
| data_scope | 출처 표시가 실제 데이터 | — | DEMO·합성 또는 혼합 | 출처 미기록 |
| source_record | 수집 기록(출처·수집 시각·파일 해시) 있음 | 누락 파일·수집 실패가 있음 | — | 수집 기록 없음 |
| source_verification | 독립 출처 가격 대조(`cross_source_price_check`)가 실행되어 통과 | 수집 기록은 있으나 대조 결과 없음 | — | 수집 기록도 대조도 없음 |
| audit_present | 감사 폴더가 있고 산출물 SHA-256이 감사 manifest와 일치 | — | 폴더·파일 누락, JSON 손상, 감사 후 산출물 변경 | 연결된 감사 없음 |
| audit_fingerprint | 실행 행의 `bars_fingerprint` = 감사 `usable_fingerprint` | — | 같은 기간인데 지문이 다름 | 지문 없음, 또는 기간이 달라 동일성 확인 불가 |
| audit_coverage | 실행 종목·날짜가 감사 범위 안 | — | 감사에 없는 종목, 감사 마지막 날짜 이후·시작일 이전 데이터 | — |
| audit_freshness | 감사 후 30일 이내, 감사 코드가 커밋 상태 | 30일 초과, 감사 시각 미기록, 미커밋 감사 코드 | — | — |
| excluded_data | `exclude` 종목 없음, 검증 전 제외 봉은 감사 적용으로 가림 | — | `exclude` 종목 포함, 또는 `exclude_until_verified` 봉이 있는 종목을 감사 적용 없이 사용 | — |
| review_symbols | `review` 종목 없음 | `review` 종목 포함(개수·비율 기록) | — | — |
| known_limitations | 미확인 기업행동 후보 없음 | 미확인 기업행동 후보 있음(배당 미반영·거래량 미수정 함께 표시) | — | — |

- 감사 결과가 없으면 감사 관련 검사는 audit_present 하나로 대표되어 INSUFFICIENT_DATA가 됩니다.
- **임계값:** `EligibilityPolicy.max_audit_age_days = 30` 하나입니다. 이 값은 결과를 보기 전에 정했습니다. 바꿀 때는 규칙 버전을 올리고 이유를 이 문서에 남깁니다.
- **출처 표시만으로는 PASS가 되지 않습니다.** 수집 기록과 독립 대조는 별도 검사입니다.

## 기록 위치

| 실행 | 위치 | 비고 |
|---|---|---|
| Factor Lab | `run_manifest.json`의 `data_eligibility` | `--audit`(감사 폴더), `--crosscheck`(crosscheck_status.json) 선택. REPORT.md 상단에 라벨 표시 |
| Strategy Library | 실행 요약 `library.data_eligibility` | 앱 DB 데이터는 수집 기록·감사 연결이 없어 실제 데이터라도 INSUFFICIENT, DEMO는 FAIL |
| Experiment Ledger | `manifest_linked` 이벤트의 `facts.data_eligibility` | 규칙 버전·상태·라벨·범위·판정 시각만 기록. 기존 이벤트는 그대로 |
| Robustness Gate (두 게이트 공통) | `data_eligibility` 검사 | 같은 함수(`eligibility.gate_check`). 기록 상태를 그대로 쓰고, 기록이 없으면 INSUFFICIENT, 기록 지문이 실행 지문과 다르면 FAIL |

## 실제 데이터 점검 (2026-10-26, 판정만 실행 · 백테스트 아님)
`data/real`을 2016-01-01부터 읽어(약 75초) 감사 `data/audit/full_20261024T060804Z_a`와 대조했습니다.

- 실행 행의 지문 `1ea20b72f9562ede`는 감사의 사용 가능 행 지문과 **일치**했습니다. 2,823종목, 2016-01-04~2026-10-23입니다.
- **감사를 적용하지 않고 읽은 경우: FAIL (UNUSABLE).**
  - `exclude` 종목 1개(084440)가 포함됩니다.
  - 검증 전 제외 봉이 있는 종목 225개를 그대로 씁니다.
- **감사 적용 조건으로 판정한 경우: WATCH (REVIEW_REQUIRED).** Factor Lab `--audit`과 같은 조건, 즉 `exclude` 종목을 빼고 검증 전 제외 봉을 가린 것으로 입력했습니다. WATCH가 나온 검사:
  - review 종목 2,225개(79%)
  - 독립 출처 대조 결과 없음
  - 미확인 기업행동 후보 8,073건
- **따라서 지금 실제 데이터로 "검증 완료(USABLE)"인 연구는 없습니다.**

## 아직 하지 않은 것
- `app.research`(전략 연구 파이프라인) 실행에는 적격성 기록을 넣지 않았습니다. 이 파이프라인은 기존 manifest의 `audit.usable_rows_match_study`만 기록합니다.
- 앱 DB 데이터에 대한 수집 기록과 감사 연결이 없습니다.
- 감사 이후 데이터 파일이 바뀌었는지는 지문 비교로만 알 수 있습니다. 수집 manifest의 파일 해시를 다시 대조하는 기능은 없습니다.
