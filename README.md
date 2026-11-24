# Pulsentra · 펄센트라

**AI-Powered Market Intelligence & Strategy Validation Platform**
데이터를 읽고, 전략을 검증하고, 위험을 관리한다.

Pulsentra는 종목을 추천하는 서비스가 아닙니다. 시장 데이터를 모으고, 투자 가설을 규칙으로 만들고, 과거 데이터로 검증합니다. 그 가설이 이후에도 유효한지 모의투자로 추적합니다. 이 프로젝트는 Market Signal Lab MVP를 확장한 것이며, MVP의 API와 데이터베이스는 그대로 호환됩니다.

> 실제 주문 기능은 없습니다. 신호 점수와 모델 확률은 수익을 보장하지 않습니다.
> v0.3부터 실제 한국 주식 일봉(2016~2026, 상장폐지 종목 포함)으로 검증한 결과가 `reports/v0.3/`에 있습니다. 검증한 전략 중 거래비용 후 일관된 우위를 보인 것은 없었습니다. 자세한 현황은 [`docs/PULSENTRA_STATUS.md`](docs/PULSENTRA_STATUS.md)를 보세요.
> 현재까지 시장 대비 지속 가능한 우위(Alpha)를 입증한 전략은 **없습니다.** 결과 상태 `VERIFIED`도 정해진 검증 항목을 통과했다는 뜻일 뿐, 수익이나 미래 성과를 보장하지 않습니다.

## 현재 상태 한눈에 보기 (2026-10-25, 브랜치 `research/v0.4-data-audit`)

| 구분 | 내용 | 근거 |
|---|---|---|
| **실행·확인함** | v0.4 데이터 감사 도구 구현, 실제 데이터 감사 2회 실행 — 두 실행의 핵심 산출물·지문 일치 | `data/audit/full_20261024T060804Z_a`, `_b` |
| | v0.3.0 코드(`a57dcc1`)와 v0.4 코드(`2edfc8b`)를 같은 실제 데이터로 실행 → 결과 동일(`same_results: true`), v0.3 결론(비용 차감 후 validation·test 480개 조합 중 양수 0개) 재확인. 원래 v0.3 실행 커밋 `4918af9`는 저장소에 없어, 원래 실행 코드와의 동일성은 증명하지 못함 | `reports/v0.4-compare/` (Git 제외) |
| | 저장된 KRX OPEN API 응답(유가증권, 날짜별 1회 요청)과 로컬 네이버 일봉의 4개 거래일 비교 | `data/krxcheck/full_market_*`, `history_*` |
| | OpenDART 대조 1건(삼성전자 2018 주식분할, 50:1 원문 확인), 원 공시·정정 공시 중복 매칭 개선 | `data/crosscheck/dart_005930_*` |
| | 다중 거래일 품질 리포트(`python -m app.krxcheck.history`)와 자동 테스트 | `tests/test_krx_history.py` |
| | 전체 테스트 `272 passed, 2 skipped, 1 warning` (경고: Starlette TestClient의 `StarletteDeprecationWarning`) | 아래 "테스트" |
| **미실행·미확인** | KIS 가격 교차 검증(인증정보 없음), 나머지 기업행동 후보 8,072건의 공시 대조, 2026-10-22·23 KRX·네이버 차이의 원인, 배당·총수익 반영, 기업행동 자동 보정, 감사·교차 검증 결과를 반영한 v0.3 전략 재실행(`--audit --crosscheck`), 새로운 Alpha 결론, 데이터 이용 권한과 외부 제공 범위 | [`docs/PULSENTRA_STATUS.md`](docs/PULSENTRA_STATUS.md) |

테스트 통과는 코드가 정해진 동작을 한다는 뜻이고, 실제 데이터로 전략이 검증됐다는 뜻이 아닙니다. 다음 목표는 **Pulsentra Alpha Research Lab v1**입니다([`docs/IMPLEMENTATION_ROADMAP.md`](docs/IMPLEMENTATION_ROADMAP.md)).

## Alpha Research Lab v1 (MVP, `research/v0.4-data-audit`에 병합 — PR #5~#7)

실험을 먼저 등록하고, 요인을 같은 규칙으로 평가하고, 고정된 검사로 판정합니다. 진행 상황: [`docs/ALPHA_RESEARCH_LAB_V1_PROGRESS.md`](docs/ALPHA_RESEARCH_LAB_V1_PROGRESS.md).

