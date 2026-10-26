# Pulsentra Alpha Research Lab v1 — 진행 보고서

- 작성: 2026-10-25 · 브랜치 `feat/alpha-research-lab-v1` (기준 `research/v0.4-data-audit` `32fe29f`)
- 이 브랜치의 변경은 **커밋하지 않은 로컬 변경**입니다(커밋·푸시·PR 없음). 아래 "코드" 경로는 작업 트리 기준입니다.
- 상태 표기: ✅ 완료 · 🟡 부분 · ⬜ 미완료 · ❔ 검증 불가
- 현재까지 시장 대비 지속 가능한 우위(Alpha)를 입증한 전략·요인은 **없습니다.** 이 문서의 어떤 결과도 투자 권유가 아닙니다.

## 1. 목적과 구조

목표 사용자는 퀀트·시스템 트레이더입니다. 핵심은 "Alpha를 찾는 것"보다 **찾았다고 착각하지 않게 하는 연구 절차**입니다.

```
사전 등록 ──► python -m app.ledger register        data/ledger/ledger.jsonl (append-only, 해시 체인)
   │
   ├─► python -m app.factorlab run --ledger-id ID   data/factorlab/<run>/  factor_*.csv · run_manifest.json · REPORT.md
   │        (실행 시작·성공/실패·manifest·산출물 해시를 원장에 자동 기록)
   │
   ├─► python -m app.robustness evaluate --ledger-id ID   data/robustness/<run>/  gate.json · GATE.md
   │        (PASS / WATCH / FAIL / INSUFFICIENT_DATA, gate.json을 원장에 연결)
   │
   └─► python -m app.ledger decide ID accept|hold|reject --reason ...   (사람이 결정, 이유 필수)
```

입력 데이터(`data/real/`)와 감사 결과(`data/audit/`)는 읽기만 합니다. 새 런타임 폴더 `data/ledger/`, `data/factorlab/`, `data/robustness/`는 `.gitignore`에 있습니다.

## 2. Phase 0 — 연구 무결성·문서 정합 ✅

| 항목 | 상태 | 근거 |
|---|---|---|
| 9개 문서의 구현·실행·미검증 범위 일치 | ✅ | 커밋 `b6b7c59`, PR #4 병합 `32fe29f` |
| v0.3.0 코드(`a57dcc1`)와 v0.4 코드(`2edfc8b`) 실제 데이터 결과 동일 | ✅ | `reports/v0.4-compare/` (Git 제외), `same_results: true` |
| 원래 v0.3 실행 커밋 `4918af9`와의 동일성 | ❔ | 커밋이 저장소에 없음 |

## 3. 기존 기능 (이 작업 전, 코드 경로)

| 기능 | 상태 | 코드 · 테스트 |
|---|---|---|
| 리서치 파이프라인(전략×청산 격자, 기간 분리·엠바고, 워크포워드, 비용 민감도, ML 비교) | ✅ | `app/research/study.py` · `tests/test_research.py`, `tests/test_research_v04.py` |
| 재현 정보 manifest v2(git, 환경, 데이터 지문, 산출물 해시) | ✅ | `app/research/manifest.py` |
| 결과 상태 INELIGIBLE / PROVISIONAL / VERIFIED | ✅ | `app/research/validity.py` |
| 종목 단위 데이터 감사 | ✅ | `app/data/audit.py`, `app/audit/` · `tests/test_audit.py` |
| KRX 대조(연구용 로컬), 앱 저장소 적재 차단 | ✅ | `app/krxcheck/`, `app/data/schema.py` · `tests/test_krx_fullmarket.py`, `tests/test_krx_history.py` |
| OpenDART 원·정정 공시 묶음 매칭 | ✅ | `app/research/crosscheck.py` · `tests/test_crosscheck.py` |
| KIS 교차 검증 | ⬜ | 인증정보 없음 |

## 4. 연구 기준선과 결론 (변경 없음)

