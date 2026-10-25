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
- **공시**: `filing_check.passed`는 모든 후보가 `confirmed_kind_match`이고 회사코드 누락이 없을 때만 참입니다(`ambiguous`는 통과가 아님). `undetected_event` 건수는 감사가 놓친 **기업행동** 수입니다(공시 문서 수가 아님).
- **원 공시와 정정 공시(2026-10-25 변경)**: OpenDART 개발가이드에서 `[기재정정]`·`[첨부정정]`·`[첨부추가]`·`[발행조건확정]`은 "본 보고서명으로 이미 제출된 보고서"를 다시 낸 것이고, `rm`의 `정`은 "본 보고서 제출 후 정정신고가 있음"입니다. 그래서 공시를 먼저 기업행동 단위로 묶습니다(`app/research/crosscheck.py`의 `group_filings`).
  - 정정 공시는 같은 종목·같은 보고서명(앞의 대괄호 표시 제외)의 이전 원 공시가 365일 안에 **하나뿐일 때만** 그 원 공시에 연결합니다. 여러 개면 `rm`에 `정`이 붙은 것이 하나일 때만 연결하고, 아니면 `ambiguous`로 남깁니다. 원 공시 두 건은 합치지 않습니다.
  - 원 공시가 없는 정정 공시는 혼자 하나의 기업행동이 됩니다(`link_basis=correction_without_original`).
  - 후보는 기업행동에 매칭되고(가까운 문서 기준), 매칭되지 않은 기업행동은 한 번만 `undetected_event`로 나옵니다. 여러 후보가 같은 기업행동에 매칭되는 것은 예전처럼 허용합니다. 맞는 종류의 서로 다른 기업행동이 같은 거리에 있으면 `ambiguous`입니다.
  - **스키마 변경(하위 호환)**: `corporate_action_matches.csv`의 기존 열(`symbol, date, kind, status, filing, filing_kind, filed, days_before_event`)과 의미는 그대로이고, `action_id, action_documents, rcept_nos, action_filings, link_basis` 열과 상태 `ambiguous`가 추가됐습니다. `filings.csv`에는 `rm` 열이 추가됩니다. `crosscheck_status.json`의 `filing_check`에 `corporate_action_documents`(문서 수)·`corporate_actions`(기업행동 수)·`share_base_actions`가 추가됩니다. 이전 실행의 `filings.csv`에는 `rm`이 없으므로 그때는 보고서명 규칙만으로 연결합니다.
- **알려진 한계**: 상장폐지 종목은 OpenDART `corpCode`에 종목코드가 없을 수 있습니다(`no_corp_code`에 기록).

## 2b. KRX 공식 시세 대조 (`python -m app.krxcheck`, KIS 없이)

KIS 경로(`app.crosscheck`)와 별개입니다. KRX 일별 서비스는 **기준일 하루의 시장 전체**를 한 번에 돌려주므로, 요청 수는 "날짜 수 × 필요한 시장 수"입니다. 종목 수와는 무관합니다.

- **필요한 환경변수**: `KRX_API_KEY`(필수), `KRX_TIMEOUT_SECONDS`(기본 20), `KRX_MIN_INTERVAL_SECONDS`(기본 0.5). `--dart`를 쓸 때만 `DART_API_KEY`.
- **안전장치**:
  - `--live`가 없으면 `plan.json`만 쓰고 네트워크를 쓰지 않습니다.
  - `--live`에는 `--spec-confirmed`가 함께 있어야 합니다. `app/providers/krx.py`의 `SERVICES`를 공식 명세와 대조한 뒤에만 붙이세요.
  - 계획이 `--max-requests`(기본 6)를 넘으면 실행을 거부합니다.
  - 인증 실패(401·403)나 한도 초과(429)가 나오면 즉시 멈추고 종료 코드 4를 냅니다.
- **산출물** (`data/krxcheck/<run>/`, Git 제외):
  - `plan.json`
  - `krx_<service>_<basDd>.csv`: 표본 종목 행만
  - `comparison_detail.csv`
  - `krxcheck_status.json`
  - 후보를 지정했을 때: `corporate_action_matches.csv`, `candidate_verdicts.csv`, `dart_filings.csv`
- **상태값**:

| status | 뜻 |
|---|---|
| `match` | OHLC가 같고 거래량이 **정확히** 같음(주 단위, 허용 오차 없음) |
| `adjusted_price_basis` | OHLC 네 가격이 **모두 같은 방향으로, 같은 비율로** 다름(하나라도 같거나 방향이 섞이면 해당 없음). 수정주가 기준 차이이며, `split_like_ratio`가 있으면 분할로 보이는 비율. 일부 필드만 같고 나머지가 1호가 다르면 조정이 아니라 `price_mismatch`(2026-10-24 수정) |
| `price_mismatch`, `volume_mismatch` | 실제 불일치 |
| `missing_in_krx`, `missing_in_primary` | 한쪽에만 데이터가 있음 |
| `not_comparable` | 양쪽 모두 없음, 거래정지 표시 행, 거래가 없는 날, 또는 한쪽에 같은 종목·날짜의 행이 둘 이상(중복 행) |
| `krx_day_empty` | KRX가 빈 응답을 줌 |
| `request_failed` | 요청 실패 |

