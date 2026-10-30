# Pulsentra 진행 현황 및 Claude Code 인수인계

작성일: 2026-10-30 (KST)

이 문서는 ChatGPT와 Claude Code가 같은 저장소 작업을 이어갈 수 있도록, 이번 작업의 변경 사항·검증 근거·미완료 항목·로컬 환경 이슈를 기록합니다. 이 문서는 진행 기록이며, 문서에 적힌 미실행 작업을 완료했다고 간주하지 않습니다.

## 1. 이번 작업 요약

### 완료된 GitHub 작업

- PR #14 `feat/pulsentra-dashboard-refresh` → `research/v0.4-data-audit` 병합 완료
  - 병합 커밋: `f098b7f23b484e2661e62d08d9a9d338a4fd55ca`
  - PR 최종 head: `867e66338598bfc96e8bc76d3edb78f47d7a3c7e`
  - 최종 head 기준 GitHub Actions 3개 작업 성공
  - 대시보드 CSS 중심의 시각 개선, 기존 DOM ID/API/탭/상호작용 계약 유지
  - 종목 마스터 스냅샷 스크립트와 정규화·출처 검증 테스트 추가
  - 데이터 소스 정책 및 제품 개선 로드맵 추가
- PR #15는 PR #14에 동일한 안정 정렬 수정이 반영되어 중복 작업이므로 병합 없이 종료
- 현재 통합 대상 브랜치 `feat/overfitting-pbo-dsr`는 로컬 확인 당시 `935a755f1857dbc163a5022dcf3510dc9ac0776a`였음
- GitHub 비교 결과 `research/v0.4-data-audit`는 해당 브랜치보다 9개 커밋 앞서 있고, 뒤처진 커밋은 없음
- 이 인수인계 문서는 해당 9개 커밋을 포함한 `research/v0.4-data-audit`에서 별도 문서 브랜치로 작성 중

### PR #14 주요 파일

| 파일 | 변경 |
|---|---|
| `static/index.html` | 기존 기능을 보존한 대시보드 스타일 개선 |
| `scripts/refresh_instrument_master.py` | FinanceDataReader KRX 종목 목록을 정규화해 CSV 및 manifest 저장 |
| `tests/test_instrument_master.py` | 정규화, 시장 구분, 중복·빈 데이터 거부, manifest 해시 테스트 |
| `requirements.txt` | FinanceDataReader 의존성 범위 추가 |
| `docs/PRODUCT_REFRESH_PLAN.md` | 데이터 소스 정책, 대시보드 방향, 단계별 로드맵 |

## 2. 검증 상태

### GitHub Actions

PR #14 최종 head `867e66338598bfc96e8bc76d3edb78f47d7a3c7e` 기준:

- Python 3.11 dependency-range job: 성공
- Python 3.12 locked-dependency job: 성공
- Python 3.13 locked-dependency job: 성공

이는 GitHub Actions 실행 결과이며, 사용자 Mac 로컬 테스트 결과가 아닙니다.

### 아직 확인하지 못한 항목

- 대시보드 브라우저 렌더링 및 데스크톱/모바일 스크린샷 검토
- 종목 마스터 스냅샷 명령 실제 실행
- OpenDART/ECOS 실 API 인증 호출
- 실시간 시세 공급원 연결 및 운영 적합성 확인

## 3. 사용자 Mac 개발환경 이슈

사용자가 실행한 진단 결과:

- 작업 디렉터리: `~/Documents/KREWX/Pulsentra`
- 브랜치: `feat/overfitting-pbo-dsr`
- 당시 HEAD: `935a755`
- Git 상태: clean
- origin: `https://github.com/yeobbro/Pulsentra.git`
- 저장소 크기: 약 963 MB
- 활성화된 것으로 보이는 `.venv`의 `VIRTUAL_ENV`가 이전 프로젝트 경로 `~/Documents/GADO_LABS/제품카테고리/Pulsentra/pulsentra-v0.3/.venv`를 가리킴
- `which python`은 `/opt/anaconda3/bin/python`, Python 3.14.6을 반환
- 해당 환경에서 FastAPI import 실패
- 현재 브랜치에 `scripts/refresh_instrument_master.py`가 없는 것은 PR #14가 아직 현재 작업 브랜치에 통합되지 않았기 때문