- v0.3: 실제 일봉 2016~2026, 비용 차감 후 validation·test 480개 조합 중 평균 순수익 양수 **0개**, ML 필터 15개 비교 모두 불채택 (`reports/v0.3/REPORT.md`).
- v0.4 재실행 비교: 같은 결론(`reports/v0.4-compare/`). 기준선 REPORT와 v0.3 REPORT의 차이는 ML 표 2행(AUC 0.491→0.490, 표본 4,756→4,757, CI −0.15%→−0.16%)과 코드 표기·실행 시간.
- 감사(`data/audit/full_20261024T060804Z_a`·`_b`): 원시 6,000,649행, 사용 5,799,758행, 2,824종목, ok 597 / review 2,225 / exclude 2, 기업행동 후보 8,073건. 두 실행 지문 일치.

## 5. 감사·교차 검증 결과와 미해결 데이터 품질 문제

| 문제 | 상태 |
|---|---|
| review 2,225종목(79%)의 발견 사항 검토 | ⬜ |
| 기업행동 후보 8,073건 중 공시 대조 | 🟡 1건(삼성전자 50:1 분할) |
| 2026-10-22·23 KRX·네이버 일봉 차이 원인(10-23 정확 일치 51/803) | ⬜ 원인 미확정, 네이버 재수집 보류 |
| 배당·총수익, 연도별 세율, 상장폐지 목록 참조 대조 | ⬜ |
| `validity.py`의 `corporate_action_disclosure_match` 설명 문구("never run on live data")가 1건 실행 이후 낡음 | ⬜ 후속 수정 대상(코드 미변경) |

## 6. 라이선스 불확실성

- KRX OPEN API: 비상업적 이용, 제3자 제공 금지(약관 제6조②, 제11조②). 앱 저장소 적재는 코드로 차단. 외부 제공·상용화 미활성.
- 네이버 일봉(연구 주 데이터): `robots.txt` 전면 Disallow, 이용약관 미확인 → 상용 사용 불가로 취급.
- 자세한 내용: `docs/DATA_SOURCES.md` "이용 조건". 이 작업은 라이선스 상태를 바꾸지 않았습니다.

## 7. 재현성 한계

- 테스트·스모크 실행은 Mac 사용자 폴더를 마운트한 Linux VM(Python 3.10, pandas 2.3.3, numpy 2.2.6)에서 했습니다. 잠금 파일은 numpy 2.5.4, CI는 Python 3.12·3.13입니다.
- 이 브랜치의 코드는 커밋 전이므로 스모크 실행 manifest는 `reproducible_from_commit: false`입니다.
- VM에는 GitHub 인증정보가 없어 원격 상태는 마지막 fetch 기준입니다.

## 8. 이번 작업에서 구현한 것

### Phase 1 — Experiment Ledger ✅ (MVP)

| 항목 | 상태 | 근거 |
|---|---|---|
| 저장 형식: JSONL + 해시 체인(`seq`, `prev_hash`, `hash`), 잠금(`fcntl.flock`)·한 줄 append·`fsync` | ✅ | `app/research/ledger.py` |
| 사전 등록 필수 항목 12개 검증, 미입력 템플릿 값 거부, 중복 ID 거부 | ✅ | `REQUIRED_FIELDS`, `validate_spec` · `test_required_fields`(12개), `test_duplicate_id_refused_and_nothing_overwritten` |
| 수정: 새 버전 + 이유 필수 + 이전 명세 보존 + `after_results` 표시, 실행 중 수정 거부 | ✅ | `Ledger.amend` · `test_amendment_*` |
| 실행 기록: 한 실험당 열린 실행 1개, 실행 ID 재사용 거부, 실패도 보존 | ✅ | `start_run`/`finish_run` · `test_runs_order_and_failures_kept` |
| manifest 연결: 실제 있는 필드만, SHA-256 기록 | ✅ | `manifest_facts` · `test_manifest_link_records_only_real_fields` |
| 결정: accept/hold/reject + 이유 필수, accept는 성공 실행과 manifest가 있어야 함 | ✅ | `_apply`, `decide` · `test_decisions` |
| 무결성 검사: 편집·삭제·순서 변경·깨진 줄·중단된 마지막 줄·재해시한 위조 이벤트 검출, 손상 시 추가 기록 거부 | ✅ | `verify` · `test_verify_*` |
| 삭제·초기화 기능 없음 | ✅ | `test_no_delete_or_reset_api` |
| CLI(template·register·list·show·amend·run-start·run-finish·link-manifest·link-artifact·decide·verify [--files]) | ✅ | `app/ledger/__main__.py` · `test_cli_flow`, `test_cli_help_runs` |
| 런타임 데이터 Git 제외 | ✅ | `.gitignore`, `tests/test_repo_hygiene.py` |

