# 실제 데이터 검증 실행 절차 (Mac, v0.4 인계)

v0.4 도구를 실제 데이터에 처음 적용하는 절차입니다. 이 문서를 쓸 때(2026-10-23)는 개발 환경에 `data/real/`과 API 키가 없어서, **아래 단계를 하나도 실제 데이터로 실행하지 않았습니다.** 각 단계의 "판정 기준"을 만족해야 그 단계가 완료된 것입니다.

**원칙**
- 입력 데이터는 읽기만 합니다. 결과는 매번 새 폴더에 씁니다.
- 원본 데이터와 키는 화면에 출력하거나 커밋하지 않습니다.
- 감사와 교차 검증이 끝나기 전에는 전략을 채택하거나 파라미터를 바꾸지 않습니다.

## 0. 준비

```bash
cd ~/Pulsentra                                   # 저장소 위치에 맞게
git fetch origin
git switch claude/hopeful-bardeen-pzn52a         # 또는 PR이 병합된 뒤 research/v0.4-data-audit
git status --short                               # 비어 있어야 함
python3 --version                                # 3.12 이상이면 잠금 파일, 3.11이면 범위 설치
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-lock.txt             # 3.11: pip install -r requirements-dev.txt -r requirements-research.txt
python -m pytest -q -W error::DeprecationWarning # 기대: 168 passed, 2 skipped (실제 API 테스트는 건너뜀)
```

긴 실행 중 Mac이 잠들지 않게 하려면 명령 앞에 `caffeinate -i`를 붙이세요. 연구 실행에는 메모리 6GB 이상이 필요합니다(v0.3 기준).

### 데이터 전제 조건 (내용은 출력하지 않고 존재만 확인)

| 경로 | 필요한 단계 | 확인 |
|---|---|---|
| `data/real/universe.csv` | 감사·교차 검증·연구 | `test -f data/real/universe.csv && echo ok` |
| `data/real/raw/<6자리코드>.csv` | 전부 | `ls data/real/raw \| wc -l` (v0.3: universe 2,824행) |
| `data/real/manifest.jsonl` | 연구(로더가 읽음) | `test -f data/real/manifest.jsonl && echo ok` |
| `data/real/index_KOSPI.csv`, `index_KOSDAQ.csv` | 감사(거래일 달력), 연구(벤치마크·국면) | `ls data/real/index_*.csv` |

### 읽기 전용 확인용 스냅샷

실행 전후로 원본의 해시를 비교합니다. 해시 파일은 저장소 밖에 둡니다.

```bash
find data/real -type f -name '*.csv' -print0 | sort -z | xargs -0 shasum -a 256 > ~/pulsentra_real_before.sha256
# … 1~4단계 실행 …
find data/real -type f -name '*.csv' -print0 | sort -z | xargs -0 shasum -a 256 > ~/pulsentra_real_after.sha256
diff ~/pulsentra_real_before.sha256 ~/pulsentra_real_after.sha256 && echo "data/real unchanged"
```

## 1. 데이터 감사 (`python -m app.audit`)

```
python -m app.audit [--data DATA] [--format {naver_raw,standard}] [--out OUT] [--start START] [--end END]
                    [--symbols [SYMBOLS ...]] [--overwrite] [--jump-threshold X] [--volume-break-factor X]
                    [--no-gap-volume-shift] [--compare A B]
```

```bash
python -m app.audit --data data/real --format naver_raw --start 2016-01-01 --out data/audit/run1
python -m app.audit --data data/real --format naver_raw --start 2016-01-01 --out data/audit/run2   # 재현 확인용 두 번째 실행
python -m app.audit --compare data/audit/run1 data/audit/run2       # 종료 코드 0 = 재현됨
```

- **`--start 2016-01-01`**: 연구 실행의 기본 시작일과 맞춥니다. 그래야 4단계에서 `audit.usable_rows_match_study`가 `true`가 될 수 있습니다. 생략하면 파일 전체를 감사합니다.
- **읽기 전용 동작**:
  - `raw/*.csv`를 문자열로 읽고, 원본은 바꾸지 않습니다.
  - 출력 폴더가 입력 폴더와 같거나, 입력 폴더를 포함하거나, `raw/` 안에 있으면 거부합니다(종료 코드 2).
  - 이전 결과가 있는 폴더는 `--overwrite` 없이는 거부합니다.
