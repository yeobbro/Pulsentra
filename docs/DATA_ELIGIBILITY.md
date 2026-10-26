# 연구 데이터 적격성과 감사 출처 (`eligibility-v3`)

코드 `app/research/eligibility.py`, `app/research/price_evidence.py` · 테스트 `tests/test_data_eligibility.py`, `tests/test_price_evidence.py`

`eligibility-v1`은 PR #6(`f927820`), `eligibility-v3`과 `price-evidence-v2`는 PR #7(`03c4463`)로 `research/v0.4-data-audit`에 병합됐습니다. `eligibility-v2`와 `price-evidence-v1`은 PR #7 작업 중 단계로, 따로 커밋되지 않았습니다.

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
| source_verification | 독립 출처 가격 대조(`cross_source_price_check`)가 연구 기간 전체에 대해 통과 | 대조 결과 없음, 또는 일부 날짜만 대조, 또는 (v3) 확인되지 않은 조정 가능 행이 있음, 또는 증거 날짜가 실행 기간 밖 | (v2) 가격 증거가 손상됐거나 만든 뒤 원본이 바뀜, 또는 실행 기간 안 대조 날짜에 설명되지 않은 가격 불일치 | 수집 기록도 대조도 없음 |
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

## v2 변경 (2026-10-26) — 가격 교차 검증 증거

- `eligibility-v2`부터 source_verification이 저장된 가격 대조 증거(`app/research/price_evidence.py`)도 읽습니다.
- 증거가 없는 입력은 v1과 같은 결과를 냅니다.
- 판정 순서(위 표 기준):
  1. 증거가 손상됐거나 만든 뒤 원본이 바뀌었으면 FAIL.
  2. 실행 기간 안의 대조 날짜에 설명되지 않은 가격 불일치가 있으면 FAIL.
  3. 대조한 날짜가 일부뿐이거나 실행 기간 밖이면 WATCH.
- 기간 전체를 대조했다는 플래그(`verified.cross_source_price_check = true`)가 있어도, 증거가 불일치를 보이면 PASS가 아닙니다.

## v3 변경 (2026-10-26) — 확인되지 않은 조정 분리

- 가격 증거 규칙이 `price-evidence-v2`로 바뀌었습니다. 허용 오차(가격 0.1%, 거래량 1%)와 최소 비교 행 수(100)는 그대로입니다.
- 날짜 상태는 세 가지입니다.

| 상태 | 조건 | `passed` |
|---|---|---|
| `PASSED` | 비교 가능 100행 이상, 미해결 가격 불일치 0행, 확인되지 않은 조정 가능 0행 | true |
| `PASSED_WITH_UNVERIFIED_ADJUSTMENTS` | 미해결 0행이지만 네 가격이 같은 배율로 다른 행(`unverified_adjustments`)이 있음 | false |
| `FAILED` | 비교 가능 행 부족, 또는 미해결 가격 불일치 있음 | false |

- `unverified_adjustments`는 확정된 조정이 아닙니다. 기업행동 공시와 맞춰 본 행이 없으므로 원인을 배정하지 않습니다.
  - 하위 집계: 분할 비율형(`split_like_…`), 1배율 ±3% 이내(`near_one_…`), 기타(`other_…`).
- 전체 `price_check.passed`는 모든 날짜가 `PASSED`일 때만 true입니다.
- 산출물(`crosscheck_status.json`, 날짜별 결과)에 `rules_version`을 기록합니다.
- 적격성 v3 source_verification:
  1. 증거 손상·원본 변경 → FAIL (v2와 같음).
  2. 실행 기간 안의 미해결 가격 불일치 → FAIL (v2와 같음).
  3. 확인되지 않은 조정 가능 행이 있으면 최소 WATCH. 기간 전체 플래그가 true여도 PASS가 아닙니다.
  4. 다른 검사가 FAIL이면 전체 판정은 FAIL입니다.
- 판정 설명은 "불일치 없음" 대신 실제 수를 적습니다: 대조 날짜 수, 통과·미검증 조정 포함 날짜 수, 미해결·미검증·근접 1배율·비교 불가 행 수, 그리고 "조정은 기업행동 자료로 확인되지 않았고, 대조한 날짜 외 기간은 검증되지 않음".
- 요약 값에는 증거 SHA-256, 규칙 버전, 증거 기간과 실행 기간, 실행 기간 안·밖 날짜도 들어갑니다.
- v1 증거 파일도 읽습니다. 그 파일의 `price_adjustment_basis_possible` 행은 확인되지 않은 조정으로 셉니다. 근접 1배율 집계는 v1에 없으므로 "집계 없음"으로 표시합니다.