명세 항목 지원 현황 — 필수: title, hypothesis, rationale, family, universe, data, periods(dev/validation/test), primary_metrics, decision_criteria, cost_assumptions, search_space, planned_experiments.
선택: data_fingerprint(게이트가 manifest와 대조), signal_rule, entry_rule, exit_rule, benchmark, gate(임계값), parent_id, hypothesis_family_id, notes.
자동: 등록 시각, spec_hash, 버전. 실제 실험 수는 manifest의 `experiments_total`에서 읽습니다.
**미지원:** 명세의 신호·진입·청산 규칙이 실제 코드와 같은지 자동 대조(텍스트로만 기록), 명세 필드의 의미 검증(예: 비용 문구), 여러 사람의 서명.

JSONL을 고른 이유: 기록이 적고 드물며, 사람이 읽고 diff할 수 있어야 하고, 시장 데이터를 담는 앱 SQLite(`data/*.sqlite3`)와 분리해야 하기 때문입니다.
해시 체인은 실수와 단순 편집을 잡지만, 파일 전체를 다시 계산해 쓰는 의도적 위조는 막지 못합니다(외부 서명·원격 보관 없음).

### Phase 2 — Factor Lab MVP ✅ (MVP)

| 항목 | 상태 | 근거 |
|---|---|---|
| 요인 8개(설정 가능): mom_20/60/120(skip 지원), rev_5, vol_20, turnover_20(로그 평균 거래대금), turnover_ratio_5_60, illiq_20(Amihud) | ✅ | `app/research/factors.py` `default_factors`, `compute_factor` · `test_momentum_and_other_factor_values` |
| 시점: 요인 ≤ t, 진입 t+1 시가, 청산 t+1+h 시가, 시장 달력(KOSPI) 기준, h일마다 리밸런싱(겹치지 않음) | ✅ | `forward_returns`, `evaluate_factor` · `test_no_lookahead_factor_unchanged_by_future_bars`, `test_forward_return_uses_next_open_not_close_t` |
| 결측·거래정지·상장폐지: 창 안·진입·청산 봉이 없으면 NaN, 사유별 집계 | ✅ | `test_missing_entry_or_exit_bar_gives_nan_and_is_counted` |
| 중복(동일 1행 유지, 충돌 행 모두 제거)·달력 밖·비양수 가격 제거 집계 | ✅ | `build_panel` · `test_duplicates_offcalendar_and_bad_prices_counted` |
| IC, Rank IC(표본 수 포함), 분위 수익률, 상위-하위 스프레드, 다리별 회전율 | ✅ | `test_perfect_ranking_*`, `test_reversed_ranking_*`, `test_turnover_formula` |
| 비용: 편도 기준(매수 = 수수료+슬리피지 11.5bp, 매도 = +거래세 31.5bp, 왕복 43bp), 다리 비용 = 편도 회전율 × (매수+매도), 기간 첫 편성은 매수만, 마지막은 청산 매도 추가 | ✅ | `test_perfect_ranking_quantiles_rank_ic_and_costs_counted_once`(이중 계산·누락 없음) |
| 벤치마크: 같은 시점 동일가중 유니버스(주), KOSPI 종가→종가(참고, 시점 차이 명시). 새 벤치마크를 만들지 않음 | ✅ | `ew_universe`, `kospi_cc` |
| 기간·엠바고: StudyConfig 기간 재사용, 엠바고 ≥ ⌈(h+1)·7/5⌉ 검사, 청산이 기간 끝을 넘으면 제외 | ✅ | `FactorLabConfig.validate`, `period_labels` · `test_period_labels_and_embargo` |
| 거래량 단절(`volume_level_break`)이 창에 있으면 거래량 요인만 제외 | ✅ | `test_volume_level_break_masks_volume_factor_only` |
| `--audit`: exclude 종목 제거, exclude_until_verified 봉 주변(조회 창·보유 창) 제외 | ✅ | `load_audit` · `test_audit_untrusted_bar_excludes_lookback_and_holding` |
| 표본 부족 날짜 제외(`min_cross_section`) | ✅ | `test_small_cross_section_yields_no_dates` |
| CLI·manifest·REPORT, 원장 연동(실패도 기록) | ✅ | `app/factorlab/__main__.py` · `test_cli_end_to_end_with_ledger` |
| 배당(총수익), 공매도 대차비용·제한, 시장충격, 업종·시가총액 중립화 | ⬜ | 미구현으로 manifest `not_implemented`에 표기 |