- **산출물** (`data/audit/run1/`, Git 제외): `findings.csv`, `corrections.csv`, `exclusions.csv`, `corporate_action_candidates.csv`, `symbol_summary.csv`, `audit_summary.json`, `audit_manifest.json`, `AUDIT_REPORT.md`
- **예상 시간**: 약 3분. 합성 데이터 78만 행에 23초가 걸린 것을 비례 계산한 값입니다. 실제로는 측정하지 않았습니다. `--no-gap-volume-shift`를 주면 약 10% 빨라집니다.

### 판정 기준

1. `--compare` 결과 `"reproduced": true`이고, 실행 전후 해시가 같아야 합니다.
2. 같은 데이터라면 감사 결과가 v0.3 품질 표(`reports/v0.3/REPORT.md` §2)를 재현해야 합니다. 감사는 로더와 같은 `check_frame`을 같은 임계값으로 쓰기 때문입니다. `AUDIT_REPORT.md` §1의 행 수를 아래 값과 비교합니다.

| 검사 | v0.3 행 | v0.3 종목 | 감사 출력에서의 이름 |
|---|---|---|---|
| non_positive_price | 1,461 | 1 | `non_positive_price` |
| ohlc_inconsistent | 179 | 23 | `ohlc_inconsistent` |
| ohlc_rounding_repaired | 24,379 | 911 | `ohlc_rounding_repaired` |
| price_jump | 2,454 | 1,169 | `price_jump` (원인별로 나뉨, 합계 비교) |
| volume_level_break | 1,991 | 559 | `volume_level_break` |
| zero_volume | 4,025 | 84 | `zero_volume` |
| missing_trading_days | 1,868 | 915 | `missing_trading_days` |
| 거래정지 제거(zero_ohl_marker) | 135,490 | 916 | `halt_marker_zero_ohl` |
| 거래정지 제거(zero_volume_flat_bar) | 63,761 | 539 | `halt_marker_flat_zero_volume` |
| 40봉 거래량 창 제외 | 39,936 | 559 | `exclusions.csv`의 `signals_excluded` 합계 |

값이 다르면 먼저 데이터가 v0.3과 같은지 확인합니다(4단계의 데이터 지문). 데이터가 같은데도 다르면 감사 코드의 문제이므로 결과를 쓰지 말고 보고합니다.

3. 원인 조사(판정이 아니라 기록할 것):
   - `non_positive_price`가 몰린 종목과 그 원인(`cause`)
   - `price_jump`의 원인별 건수
   - `exceeds_daily_price_limit`와 `bars_after_delisting` 건수
   - `weekend_date` 건수 — 현재 로더가 제거하지 않는 행입니다
   - 기업행동 후보의 신뢰도별 건수

## 2. 공식 출처 교차 검증 (`python -m app.crosscheck`)

```
python -m app.crosscheck --data DATA [--audit AUDIT] [--sample SAMPLE] [--symbols [SYMBOLS ...]] [--seed SEED]
                         [--start START] [--end END] [--out OUT] [--overwrite]
```

### 환경변수 (`.env`, Git 제외. 값을 출력하거나 공유하지 마세요)

| 이름 | 필수 | 내용 |
|---|---|---|
| `KIS_APP_KEY`, `KIS_APP_SECRET` | 가격 대조 | KIS Developers에서 앱 등록 후 발급 |
| `KIS_PAPER` | 선택 | `true`면 모의투자 도메인(`openapivts…:29443`), 기본 `false`(실전 `openapi…:9443`) |
| `KIS_MIN_INTERVAL_SECONDS` | 선택 | 호출 간격, 기본 0.35초. KIS 공식 예제는 실전 0.05초, 모의 0.5초를 씀 → **모의 도메인이면 0.5로** |
| `KIS_TIMEOUT_SECONDS` | 선택 | 기본 12초 |
| `DART_API_KEY` | 공시 대조 | opendart.fss.or.kr에서 인증키 신청 |
| `DART_TIMEOUT_SECONDS` | 선택 | 기본 12초 |

값을 보지 않고 설정 여부만 확인하는 방법:

```bash
python -c "from app.providers.kis import KISClient; from app.providers.dart import DartClient; print('KIS', KISClient().configured, 'DART', DartClient().configured)"
```

### 인증과 호출 제한

