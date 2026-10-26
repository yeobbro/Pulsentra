# 데이터 품질 감사 기준 (v0.4)

`python -m app.audit`는 종목 단위로 원본 일봉을 감사합니다. 원칙은 세 가지입니다.

1. **읽기만 합니다.** 입력 파일을 고치지 않습니다. 결과는 별도 폴더(기본 `data/audit/<UTC 시각>/`, Git 제외)에 씁니다. 입력 폴더나 그 안의 `raw/`에는 쓰기를 거부하고, 이전 결과가 있으면 `--overwrite` 없이는 덮어쓰지 않습니다.
2. **리서치 로더와 같은 행을 봅니다.** 감사는 로더와 똑같은 정리 단계(거래정지 표시 행 제거 → `check_frame`)를 거칩니다. 그래서 "백테스트가 쓰는 행"이 감사와 연구에서 같습니다. `tests/test_audit.py::test_usable_rows_reconcile_with_research_loader`가 행 단위로 일치하는지 확인합니다.
3. **판정과 수정을 분리합니다.** 이상이 있다고 해서 바로 지우거나 고치지 않습니다. 자동 수정은 수정주가 반올림 오차 한 가지뿐이고, 전후 값을 `corrections.csv`에 남깁니다.

## 실행

```bash
python -m app.audit --data data/real --format naver_raw --out data/audit/2026-10-23
python -m app.audit --data data/real --symbols 005930 000660        # 일부 종목만
python -m app.audit --compare data/audit/A data/audit/B             # 재실행 재현 여부 (지문 비교, 다르면 종료 코드 1)
python -m app.research --data data/real --format naver_raw --out reports/v0.4 --audit data/audit/2026-10-23   # 감사 결과를 연구에 반영
```

규모: 합성 데이터 78만 행(300종목 × 2,600거래일)에 23초가 걸렸습니다. 실제 데이터(원본 600만 행, 2,824종목)에서는 92.7초·95.9초였습니다(2026-10-24, `audit_manifest.json`의 `seconds`). 메모리 사용량은 측정하지 않았습니다.

## 출력

| 파일 | 내용 |
|---|---|
| `findings.csv` | 한 행 = (종목, 날짜, 검사). `observed`, `expected`, `rationale`, `cause`(원인 가설), `review_reason`, `auto_fix`(none/repaired/removed), `backtest_use`(현재 파이프라인 동작), `recommendation`(ok / review / exclude_until_verified), `follow_up` |
| `corrections.csv` | 자동 보정의 필드별 전후 값과 규칙 |
| `exclusions.csv` | 제외 구간: `row_removed`(데이터에서 빠진 행)와 `signals_excluded`(행은 남지만 신호 계산에서 빠진 구간), 사유, 행 수 |
| `corporate_action_candidates.csv` | 분할·병합·무상증자 의심 구간: 추정 비율, 가격·거래량 비율, 거래정지 길이, 신뢰도, 확인 방법 |
| `symbol_summary.csv` | 종목별 원본 행 수, 사용 가능 행 수, 제거·보정·검토 건수, `audit_status`(ok / review / exclude) |
| `audit_summary.json` | 검사별 건수, 종목 집중도(`top_symbol_share`), 설정, 지문 3종 |
| `audit_manifest.json` | 명령, 코드 커밋, 패키지 버전, 지문, 출력 파일 해시 |
| `AUDIT_REPORT.md` | 사람이 읽는 요약 |

지문은 세 가지입니다. `input_fingerprint`(원본 행), `findings_fingerprint`(감사 결과), `usable_fingerprint`(백테스트에 쓰이는 행)입니다. 같은 입력과 설정이면 지문도 같아야 합니다.

## 검사 항목과 판단 근거

