# Render 배포 가이드 — 개인용 Pulsentra 웹 (단일 사용자)

이 문서는 저장소 루트의 [`render.yaml`](../render.yaml)로 Pulsentra를 Render에 올리는 방법입니다. 작성 시점(2026-10-27)에는 **실제 서비스를 만들거나 배포하지 않았습니다.** 아래 단계는 사용자가 Render Dashboard에서 직접 진행합니다.

> Pulsentra는 종목 추천 서비스가 아닙니다. 현재까지 시장 대비 지속 가능한 우위(Alpha)를 입증한 전략은 **없고**, 연구 데이터 전체의 출처 검증도 끝나지 않았습니다(KOSPI 4개 거래일만 대조, [`DATA_ELIGIBILITY.md`](DATA_ELIGIBILITY.md)). 웹 배포는 화면에서 도구를 쓰기 위한 것이며, 결과의 신뢰도를 바꾸지 않습니다.

## 1. 무엇이 배포되는가

| 항목 | 내용 |
|---|---|
| 서비스 | 웹 서비스 `pulsentra-web`, Python 런타임, 리전 `singapore`, 인스턴스 `0.5c-512mb`(유료) |
| 배포 브랜치 | `research/v0.4-data-audit` |
| 빌드 / 시작 | `pip install -r requirements.txt` / `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| 상태 확인 | `GET /healthz` → `{"status":"ok"}` (인증 없이 열린 유일한 경로, 다른 정보 없음) |
| 자동 배포 | `autoDeployTrigger: checksPass` — 브랜치의 새 커밋은 GitHub 검사(`.github/workflows/tests.yml`)가 통과한 뒤에만 배포 |
| 영구 디스크 | `pulsentra-data`, 마운트 `/var/data`, 1 GB(나중에 늘릴 수 있고 줄일 수 없음) |
| 데이터베이스 | SQLite `/var/data/market_signal_lab.sqlite3`, 모델 파일 `/var/data/models`, `/var/data/model.joblib`, `/var/data/model_meta.json` |
| 처음 데이터 | 데이터베이스가 비어 있으면 첫 시작 때 **합성 DEMO 데이터**를 한 번 넣음(`PULSENTRA_AUTOLOAD_DEMO=true`) |

배포되는 것은 Git에 있는 파일뿐입니다. `.gitignore`에 있는 로컬 데이터(`data/real/`, `data/audit/`, `data/krxcheck/`, `data/crosscheck/`, 로컬 SQLite), `.env`와 API 키, 연구 산출물(`data/ledger/`, `data/factorlab/` 등)은 **원격에 올라가지 않습니다.** KIS·OpenDART·KRX 키 없이도 접속과 DEMO 기능은 동작합니다.

## 2. 무료 인스턴스와 유료 영구 디스크

| | Free | 유료 인스턴스 + 영구 디스크(이 설정) |
|---|---|---|
| 디스크 | 붙일 수 없음. 파일 시스템이 임시라 재배포·재시작·휴면 때 SQLite가 사라짐 | `/var/data` 아래 파일만 재배포·재시작 후에도 남음 |
| 휴면 | 15분 동안 요청이 없으면 멈춤 | 멈추지 않음 |
| 비용 | 없음 | 인스턴스 요금 + 디스크 GB당 요금(현재 가격은 Render Pricing 페이지에서 확인) |

- 디스크가 붙은 서비스는 **무중단 배포가 되지 않습니다.** 배포 때 이전 인스턴스를 멈춘 뒤 새 인스턴스를 띄우므로 몇 초 동안 접속이 끊깁니다.
- 디스크가 붙은 서비스는 인스턴스를 여러 개로 늘릴 수 없습니다. 디스크는 빌드 단계에서는 보이지 않고 실행 중에만 보입니다.
- `/var/data` 밖에 쓴 파일은 재배포 때 사라집니다.
- 디스크 최소 용량: Render 문서에 명시된 최소값을 찾지 못했습니다. `sizeGB: 1`로 두었으며, Dashboard가 더 큰 최소값을 요구하면 그 값으로 바꿔야 합니다.

## 3. Render에 연결하기 (사용자가 직접)

1. Render Dashboard에서 **New → Blueprint**를 고르고 GitHub 저장소 `yeobbro/Pulsentra`를 연결합니다(Render GitHub 앱에 저장소 접근 권한 부여).
2. Blueprint 파일로 `render.yaml`을 고르고, 브랜치는 `research/v0.4-data-audit`가 GitHub에 있는지 확인합니다. `render.yaml`은 현재 기준 브랜치 `research/v0.4-data-audit`에 포함되어 있습니다. Blueprint 생성 전 이 브랜치가 선택되어 있는지 확인하세요.
3. 생성 화면에서 Render가 `sync: false` 값 두 개를 묻습니다.
   - `PULSENTRA_ACCESS_USERNAME`: 로그인 이름
   - `PULSENTRA_ACCESS_PASSWORD`: 긴 무작위 비밀번호(예: 비밀번호 관리자에서 24자 이상 생성)
   - 이 값은 `render.yaml`, Git, 문서, 채팅에 적지 않습니다. 나중에 바꿀 때는 서비스의 **Environment** 화면에서 바꾸고 다시 배포합니다(`sync: false`는 최초 생성 때만 입력을 묻습니다).
4. 플랜(`0.5c-512mb`)과 디스크(1 GB)가 유료임을 확인하고 생성합니다.
5. 첫 배포 로그에서 빌드와 시작이 끝나고 상태 확인(`/healthz`)이 통과하는지 봅니다.
   - 시작 로그에 `AccessConfigError`가 보이면 3번의 두 값이 비어 있는 것입니다. 이 경우 앱은 일부러 시작하지 않습니다.
6. Python 버전: `render.yaml`은 버전을 고정하지 않으므로 Render의 기본 Python 버전이 쓰입니다. CI는 3.12·3.13, Mac 로컬은 3.14.6입니다. 고정하려면 Render 문서의 Python 버전 지정 방법을 확인해 `PYTHON_VERSION` 환경 변수를 추가하세요(이번 작업에서는 추가하지 않음).

## 4. 실제 데이터 API 키 설정

Render Dashboard → 서비스 → **Environment**에서 아래 키를 추가한 뒤 재배포합니다. 값은 Git·코드·채팅에 절대 넣지 않습니다.

| 환경변수 | 용도 | 서비스 동작 |
|---|---|---|
| `KIS_APP_KEY` | 한국투자증권 앱 키 | 국내 현재가 REST 조회 및 일봉 동기화 |
| `KIS_APP_SECRET` | 한국투자증권 앱 시크릿 | KIS 인증 |
| `KIS_PAPER` | `false` 실전 시세 / `true` 모의 도메인 | 주문 기능은 없음 |
| `DART_API_KEY` | OpenDART 인증키 | 기업 공시 조회·위험 플래그 |
| `KRX_API_KEY` | KRX Open API 인증키 | 연구용 로컬 교차 검증만. 앱 DB 적재 및 외부 화면/API 시세 제공은 하지 않음 |

**주의**
- KIS는 현재가 REST 스냅샷과 일봉 조회를 지원합니다. WebSocket 실시간 체결 스트림은 아직 구현되지 않았습니다.
- OpenDART 공시는 `POST /api/dart/sync`로 종목 코드와 조회 기간을 지정해 동기화합니다.
- KRX 키는 발급만으로 모든 서비스 이용 권한이 생기는 것이 아닙니다. 각 서비스 승인 상태와 약관을 확인해야 하며, 현재 코드의 라이선스 차단을 우회하지 않습니다.
- KIS 키가 아직 없다면 KIS 환경변수는 비워두고, OpenDART 연동부터 검증할 수 있습니다.
- 키 등록 후 `GET /api/provider-status`에서 설정 여부만 확인할 수 있습니다. 응답에는 키 값이 포함되지 않습니다.

## 5. 접속과 기능 확인

| 순서 | 확인할 것 |
|---|---|
| 접속 | `https://<서비스 이름>.onrender.com/` → 브라우저 로그인 창 → 3번의 이름·비밀번호 |
| 로그인 실패 | 틀린 값이면 다시 로그인 창(401). `/healthz` 외의 모든 경로(화면, `/static`, `/api`, `/docs`, `/redoc`, `/openapi.json`)가 같음 |
| DEMO | 대시보드 상단에 DEMO 상태와 "합성 데이터" 경고가 보여야 함. DEMO는 실제 시세가 아님 |
| CSV 업로드 | 데이터 센터 → "CSV 일봉 업로드"(date, open, high, low, close, volume). 업로드한 실제 데이터는 HISTORICAL로 표시 |
| 백테스트 | Strategy Lab에서 DEMO 시장으로 실행. 결과에 합성 데이터 경고가 붙음 |
| 모의투자 | 모의투자 탭에서 계좌를 만들고 실행. 실제 주문 기능은 없음 |
| 재시작 | Dashboard에서 **Manual Deploy** 또는 재시작 후 업로드한 데이터와 저장한 스캔이 남아 있는지 확인. DEMO는 다시 들어가지 않음 |