### Phase 3 — Robustness Gate MVP ✅ (MVP)

검사 최대 17종(원장 연결 시 4종 추가)과 임계값: [`docs/ROBUSTNESS_GATE.md`](ROBUSTNESS_GATE.md). 코드 `app/research/robustness.py`, CLI `app/robustness/__main__.py`.

| 요구 | 테스트 |
|---|---|
| 모든 검사 통과 시에만 PASS | `test_pass_only_when_every_check_passes` (테스트 픽스처) |
| 사전 등록 없음 / 실행 후 등록 / 결과 후 수정 / 계획보다 많은 실험 | `test_no_preregistration_is_watch`, `test_registered_after_run_fails`, `test_amended_after_results_is_watch`, `test_more_experiments_than_planned_is_watch` |
| 표본 부족 | `test_insufficient_data` |
| 기간 겹침 / 엠바고 부족 / 보유 구간 경계 넘음 | `test_overlapping_periods_fail`, `test_short_embargo_fails`, `test_holding_window_crossing_period_end_fails` |
| dev 효과 없음 / validation 반전 / test로 방향을 정하지 않음 / 음의 방향 | `test_no_dev_effect_fails`, `test_validation_reversal_fails`, `test_direction_fixed_on_dev_not_test`, `test_negative_direction_uses_bottom_leg` |
| 비용 증가 | `test_higher_costs_break_a_thin_edge` |
| 파라미터 이웃 불안정·이웃 없음 | `test_fragile_parameter_neighbourhood_is_watch` |
| 감사 미적용·review 비율 높음 / 데이터 상태 | `test_audit_warnings`, `test_data_validity_status` |
| 재현성(미커밋 코드, 산출물 변경, 지문·기간이 등록과 다름) | `test_reproducibility` |
| 결과 후 임계값 변경 | `test_changed_thresholds_are_watch` |
| CLI·원장 연결 | `test_cli_with_factorlab_output_and_ledger` |

PBO, Deflated Sharpe Ratio, Reality Check/SPA, 국면별 판정은 **구현하지 않았고** 값을 만들지 않습니다(gate.json `not_implemented`).

### 추가 — Strategy Library 🟡 (MVP, 미커밋)

자세한 내용: [`docs/STRATEGY_LIBRARY.md`](STRATEGY_LIBRARY.md).

| 항목 | 상태 | 근거 |
|---|---|---|
| 기존 전략 조사: 등록 전략 13개(`app/strategies/library.py`), API `/api/strategies`·`/api/backtests/*`, 엔진 `run_portfolio` 파라미터(ExitConfig·RiskConfig·CostModel), 벤치마크(지수 또는 동일가중 대용) | ✅ | 카탈로그가 기존 전략 12개를 그대로 연결(중복 구현 없음), `disclosure_event`는 PLANNED |
| 카탈로그 18개: AVAILABLE 13 · LIMITED 2(상대강도 — 지수 필요·절대 조건 필터, ATR 돌파 — 종가 확인형만) · PLANNED 3(공시 이벤트, 페어, 가치·퀄리티) | ✅ | `app/strategies/catalog.py` · `test_ids_unique_and_metadata_complete`, `test_requested_candidates_are_covered_with_honest_status` |
| 새 전략 3개: `donchian_breakout`, `bb_reversion`, `atr_breakout` | ✅ | `test_donchian_*`, `test_bb_reversion_and_atr_breakout_signals`, `test_future_bars_do_not_change_new_signals` |
| 전략 자체 청산 규칙(엔진 옵션, 기본 꺼짐) + 4개 전략의 청산 규칙 | ✅ | `ExitConfig.strategy_exit`, `Strategy.exit_signals` · `test_strategy_exit_is_off_by_default_and_fills_next_open` |
| 파라미터 형식·범위·상호 조건 검증 | ✅ | `validate_params` · `test_invalid_parameters_rejected`(10가지) |
| 엔진·비용 재사용(Strategy Lab과 같은 결과, 거래별 비용 한 번) | ✅ | `test_library_run_reuses_engine_and_costs` |
| 같은 조건 비교, 비교 불가 사유, 저장 실행 비교의 설정 일관성 검사 | ✅ | `test_compare_same_conditions_and_not_comparable_reasons`, `test_compare_saved_runs_checks_settings` |
| Ledger 사전 등록·설정별 실험 분리·이력 보존·실패 기록 | ✅ | `test_ledger_registration_and_history_preserved`, `test_ledger_records_failed_run` |
| 화면: 카드·계열 필터·검색·상세·파라미터 편집·실행·Strategy Lab으로 보내기·비교·저장 실행 비교, PLANNED는 실행 버튼 없음 | ✅ | `static/index.html` · `test_ui_has_library_tab_wired_to_api`, 브라우저 확인(아래) |
| 실제 데이터(`data/real`)로 라이브러리 실행 | ⬜ | 앱 DB에는 실제 데이터를 적재하지 않음(KRX 라이선스 게이트·네이버 이용 조건). 검증은 DEMO 합성 데이터로만 함 |
| 순위형 상대강도, 장중 돌파 체결, 페어·가치 전략 | ⬜ | 엔진·데이터 미지원 |