| 검사 | 범주 | 심각도 | 판단 근거 | 기본 권고 |
|---|---|---|---|---|
| `missing_columns`, `unparsable_date`, `non_numeric_value`, `missing_value` | schema | error | 형식이 깨진 행은 시점이나 값을 알 수 없음 | 제거 |
| `date_has_time_component` | schema | warning | 일봉 날짜에 시각이 붙음(시간대 처리 의심) | 검토 |
| `weekend_date` | calendar | error | KRX는 주말에 거래하지 않음. 현재 로더는 이 행을 **제거하지 않음** | 확인 전 제외 |
| `date_not_in_market_calendar` | calendar | warning | KOSPI 지수 달력에 없는 평일(휴장일 기록 또는 지수 누락) | 검토 |
| `missing_trading_days` | calendar | warning | 5거래일 이상 공백(거래정지 또는 행 누락) | 검토 |
| `symbol_format` | identifier | warning | KRX 단축코드는 6자리(스프레드시트에서 앞자리 0 손실) | 검토 |
| `halt_marker_zero_ohl`, `halt_marker_flat_zero_volume` | suspension | info | 네이버는 거래정지일을 O=H=L=0, 또는 O=H=L=C·V=0 행으로 표시함. 거래 가능한 봉이 아님 | 제거(정상 동작) |
| `non_positive_price` | price | error | 0 이하 가격은 거래도 수정도 불가. 원인을 `all_prices_zero` / `close_not_positive` / `partial_zero_prices`로 나눔 | 제거, 공식 출처 대조 |
| `ohlc_inconsistent` | price | error | 저가 ≤ 시가·종가 ≤ 고가 위반이 반올림 허용치(0.2%)를 넘음. 위반 크기가 분할 비율과 같으면 `mixed_adjusted_and_unadjusted_fields` | 제거, 대조 |
| `ohlc_rounding_repaired` | price | warning | 수정주가는 필드별로 반올림되어 종가가 고가보다 1원 클 수 있음. 수정주가 행이고 0.2% 이내일 때만 고가·저가를 넓힘 | 보정(전후 값 기록) |
| `duplicate_identical` / `duplicate_conflict` | duplicate | warning / error | 같은 날짜가 여러 번. 값이 같으면 하나만 남기고, 다르면 모두 제거 | ok / 대조 |
| `zero_volume`, `negative_volume` | volume | warning / error | 가격이 있는데 거래량 0, 또는 음수 거래량 | 검토 / 제거 |
| `price_jump` | price·corporate_action | info~error | 아래 "가격 급변 분류" 참고 | 원인별 |
| `volume_level_break` | volume | warning | 거래정지 공백을 사이에 두고 60거래일 중앙 거래량이 3배 이상 변함. 연구는 그 지점을 포함하는 40봉 신호를 제외함 | 검토 |
| `volume_level_shift_no_gap` | volume | info | 공백 없이 중앙 거래량이 5배 이상 변함. 분할 비율과 같으면 무상증자 의심 | 비율 일치 시 검토, 아니면 ok |
| `bars_before_listing`, `bars_after_delisting` | identifier | warning / error | 상장일 전·폐지일 후의 봉(종목코드 재사용 또는 잘못된 매핑) | 검토 / 확인 전 제외 |
| `data_ends_before_delisting` | identifier | warning | 마지막 봉이 폐지일보다 30일 이상 앞섬. 정리매매 가격이 없어서 백테스트가 오래된 가격에 청산하고 손실을 과소평가함 | 검토 |
| `no_bars_for_delisted` | identifier | warning | 목록상 상장폐지 종목인데 데이터가 없음(생존편향) | 검토 |

### 가격 급변 분류

종가가 전일 대비 30%를 넘게 움직였거나 그 시기의 일일 가격제한폭을 넘었으면 원인을 다음 순서로 판정합니다.

| 상황 | 원인(`cause`) | 권고 |
|---|---|---|
| 거래정지 공백 뒤 + 가격 비율이 분할 비율(2, 3, 4, 5, 10, 20, 25, 50 또는 그 역수)과 3% 이내 | `unadjusted_split_or_merge` 또는 `adjustment_missing_after_suspension` | 확인 전 제외, 기업행동 후보 |
| 거래정지 공백 뒤 + 분할 비율 아님 | `base_price_reset_after_suspension` (상장·합병·감자 후 기준가 재설정 가능) | 검토 |
| 연속 거래일 + 분할 비율 + 수정주가 아님 | `unadjusted_split_or_merge` | 확인 전 제외 |
| 연속 거래일 + 가격제한폭 초과 | `exceeds_daily_price_limit` 또는 `split_like_step_on_adjusted_series` | 확인 전 제외 |
| 그 외 | `large_move_within_limit` | ok |

가격제한폭은 2015-06-15부터 ±30%, 그 전에는 ±15%로 두었습니다. 호가 단위 반올림 여유로 0.5%를 더합니다. 신규 상장일, 정리매매, 기준가가 재설정되는 경우에는 제한폭이 다르므로 이 검사가 오탐할 수 있습니다. 그래서 공백 뒤의 급변은 검토로만 분류합니다.