기존 가상환경과 프로젝트 데이터는 삭제하지 않습니다. 별도 Python 3.12 환경 `.venv312`를 만들어 기존 환경을 보존하는 방향을 안내했습니다.

초기 환경 복구에 사용한 설치 절차(실행 완료):

```bash
cd ~/Documents/KREWX/Pulsentra

deactivate 2>/dev/null || true
conda deactivate 2>/dev/null || true

if ! command -v python3.12 >/dev/null 2>&1; then
    brew install python@3.12
fi

"$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv312
source .venv312/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt

python --version
python -c "import pandas, fastapi, FinanceDataReader; print('Pulsentra environment OK')"
```

**2026-10-30 사용자 실행 결과: 성공.** Homebrew로 Python 3.12.16 설치, 저장소 안에 별도 `.venv312` 생성, `requirements-lock.txt` 전체 설치 완료. 마지막 검증에서 `Python 3.12.16` 및 `Pulsentra environment OK` 출력 확인. 따라서 새 환경의 pandas, FastAPI, FinanceDataReader import가 성공했습니다. 기존 `.venv`/Conda 문제는 해결된 것으로 보이며, 기존 환경은 삭제하지 않고 보존합니다. 명령 블록의 주석 줄이 대화형 zsh에서 개별 실행되어 `zsh: command not found: #`가 출력됐지만, 설치와 최종 import 검증은 정상 완료했습니다.

**2026-10-30 로컬 전체 테스트 결과: 성공.** 사용자가 Mac의 `.venv312` (Python 3.12.16)에서 `python -m pytest -q -W error::DeprecationWarning` 실행. 결과: **498 passed, 2 skipped, 1 warning**, 82.56초. 테스트 실패는 없습니다. 경고는 `fastapi/testclient.py`에서 발생한 `StarletteDeprecationWarning`으로, Starlette의 `TestClient`와 `httpx` 조합 사용이 deprecated 되었다는 내용입니다. 테스트는 통과했으므로 즉시 의존성을 변경하지 않고 별도 호환성 정리 항목으로 추적합니다. 이 결과는 사용자 제공 로컬 실행 로그에 근거하며 GitHub Actions 결과와 구분합니다.

## 4. 데이터 소스 및 API 진행 원칙

- 기존 Naver 기반 OHLCV 아카이브는 로컬 연구용으로 보존합니다. 출처 약관·robots·상업적 재배포 권한이 확인되지 않아 추가 대량 수집이나 공개 서비스 제공은 보류합니다.
- FinanceDataReader KRX 상장 목록은 제3자 캐시 기반 참고 스냅샷입니다. 공식 실시간 종목 마스터나 재배포 허가 데이터로 표현하지 않습니다.
- data.go.kr 금융위원회 상장종목정보는 공식 종목 마스터 후보입니다. 별도 서비스 신청·인증키 및 이용조건 검토가 필요하며 증권 계좌는 필요하지 않습니다.
- OpenDART는 API 키를 발급받으면 공시·재무 데이터 연결 가능하며 증권 계좌는 필요하지 않습니다. 현재 어댑터는 있으나 실제 키를 이용한 호출 검증은 미완료입니다.
- ECOS 거시지표는 별도 ECOS 키·신청이 필요합니다. 아직 연동 구현·실호출 검증은 미완료입니다.
- KRX Open API는 저장소 문서상 제3자 제공 제한이 있어 공개 대시보드 데이터 공급원으로 사용하지 않습니다. 연구용 로컬 교차검증 범위로 제한합니다.
- KIS Open API는 사용자 계좌 및 APP KEY/APP SECRET이 필요합니다. 사용자는 현재 키를 신청하지 않았다고 확인했습니다. 문서 파일은 API 문서이며 인증정보가 아닙니다.
- 주문 기능은 범위 밖입니다. 향후 KIS 연결도 우선 읽기 전용 현재가·일봉부터 검증합니다.
- 뉴스는 공식 발행처 RSS 또는 라이선스가 확인된 피드만 사용하고, 무단 본문 스크래핑·재배포는 하지 않습니다.