화면 확인: 클라우드 작업공간에 앱 코드를 복사해 DEMO 데이터로 실행하고 Chromium(Playwright)으로 목록·실행·PLANNED 상세·비교·검색·Strategy Lab 연결을 눌러 봤습니다. 콘솔 오류 0건.

### 추가 — Strategy Library × Robustness Gate 🟡 (2026-10-26, 미커밋)

| 항목 | 상태 | 근거 |
|---|---|---|
| 별도 어댑터(Factor Lab 게이트 코드 변경 없음, 상태·종합 규칙만 공유) | ✅ | `app/research/robustness_strategy.py` · `tests/test_robustness.py` 기존 21개 그대로 통과 |
| 실행 시 게이트 근거 저장(기간별 벤치마크·초과수익, 기대수익 95% 구간, 비용 2·3배 재실행, 연도별, 벤치마크 종류, 데이터 출처) | ✅ | `app/strategy_library.py` · `test_library_run_records_gate_evidence` |
| 저장 실행 평가 API·화면·결과 파일·원장 연결, 임계값 사전 등록 | ✅ | `POST/GET /api/library/runs/{id}/robustness` · `test_saved_demo_run_through_the_gate`, `test_gate_api_errors_and_threshold_overrides` |
| 미기록 근거는 INSUFFICIENT_DATA, 지표 생성 없음 | ✅ | `test_unrecorded_evidence_is_insufficient_not_invented` |
| DEMO 결과는 FAIL, `real_data_verified` false | ✅ | `test_demo_data_never_counts_as_real_verification`, `test_saved_demo_run_through_the_gate` |
| 기간 겹침·누락·경계 거래, 설정 해시·비용 위변조, 지문 불일치, 사후 등록 | ✅ | `test_period_overlap_missing_and_crossing`, `test_gate_detects_tampered_settings`, `test_preregistration_selection_and_integrity` |
| 벤치마크 종류 기록, 다른 벤치마크 비교 차단, 대용 벤치마크 표기 | ✅ | `test_compare_saved_runs_with_different_benchmarks_is_blocked`, `test_proxy_benchmark_is_never_labelled_as_index` |
| 근거가 모두 있으면 PASS 가능(무조건 차단 아님) | ✅ | `test_pass_is_reachable_only_with_full_evidence`(수작업 근거) |
| 앱 DB 실행에 감사·결과 상태 판정 연결 | ⬜ | 그래서 실제 데이터 실행도 현재 최대 WATCH |
| 실제 데이터로 라이브러리 실행·게이트 평가 | ⬜ | 앱 DB에 실제 데이터 미적재, 전체 기간 백테스트 미실행 |
| Mac·CI 테스트 | ⬜ | Linux VM(Python 3.10)과 클라우드 Linux(Python 3.14.6 + 잠금 의존성)에서만 실행. Mac 본체·CI 미검증 |

화면 확인: 클라우드 작업공간의 앱 사본(같은 파일)을 DEMO로 띄워 실행 → 게이트 평가, 저장 실행 표에서 평가, 서버 오류(500) 표시를 확인. 콘솔 오류 0건.
작업 중 사용자 컴퓨터 연결이 한 번 끊겨, 변경은 클라우드 사본에서 마무리한 뒤 해시로 원본이 바뀌지 않았음을 확인하고 반영했습니다.