### 거래량 수준 변화의 원인 구분

| 관측 | 원인 가설 | 신뢰도 |
|---|---|---|
| 공백 뒤 거래량 비율 ≈ 분할 비율 k (로그 거리 ≤ log 1.25), 가격은 연속(제한폭 이내) | `volume_not_adjusted_for_split_or_merge(xk)`: 가격은 수정됐지만 거래량은 미수정 | medium |
| 공백 뒤 거래량 비율 ≈ k, 가격도 비율만큼 변함 | `split_or_merge_both_series(xk)`: 두 시계열 모두 미수정 | medium |
| 공백 뒤 비율이 분할 비율과 무관 | `volume_level_shift_after_suspension`: 실제 거래 관심 변화 또는 알 수 없는 이벤트 | low |
| 공백 없음 + 비율 ≈ k | `possible_bonus_issue(xk)`: 무상증자 의심 | low |
| 공백 없음 + 비율이 분할 비율과 무관 | `market_volume_change`: 실제 시장 변화로 보고 오류로 판정하지 않음 | — |

어떤 경우든 "확정"이라고 판정하지 않습니다. 공식 출처와 대조해야 확정됩니다(`docs/CROSS_VALIDATION.md`).

## 임계값 (`app/data/audit.py` `AuditConfig`)

| 설정 | 기본값 | 근거 | 한계 |
|---|---|---|---|
| `jump_threshold` | 0.30 | 리서치 로더와 같은 값. 현행 일일 가격제한폭 | 2015-06 이전에는 `price_limit_before`(0.15)가 따로 적용됨 |
| `rounding_tolerance` | 0.002 | v0.3에서 확인한 수정주가 반올림 오차(1원 단위)를 덮는 최소 폭 | 저가주에서는 1원이 0.2%를 넘을 수 있음 → 오류로 남음 |
| `gap_sessions` | 5 | 리서치 로더와 같은 값 | 짧은 거래정지는 경고하지 않음 |
| `volume_break_factor` / `volume_window` | 3.0 / 60 | 로더의 `volume_level_breaks`와 같은 값. 분할 비율 2부터 잡기에는 부족할 수 있음 | 감사 보고 기준만 바꿈. 로더 판정은 그대로 3.0 |
| `signal_window` | 40 | `study.prepare()`의 오염 창 | — |
| `split_price_tolerance` / `split_volume_tolerance` | 3% / 25% | 가격은 수정 반올림, 거래량은 일별 변동이 커서 넓게 둠 | 비율 근처의 실제 급변을 기업행동으로 오인할 수 있음 |
| `no_gap_shift_factor` | 5.0 | 일반적인 거래량 변화와 구분하려고 보수적으로 둠 | 2~4배 무상증자는 놓칠 수 있음 |
| `delisting_gap_days` | 30 | 정리매매 기간과 공시 지연을 감안한 여유 | 장기 거래정지 후 폐지는 모두 잡힘(의도) |

CLI에서 `--jump-threshold`, `--volume-break-factor`, `--no-gap-volume-shift`로 바꿀 수 있습니다. 바꾼 값은 manifest의 `config`에 남습니다.

## 종목 상태 (`audit_status`)

- `exclude`: 사용 가능한 행이 없거나, 오류로 제거된 행이 원본의 5%를 넘음
- `review`: 검토 또는 확인 전 제외 권고가 하나 이상 있음
- `ok`: 그 외

연구 실행에 `--audit`을 주면 다음과 같이 동작합니다.

- `exclude` 종목은 연구에서 뺍니다.
- `exclude_until_verified` 날짜를 포함하는 120봉 지표 창과 최장 보유 기간 안의 신호를 제외합니다.
- 제외한 행 수를 manifest의 `audit`에 기록합니다.

`--audit`을 주지 않으면 v0.3과 같은 결과가 나옵니다.

## 검증 상태

