# 기존 MVP 분석 (Market Signal Lab → Pulsentra)

조사 대상: `market-signal-lab-mvp.zip` (27개 파일). 요청서에 적힌 파일명은 `Pulsentra-MVP.zip`이었지만, 연결된 폴더에서 찾은 MVP 압축 파일은 이 파일 하나였습니다.

## 1. 확인한 구조

| 영역 | MVP 구현 | 비고 |
|---|---|---|
| 기술 스택 | Python 3.11+, FastAPI, pandas, numpy, scikit-learn, SQLite, 단일 HTML 대시보드(바닐라 JS + canvas) | 유지 |
| API | `/api/health`, `/api/symbols`, `/api/signals`, `/api/history/{symbol}`, `/api/data/import`, `/api/demo/load`, `/api/sync/{symbol}`, `/api/quote/{symbol}`, `/api/backtest`, `/api/train`, `/api/predict/{symbol}`, `/api/model-status` | 전부 유지(계약 호환), 일부는 내부 구현 교체 |
| DB | `candles` 테이블 1개 (market, symbol, date, OHLCV, turnover, source) | 열 추가 방식으로 확장, 기존 DB 그대로 사용 가능 |
| 지표 | `indicators.py`: 수익률, 20일 거래량/거래대금 배수, ATR(14, 단순평균), CLV, 거래량 가중 CLV(20), 이평선, 변동성 | 유지 + 확장 |
| 스캐너 | `screen_candidates`: 거래량 3배·등락 ±3%·거래대금 1.5배 + 규칙 점수 | `app/scanner.py`로 이전, 함수는 호환 래퍼로 유지 |
| 백테스트 | `backtest.py`: 단일 종목, 다음 날 시가 진입, ATR 손절, R 익절, 같은 봉 손절 우선 | 단일 종목 빠른 백테스트로 유지. 포트폴리오 엔진을 새로 추가 |
| ML | `ml.py`: 삼중 장벽 이진 라벨, 80/20 시간순 홀드아웃 + 퍼지, HistGradientBoosting | 새 ML Lab으로 대체(파일은 참고용으로 남김) |
| KIS 어댑터 | 토큰 발급, 현재가(FHKST01010100), 기간별 시세(FHKST03010100) | 유지 + 수정주가 표시·호출 간격·세션 주입 |
| 테스트 | `tests/test_indicators.py` 4개 (unittest) | 그대로 통과, pytest로 함께 실행 |
| 데모 | 합성 3종목 420일, `source=synthetic_demo` | 12종목 + 합성 지수 + 종목 메타데이터로 확장 |

## 2. 분류

### 유지
- FastAPI + SQLite + 단일 HTML 구조 (별도 Next.js 프런트엔드는 추가하지 않음 — 현재 규모에서 빌드 체계를 늘릴 이유가 없음)
- 신호 t일 종가 → 진입 t+1 시가, 같은 봉 손절·익절 동시 도달 시 손절 우선, 갭 시가 체결 규칙
- 데모 데이터와 실데이터 분리 원칙, 실제 주문 기능 없음

### 개선
- CSV 적재: 한 행이 잘못되면 파일 전체를 거부하던 방식 → 행 단위 검증, 오류 행만 제외, 품질 이슈·수집 기록 저장
- ML: 검증 결과와 무관하게 예측을 제공하던 구조 → 워크포워드 + 채택 게이트 통과 모델만 예측 제공
- KIS: 수정주가 여부 기록, 호출 간격, 실패 기록
- 지표: 데이터 부족 시 미확인(NaN) 유지, ATR Wilder 버전·RSI·볼린저·상대강도 등 추가

### 새로 구현
데이터 상태(LIVE/DELAYED/HISTORICAL/DEMO/STALE/ERROR), 품질 검사, 종목 메타데이터(상장·폐지·업종·벤치마크), 전략 플러그인 10종, 포트폴리오 백테스트, 청산 규칙 비교, 워크포워드, 무작위 진입 기준, 국면별 성과, ML Lab, 모의투자, 알림, 틱→분봉 집계, OpenDART 어댑터, 탭형 대시보드.

## 3. 발견한 문제 (MVP 코드 기준)

| # | 문제 | 영향 | 조치 |
|---|---|---|---|
| 1 | 로드맵에 "Unit and API smoke tests" 완료로 표시됐지만 API 테스트는 없었음 (unittest 4개뿐) | 회귀를 잡지 못함 | API 통합 테스트 추가 (`tests/test_api.py`) |
| 2 | `/api/train`이 기준선보다 나은지와 무관하게 모델을 저장하고 `/api/predict`가 그대로 확률을 제공 | 검증되지 않은 확률이 화면에 노출 | 채택 게이트, 미채택 시 409 |
| 3 | 함수 이름은 `train_walk_forward`지만 실제로는 80/20 단일 홀드아웃 | 검증 강도를 과대 표시 | 실제 워크포워드(폴드별 퍼지·보정) 구현 |
| 4 | CSV 한 행 오류 시 파일 전체 거부, 중복 행 처리 없음, 품질 기록 없음 | 사용성·추적성 부족 | `app/data/quality.py`, `ingest.py` |
| 5 | 한국 종목코드 앞자리 0이 사라진 CSV(예: `5930`)를 별도 종목으로 저장 | 같은 종목이 둘로 나뉨 | 6자리 0 채움 |
| 6 | `list_symbols`가 `MAX(source)`만 보여 데모·실데이터 혼합을 숨김 | 혼합 데이터로 분석할 위험 | 출처 목록 전체 표시, 혼합 시 경고 |
| 7 | KIS 일봉 요청은 `FID_ORG_ADJ_PRC=0`(수정주가)인데 저장 시 수정 여부를 기록하지 않음, 호출 간 대기 없음 | 수정주가 여부 추적 불가, 유량 제한 오류 가능 | `adjusted=True` 저장, `KIS_MIN_INTERVAL_SECONDS` |
| 8 | 백테스트 자산곡선이 거래마다 전액 복리 — 수량·현금·동시 보유 미반영 | 성과·낙폭 왜곡 | 포트폴리오 엔진 추가 (MVP 함수는 빠른 점검용으로 유지) |
| 9 | 벤치마크·시장지수 개념 없음 | 상대강도·초과수익·국면 분석 불가 | `instruments.is_benchmark` |
| 10 | `@app.on_event("startup")` 사용 (FastAPI에서 폐기 예정) | 향후 호환성 | lifespan으로 교체 |

참고: MVP의 `clv_20`(거래량 가중 CLV 20일)은 정의상 CMF(20)와 같습니다. 이름만 `cmf_20`을 함께 제공합니다.

## 4. 기존 기능과 충돌한 지점

| 지점 | 변경 | 회귀 테스트 |
|---|---|---|
| `/api/train` → 새 ML Lab 경유 | 합성 데이터만 있으면 400, 통과 못 하면 미채택 | `test_ml_refuses_demo_and_predict_needs_adopted_model` |
| `/api/predict` | 채택 모델이 없으면 409 | 같은 테스트 |
| `/api/signals` | 응답 필드 유지 + `positives/negatives/unknown/data_status` 추가 | `test_dashboard_and_legacy_endpoints` |
| `/api/backtest` (MVP) | 그대로 | 같은 테스트 + MVP 원본 `test_indicators.py` |
| DB 스키마 | 열 추가만 | `test_migration_from_mvp_schema` |