```bash
python -m app.ledger template > spec.json                    # 사전 등록 양식 (채워서 등록)
python -m app.ledger register spec.json                      # data/ledger/ledger.jsonl (append-only, 해시 체인)
python -m app.factorlab run --data data/real --out data/factorlab/run1 --audit data/audit/run1 --ledger-id <ID>
python -m app.robustness evaluate --factorlab data/factorlab/run1 --factor mom_60 --out data/robustness/run1 --ledger-id <ID>
python -m app.ledger decide <ID> hold --reason "..."         # 결정은 사람이, 이유와 함께
python -m app.ledger verify --files                          # 원장·연결 파일 무결성
```

- Factor Lab: 모멘텀(20/60/120)·반전·변동성·거래대금·거래대금 비율·비유동성, IC·Rank IC·분위 수익률·회전율·비용 후 동일가중 대비 초과수익. 요인 ≤ t, 진입 t+1 시가, 청산 t+1+h 시가.
- Robustness Gate: PASS / WATCH / FAIL / INSUFFICIENT_DATA, 기준은 [`docs/ROBUSTNESS_GATE.md`](docs/ROBUSTNESS_GATE.md). 선택 편향 검사로 PBO(CSCV)와 Deflated Sharpe Ratio를 포함(Factor Lab 게이트만, 2026-10-30).
  Strategy Library 저장 실행도 별도 어댑터로 평가합니다(`POST /api/library/runs/{id}/robustness`).