- **판정 기준**: `krx.passed`는 비교 가능한 쌍이 있고, `price_mismatch`·`volume_mismatch`가 0일 때만 참입니다. 후보의 `verified`는 **같은 종류의 OpenDART 공시와 KRX 가격 단계가 모두 맞을 때만** 나옵니다. 나머지는 이유와 함께 `unverified`로 남습니다.
- **삼성전자 005930, 2018-05-04 후보**: 공시와 KRX 자료로 실제 확인하기 전까지는 확정하지 않습니다. 분할 전에 거래정지가 있었다면, "직전일"은 마지막으로 실제 일봉이 있는 날로 자동 선택됩니다.

## 2c. KRX 전체 시장 대조 (`python -m app.krxcheck.fullmarket`)

한 날짜의 **시장 전체**를 KRX에서 한 번만 받아 보관하고, 비교는 로컬 파일로만 반복합니다. 표본 대조(2b)가 고른 몇 종목만 보는 것과 달리 그날 KRX에 있는 모든 종목을 봅니다.

```
# 1) 받기: 요청 1회, 재시도 없음. --out이 비어 있지 않으면 거부(같은 날짜 중복 호출 방지)
python -m app.krxcheck.fullmarket fetch --date 2026-10-23 --market KOSPI --out data/krxcheck/full_market_20261023 --live --spec-confirmed
# 2) 비교: 네트워크 없음, data/real에는 쓰지 않음. 기본 출력은 <krx-dir>/compare
python -m app.krxcheck.fullmarket compare --krx-dir data/krxcheck/full_market_20261023 --data data/real --date 2026-10-23
```

- `--live --spec-confirmed`가 없으면 보낼 요청 1건만 출력하고 아무것도 받지 않습니다.
- KRX는 가장 최근 거래일을 **다음 날 08시 이후**에만 돌려줍니다(공식 서비스 정의). 그 전에 부르면 빈 응답(종료 코드 5)입니다.
- **fetch 산출물:** `krx_raw_<service>_<basDd>.csv`(응답 원문, 모든 값 문자열), `krx_<service>_<basDd>.csv`(정규화), `fetch_meta.json`(요청·수신 시각, 행 수, 중복·불량 코드 수, 파일 SHA-256, 완전성 메모). 실패하면 `error.json`(오류 종류, 시도 횟수). 키는 어디에도 쓰지 않습니다.
- **compare 산출물:** `comparison_detail.csv`(KRX 행마다 1행: 양쪽 OHLCV, 필드별 일치 여부, 종가 차이, 거래량 차이와 비율, 상태, 사유, 기존 표본 분류기의 판정 `classifier_status`), `comparison_summary.json`, `COMPARISON.md`.
- **상태(서로 겹치지 않음, 위에서부터 먼저 해당하는 것):**

| status | 뜻 |
|---|---|
| `krx_row_invalid` | KRX 행의 코드가 쓸 수 없음, 같은 코드가 응답에 두 번 이상, 또는 숫자가 아닌 값 |
| `missing_local_file` | `raw/<코드>.csv` 없음. `in_universe`로 수집 누락인지 수집 범위 밖(우선주·리츠 등)인지 구분 |
| `local_row_invalid` | 로컬 파일에 그 날짜가 두 번 이상이거나 값이 비었거나 숫자가 아님 |
| `missing_local_date` | 로컬 파일에 그 날짜 행이 없음 |
| `not_comparable` | 한쪽에 거래가 없음(KRX 거래량 0·가격 없음, 로컬 거래정지 표시 행) |
| `match` | O·H·L·C·거래량 모두 같음 |
| `volume_mismatch_only` | 가격 4개는 같고 거래량만 다름 |
| `close_mismatch` | 종가가 다름(다른 필드는 `*_equal` 열 참고) |
| `ohl_mismatch_close_eq` | 종가는 같고 시가·고가·저가 중 하나 이상이 다름 |

- "OHLC 불일치(전체)" = `close_mismatch` + `ohl_mismatch_close_eq`이며, 요약에 겹치는 합계로 따로 적습니다.
- 일치율의 분모는 비교 가능 행(`match` + `volume_mismatch_only` + `close_mismatch` + `ohl_mismatch_close_eq`)이고, 요약에 분모를 항상 함께 적습니다.
- 일치 기준은 **정확히 같음**(원 단위 가격, 주 단위 거래량)입니다. 과거 날짜는 네이버가 수정주가라서 기업행동이 있었던 종목이 `close_mismatch`로 나옵니다. 이런 행은 `classifier_status=adjusted_price_basis`로 구분됩니다.
- 2026-10-24 결과와 해석은 `docs/DATA_SOURCES.md`의 "2026-10-24 전체 시장 대조 결과"에 있습니다.
- **라이선스:** KRX OPEN API 응답은 연구용 로컬 검증 자료로만 씁니다. 이용약관 제11조②(제3자 제공 금지)·제6조②(비상업적 이용) 때문에 앱 저장소 적재는 코드에서 거부됩니다(`app/data/schema.py`의 `license_refusal`. 적재 함수의 출처 인자, 표의 `source` 열, 쓰기 함수 `db.upsert_candles` 세 곳에서 대소문자·공백과 무관하게 확인). `source` 열을 지운 파일은 구별할 수 없습니다. 화면·API·다운로드로 외부에 제공하는 기능은 라이선스를 서면으로 확인하기 전까지 미구현입니다. 체크리스트와 질문 목록은 `docs/DATA_SOURCES.md`에 있습니다.

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