### 증거 생성
`python -m app.krxcheck.evidence --out data/crosscheck/price_evidence_<시각> --audit data/audit/<run>`

- 저장된 KRX 응답만 씁니다. 네트워크 요청과 키는 쓰지 않습니다.
- 행 분류는 `fullmarket.compare_market`과 `classify_pair`를 그대로 씁니다.
- 허용 오차는 기존 값(가격 0.1%, 거래량 1%)이고, 날짜당 비교 가능 행이 100개 이상이어야 합니다.
- 산출물의 `crosscheck_status.json`은 기존 계약(`verified`)과 호환됩니다. `verified.cross_source_price_check`는 기간 전체를 대조했을 때만 true이며, 날짜 표본만으로는 항상 false입니다.
- `source_files.json`에 원본 857개 파일의 SHA-256을 남기고, 증거를 읽을 때 다시 확인합니다.
- Factor Lab에서는 `--crosscheck <증거>/crosscheck_status.json`으로 연결합니다.

### 실제 실행 결과 (`price-evidence-v2`, `data/crosscheck/price_evidence_20261026T062502Z`, Git 제외)

이 산출물은 병합 전 작업 트리 코드로 생성되어 `reproducible_from_commit: false`로 기록돼 있습니다. 병합 커밋(`03c4463`)의 코드로 임시 폴더에 다시 생성해 비교한 결과, 생성 시각·도구 정보를 제외한 판정 파일, 날짜별 비교 파일 4개, 원본 파일 해시 목록이 일치했습니다(2026-10-26). 원본 산출물은 수정하지 않았으며 재생성 결과도 저장소에 추가하지 않았습니다. 원본 산출물의 미커밋 파일 목록에 있는 `pp/factorlab/__main__.py`는 PR #8(`654f277`)로 고친 경로 파싱 버그의 흔적이며, 실제 경로는 `app/factorlab/__main__.py`입니다(산출물은 소급 수정하지 않음).

v1 결과 폴더 `price_evidence_20261026T045447Z`(커밋되지 않은 작업 중 코드로 생성)는 그대로 두었습니다. 행 수는 같고 날짜 상태만 다릅니다.

| 날짜 | KRX 행 | 비교 가능 | 종가 정확 일치 | 종가 0.1% 이내 | 거래량 정확 일치 | 미검증 조정 가능(분할형/근접1/기타) | 미해결 가격 | v2 상태 | v1 통과 |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| 2025-02-27 | 961 | 826 | 712 (86.2%) | 712 (86.2%) | 712 (86.2%) | 114 (68/10/36) | 0 | PASSED_WITH_UNVERIFIED_ADJUSTMENTS | 예 |
| 2025-06-12 | 962 | 823 | 720 (87.5%) | 720 (87.5%) | 720 (87.5%) | 103 (64/8/31) | 0 | PASSED_WITH_UNVERIFIED_ADJUSTMENTS | 예 |
| 2026-10-22 | 940 | 803 | 112 (13.9%) | 131 (16.3%) | 803 (100%) | 0 | 711 | FAILED | 아니오 |
| 2026-10-23 | 940 | 803 | 131 (16.3%) | 149 (18.6%) | 103 (12.8%) | 0 | 675 | FAILED | 아니오 |

비교 불가 행은 4일 합계 85행(2025년 2일 41행)입니다.

**적격성 판정 변화** (2016-01-01부터 감사 적용 조건, 판정만 실행):

| 경우 | v2 판정 | v3 판정 |
|---|---|---|
| 증거 없음 | REVIEW_REQUIRED | REVIEW_REQUIRED (같음) |
| 증거 있음, 데이터 끝이 2026-10-23 | UNUSABLE | **UNUSABLE** (같음) — 실행 기간 안의 10-22·10-23에 미해결 가격 불일치 1,386행 |
| 증거 있음, 데이터를 2026-10-21에서 자름 | source_verification WATCH, 설명 "2개 거래일만 대조(불일치 없음)" | source_verification WATCH, 설명 "2개 거래일 대조: 통과 0일, 미검증 조정 포함 2일 · 미해결 0행 · 확인되지 않은 조정 가능 217행(근접 1배율 18행) · 비교 불가 41행". 전체 판정은 둘 다 INSUFFICIENT_EVIDENCE — 감사 기간과 달라 지문 동일성을 확인할 수 없음 |

v1 증거 폴더로 v3 판정을 실행해도 같은 판정이 나옵니다(근접 1배율만 "집계 없음").