### 추가 — 연구 데이터 적격성·감사 출처 🟡 (2026-10-26, `feat/research-data-eligibility`, 미커밋)

자세한 규칙: [`DATA_ELIGIBILITY.md`](DATA_ELIGIBILITY.md).

| 항목 | 상태 | 근거 |
|---|---|---|
| 적격성 모델·판정(`eligibility-v1`, 상태 4종, 감사 산출물 해시 확인, 지문 대조, 범위·신선도, ok/review/exclude, 출처 기록·독립 대조 분리) | ✅ | `app/research/eligibility.py` · `tests/test_data_eligibility.py` 16개 |
| Factor Lab 실행 기록(`data_eligibility`), `--crosscheck` 입력, REPORT 표시 | ✅ | `app/factorlab/__main__.py` · `test_factorlab_run_records_eligibility` |
| Strategy Library 실행 기록 | ✅ | `app/strategy_library.py` · `test_strategy_library_run_records_eligibility` |
| 원장 manifest facts에 적격성 요약 | ✅ | `app/research/ledger.py` · `test_ledger_and_manifest_preserved` |
| 두 게이트 공통 검사 | ✅ | `robustness.py`, `robustness_strategy.py` · `test_both_gates_read_the_record_the_same_way` |
| 실제 데이터 판정(백테스트 없이 판정만) | ✅ | 지문 일치 `1ea20b72f9562ede`. 감사 미적용 시 UNUSABLE, 감사 적용 조건이면 REVIEW_REQUIRED |
| `app.research` 전략 연구 파이프라인 연결 | ⬜ | 후속 과제 |
| 앱 DB 데이터의 수집 기록·감사 연결 | ⬜ | 그래서 Strategy Library 실행은 실제 데이터여도 INSUFFICIENT |

후속 과제와 우선순위(데이터 적격성에 직접 영향을 주는 순):
1. **P1** — review 종목 2,225개 검토와 기업행동 후보 8,073건 공시 대조. 끝나기 전에는 실제 데이터가 REVIEW_REQUIRED를 벗어날 수 없음.
2. **P1** — 독립 출처 가격 대조(KIS·KRX) 결과를 `crosscheck_status.json`의 `cross_source_price_check`로 남기는 실행. 지금은 대조가 없어 source_verification이 WATCH.
3. **P2** — 배당·총수익, 거래량 수정, 연도별 세율. 적격성에서는 known_limitations로만 표시.
4. **P2** — Strategy Library 게이트의 "시도한 설정 수"와 "주변 파라미터"가 최근 실행 200개만 봄(`strategy_library.py` `evidence_for_run`). 실행이 많아지면 선택 편향 경고가 약해짐.
5. **P2** — Factor Lab 게이트와 Strategy Library 게이트의 검사 기준 차이(Rank IC t값 vs 거래 기대수익 구간). 같은 척도로 비교하지 않는다는 점만 문서화.
6. **P3** — PBO·Deflated Sharpe 미구현. 실험 수 기록만 있음.

### 추가 — 가격 교차 검증 증거 🟡 (2026-10-26, `feat/cross-source-price-evidence`, 미커밋)

| 항목 | 상태 | 근거 |
|---|---|---|
| 증거 생성기(저장된 KRX 응답만, 날짜·필드별 일치율과 분모, 매핑·한쪽 출처 종목, 불일치 분류, 원본 해시) | ✅ | `app/krxcheck/evidence.py`, `app/research/price_evidence.py` · `tests/test_price_evidence.py` 18개 |
| 적격성 v2(증거 손상·원본 변경·미해결 불일치 → FAIL, 일부 날짜만 → WATCH) | ✅ | `app/research/eligibility.py` |
| 가격 증거 v2(날짜 상태 3종, 확인되지 않은 조정 행 별도 집계·근접 1배율 하위 집계, 미검증 행이 있으면 통과 아님)와 적격성 v3(미검증 행 → 최소 WATCH, 실제 수로 설명) | ✅ | `price-evidence-v2` · `eligibility-v3`. v1 산출물 보존 |
| Factor Lab `--crosscheck`로 증거 연결 | ✅ | `test_factorlab_reads_price_evidence` |
| 실제 4개 거래일 비교·재현 | ✅ | v2: 2025년 2일 `PASSED_WITH_UNVERIFIED_ADJUSTMENTS`(114·103행), 2026-10-22·23 `FAILED`(711·675행). 기존 비교 결과와 같음 |
| 2025년 조정 가능 행의 원인(기업행동 공시 대조) | ⬜ | 미확인. 이번 작업 범위 밖 |
| 2026-10-22·23 불일치 원인 | ⬜ | 미확정. 수집 시각은 장 마감 후 |
| 연구 기간 전체의 출처 검증 | ⬜ | 4개 거래일·KOSPI만 있음. 추가 KRX 응답 수집은 사용자 승인 필요 |