- **KIS 인증**:
  - `POST /oauth2/tokenP`로 토큰을 받고(유효 1일), 실행 중에는 재사용합니다.
  - 실행할 때마다 새로 발급하므로 짧은 간격으로 반복 실행하지 마세요. KIS는 토큰 발급 빈도를 제한할 수 있습니다. 현재 한도는 KIS 포털에서 확인하세요.
- **KIS 호출량**: 종목당 약 30회 호출이 필요합니다(2016년~현재를 130일 창으로 나눔). 30종목이면 약 900회, 0.35초 간격으로 약 6분입니다.
- **KIS 한도 초과 처리**: 초당 한도를 넘으면 `EGW00201` 응답이 오고, 1·2·4초 간격으로 최대 3회 재시도합니다.
- **OpenDART**:
  - `corpCode.xml`(전체 회사 코드 ZIP)을 1회, 후보 종목마다 `list.json`을 최대 5페이지 호출합니다.
  - 상태 `020`(요청 한도 초과)이면 재시도하지 않고 중단합니다. 일일 한도는 OpenDART 안내를 확인하세요.
- **사용 경로**: 시세 조회와 공시 목록만 호출합니다. 주문·잔고 경로는 코드에 없고, 호출을 시도하면 거부됩니다.

### 실행

```bash
caffeinate -i python -m app.crosscheck --data data/real --audit data/audit/run1 --sample 30 --seed 7 --out data/crosscheck/run1
PULSENTRA_RUN_INTEGRATION=1 python -m pytest -m integration -q     # (선택) 실제 API 스모크 2개
```

- **표본**: 감사 후보(신뢰도 높은 순)에서 절반을 뽑고, 나머지는 시드 7로 무작위로 뽑습니다. 종목 목록은 `crosscheck_status.json`의 `symbols`에 남습니다.
- **키가 없을 때**: 해당 단계를 건너뛰고 이유를 기록하며 종료 코드 3을 냅니다. 결과를 만들어 넣지 않습니다.
- **산출물** (`data/crosscheck/run1/`, Git 제외):
  - `crosscheck_status.json`: 실행 여부, 판정, `verified` 플래그
  - `price_comparison.csv`, `price_detail_<code>.csv`
  - `kis_event_days_<code>.csv`: KIS가 `mod_yn=Y`이거나 `prtt_rate≠0`으로 표시한 날
  - `filings.csv`, `corporate_action_matches.csv`

### 판정 기준

- **가격**: `price_check.passed`는 모든 표본이 `consistent` 또는 `adjustment_basis_differs`일 때만 참입니다. `record_errors`가 있는 종목은 날짜별 상세 CSV로 원인을 확인합니다.
- **거래량**: `volume_ratio_median`과 분할 이전 구간의 비율로, 네이버 거래량이 미수정인지 확인합니다.
- **KIS 필드 해석**: `kis_event_days_*.csv`의 날짜가 감사 후보와 겹치는지 봅니다. 이 결과를 근거로 `flng_cls_code`와 `prtt_rate`의 의미를 문서화합니다(지금은 해석하지 않음).
- **공시**: `filing_check.passed`는 모든 후보가 `confirmed_kind_match`이고 회사코드 누락이 없을 때만 참입니다. `undetected_event` 건수는 감사가 놓친 이벤트 수입니다.
- **알려진 한계**: 상장폐지 종목은 OpenDART `corpCode`에 종목코드가 없을 수 있습니다(`no_corp_code`에 기록).

## 3. 연구 재현과 차이 분리

```
python -m app.research --data DATA [--format {naver_raw,standard}] --out OUT [--start START] [--end END]
                       [--min-turnover X] [--random-pool N] [--commission-bps X] [--sell-tax-bps X]
                       [--slippage-bps X] [--overwrite] [--crosscheck CROSSCHECK] [--audit AUDIT]
```

`--out`은 반드시 **새 폴더**로 지정하세요. `reports/v0.3`에는 Git에 추적되는 기준선 보고서가 있습니다. v0.4 코드는 비어 있지 않은 폴더에 쓰기를 거부합니다(종료 코드 2).

