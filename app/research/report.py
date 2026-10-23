"""REPORT.md for a research run. Numbers come only from the run's own tables."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _p(v, d=2):
    return "—" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v * 100:.{d}f}%"


def _f(v, d=2):
    return "—" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{d}f}"


def _ci(c, pct=True):
    if c is None or (isinstance(c, float) and np.isnan(c)):
        return "—"
    a, b = c
    return f"[{_p(a)}, {_p(b)}]" if pct else f"[{_f(a)}, {_f(b)}]"


def _table(df: pd.DataFrame, cols: list[tuple[str, str, callable]]) -> str:
    if df is None or df.empty:
        return "_(없음)_\n"
    head = "| " + " | ".join(h for h, _, _ in cols) + " |\n|" + "---|" * len(cols) + "\n"
    rows = ["| " + " | ".join(fn(r.get(k)) if fn else str(r.get(k, "")) for _, k, fn in cols) + " |" for r in df.to_dict("records")]
    return head + "\n".join(rows) + "\n"


def write_report(res: dict, cfg, out: Path) -> Path:
    s = res["sample"]
    m = res["manifest"]
    prov = m["provenance"]
    g = res["grid"]
    chosen = res["chosen"]
    L: list[str] = []
    L.append("# Pulsentra 리서치 실행 — 실제 데이터 전략 검증 결과\n")
    L.append("> 자동 생성 보고서. 이 문서의 수치는 아래 데이터·설정·코드 버전으로 재현됩니다. 과거 검증 결과이며 미래 수익을 보장하지 않습니다.\n")
    v = m.get("validity")
    if v:
        L.append(f"\n**결과 상태: {v['status']}** — INELIGIBLE은 어떤 결정에도 쓰면 안 되는 결과, PROVISIONAL은 내부적으로 일관되지만 "
                 "검증되지 않은 데이터 가정에 기대는 결과입니다.\n")
        for r in v.get("ineligible_reasons", []):
            L.append(f"- 부적격 사유: {r}\n")
        if v.get("unverified_items"):
            L.append(f"- 미검증·부분 처리 항목: {', '.join(v['unverified_items'])} (자세한 내용은 `run_manifest.json`의 `data_treatments`)\n")
    L.append("## 1. 데이터 출처와 표본\n")
    L.append(f"- 가격: {prov.get('price_source')}\n- 상장/폐지 목록: {prov.get('listing_source')}\n- 지수: {prov.get('index_source')}\n"
             f"- 수집 시각(UTC): {prov.get('fetched_at_range')}\n- 데이터 지문: `{m['data_fingerprint']}` · 코드: `{m.get('git_commit')}`\n")
    L.append(f"- 기간: {s['first_date']} ~ {s['last_date']} · 종목 {s['instruments_with_bars']:,}개 · 일봉 {s['bars']:,}개 · 거래소 {s['by_exchange']}\n"
             f"- 상장폐지 종목: 목록 {s['delisted_in_universe']}개 중 데이터 확보 {s['delisted_with_bars']}개 → {s['survivorship']}\n"
             f"- 출처 유형: 전부 REAL (합성 데이터 혼입 시 실행 중단)\n")
    L.append("**알려진 데이터 한계**: 가격은 분할·권리락 등 수정주가이나 거래량은 수정되지 않음(거래 정지 구간을 가로지르는 거래량 수준 급변을 탐지해 해당 40거래일 창의 신호를 제외). "
             "거래대금은 제공되지 않아 `수정종가 × 거래량`으로 추정(유동성 필터에만 사용). 현금 배당은 가격에 반영되지 않아 수익률은 배당 제외 가격 수익률. "
             "무상증자처럼 거래 정지가 없는 이벤트의 거래량 불연속은 탐지하지 못함.\n")
    L.append("## 2. 데이터 품질 검사\n")
    L.append(_table(res["quality"], [("검사", "check", None), ("구분", "severity", None), ("행", "rows", lambda v: f"{int(v):,}"), ("종목", "symbols", lambda v: f"{int(v):,}")]))
    L.append("\n## 3. 전략 정의 (모두 같은 데이터·비용·체결 규칙)\n")
    L.append("- 공통: 신호는 t일 종가까지의 정보, 진입은 t+1일 시가 + 슬리피지. 같은 봉 손절·익절 동시 도달 시 손절. 갭은 시가 체결. "
             f"비용 편도 수수료 {cfg.costs['commission_bps']}bp, 매도세 {cfg.costs['sell_tax_bps']}bp, 슬리피지 편도 {cfg.costs['slippage_bps']}bp (가정값). "
             f"적격 조건: 20일 평균 추정 거래대금 ≥ {cfg.min_avg_turnover / 1e8:.0f}억 원, 종가 ≥ {cfg.min_price:,.0f}원, 거래량 창 오염 없음.\n"
             "- A `volume_quiet`: 20일 누적 거래량 ÷ 직전 20일 ≥ 3, 20일 등락 −3%~+3%, 거래대금 배수 ≥ 1.5, 신호 다음 날 진입.\n"
             "- B1 `volume_quiet_breakout`: A 조건이 최근 10거래일 안에 있었고 오늘 종가가 직전 20일 고가를 돌파.\n"
             "- B2 `breakout_volume`: 독립 돌파 — 직전 20일 고가 돌파 + 당일 거래량 ≥ 직전 20일 평균의 1.5배.\n"
             "- C `pullback_rebound`: 20일선>60일선, 60일선 20일 기울기>0, 종가>60일선; 직전 5거래일 내 저가가 20일선+2% 이내로 조정되며 5일/20일 평균 거래량 ≤ 0.8; 오늘 종가 > 전일 고가 & 20일선.\n"
             "- 기준 BASE: 120거래일 종가 신고가 + 120일 수익률 > 0 (단순 모멘텀), 무작위 진입(적격 행에서 무작위 추출).\n")
    periods = cfg.periods
    L.append(f"\n## 4. 기간 분리와 선택 규칙\n\n- 개발(dev) {periods['dev'][0]}~{periods['dev'][1]}, 검증(validation) {periods['validation'][0]}~{periods['validation'][1]}, "
             f"최종 평가(test) {periods['test'][0]}~데이터 끝. 각 기간 끝 {cfg.embargo_days}일 이내 신호는 제외(다음 기간 데이터로 청산되는 것 방지).\n"
             f"- 청산 규칙 {m['configs_tested_per_strategy']}개 조합(손절 4 × 익절 4 × 보유 3)을 전부 계산해 `strategy_exit_grid.csv`에 보존. "
             f"전략별로 **dev 기간 거래당 평균 순수익이 가장 높은 조합 하나**(dev 거래 ≥ {cfg.min_dev_trades})를 고르고, 그 조합을 validation·test에 그대로 적용.\n")
    L.append("\n## 5. 전략별 결과 (거래 단위, dev에서 고른 청산 규칙)\n")
    sel = []
    for lab, ex in chosen.items():
        for per in list(periods):
            r = g[(g.strategy == lab) & (g.exit == ex) & (g.period == per)]
            if len(r):
                sel.append({**r.iloc[0].to_dict(), "strategy": lab, "exit": ex, "period": per})
        rr = g[(g.strategy == "RANDOM_pool") & (g.exit == ex)]
        for per in periods:
            q = rr[rr.period == per]
            if len(q):
                sel.append({**q.iloc[0].to_dict(), "strategy": f"└ 무작위 진입 (같은 청산: {lab.split('_')[0]})", "exit": ex, "period": per})
    sdf = pd.DataFrame(sel)
    L.append(_table(sdf, [("전략", "strategy", None), ("청산", "exit", None), ("기간", "period", None), ("거래", "n", lambda v: "—" if v != v else f"{int(v):,}"),
                          ("승률", "win_rate", _p), ("평균 순수익", "mean_net", _p), ("95% CI(월 군집)", "mean_net_ci95_cluster", _ci),
                          ("비용 전", "mean_gross", _p), ("평균 R", "mean_r", _f), ("PF", "profit_factor", _f), ("최대 연속손실", "max_consecutive_losses", lambda v: "—" if v != v or v is None else str(int(v)))]))
    L.append("\n무작위 비교 (같은 기간·같은 청산 규칙·같은 거래 수로 무작위 추출 1,000회):\n\n")
    rd = res["random"].copy()
    if len(rd):
        rd["band"] = [f"[{_p(a)}, {_p(b)}]" for a, b in zip(rd["random_same_n_p05"], rd["random_same_n_p95"])]
    L.append(_table(rd, [("전략", "strategy", None), ("기간", "period", None), ("거래", "n", lambda v: f"{int(v):,}"), ("전략 평균", "strategy_mean_net", _p),
                         ("무작위 평균", "random_mean_net", _p), ("무작위 같은 n 5~95%", "band", None), ("이긴 비율", "share_of_random_draws_beaten", lambda v: _p(v, 1)),
                         ("단측 p", "one_sided_p_value", lambda v: _f(v, 3))]))
    L.append("\n## 6. 연도별 워크포워드 (해당 연도 이전 데이터로만 청산 규칙 선택)\n")
    wf = res["wf"]
    if len(wf):
        agg = wf.groupby("strategy").apply(lambda d: pd.Series({"years": len(d), "oos_trades": int(d["oos_n"].fillna(0).sum()),
                                                                 "years_positive": int((d["oos_mean_net"] > 0).sum()),
                                                                 "mean_of_yearly_means": float(d["oos_mean_net"].mean())}), include_groups=False).reset_index()
        L.append(_table(agg, [("전략", "strategy", None), ("연도 수", "years", lambda v: str(int(v))), ("표본 외 거래", "oos_trades", lambda v: f"{int(v):,}"),
                              ("평균>0 연도", "years_positive", lambda v: str(int(v))), ("연도 평균의 평균", "mean_of_yearly_means", _p)]))
    L.append("\n## 7. 포트폴리오 백테스트 (자금·보유 수 제약, dev에서 고른 청산 규칙)\n")
    port = res["portfolio"]
    pt = port[~port["run"].str.contains("yearly", na=False)]
    L.append(_table(pt, [("실행", "run", None), ("기간", "period", None), ("총수익", "total_return", _p), ("연환산", "cagr", _p), ("MDD", "max_drawdown", _p),
                         ("샤프", "sharpe", _f), ("비용 전 총수익", "gross_total_return", _p), ("KOSPI 대비", "excess_vs_KOSPI", _p), ("거래", "trades", lambda v: "—" if v is None or v != v else f"{int(v):,}"),
                         ("승률", "win_rate", _p), ("PF", "profit_factor", _f),
                         ("진입 중단일", "halted_days", lambda v: "—" if v is None or v != v else f"{int(v):,}")]))
    L.append("\n포트폴리오 규칙: 초기 자본 1억 원, 거래당 위험 0.5%, 최대 10종목, 종목당 20%, 일일 손실 3% 시 다음 날 진입 중단, "
             "고점 대비 25% 낙폭 시 20영업일 신규 진입 중단 후 고점 재설정. 포트폴리오 결과는 자금·보유 수 제약 때문에 경로 의존적이다 — "
             "비용 0 실행은 다른 거래 순서를 따르므로 '비용 전 총수익'과 '총수익'의 차이를 비용 효과로 읽으면 안 된다(비용 효과는 9절 거래 단위 표 참고).\n")
    L.append("\n## 8. 시장 국면별 (KOSPI 60거래일 수익률 ±5%)\n")
    reg = res["regime"]
    L.append(_table(reg, [("전략", "strategy", None), ("기간", "period", None), ("국면", "regime", None), ("거래", "n", lambda v: f"{int(v):,}"),
                          ("평균 순수익", "mean_net", _p), ("95% CI", "mean_net_ci95_cluster", _ci), ("승률", "win_rate", _p)]))
    L.append("\n## 9. 비용 민감도 (거래 단위 평균 순수익)\n")
    cost = res["cost"]
    if len(cost):
        pv = cost.pivot_table(index=["strategy", "period"], columns="cost_model", values="mean_net").reset_index()
        L.append(_table(pv, [("전략", "strategy", None), ("기간", "period", None), ("비용 0", "gross(0)", _p), ("기본", "base", _p), ("슬리피지 3배", "slippage x3", _p)]))
    L.append("\n## 10. ML 필터 vs 단순 규칙\n")
    L.append("dev 기간으로 학습(마지막 20%로 확률 보정), validation에서 채택 여부 판정, test는 최종 보고만. "
             f"라벨: {cfg.ml_exit} 기준 익절이 손절보다 먼저(시간 청산은 음성). 선택 = 예측확률 ≥ dev 양성비율 + 5%p.\n\n")
    L.append(_table(res["ml"], [("전략", "strategy", None), ("모델", "model", None), ("기간", "period", None), ("AUC", "auc", lambda v: _f(v, 3)),
                                ("Brier skill", "brier_skill", lambda v: _f(v, 3)), ("규칙 거래", "rule_n", lambda v: "—" if v is None or v != v else f"{int(v):,}"),
                                ("규칙 평균", "rule_mean_net", _p), ("ML 거래", "ml_n", lambda v: "—" if v is None or v != v else f"{int(v):,}"), ("ML 평균", "ml_mean_net", _p),
                                ("ML 95% CI", "ml_mean_net_ci95", _ci), ("채택(검증)", "adopted_on_validation", lambda v: "—" if v is None or v != v else ("예" if v else "아니오")),
                                ("불채택 사유 / 비고", "rejection_reasons", lambda v: "" if v is None or v != v else str(v))]))
    L.append("\n## 11. 재현\n\n```bash\npython scripts/collect_kr_daily.py --out data/real --start 2016-01-01   # 데이터 수집 (재실행 시 이어받기)\n"
             f"{m['command']}\n```\n\n실행 시간 {m['seconds']}초 · 단계별 {m['timing']}\n")
    git = m.get("git") or {}
    if git:
        env = m.get("environment", {})
        L.append(f"\n- 코드: `{git.get('commit')}` (브랜치 `{git.get('branch')}`) · 커밋만으로 재현 가능: "
                 f"{'예' if git.get('reproducible_from_commit') else '아니오 — 커밋되지 않은 코드 변경: ' + ', '.join(git.get('uncommitted_code_files') or [])}\n"
                 f"- Python {env.get('python')} · 패키지 {env.get('packages')}\n"
                 f"- 실행 중 경고 {sum(w['count'] for w in m.get('warnings', []))}건 · 실험 조합 수 {m.get('experiments_total')} (선택 편향 해석 시 참고)\n")
        bc = m.get("boundary_crossing_trades") or {}
        if bc:
            L.append("- 기간 끝을 넘어 청산된 거래(신호 기간 기준): " + ", ".join(f"{k} {v['crossing']}/{v['trades']}" for k, v in bc.items()) + "\n")
    L.append("\n## 12. 파일\n\n`strategy_exit_grid.csv`(전 조합×기간), `walk_forward_by_year.csv`, `vs_random_entry.csv`, `by_regime.csv`, `cost_sensitivity.csv`, "
             "`portfolio_results.csv`, `ml_vs_rule.csv`, `ml_reliability.json`, `data_quality_summary.csv`, `data_quality_issues.csv`, `provider_halt_rows.csv`, "
             "`trades/portfolio_*.csv`(포트폴리오 거래별 진입·청산), `trades/signals_*.csv`(신호별 결과), `run_manifest.json`.\n")
    p = out / "REPORT.md"
    p.write_text("".join(x if x.endswith("\n") else x + "\n" for x in L), encoding="utf-8")
    return p