- **합성 데이터로 검증함**(`tests/test_audit.py`, 20개 함수): 위 표의 모든 검사, 전후 값 기록, 제외 구간, 기업행동 후보, 로더와의 행 일치, 재실행 지문 일치, 출력 폴더 보호
- **실제 데이터로 실행함(2026-10-24, 2회)**: `data/audit/full_20261024T060804Z_a`, `_b`. v0.3 보고서의 수치(`non_positive_price` 1,461행·1종목, `ohlc_inconsistent` 179행·23종목, `volume_level_break` 1,991건·559종목, `price_jump` 2,454행, 반올림 보정 24,379행)가 그대로 재현됐고, 두 실행의 지문이 같습니다. 종목 상태: ok 597 / review 2,225 / exclude 2.
- **아직 하지 않은 것**: 이상의 원인 확인(예: `non_positive_price`가 몰린 084440), `review` 종목 검토, 감사 결과를 반영한 연구 재실행(`--audit`).
- **감사와 연구 적격성은 별개입니다.** 감사 결과를 연구 실행에 어떻게 적용·판정하는지는 [`DATA_ELIGIBILITY.md`](DATA_ELIGIBILITY.md)에 있습니다. 2026-10-26 판정만 실행해 본 결과, 실제 데이터 행 지문은 감사 `_a`의 사용 가능 행 지문 `1ea20b72f9562ede`와 일치했습니다.

## 가격 교차 검증 증거 (2026-10-26, `price-evidence-v2`)

코드는 PR #7(`03c4463`)로 병합됐습니다. 아래 결과는 KOSPI 4개 거래일만 대조한 로컬 실행 결과이며, 연구 기간 전체 검증이 아닙니다.

- 저장된 KRX 유가증권 응답 4개 거래일을 로컬 naver_raw와 다시 비교했습니다(`python -m app.krxcheck.evidence`, 오프라인).
  - 기존 비교 결과(`data/krxcheck/full_market_*/compare`)와 상태별 행 수, 로컬 파일 지문, KRX 원본 해시가 모두 같아 **재현됐습니다**.
  - 기존 결과 파일은 수정하지 않았습니다.
  - 규칙별 산출물(Git 제외, 둘 다 보존, 둘 다 병합 전 작업 트리 코드로 생성): v1 `data/crosscheck/price_evidence_20261026T045447Z`, v2 `data/crosscheck/price_evidence_20261026T062502Z`.
- **날짜 상태(v2):** `PASSED` 0일, `PASSED_WITH_UNVERIFIED_ADJUSTMENTS` 2일, `FAILED` 2일. 전체 `price_check.passed`는 false입니다.
- **2025-02-27·2025-06-12 → `PASSED_WITH_UNVERIFIED_ADJUSTMENTS`:** 미해결 가격 불일치는 0행입니다.
  - 차이가 난 114·103행은 네 가격이 같은 방향·같은 배율로 다른 행으로, `unverified_adjustments`(확인되지 않은 조정 가능)로 따로 셉니다.
  - 내역: 분할 비율형 68·64행, 1배율 ±3% 이내(`near_one_unverified_adjustments`) 10·8행, 기타 36·31행.
  - 기업행동 공시로 확인한 행은 0행입니다. 원인을 배정하지 않으며, 이 행이 있는 날짜는 통과(`passed`)로 세지 않습니다.
  - v1은 같은 행을 `price_adjustment_basis_possible`로 분류하고 날짜를 통과로 셌습니다. 그 결과는 v1 폴더에 그대로 두었습니다.
- **2026-10-22 → `FAILED`:** 비교 가능 803행 모두 시가·거래량이 일치합니다. 종가는 112행(13.9%)만 일치하고, 미해결 가격 불일치가 711행입니다.
- **2026-10-23 → `FAILED`:** 미해결 가격 불일치 675행, 거래량 정확 일치 103행(12.8%)이며 로컬 거래량이 KRX보다 작은 비율이 87%입니다.
- **원인은 확정하지 않았습니다.** 확인된 사실은 다음과 같습니다.
  - 불일치는 두 최근 날짜에만 있습니다.
  - 로컬 수집 기록상 수집 시각은 2026-10-23 10:02~10:31 UTC(한국 19:02~19:31)로, 두 날짜의 정규장 마감 이후입니다.
  - 따라서 "장중 수집으로 일봉이 미완성"이라는 설명은 수집 시각과 맞지 않으며, 다른 가설도 검증되지 않았습니다.
- **범위 한계:** KOSPI 4개 거래일뿐입니다. KOSDAQ과 2016년 이후 다른 날짜는 대조하지 않았습니다. 비교 불가 행은 4일 합계 85행으로 따로 셉니다.