DEMO 자동 적재 규칙:
- 데이터베이스에 종목(instruments)도 일봉(candles)도 없을 때만 한 번 넣습니다. 이미 데이터가 있으면 아무것도 지우거나 덮어쓰지 않습니다.
- 넣은 뒤 `/var/data/market_signal_lab.sqlite3.demo-autoloaded` 표시 파일을 남깁니다. 그래서 DEMO를 화면에서 일부러 지운 뒤 재시작해도 다시 넣지 않습니다. 다시 넣으려면 화면의 DEMO 불러오기를 씁니다.

## 6. 보안 범위와 한계

- **단일 계정입니다.** 사용자별 데이터 분리는 없습니다. 이름·비밀번호를 아는 사람은 모두 같은 데이터베이스를 보고 바꿉니다.
- 인증은 HTTP Basic입니다(`app/security.py`). Render가 HTTPS를 제공하므로 비밀번호는 암호화된 연결로만 보내집니다. HTTP 주소로 접속하지 마세요.
- 비밀번호 비교는 상수 시간 비교(`hmac.compare_digest`)이며, 앱은 인증정보를 로그나 응답에 쓰지 않습니다.
- 운영 모드에서 POST·PUT·PATCH·DELETE는 같은 출처에서 온 요청만 받습니다(`Origin`, 없으면 `Sec-Fetch-Site`). 다른 사이트가 로그인된 브라우저를 이용해 상태를 바꾸는 요청(CSRF)을 막기 위한 것입니다. 브라우저가 아닌 도구(curl 등)는 이 헤더가 없으므로 인증만 확인합니다.
- 응답에 `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`을 붙입니다.
- 로그아웃 버튼은 없습니다. Basic 인증은 브라우저가 창을 닫을 때까지 자격 증명을 기억합니다. 공용 컴퓨터에서 쓰지 마세요.
- 로그인 실패 횟수 제한(무차별 대입 방지)은 구현하지 않았습니다. 긴 무작위 비밀번호를 쓰세요.
- `/docs`·`/redoc` 화면은 인증 뒤에도 Swagger·ReDoc 자산을 외부 CDN에서 불러옵니다.
- KRX 데이터는 이용약관상 제3자 제공이 금지돼 있어, 원격 서비스에 올리거나 다른 사람에게 보여 주지 마세요([`DATA_SOURCES.md`](DATA_SOURCES.md) "이용 조건").

