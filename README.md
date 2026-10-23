# Pulsentra · 펄센트라

**AI-Powered Market Intelligence & Strategy Validation Platform**
데이터를 읽고, 전략을 검증하고, 위험을 관리한다.

Pulsentra는 종목을 추천하는 서비스가 아닙니다. 시장 데이터를 모으고, 투자 가설을 규칙으로 만들고, 과거 데이터로 검증합니다. 그 가설이 이후에도 유효한지 모의투자로 추적합니다. 이 프로젝트는 Market Signal Lab MVP를 확장한 것이며, MVP의 API와 데이터베이스는 그대로 호환됩니다.

> 실제 주문 기능은 없습니다. 신호 점수와 모델 확률은 수익을 보장하지 않습니다.
> v0.3부터 실제 한국 주식 일봉(2016~2026, 상장폐지 종목 포함)으로 검증한 결과가 `reports/v0.3/`에 있습니다. 검증한 전략 중 거래비용 후 일관된 우위를 보인 것은 없었습니다. 자세한 현황은 [`docs/PULSENTRA_STATUS.md`](docs/PULSENTRA_STATUS.md)를 보세요.

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
- 설치·테스트·재현: [`docs/SETUP.md`](docs/SETUP.md) · 구조·방법론·평가 기준: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- 모든 연구 실행은 결과 상태(INELIGIBLE / PROVISIONAL / VERIFIED)와 그 근거를 `run_manifest.json`과 `REPORT.md`에 남깁니다.
- **미실행:** 이 개발 환경에는 실제 데이터와 API 키가 없어, 실제 데이터 감사와 KIS·OpenDART 대조는 아직 실행하지 않았습니다.

## v0.3 — 실제 데이터 전략 검증

v0.3은 기능 추가가 아니라 **실제 과거 주가로 기존 전략을 검증**하는 단계입니다.

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
| ML Model Lab | 삼중 장벽(익절·손절·기간) / 1·5·20·60·252일 방향, 로지스틱·랜덤포레스트·HistGB vs 기준 확률·단순 규칙, 퍼지된 워크포워드, 확률 보정, 누수 검사, 채택 게이트, 모델 기록 | 구현 (실데이터 검증 전, 채택 모델 없음) |
| Paper Trading | 종가 기준 가상 계좌·주문(시장가/지정가, 만료·거부), 손절·익절·시간 청산, 백테스트와 비교 | 구현 |
| Alerts | 거래량·횡보·돌파·추세 이탈·손절/익절 근접·데이터 지연/오류·공시 위험, 중복 방지, 입력값 저장 | 구현 (종가 기준) |
| 실시간 | 틱→1·5분봉 집계, 수신 지연 판정 | 일부 구현. KIS WebSocket 클라이언트는 미구현 |
| OpenDART | 고유번호·공시 목록 어댑터, 희석 위험 분류 | 구현 (모의 응답으로만 테스트) |
| KRX OPEN API | — | 미구현 |

## 설치와 실행 (macOS / Linux, Python 3.11+)

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
| `PULSENTRA_DATA_DIR`, `PULSENTRA_DB_PATH`, `PULSENTRA_MODEL_DIR` | 저장 위치 (`MSL_*` 이름도 계속 인식) |
| `PULSENTRA_COMMISSION_BPS`, `PULSENTRA_SELL_TAX_BPS`, `PULSENTRA_SLIPPAGE_BPS` | 비용 기본값(가정값). 증권사 수수료와 해당 연도·시장의 세율을 확인해 설정 |
| `PULSENTRA_STALE_AFTER_BUSINESS_DAYS` | 일봉이 STALE로 바뀌는 영업일 수 (기본 2) |

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
  research/          리서치 로더·연구·보고서, manifest(재현 정보), validity(결과 상태), crosscheck(비교 로직)
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
