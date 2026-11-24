# 설치, 테스트, 실험 재현 (v0.4)

## Python과 의존성

| 파일 | 용도 |
|---|---|
| `requirements.txt` | 앱·리서치 실행에 필요한 범위 지정 의존성 (FastAPI, pandas, numpy, scikit-learn, requests …) |
| `requirements-dev.txt` | 위 + pytest, httpx(테스트 클라이언트) |
| `requirements-research.txt` | 실데이터 수집기(`scripts/collect_kr_daily.py`)만 쓰는 finance-datareader |
| `requirements-lock.txt` | 위 세 파일을 설치했을 때 테스트가 통과한 **정확한 버전 한 벌**. 리서치 결과를 그대로 재현할 때 사용 |
| `pyproject.toml` | 도구 설정만 담음(pytest 마커, `requires-python >= 3.11`) |

검증한 조합 (2026-10-23, Linux x86_64):

| Python | 설치 방법 | 결과 |
|---|---|---|
| 3.13 | `requirements-lock.txt` (numpy 2.5.4, pandas 2.3.3, scikit-learn 1.9.1) | 전체 통과, DeprecationWarning을 오류로 바꿔도 통과 |
| 3.12 | `requirements-lock.txt` | 같음 |
| 3.11 | `requirements-dev.txt` 범위 (numpy 2.4.6) | 같음. numpy 2.5는 Python 3.12 이상만 지원하므로 잠금 파일은 3.11에서 설치되지 않음 |

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-lock.txt                         # 재현용(3.12+), 또는
pip install -r requirements-dev.txt -r requirements-research.txt   # 범위 설치
```

### 알려진 호환성 주의

pandas 2.3과 NumPy 2.5를 함께 쓰면, pandas 내부의 일부 경로가 "generic unit timedelta" DeprecationWarning을 냅니다. NumPy는 이 동작을 나중에 오류로 바꿀 예정입니다. v0.3 코드에서는 이 경고가 173건 나왔고, 원인과 처리는 다음과 같습니다.

- `pd.Timedelta(days=n)` 키워드 생성자 → `pd.Timedelta(n, unit="D")`로 교체
- `resample("ME"/"YE")` → 결과가 같은 `groupby(to_period)`로 교체(무작위 자산곡선 200개로 동일함을 확인)
- 시간 단위가 다른 datetime 열끼리 `concat` → 같은 단위를 유지

경고를 숨기지 않고 원인 코드를 고쳤습니다. CI는 DeprecationWarning을 오류로 처리하므로 새로 생기면 바로 드러납니다. 남은 경고 1건은 Starlette가 TestClient의 httpx 사용에 대해 내는 서드파티 경고입니다.

## 테스트

```bash
python -m pytest -q                                   # 기본(오프라인): 합성·TEST 데이터와 모의 API만 사용
python -m pytest -q -W error::DeprecationWarning      # CI와 같은 엄격 모드
PULSENTRA_RUN_INTEGRATION=1 python -m pytest -m integration   # 실제 API 스모크(키 필요, 선택)
```

| 범주 | 테스트 |
|---|---|
| 스키마·입력 검증 | `test_data_center.py`, `test_audit.py` (형식 오류, 주말, 식별자) |
| 가격·거래량 품질 | `test_audit.py`, `test_research.py` (반올림 보정, 거래량 수준 변화) |
| 기업행동 의심 | `test_audit.py` (분할·무상증자 유형), `test_crosscheck.py` (공시 매칭) |
| 기간 분리·미래 정보 누수 | `test_validation_and_ml.py`, `test_research_v04.py` (기간 경계, embargo) |
| 비용·세금 | `test_backtest_engine.py`, `test_research_v04.py` (비용 변경 기록) |
| 포지션·체결 | `test_backtest_engine.py`, `test_research.py` (일괄·단일 시뮬레이터 일치) |
| 성과 지표 | `test_backtest_engine.py`, `test_validation_and_ml.py` |
| 누락·중복 | `test_audit.py`, `test_data_center.py` |
| API 응답·실패 | `test_providers.py`, `test_api.py` |
| manifest·재현성 | `test_research_v04.py` (두 번 실행 시 결과 해시 동일), `test_audit.py` (지문) |
| 리포트 생성 | `test_research.py`, `test_audit.py` (CLI 출력) |
| 민감 정보·로컬 데이터 보호 | `test_repo_hygiene.py`, `test_providers.py` (오류 메시지에 키 없음), `test_research_v04.py` (manifest에 키 없음) |

CI(`.github/workflows/tests.yml`)는 잠금 의존성으로 Python 3.12·3.13에서 엄격 모드를, 범위 의존성으로 Python 3.11에서 기본 모드를 실행합니다. 비밀값은 쓰지 않습니다.

## 실험 재현

```bash
python scripts/collect_kr_daily.py --out data/real --start 2016-01-01       # 1) 수집(재실행 시 이어받기)
python -m app.audit --data data/real --out data/audit/run1                   # 2) 감사(읽기 전용)
python -m app.crosscheck --data data/real --audit data/audit/run1 --out data/crosscheck/run1   # 3) 교차 검증(키 필요)
python -m app.research --data data/real --format naver_raw --out reports/v0.4 \
    --audit data/audit/run1 --crosscheck data/crosscheck/run1/crosscheck_status.json        # 4) 연구
```

`--audit`과 `--crosscheck` 없이 실행하면 v0.3과 같은 방식으로 계산합니다. `--out`에는 새 폴더를 지정합니다. 비어 있지 않은 폴더는 `--overwrite` 없이는 거부합니다.

`python scripts/compare_baseline.py --data data/real --out reports/v0.4-compare`는 같은 데이터에 v0.3.0 코드와 현재 코드를 돌려 모든 결과 표를 비교합니다. 실제 데이터 단계별 절차는 [`REAL_DATA_RUNBOOK.md`](REAL_DATA_RUNBOOK.md)에 있습니다.

### `run_manifest.json` (manifest_version 2)

| 키 | 내용 |
|---|---|
| `git` | 전체 커밋 SHA, 브랜치, `describe`, 커밋되지 않은 코드 파일 목록, `reproducible_from_commit` |
| `environment` | Python, 플랫폼, 주요 패키지 버전 |
| `config`, `cost_assumptions` | 전략·청산 격자·기간·임계값, 비용 가정과 기본값에서 바꾼 항목(`changed_from_defaults`) |
| `random_seeds` | 무작위 표본·비교·포트폴리오·부트스트랩 시드 |
| `data_fingerprint`, `data_range`, `sample`, `provenance` | 사용한 데이터의 지문과 범위, 출처(키·원본 값은 넣지 않음) |
| `exclusions`, `audit` | 품질 검사로 제거한 행, 거래정지 행, 오염 창, 감사로 제외한 행 |
| `boundary_crossing_trades`, `incomplete_trades_dropped` | 기간 끝을 넘어 청산된 거래, 데이터가 끝나 통계에서 빠진 거래(상장폐지 종목 수 포함) |
| `experiments_total` | 시험한 전략×청산 조합 수(선택 편향 해석용) |
| `warnings` | 실행 중 발생한 경고(종류·메시지·위치·횟수) |
| `validity`, `data_treatments` | 결과 상태(INELIGIBLE / PROVISIONAL / VERIFIED)와 그 근거 |
| `outputs` | 모든 산출물의 경로·크기·SHA-256 |

v0.3 실행(`reports/v0.3/run_manifest.json`)은 이전 형식이고, 거기 적힌 커밋 `4918af9`는 이 저장소에 없습니다(`docs/RESEARCH_V0_4.md` §1).
