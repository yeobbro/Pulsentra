# 공식 출처 교차 검증과 API 설정 (v0.4)

## 현재 상태

| 검증 | 코드 | 오프라인 테스트 | 실제 실행 |
|---|---|---|---|
| 네이버 일봉 ↔ KIS 일봉 (종가·거래량·거래일·누락률·수정 기준 차이) | `app/research/crosscheck.py` `compare_daily`, 실행기 `python -m app.crosscheck` | 합성 시계열·가짜 클라이언트 | **실행 안 함** — KIS 키 없음 |
| 기업행동 후보 ↔ OpenDART 공시 | `match_corporate_actions`(원 공시·정정 공시를 `group_filings`로 묶음), `DartClient.disclosures` | 모의 응답·합성 공시 | 🟡 **삼성전자 1건만** 실행(2026-10-25, `data/crosscheck/dart_005930_*`). 나머지 후보 8,072건 미실행 |
| 네이버 일봉 ↔ KRX OPEN API 일별매매정보 | `app/krxcheck/`(표본·전체 시장·다중 거래일), `app/research/krx_crosscheck.py` | 모의 응답·TEST 데이터 | ✅ 4개 거래일(날짜별 1회 요청, 비교는 저장된 응답으로 오프라인). 결과는 `docs/DATA_SOURCES.md`·`docs/RESEARCH_V0_4.md` §6 |
| KIS가 표시한 일별 기업행동 필드(`prtt_rate` 등) | `parse_daily_rows`가 원문 그대로 보존, 실행기가 `kis_event_days_*.csv`로 저장 | 모의 응답 | **실행 안 함** |
| 상장폐지 목록 완전성 | `compare_listings` (참조 목록과 비교) | 합성 목록 | **실행 안 함** — 참조 목록(KRX KIND 등) 없음 |

KIS 행은 2026-10-25 현재도 **실행 안 함**(인증정보 없음)입니다. 키를 받았다고 가정하거나 결과를 만들어 넣지 않았습니다. 실행기는 키가 없으면 그 단계를 건너뛰고, 건너뛴 이유를 `crosscheck_status.json`에 남깁니다(종료 코드 3).

## API 설정

`.env`(Git 제외)에 다음 값을 넣습니다. 값은 출력하거나 커밋하지 마세요. 코드는 값의 존재 여부만 확인합니다.

| 변수 | 용도 | 발급처 |
|---|---|---|
| `KIS_APP_KEY`, `KIS_APP_SECRET` | 한국투자증권 Open API 시세 조회(읽기 전용) | KIS Developers 포털 |
| `KIS_PAPER` | `true`면 모의투자 도메인 | — |
| `KIS_MIN_INTERVAL_SECONDS` | 호출 간 최소 간격(기본 0.35초) | — |
| `DART_API_KEY` | OpenDART 인증키 | opendart.fss.or.kr |

```bash
cp .env.example .env     # 값 입력
python -m app.audit --data data/real --out data/audit/run1
python -m app.crosscheck --data data/real --audit data/audit/run1 --sample 30 --out data/crosscheck/run1
PULSENTRA_RUN_INTEGRATION=1 python -m pytest -m integration -q   # 실제 API 스모크 테스트(선택)
```

### 안전장치

- **KIS**: 호출할 수 있는 경로를 허용 목록(토큰, 현재가, 기간별 시세)으로 고정했습니다. 주문·잔고(`/trading/`) 경로는 코드에 없고, 호출을 시도하면 거부됩니다(`tests/test_providers.py::test_kis_adapter_is_read_only`).
- **재시도**: 연결 오류, 타임아웃, HTTP 429·5xx, KIS `EGW00201`(초당 한도)만 재시도합니다. 최대 3회, 대기는 1·2·4초입니다. 인증 오류(그 밖의 4xx)는 바로 실패합니다.
- **타임아웃**: 모든 호출에 적용합니다(`KIS_TIMEOUT_SECONDS`, `DART_TIMEOUT_SECONDS`, 기본 12초).
- **응답 검증**: KIS `output2`는 객체 배열이어야 하고 문서화된 필드가 모두 있어야 합니다. OpenDART 항목은 `rcept_dt`가 YYYYMMDD여야 합니다. 어긋나면 결과를 쓰지 않고 오류를 냅니다.
- **페이지 수 제한**: OpenDART 목록은 `max_pages`(기본 5)까지만 읽습니다.
- **오류 메시지**: 헤더, 토큰, 쿼리 문자열을 넣지 않습니다(키가 거기에 실립니다).

## 공식 문서와 코드 대조 기록 (2026-10-23)

