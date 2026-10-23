# 아키텍처, 백테스트 방법론, 전략 평가 기준 (v0.4)

## 데이터 흐름

```
scripts/collect_kr_daily.py ──► data/real/ (raw/*.csv, universe.csv, index_*.csv, manifest.jsonl)   [Git 제외, 읽기 전용으로 사용]
        │
        ├─► python -m app.audit ──────► data/audit/<run>/  findings · corrections · exclusions · candidates · symbol_summary · AUDIT_REPORT.md
        │         (app/data/audit.py: 로더와 같은 정리 + 원인 분류)
        │
        ├─► python -m app.crosscheck ─► data/crosscheck/<run>/  price_comparison · corporate_action_matches · crosscheck_status.json
        │         (app/research/crosscheck.py + app/providers/kis.py, dart.py; 키 필요)
        │
        └─► python -m app.research ───► reports/<run>/  REPORT.md · run_manifest.json · *.csv
                  (app/research/data.py → study.py → report.py; --audit, --crosscheck 선택)
```

## 모듈

| 경로 | 역할 |
|---|---|
| `app/data/schema.py` | 출처 유형(REAL / SYNTHETIC / TEST), 데이터 상태 |
| `app/data/quality.py` | 행·시계열 품질 검사 `check_frame` (로더와 감사가 함께 사용) |
| `app/data/audit.py` | 종목 단위 감사, 원인 분류, 지문 |
| `app/audit/` | 감사 입출력과 CLI, 출력 폴더 보호, 실행 비교 |
| `app/data/ingest.py` | CSV 적재(앱 DB), REAL과 합성 데이터의 혼합 거부 |
| `app/providers/http.py` | 재시도 상한·백오프·타임아웃 |
| `app/providers/kis.py`, `dart.py` | 읽기 전용 어댑터(경로 허용 목록, 스키마 검증) |
| `app/research/data.py` | 리서치 로더(`naver_raw`, `standard`) |
| `app/research/study.py` | 전략×청산 격자, 기간 분리, 워크포워드, 무작위 비교, 국면, 비용 민감도, 포트폴리오, ML 비교 |
| `app/research/manifest.py` | 재현 정보(git, 패키지, 경고, 산출물 해시) |
| `app/research/validity.py` | 처리 범위 표와 결과 상태 판정 |
| `app/research/crosscheck.py` | 출처 간 비교, 공시 매칭, 상장폐지 목록 비교 |
| `app/crosscheck/` | 실제 API 교차 검증 실행기 |
| `app/backtesting/` | 포트폴리오 엔진(`engine.py`), 일괄 신호 시뮬레이터(`batch.py`), 지표·보고서 |
| `app/strategies/`, `app/risk/`, `app/indicators.py` | 전략 플러그인, 비용·손절·수량, 인과적 지표 |
| `app/models/` | ML Lab(실험용, 채택 게이트) |
| `app/main.py`, `static/` | FastAPI 대시보드(MVP 호환) |

## 백테스트 방법론

- **정보 시점:** 신호는 t일 종가까지의 데이터로 계산하고, t+1일 시가에 슬리피지를 더해 체결합니다. 모든 지표의 인과성을 테스트합니다(미래 값을 바꿔도 과거 지표가 변하지 않아야 함).
- **청산:** 같은 봉에서 손절과 익절이 모두 닿으면 손절로 처리합니다(보수적). 갭은 시가에 체결합니다. 추적 손절과 이동평균 청산은 다음 봉부터 적용합니다.
- **비용:** 편도 수수료 1.5bp, 매도세 20bp, 편도 슬리피지 10bp입니다(왕복 약 43bp). 모두 가정값이고, `--commission-bps`, `--sell-tax-bps`, `--slippage-bps`로 바꿉니다. 바꾼 값은 manifest의 `changed_from_defaults`에 남습니다. 비용 전 결과와 슬리피지 3배 결과를 함께 냅니다.
- **유동성:** 20일 평균 추정 거래대금 10억 원 이상, 종가 1,000원 이상인 행만 신호를 냅니다. 시장 충격과 호가 단위는 모형화하지 않습니다.
- **거래정지·상장폐지:** 거래정지 표시 행은 제거하고, 포지션은 다음 실제 봉까지 보유합니다. 포트폴리오 엔진은 상장폐지 종목을 마지막 종가에 청산합니다. 신호 단위 통계는 데이터가 청산 전에 끝난 거래를 제외하고, 그 수를 보고합니다.
- **두 시뮬레이터:** 일괄 시뮬레이터와 단일 시뮬레이터가 신호마다 같은 결과를 내는지 테스트합니다.

## 전략 평가 기준

- **기간:** dev(2016–2021), validation(2022–2023), test(2024–)를 나눕니다. 각 기간 끝 35일 이내의 신호는 제외합니다(embargo). 청산 규칙은 dev에서만 고르고, validation과 test는 고른 규칙을 그대로 적용합니다.
- **기준 비교:** 같은 적격 행에서 같은 수를 뽑은 무작위 진입(1,000회), 단순 모멘텀, KOSPI·KOSDAQ 매수 후 보유와 비교합니다.
- **불확실성:** 월 단위 군집 부트스트랩 95% CI, 승률 Wilson 구간, 표본 수 경고를 함께 봅니다.
- **채택 조건(전략):** 비용 차감 후 validation과 test 모두에서 평균이 양수이고, CI가 0을 넘으며, 무작위 진입보다 나아야 합니다. dev 성과만으로는 채택하지 않습니다.
- **채택 조건(ML):** `docs/PULSENTRA_STATUS.md` §2의 게이트를 따릅니다. AUC만으로는 채택하지 않고, 거래비용 후 성과와 안정성을 함께 봅니다.
- **선택 편향:** 시험한 조합 수(`experiments_total`)를 기록합니다. 같은 test 기간을 선택 기준으로 다시 쓰지 않습니다.
- **결과 상태:** `validity.assess`가 INELIGIBLE이면 결과를 어떤 결정에도 쓰지 않습니다. PROVISIONAL이면 미검증 가정을 함께 보고합니다.