- **연구 데이터 적격성**(PR #6으로 도입, 현재 `eligibility-v3`; 가격 교차 검증 증거 `price-evidence-v2`는 PR #7): Factor Lab·Strategy Library 실행마다 데이터 출처·감사 지문 일치·제외/검토 종목·감사 범위와 신선도를 판정해 USABLE / REVIEW_REQUIRED / UNUSABLE / INSUFFICIENT_EVIDENCE로 기록하고, 두 게이트가 같은 방식으로 읽습니다. 현재 실제 데이터는 (가격 증거 없이) 감사 적용 시 REVIEW_REQUIRED, 미적용 시 UNUSABLE이며, KOSPI 4개 거래일 가격 증거를 연결하면 2026-10-22·23을 포함한 실행은 UNUSABLE입니다. [`docs/DATA_ELIGIBILITY.md`](docs/DATA_ELIGIBILITY.md) 현재 앱 DB 실행은 감사·결과 상태가 연결되지 않아 PASS가 나올 수 없고, DEMO 실행은 FAIL입니다.
- 실제 데이터는 범위를 제한한 스모크 실행 1회뿐이며(2023년 이후, 판정 INSUFFICIENT_DATA) 연구 결론이 아닙니다.
- **Strategy Library**(대시보드 "Strategy Library" 탭, `/api/library/*`): 대표 전략 템플릿 18개(AVAILABLE 13 · LIMITED 2 · PLANNED 3)의 원리·규칙·파라미터 범위를 보고, Strategy Lab과 같은 엔진으로 백테스트·같은 조건 비교·Experiment Ledger 사전 등록을 합니다. 인기·성과 순위가 아닙니다. [`docs/STRATEGY_LIBRARY.md`](docs/STRATEGY_LIBRARY.md)

## v0.4 (진행 중) — 데이터 감사·재현성·교차 검증

v0.4는 v0.3 결과를 믿을 수 있는지 판단할 도구를 만드는 단계입니다. 새 전략이나 성과는 없습니다. 자세한 내용은 [`docs/RESEARCH_V0_4.md`](docs/RESEARCH_V0_4.md)에 있습니다.

```bash
pip install -r requirements-lock.txt          # 검증된 버전 한 벌 (Python 3.12+). 3.11은 requirements*.txt
python -m app.audit --data data/real --out data/audit/run1                       # 종목 단위 데이터 감사 (읽기 전용)
python -m app.crosscheck --data data/real --audit data/audit/run1 --out data/crosscheck/run1   # KIS·OpenDART 대조 (키 필요)
python -m app.research --data data/real --format naver_raw --out reports/v0.4 \
    --audit data/audit/run1 --crosscheck data/crosscheck/run1/crosscheck_status.json
```

- 감사 기준과 출력: [`docs/DATA_AUDIT.md`](docs/DATA_AUDIT.md) · 교차 검증·API 설정: [`docs/CROSS_VALIDATION.md`](docs/CROSS_VALIDATION.md)
- **실제 데이터 검증 절차(Mac): [`docs/REAL_DATA_RUNBOOK.md`](docs/REAL_DATA_RUNBOOK.md)**
- 설치·테스트·재현: [`docs/SETUP.md`](docs/SETUP.md) · 구조·방법론·평가 기준: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- 모든 연구 실행은 결과 상태(INELIGIBLE / PROVISIONAL / VERIFIED)와 그 근거를 `run_manifest.json`과 `REPORT.md`에 남깁니다.
- **실행 범위(2026-10-25):** 실제 데이터 감사 2회, v0.3.0 코드와 v0.4 코드(`2edfc8b`)의 실제 데이터 비교, KRX 4개 거래일 대조, OpenDART 1건 대조를 실행했습니다. KIS 대조는 인증정보가 없어 실행하지 않았고, `--audit --crosscheck`를 반영한 연구 재실행도 하지 않았습니다. KRX·OpenDART 응답은 연구용 로컬 검증 자료이며 외부에 제공하지 않습니다([`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md)).

## v0.3 — 실제 데이터 전략 검증

v0.3은 기능 추가가 아니라 **실제 과거 주가로 기존 전략을 검증**하는 단계입니다. 결론(비용 차감 후 validation·test 양쪽에서 우위가 있는 조합 없음)은 2026-10-24에 v0.3.0 코드로 같은 실제 데이터를 다시 실행해 재확인했습니다. 원래 v0.3 실행 커밋 `4918af9`는 저장소에 없으므로, 원래 실행 코드와의 동일성은 증명되지 않았습니다. 감사 결과를 반영한 재실행은 아직 하지 않았습니다.

```bash
pip install -r requirements.txt -r requirements-research.txt
# 1) 실데이터 수집 (네이버 금융 일봉 + KRX 상장/폐지 목록 + 지수, 재실행 시 이어받기, 약 30분)
python scripts/collect_kr_daily.py --out data/real --start 2016-01-01
# 2) 검증 실행 (약 15분, 메모리 6GB 이상 권장)
python -m app.research --data data/real --format naver_raw --out reports/v0.3
# 직접 준비한 CSV(표준 형식)로 실행할 때
python -m app.research --data my_csv_dir --format standard --out reports/mine
```

- 결과: `reports/v0.3/REPORT.md` 와 CSV들 (전략×청산 조합×기간 결과, 워크포워드, 무작위 비교, 국면별, 비용 민감도, 포트폴리오, ML vs 규칙, 품질 검사, 거래별 기록)
- 데이터 출처·한계: [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) · 방법과 결론: [`docs/RESEARCH_V0_3.md`](docs/RESEARCH_V0_3.md)
- 모든 데이터에 출처 유형(`REAL` / `SYNTHETIC` / `TEST`)을 기록하며, 리서치 실행은 REAL 데이터만 받습니다.

## 기능

| 모듈 | 내용 | 상태 |
|---|---|---|
| Market Data Center | CSV·KIS 일봉 적재, 행 단위 품질 검사(중복·결측·가격 이상·고저 역전·음수 거래량·급변·분할 의심·거래일 누락·거래량 0), 수집 기록, 데이터 상태 LIVE/DELAYED/HISTORICAL/DEMO/STALE/ERROR, 종목 메타데이터(상장·폐지·업종·벤치마크) | 구현 |
| Market Scanner | 거래량 급증 + 횡보 기본 조건(모두 설정 가능), 긍정·부정 신호, 미확인 항목 표시, 조건 저장, 정렬·필터 | 구현 |
| Stock Detail | 캔들·거래량·이평선·볼린저·참고 손절/익절선, 지표, 과거 유사 신호 이후 수익률(신뢰구간), 공시, 품질 이력 | 구현 |
| Strategy Lab | 전략 10종(플러그인), 손절(ATR·스윙 저점·고정 %), 익절(R 배수·일부 익절+추적·이평 이탈·ATR 추적·시간), 위험 설정 | 구현 (공시 이벤트 전략은 미구현으로 표시) |
| Backtest Engine | 포트폴리오 시뮬레이션: 다음 날 시가 체결, 같은 봉 손절 우선, 갭, 현금·보유 수·업종 한도, 일일 손실·낙폭 중단, 상장폐지 처리, 비용·세금·슬리피지 | 구현 |
| 검증 | 비용 전·후, 벤치마크·무작위 진입 기준, 월별·연도별, 국면별, 승률·기대값 신뢰구간, 표본 경고, 청산 규칙 공정 비교, 워크포워드 파라미터 선택 | 구현 |
| ML Model Lab | 삼중 장벽(익절·손절·기간) / 1·5·20·60·252일 방향, 로지스틱·랜덤포레스트·HistGB vs 기준 확률·단순 규칙, 퍼지된 워크포워드, 확률 보정, 누수 검사, 채택 게이트, 모델 기록 | 구현 (v0.3 실데이터에서 ML 필터 15개 비교 모두 채택 기준 불통과, 채택 모델 없음) |
| Paper Trading | 종가 기준 가상 계좌·주문(시장가/지정가, 만료·거부), 손절·익절·시간 청산, 백테스트와 비교 | 구현 |
| Alerts | 거래량·횡보·돌파·추세 이탈·손절/익절 근접·데이터 지연/오류·공시 위험, 중복 방지, 입력값 저장 | 구현 (종가 기준) |
| 실시간 | 틱→1·5분봉 집계, 수신 지연 판정 | 일부 구현. KIS WebSocket 클라이언트는 미구현 |
| OpenDART | 고유번호·공시 목록 어댑터, 희석 위험 분류, 기업행동 후보와 공시 매칭(원 공시·정정 공시를 한 기업행동으로 묶음) | 구현. 실제 호출은 삼성전자 1건만(2026-10-25) |
| KRX OPEN API | 일별매매정보 연구용 대조(`python -m app.krxcheck`, `.fullmarket`, `.history`), 앱 저장소 적재 차단 | 연구용 로컬 검증만 구현. 화면·API로 외부 제공하는 기능은 이용약관(제3자 제공 금지) 때문에 미구현 |

## 설치와 실행 (macOS / Linux, Python 3.11+)

개인용 웹 배포(Render, 단일 사용자 Basic 인증, 영구 디스크): [`docs/DEPLOY_RENDER.md`](docs/DEPLOY_RENDER.md) · 설정 파일 [`render.yaml`](render.yaml)

```bash
cd pulsentra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # 키가 없어도 CSV·DEMO로 모든 분석 기능이 동작합니다
uvicorn app.main:app --reload # http://127.0.0.1:8000
```

`./run_mac.sh` 스크립트도 같은 일을 합니다.

### 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest -q                               # 오프라인(합성·TEST 데이터, 모의 API)
python -m pytest -q -W error::DeprecationWarning  # CI와 같은 엄격 모드
```

실제 API를 부르는 테스트는 `integration` 마커가 붙어 있고, `PULSENTRA_RUN_INTEGRATION=1`과 키가 있을 때만 실행됩니다.

`python -m pytest -q -W error::DeprecationWarning` 결과. 현재 기준은 첫 행이고, 나머지는 병합 전 작업 트리에서의 당시 기록입니다:

| 환경 | 결과 |
|---|---|
| **PR #8 병합 후 `654f277`**, Linux VM Python 3.10.12 (2026-10-26) | **427 passed, 2 skipped, 1 warning** |
| PR #7 병합 후 `03c4463`, Linux VM Python 3.10.12 (2026-10-26) | 423 passed, 2 skipped, 1 warning |
| (당시 `feat/alpha-research-lab-v1`) Linux VM, Python 3.10.12, numpy 2.2.6 · pandas 2.3.3 | **389 passed, 2 skipped, 1 warning** (기존 272 + 원장·Factor Lab·Gate 67 + Strategy Library 31 + Strategy Library 게이트 19) |
| 클라우드 Linux, Python 3.14.6 + `requirements-lock.txt`(numpy 2.5.4, Mac `.venv`와 같은 버전) | 날짜 계산 3곳 수정 전 34 failed · 수정 후 **389 passed, 2 skipped, 1 warning**(임시 git 저장소에서 실행, `.git` 없이 실행하면 저장소 규칙 테스트 23개가 추가로 skip) |
| (당시 `feat/research-data-eligibility` 작업 트리) Linux VM Python 3.10.12 | **405 passed, 2 skipped, 1 warning** (위 389 + 데이터 적격성 16) |
| Mac 본체(`.venv`, Python 3.14.6) | PR #8 기준 427 passed, 2 skipped(사용자 보고) · 이전 389 passed, 2 skipped, 1 warning(사용자 보고, `e4912a3` 기준) |
| CI(GitHub Actions) | **미확인** — 병합됐지만 이 환경에서 Actions 결과를 조회하지 않음 |

numpy 2.5에서 `pd.Timedelta(days=N)`이 DeprecationWarning을 내 엄격 모드에서 실패하던 것을 `pd.Timedelta(N, unit="D")`로 고쳤습니다.

이전 결과(2026-10-25, `research/v0.4-data-audit` `2292168`): `python -m pytest -q` → **272 passed, 2 skipped, 1 warning**. Mac Python 3.14.6(사용자 실행)과 Linux Python 3.10에서 같은 결과입니다. 건너뛴 2개는 키가 필요한 실제 API 스모크 테스트이고, 경고 1건은 `starlette.testclient`의 `httpx` 사용 방식에 대한 `StarletteDeprecationWarning`입니다.

## 처음 사용하는 순서

1. **데이터 센터**에서 일봉 CSV를 올립니다. 필수 열은 `date,open,high,low,close,volume`, 권장 열은 `market,symbol,turnover,adjusted`입니다. 키가 있으면 KIS 일봉을 동기화합니다. 화면만 보려면 **DEMO 불러오기**를 누릅니다.
2. **종목 메타데이터 CSV**를 함께 올립니다. 형식은 `market,symbol,name,sector,listed_date,delisted_date,is_benchmark`입니다. 시장지수 1개를 `is_benchmark=true`로 넣으면 상대강도·초과수익·국면 분석이 켜집니다. 상장폐지 종목도 넣어야 생존편향을 줄일 수 있습니다.
3. **Market Scanner** → **종목 상세** → **Strategy Lab** 순서로 가설을 검증합니다. 전략 비교, 청산 규칙 비교, 워크포워드 결과를 함께 봅니다.
4. **ML Model Lab**에서 모델이 단순 규칙보다 나은지 표본 외 데이터로 확인합니다. 채택 기준을 통과하지 못한 모델은 예측에 쓰지 않습니다.
5. **모의투자** 계좌를 만들어 같은 규칙을 종가 기준으로 추적합니다.

## 환경 변수

| 이름 | 용도 |
|---|---|
| `KIS_APP_KEY`, `KIS_APP_SECRET` | 한국투자증권 Open API (서버 전용, 읽기 전용 시세) |
| `KIS_PAPER` | `true`면 모의투자 도메인 사용 (주문 기능은 없음) |
| `KIS_MIN_INTERVAL_SECONDS` | KIS 호출 간 최소 간격 (기본 0.35초) |
| `DART_API_KEY` | OpenDART 인증키 |
| `KIS_TIMEOUT_SECONDS`, `DART_TIMEOUT_SECONDS` | 호출 타임아웃(기본 12초). 재시도는 최대 3회(429·5xx·연결 오류만) |
| `PULSENTRA_DATA_DIR`, `PULSENTRA_DB_PATH`, `PULSENTRA_MODEL_DIR`, `PULSENTRA_MODEL_PATH`, `PULSENTRA_MODEL_META_PATH` | 저장 위치 (`MSL_*` 이름도 계속 인식). Render에서는 모두 영구 디스크 `/var/data` 아래(`render.yaml`) |
| `PULSENTRA_COMMISSION_BPS`, `PULSENTRA_SELL_TAX_BPS`, `PULSENTRA_SLIPPAGE_BPS` | 비용 기본값(가정값). 증권사 수수료와 해당 연도·시장의 세율을 확인해 설정 |
| `PULSENTRA_STALE_AFTER_BUSINESS_DAYS` | 일봉이 STALE로 바뀌는 영업일 수 (기본 2) |
| `PULSENTRA_ENV` | `production`이면 접근 제어 필수(인증정보 없으면 시작 실패)·상태 변경 요청의 출처 검사 (기본 `development`) |
| `PULSENTRA_ACCESS_USERNAME`, `PULSENTRA_ACCESS_PASSWORD` | 단일 사용자 HTTP Basic 인증. 둘 다 있을 때만 켜짐(운영 모드에서는 필수). 값은 저장소에 넣지 않음 |
| `PULSENTRA_AUTOLOAD_DEMO` | `true`면 데이터베이스가 비어 있을 때 첫 시작에 합성 DEMO를 한 번 적재 (기본 `false`) |

`.env`는 `.gitignore`에 포함되어 있습니다. 키를 프런트엔드나 저장소, 로그에 넣지 마세요.

## 백테스트 가정

- 신호는 t일 종가까지의 데이터로 계산하고, t+1일 시가에 슬리피지를 더해 체결합니다.
- 같은 일봉에서 손절가와 익절가를 모두 지나면 손절이 먼저 일어난 것으로 처리합니다. 일봉으로는 순서를 알 수 없기 때문입니다.
- 시가가 손절가 아래에서 시작하면 시가에 손절하고, 익절가 위에서 시작하면 시가에 익절합니다.
- 추적 손절과 이동평균 청산은 종가로 판단해 다음 봉부터 적용합니다.
- 상장폐지 종목은 마지막 종가에 청산합니다. 종목 메타데이터가 없으면 생존편향 여부를 확인할 수 없다고 보고서에 표시합니다.
- 비용은 수수료(편도)·매도세·슬리피지 가정값입니다.

## 프로젝트 구조

```
app/
  main.py            FastAPI 라우트 (MVP 엔드포인트 유지)
  settings.py        환경 변수
  db.py              SQLite 스키마·마이그레이션
  data/              데이터 표준, 품질 검사(quality), 종목 단위 감사(audit), 적재 기록, 데이터 상태
  audit/             감사 CLI (python -m app.audit)
  crosscheck/        공식 출처 교차 검증 CLI (python -m app.crosscheck)
  krxcheck/          KRX 시세 대조 CLI (표본·전체 시장·다중 거래일 리포트, 연구용 로컬 전용)
  research/          리서치 로더·연구·보고서, manifest(재현 정보), validity(결과 상태), crosscheck(비교 로직),
                     ledger(실험 원장), factors(Factor Lab), robustness(Robustness Gate)
  strategies/catalog.py  Strategy Library 카탈로그(메타데이터·파라미터 범위)
  strategy_library.py    Strategy Library 실행·비교·원장 연결·게이트 근거 수집
  research/robustness_strategy.py  Strategy Library 실행용 Robustness Gate
  ledger/            실험 원장 CLI (python -m app.ledger)
  factorlab/         Factor Lab CLI (python -m app.factorlab)
  robustness/        Robustness Gate CLI (python -m app.robustness)
  providers/         KIS(시세), OpenDART(공시), http(재시도 상한)
  indicators.py      인과적 지표 (+ INDICATOR_SPECS 계산식)
  scanner.py         스캐너, 긍정·부정 근거
  strategies/        진입 전략 플러그인
  risk/              비용, 손절·익절, 수량, 위험 설정
  backtesting/       포트폴리오 엔진, 신호 단위 연구, 지표, 보고서
  validation/        시간순 폴드, 국면, 파라미터 격자
  models/            라벨, ML Lab (워크포워드·보정·채택 게이트)
  paper_trading/     모의투자
  alerts.py          알림
  realtime/          틱 → 분봉, 수신 지연
  services.py        저장소 ↔ 분석 연결
  backtest.py, ml.py MVP 원본 (호환·참고용)
static/index.html    대시보드
tests/               pytest (MVP unittest 포함)
docs/                분석·현황·전략 명세·로드맵
```

## API 요약

| 경로 | 설명 |
|---|---|
| `GET /api/health`, `/api/provider-status`, `/api/dashboard` | 상태·요약 |
| `GET /api/data/status`, `/api/data/quality`, `/api/data/runs` | 데이터 상태·품질·수집 기록 |
| `POST /api/data/import`, `/api/instruments/import`, `/api/demo/load`, `DELETE /api/demo` | 적재 |
| `POST /api/sync/{symbol}`, `GET /api/quote/{symbol}`, `POST /api/dart/sync` | 외부 연동 |
| `GET /api/signals` (MVP), `POST /api/scan`, `GET/POST/DELETE /api/scans` | 스캐너 |
| `GET /api/stock/{market}/{symbol}`, `/api/history/{symbol}`, `/api/disclosures/{symbol}` | 종목 상세 |
| `GET /api/strategies`, `GET/POST /api/strategy-configs` | 전략 |
| `POST /api/backtest` (MVP), `/api/backtests/run`, `/compare`, `/exits`, `/walk-forward`, `GET /api/backtests` | 백테스트 |
| `POST /api/ml/run`, `/api/train` (MVP), `GET /api/ml/models`, `/api/model-status`, `/api/predict/{symbol}` | ML |
| `/api/paper/accounts…` | 모의투자 |
| `POST /api/alerts/evaluate`, `GET /api/alerts` | 알림 |