이 개발 환경에서 `opendart.fss.or.kr`와 `apiportal.koreainvestment.com`은 네트워크 정책 때문에 접근할 수 없었습니다(프록시 403). 대신 KIS가 GitHub에 공개한 공식 예제 코드(`koreainvestment/open-trading-api`)를 읽어 대조했습니다.

| 항목 | 공식 예제 | Pulsentra 코드 | 일치 |
|---|---|---|---|
| 기간별 시세 경로 | `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | 같음 | 예 |
| TR ID | `FHKST03010100` (실전·모의 동일) | 같음 | 예 |
| 파라미터 | `FID_COND_MRKT_DIV_CODE`, `FID_INPUT_ISCD`, `FID_INPUT_DATE_1/2`, `FID_PERIOD_DIV_CODE`, `FID_ORG_ADJ_PRC` | 같음 | 예 |
| 수정주가 | `FID_ORG_ADJ_PRC` 0=수정주가, 1=원주가 | 0 사용, `adjusted=True` | 예 |
| 한 번에 받는 행 수 | 최대 100건 | 130일 창(평일 최대 94일) | 예 |
| 응답 필드 | `stck_bsop_date`, `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `stck_clpr`, `acml_vol`, `acml_tr_pbmn`, 그리고 `flng_cls_code`(락 구분), `prtt_rate`(분할 비율), `mod_yn`(변경 여부), `revl_issu_reas`(재평가 사유) | 앞의 7개를 사용. 뒤의 4개는 v0.4부터 원문 그대로 보존 | 예 |
| 도메인 | 실전 `openapi.koreainvestment.com:9443`, 모의 `openapivts.koreainvestment.com:29443` | 같음 | 예 |
| 토큰 경로 | `/oauth2/tokenP`, 유효 1일 | 같음, 만료 60초 전 갱신 | 예 |

**확인하지 못한 것:**

- `FID_ORG_ADJ_PRC=0`일 때 거래량(`acml_vol`)도 수정되는지
- `flng_cls_code` 값의 의미
- OpenDART 상태 코드 전체 목록
- `list.json`에 거래소 공시(주식분할결정 등)가 포함되는지

이 항목들은 첫 실제 호출 결과를 보고 확인해야 합니다. OpenDART 어댑터는 v0.2 문서에 적힌 개발가이드 기준(`corpCode.xml`, `list.json`, 상태 000·013)에 따르며, 이번에는 020(요청 한도 초과)을 별도로 처리하도록 추가했습니다.

## 비교 결과 해석 (`compare_daily`)

| verdict | 뜻 |
|---|---|
| `consistent` | 공통 거래일의 종가가 0.1% 이내로 같고, 수정 기준 차이도 없음 |
| `adjustment_basis_differs` | 구간마다 일정한 비율로 어긋남. 두 출처의 수정 기준(분할 반영 시점 등)이 다름. 레코드 오류는 아님 |
| `record_errors` | 같은 구간의 다른 날짜들과 맞지 않는 개별 날짜가 있음. 어느 쪽이 맞는지는 세 번째 근거로 판단해야 함 |
| `no_overlap` | 겹치는 기간이 없음 |

누락 거래일은 `missing_in_primary` / `missing_in_reference`로 따로 셉니다. 거래량 차이(`volume_mismatch_days`, `volume_ratio_median`)는 판정에 넣지 않고 따로 보고합니다. 네이버 거래량은 수정되지 않은 것으로 알려져 있어서, 분할 이전 구간의 거래량 비율이 분할 비율로 나타나야 정상입니다.

## 공시 매칭 해석 (`match_corporate_actions`)

공시 접수일(`rcept_dt`)이 이벤트일 120일 전부터 10일 후 사이에 있으면 같은 이벤트로 봅니다.

| status | 뜻 |
|---|---|
| `confirmed_kind_match` | 같은 종류의 공시가 있음(분할 의심 → 주식분할결정 등) |
| `filing_kind_mismatch` | 공시는 있으나 종류가 다름(예: 유상증자). 원인 재검토 필요 |
| `no_filing_found` | 기간 안에 관련 공시가 없음. 데이터 오류 가능성이 커짐 |
| `undetected_event` | 주식 수를 바꾸는 공시가 있는데 가격·거래량 검사가 잡지 못함(무상증자 등). 감사 검사의 누락 |

`app/research/validity.py`의 `cross_source_price_check`와 `corporate_action_disclosure_match`는 실행기가 실제로 돌고 통과했을 때만 검증 완료로 바뀝니다. 연구 실행에는 `python -m app.research ... --crosscheck data/crosscheck/run1/crosscheck_status.json`으로 넘깁니다. 두 항목이 모두 통과해도, 배당처럼 처리하지 않는 항목이 남아 있으면 결과 상태는 PROVISIONAL에 머뭅니다.