## 9. 테스트와 실행 환경

| 명령 (Linux VM, Python 3.10, `$HOME/venv`) | 결과 |
|---|---|
| 기준선(변경 전, `32fe29f`): `python -m pytest -q -p no:cacheprovider` | 272 passed, 2 skipped, 1 warning |
| 변경 후 전체: 같은 명령 | **339 passed, 2 skipped, 1 warning** (기존 272 그대로 + 신규 67) |
| 변경 후 전체, CI 엄격 모드 `-W error::DeprecationWarning` | 339 passed, 2 skipped, 1 warning |
| 가격 증거 v2·적격성 v3 후 전체(엄격 모드, 2026-10-26, `feat/cross-source-price-evidence`) | **423 passed, 2 skipped, 1 warning** (가격 증거 테스트 13 → 18, 조정 행 테스트 1개는 새 상태 계약으로 강화) |
| 가격 교차 검증 증거 추가 후 전체(엄격 모드, 2026-10-26, `feat/cross-source-price-evidence`) | **418 passed, 2 skipped, 1 warning** (신규 13. 클라우드 Python 3.14.6 + 잠금 의존성에서도 같은 결과) |
| 데이터 적격성 추가 후 전체(엄격 모드, 2026-10-26, `feat/research-data-eligibility`) | **405 passed, 2 skipped, 1 warning** (신규 16, 기존 단언 변경 없음, 게이트 픽스처 2곳에 적격성 기록 추가) |
| 날짜 계산 3곳 수정 후 전체(엄격 모드, 2026-10-26) | **389 passed, 2 skipped, 1 warning** |
| Strategy Library 게이트 연결 후 전체(엄격 모드, 2026-10-26) | **389 passed, 2 skipped, 1 warning** (신규 `tests/test_strategy_gate.py` 19, 기존 기대값 변경 없음, 경고는 기존 Starlette 1건) |
| Strategy Library 추가 후 전체(엄격 모드) | **370 passed, 2 skipped, 1 warning** (신규 `tests/test_strategy_library.py` 30 + 저장소 규칙 1, 기존 기대값 변경 없음) |
| 신규만: `tests/test_ledger.py` 28 · `tests/test_factorlab.py` 14 · `tests/test_robustness.py` 21 · `tests/test_repo_hygiene.py` +4 | 모두 통과 (`-W error::FutureWarning -W error::RuntimeWarning`에서도 통과) |

건너뛴 2개는 키가 필요한 실제 API 스모크 테스트, 경고 1건은 `StarletteDeprecationWarning`(기존과 같음). 기존 테스트의 기대값은 바꾸지 않았습니다.
위 표는 모두 **Linux VM, Python 3.10.12(numpy 2.2.6)** 결과입니다. 이 환경에는 numpy 2.5의 timedelta 경고가 없어 아래 문제가 드러나지 않았습니다.

| 다른 환경 (2026-10-26) | 결과 |
|---|---|
| 클라우드 Linux, Python 3.14.6 + `requirements-lock.txt`(numpy 2.5.4 · pandas 2.3.3 · fastapi 0.161.1 · starlette 1.9.0, Mac `.venv`와 같은 버전), 엄격 모드 | 수정 전 **34 failed**, 332 passed, 25 skipped — 원인: numpy 2.5에서 `pd.Timedelta(days=N)`과 해상도가 다른 datetime 열의 concat이 DeprecationWarning |
| 같은 환경, 날짜 계산 3곳 수정 후(`app/strategy_library.py` 1줄, `tests/test_robustness.py` 1줄, `tests/test_factorlab.py` 2줄, 기대값 변경 없음) | **389 passed, 2 skipped, 1 warning**(같은 파일로 만든 임시 git 저장소). `.git` 없는 사본에서는 366 passed, 25 skipped(저장소 규칙 테스트 23개가 "not a git checkout"으로 skip) |
| Mac 본체(`.venv`, Python 3.14.6) | ❔ 미검증 — 작업 환경에서 Mac 터미널에 명령을 입력할 수 없음 |
| CI(GitHub Actions, Python 3.12·3.13 잠금 의존성) | ❔ 미검증 — 커밋 전 |