## 7. SQLite 백업과 복구

Render는 영구 디스크의 스냅샷을 하루 한 번 만들고 7일 보관합니다. 다만 Render 문서는 데이터베이스가 들어 있는 디스크는 스냅샷 복원 대신 데이터베이스 자체 도구로 백업하라고 권합니다. 스냅샷 복원은 디스크 전체를 그 시점으로 되돌리고, 이후 변경은 모두 사라집니다.

권장 백업(서비스의 **Shell** 탭에서 — 유료 서비스의 Shell 제공 여부는 Render 문서로 확인, 서비스가 실행 중일 때도 일관된 사본을 만드는 SQLite 백업 API 사용):

```bash
python - <<'PY'
import sqlite3, datetime
src = sqlite3.connect("/var/data/market_signal_lab.sqlite3")
dst = sqlite3.connect(f"/var/data/backup-{datetime.date.today():%Y%m%d}.sqlite3")
src.backup(dst); dst.close(); src.close()
PY
ls -l /var/data
```

- 백업 파일을 디스크 밖(본인 컴퓨터)으로 옮기는 방법(SSH·scp 등)은 Render 문서에서 지원 여부를 확인하세요. 이 방법은 이번 작업에서 실행해 보지 않았습니다.
- 백업 파일도 디스크 용량을 씁니다. 1 GB 디스크라면 오래된 백업을 지우세요.

복구:
1. 복구할 백업 파일을 `/var/data`에 둡니다.
2. Shell에서 현재 파일을 따로 보관한 뒤 백업으로 바꿉니다: `cp /var/data/market_signal_lab.sqlite3 /var/data/before-restore.sqlite3 && cp /var/data/backup-YYYYMMDD.sqlite3 /var/data/market_signal_lab.sqlite3`
3. 서비스를 재시작합니다(실행 중 연결이 바뀐 파일을 계속 쓰지 않도록).

## 8. 이번 준비 작업에서 확인한 것과 하지 않은 것

| 항목 | 상태 |
|---|---|
| 인증·`/healthz`·출처 검사·DEMO 자동 적재·영구 경로 회귀 테스트 | 자동 테스트(`tests/test_deploy_access.py`) |
| 실제 uvicorn 서버(Python 3.14.6 + 잠금 의존성)에서 401/200, DEMO 적재, 재시작 후 데이터 유지, 비밀번호 없는 운영 시작 실패 | 로컬 확인(Render 아님) |
| 실제 브라우저(Chromium)에서 로그인 후 화면·같은 출처 POST(스캔 저장, DEMO 백테스트, CSV 업로드) 정상, 다른 출처 페이지의 POST·폼 제출 403 | 로컬 확인 |
| Render 서비스 생성·배포, Render 프록시 뒤 HTTPS, Render Shell 백업, 디스크 최소 용량 | **하지 않음** |