| 순서 | 명령 | 목적 | 시간(v0.3 기준) |
|---|---|---|---|
| 3a | `python scripts/compare_baseline.py --data data/real --out reports/v0.4-compare` | 같은 데이터에 v0.3.0 코드와 현재 코드를 돌려 **코드 차이**를 분리. 종료 코드 0 = 결과 동일 | 약 26분(2회 실행) |
| 3b | (3a의 `current/` 결과) | v0.3과 같은 방식의 재실행. `run_manifest.json`의 `data_fingerprint`를 v0.3의 `de52ccfa16e847ee`와 비교해 **데이터 차이**를 확인 | — |
| 3c | `python -m app.research --data data/real --format naver_raw --out reports/v0.4-audited --audit data/audit/run1 --crosscheck data/crosscheck/run1/crosscheck_status.json` | 감사 제외와 검증 상태를 반영한 실행. 3b와의 차이가 **감사 제외의 효과** | 약 13분 |
| 3d | (필요할 때만) `--sell-tax-bps 18` 같은 비용 변경 | **비용 가정의 효과**. 민감도 확인용이고 선택에는 쓰지 않음 | 약 13분 |

`docs/RESEARCH_V0_3.md`는 데이터 지문을 `135e65d0406b956e`로 적고 있지만, 같은 실행의 `REPORT.md`와 `run_manifest.json`은 `de52ccfa16e847ee`로 일치합니다. 요약 문서의 다른 핵심 수치(133.77%, 55.19%, 107.89%, −14.48%, 0.63%, 5,799,758봉)는 보고서와 일치하므로, 대조 기준으로는 보고서 값을 씁니다. 불일치 원인은 확인하지 못했습니다.

### v0.3 결과와 차이가 날 때 원인을 나누는 방법

| 원인 | 확인 위치 | 해석 |
|---|---|---|
| 실행 코드 | 3a `comparison.json`의 `same_results`, `different` | 합성 데이터에서는 동일(ML 표 1곳이 2e-16 차이). 실제 데이터에서 다르면 코드 차이이므로 보고 |
| 데이터 | `data_fingerprint`, `sample.bars`, `provenance.fetched_at_range` | v0.3과 다르면 데이터가 다시 수집된 것. 이때는 수치 비교가 아니라 결론 비교만 의미 있음 |
| 데이터 수정·제외 | 3c manifest의 `audit.rows_signal_excluded_by_audit`, `audit.symbols_dropped`, `exclusions` | 감사가 제외한 행·종목만큼의 차이 |
| 비용 가정 | `cost_assumptions.changed_from_defaults` | 기본값에서 바꾼 항목만 표시됨 |
| 상장폐지 미청산 | `incomplete_trades_dropped` | 거래 단위 통계가 위로 치우쳤을 가능성의 크기 |
| 기간 경계 | `boundary_crossing_trades` | 다음 기간 가격이 섞인 거래 수 |

### 하지 않을 것

- 3c의 결과가 좋아 보여도 채택하지 않습니다. `validity.status`는 배당 미반영 등으로 계속 PROVISIONAL입니다.
- validation·test 결과를 보고 청산 규칙이나 임계값을 바꾸지 않습니다. test 기간(2024–)은 이미 v0.3에서 썼습니다.
- 감사 임계값을 바꿀 때는 새 감사 폴더에 새 실행으로 기록합니다.

## 4. 결과 정리와 커밋 범위

- **커밋하지 않음**: `data/audit/`, `data/crosscheck/`, 각 실행의 `*.csv`. 가격이 들어 있으며 `.gitignore` 대상입니다.
- **검토 후 커밋 가능한 요약**: `AUDIT_REPORT.md`, `audit_summary.json`, `crosscheck_status.json`, `REPORT.md`, `run_manifest.json`. 종목코드·날짜·건수·비율 위주이지만, 공유 전에 제공자 약관(`docs/DATA_SOURCES.md`)을 확인하세요. 예: `reports/v0.4/audit/`, `reports/v0.4/run/`
- **기록**: 결과는 `docs/RESEARCH_V0_4.md` §4에 v0.3과 비교해 적습니다. 부정적 결과도 그대로 적습니다.

## 알려진 문제

- 기본 기간 설정에서 어느 기간에 거래일이 하나도 없으면, 포트폴리오 단계가 `KeyError: 'halted_days'`로 중단됩니다. v0.3.0 코드와 현재 코드가 같습니다. 2016~현재 실제 데이터에서는 생기지 않으며, `--start`를 2022년 이후로 주면 생깁니다.
- 감사 실행 시간과 메모리 사용량은 실제 규모에서 측정하지 않았습니다.