## 5. 제품·연구 안전 기준

- Pulsentra는 종목 추천 또는 수익 보장 서비스로 설명하지 않습니다.
- v0.3 실데이터 연구에서는 거래비용 차감 후 validation/test에서 일관된 우위를 입증한 전략이 없었습니다.
- PBO/DSR 및 견고성 평가를 포함한 연구 결과도 사전 정의된 게이트와 데이터 적격성 검토를 통과해야 합니다.
- DEMO / TEST / REAL / 제한된 연구용 데이터를 섞지 않습니다.
- 시세 카드에는 공급원, 데이터 유형, 관측 시각, 신선도와 제한 사항을 명시합니다.
- 클라우드 배포는 현재 중단 상태입니다. 명시적인 별도 승인 전에는 Render/Vercel 배포, 환경변수 변경, DB 마이그레이션 등 운영 인프라 변경을 하지 않습니다.

## 6. 다음 작업 순서

1. **완료:** 사용자 Mac의 새 `.venv312` 설치 및 핵심 패키지 import 검증
2. 로컬 작업 트리와 Claude Code의 현재 작업 상태를 확인한 뒤, `origin/research/v0.4-data-audit`의 9개 커밋을 `feat/overfitting-pbo-dsr`에 안전하게 통합
   - 작업 트리가 clean인지 확인
   - Claude가 같은 브랜치에서 미커밋 작업 중이면 먼저 커밋/백업 또는 별도 브랜치로 보존
   - reset, force push, 기존 데이터 삭제 금지
3. **완료:** Mac Python 3.12.16 `.venv312`에서 전체 테스트 498 passed / 2 skipped / 1 warning (82.56초). Starlette TestClient/httpx deprecation 경고는 후속 의존성 정리 항목으로 기록
4. 로컬 FastAPI 대시보드를 실행해 데스크톱·모바일 UI 확인
5. 종목 마스터 스냅샷을 실행하되 생성 파일과 데이터 출처 manifest 확인
6. OpenDART·ECOS 등 자격증명이 준비된 공급원부터 개별적으로 통합
7. 각 작업은 작은 PR로 분리하고, PR 본문에 변경 이유·파일·검증 명령/결과·미검증 사항·데이터/라이선스 위험을 기록

## 7. Claude Code 작업 규칙

- 이 문서를 작업 시작 시 읽고, 이후 진행 상황을 같은 문서에 업데이트합니다.
- 다른 에이전트가 수정 중인 파일은 덮어쓰지 않습니다. 수정 전 `git status`, `git diff`, `git log`를 확인합니다.
- 병렬 작업 시 파일/모듈 소유 범위를 명확히 나누고, 동일 파일 동시 수정은 피합니다.
- 테스트를 실제 실행하지 않았다면 통과했다고 쓰지 않습니다.
- CI 통과와 로컬 검증을 구분해 기록합니다.
- 실 API 호출 여부와 키 보유 여부를 구분합니다. 키 값은 절대 커밋하지 않습니다.
- 모든 변경은 브랜치와 PR을 통해 리뷰 가능하게 남깁니다.
- 클라우드 배포 및 운영 데이터 변경은 별도 승인 전까지 하지 않습니다.

## 8. 참조 링크

- PR #14 (병합): https://github.com/yeobbro/Pulsentra/pull/14
- PR #15 (중복으로 종료): https://github.com/yeobbro/Pulsentra/pull/15
- 제품 개선·데이터 소스 계획: `docs/PRODUCT_REFRESH_PLAN.md`
- 설치·테스트·재현: `docs/SETUP.md`
- 현재 프로젝트 개요: `README.md`