## 10. 실제 데이터 스모크 실행 (범위 제한, 결과 해석 금지)

목적은 실제 `naver_raw` 형식에서 파이프라인이 끝까지 도는지 확인하는 것뿐입니다. **연구 결과로 쓰지 않습니다.**

| 항목 | 값 |
|---|---|
| 명령 | `python -m app.factorlab run --data data/real --format naver_raw --out data/factorlab/smoke_20261025_mom20_rev5 --start 2023-01-01 --factors mom_20,rev_5 --horizon 20 --audit data/audit/full_20261024T060804Z_a` |
| 입력 | 2023-01-01 이후 2,237,290행, 데이터 지문 `8b6efbe3f6cf6c2f`, 결과 상태 PROVISIONAL, 실행 81.8초 |
| 범위 | dev 기간 없음(2023 이후만), validation 11개·test 33개 리밸런싱 날짜, 평균 약 1,060~1,170종목 |
| 게이트 | `data/robustness/smoke_20261025_mom20_v2/gate.json` → **INSUFFICIENT_DATA** (dev 없음, 사전 등록 없음, 감사 review 79%, PROVISIONAL, 미커밋 코드) |
| 원장 | 기록하지 않음(스모크를 영구 원장에 남기지 않기 위해) |
| `data/real/` 무결성 | 실행 전후 SHA-256 2,830개 파일 일치 |

`data/robustness/smoke_20261025_mom20/`(v2 아님)은 수정 전 코드가 만든 결과로 **FAIL**이 잘못 나왔습니다: dev 방향이 없을 때 benchmark 검사가 FAIL을 냈습니다.
수정: 방향이 없으면 benchmark를 INSUFFICIENT_DATA로 처리(`robustness.py`), 회귀 테스트 추가(`test_cli_with_factorlab_output_and_ledger`). 이전 폴더는 지우지 않고 남겼습니다.

## 11. Phase 1–5 로드맵 현황

| 단계 | 상태 |
|---|---|
| Phase 0 — Research Integrity & Documentation Reconciliation | ✅ (`32fe29f`) |
| Phase 1 — Experiment Ledger | ✅ MVP (미커밋) |
| Phase 2 — Factor Lab | ✅ MVP (미커밋). 전체 기간 실제 데이터 실행은 하지 않음 |
| Phase 3 — Robustness Gate | ✅ MVP (미커밋). PBO·DSR 미구현 |
| 추가 — Strategy Library | ✅ MVP (미커밋) |
| 추가 — Strategy Library × Robustness Gate | 🟡 연결됨(별도 어댑터). 앱 DB 실행의 감사·결과 상태 미연결, 실제 데이터 실행 없음 |
| Phase 4 — Edge Decay Monitor | ⬜ |
| Phase 5 — Evidence-based Research Copilot | ⬜ |

## 12. 다음 단계와 차단 요인

1. 이 브랜치 리뷰 후 커밋(사용자 승인 필요). Mac·CI에서 테스트 재실행.
2. 실제 실험을 원장에 사전 등록한 뒤(`python -m app.ledger template`), 전체 기간 Factor Lab 실행(사용자 승인 필요, 약 2~4분 예상 ❔).
3. 감사 review 2,225종목 검토와 기업행동 후보 대조 — 끝나기 전에는 실제 데이터 판정이 PASS가 될 수 없음(PROVISIONAL).
4. PBO(CSCV)·Deflated Sharpe 구현, 다중 실험 보정.
5. `validity.py` 공시 대조 설명 문구 갱신.

차단 요인: KIS 인증정보 없음, 데이터 이용 권한 미확정, 네이버 재수집 보류, VM에서 GitHub 인증 불가(푸시는 Mac에서).
