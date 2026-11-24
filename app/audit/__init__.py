"""Read-only data audit runs: load raw files, audit them (app.data.audit), write findings and a report.

Input files are only read. Outputs go to a separate folder (default under data/audit/, ignored by Git) and an
existing output folder is never overwritten unless asked.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.data.audit import AuditConfig, AuditResult, audit_frame, to_json
from app.research.manifest import environment_info, git_info, output_inventory

OUTPUT_FILES = ("findings.csv", "corrections.csv", "exclusions.csv", "corporate_action_candidates.csv", "symbol_summary.csv",
                "audit_summary.json", "audit_manifest.json", "AUDIT_REPORT.md")


def _calendar(d: Path) -> pd.DatetimeIndex | None:
    p = d / "index_KOSPI.csv"
    if not p.exists():
        cands = sorted(d.glob("index_*.csv"))
        p = cands[0] if cands else None
    if p is None:
        return None
    df = pd.read_csv(p)
    col = "Date" if "Date" in df.columns else "date"
    return pd.DatetimeIndex(pd.to_datetime(df[col], errors="coerce").dropna().sort_values().unique())


def load_raw_naver(data_dir: str | Path, start: str | None = None, end: str | None = None,
                   symbols: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex | None]:
    """Raw rows exactly as collected (halt markers included), plus the universe and the market calendar."""
    d = Path(data_dir)
    uni = pd.read_csv(d / "universe.csv", dtype={"symbol": str})
    if symbols:
        uni = uni[uni["symbol"].isin(symbols)]
    parts = []
    for sym in uni["symbol"]:
        f = d / "raw" / f"{sym}.csv"
        if not f.exists():
            continue
        raw = pd.read_csv(f, dtype=str)                 # strings: the audit itself reports unparsable values
        raw["symbol"] = sym
        parts.append(raw)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["symbol", "date"])
    bars["market"] = "KR"
    bars["adjusted"] = True                              # provider-adjusted prices (volume is not; see DATA_SOURCES.md)
    bars["source"] = "naver_fchart"
    bars = _window(bars, start, end)
    cal = _calendar(d)
    return bars, uni, cal


def load_raw_standard(data_dir: str | Path, start: str | None = None, end: str | None = None,
                      symbols: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DatetimeIndex | None]:
    from app.data.ingest import parse_csv
    d = Path(data_dir)
    parts = []
    for f in sorted(p for p in d.glob("*.csv") if p.name != "instruments.csv" and not p.name.startswith("index_")):
        df = parse_csv(f.read_bytes())
        if symbols:
            df = df[df["symbol"].isin(symbols)]
        parts.append(df)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["symbol", "date"])
    inst_path = d / "instruments.csv"
    inst = pd.read_csv(inst_path, dtype={"symbol": str}) if inst_path.exists() else None
    return _window(bars, start, end), inst, _calendar(d)


def _window(bars: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if bars.empty or not (start or end):
        return bars
    d = pd.to_datetime(bars["date"], errors="coerce")
    keep = d.isna()                                      # unparsable dates stay in so the audit can report them
    inside = pd.Series(True, index=bars.index)
    if start:
        inside &= d >= pd.Timestamp(start)
    if end:
        inside &= d <= pd.Timestamp(end)
    return bars[keep | inside].reset_index(drop=True)


def check_output_dir(out: Path, data_dir: Path, overwrite: bool) -> None:
    """Refuse output locations that could touch the input data."""
    out_r, data_r = out.resolve(), data_dir.resolve()
    if out_r == data_r or out_r in data_r.parents:
        raise ValueError(f"출력 폴더가 입력 데이터 폴더와 같거나 그것을 포함합니다: {out}")
    if data_r in out_r.parents and out_r.relative_to(data_r).parts[0] == "raw":
        raise ValueError(f"출력 폴더가 원본 일봉 폴더(raw/) 안에 있습니다: {out}")
    if out.exists() and any(out.iterdir()):
        foreign = [p.name for p in out.iterdir() if p.name not in OUTPUT_FILES]
        if foreign:
            raise ValueError(f"출력 폴더에 감사 결과가 아닌 파일이 있습니다: {foreign[:5]}")
        if not overwrite:
            raise ValueError(f"출력 폴더에 이전 감사 결과가 있습니다: {out} (--overwrite 로 덮어쓰기)")


def write_audit(res: AuditResult, out: Path, meta: dict) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    res.findings.to_csv(out / "findings.csv", index=False)
    res.corrections.to_csv(out / "corrections.csv", index=False)
    res.exclusions.to_csv(out / "exclusions.csv", index=False)
    res.candidates.to_csv(out / "corporate_action_candidates.csv", index=False)
    res.symbols.to_csv(out / "symbol_summary.csv", index=False)
    (out / "audit_summary.json").write_text(to_json(res.summary), encoding="utf-8")
    write_markdown(res, out / "AUDIT_REPORT.md", meta)
    manifest = {**meta, "git": git_info(), "environment": environment_info(),
                "fingerprints": {k: res.summary.get(k) for k in ("input_fingerprint", "findings_fingerprint", "usable_fingerprint")},
                "outputs": output_inventory(out, skip=("audit_manifest.json",))}
    (out / "audit_manifest.json").write_text(to_json(manifest), encoding="utf-8")
    return out


def compare_runs(a: Path, b: Path) -> dict:
    """Same input + same config should give the same fingerprints. Returns what differs."""
    ma = json.loads((a / "audit_manifest.json").read_text(encoding="utf-8"))
    mb = json.loads((b / "audit_manifest.json").read_text(encoding="utf-8"))
    sa, sb = ma.get("fingerprints", {}), mb.get("fingerprints", {})
    same_input = sa.get("input_fingerprint") == sb.get("input_fingerprint")
    same_cfg = ma.get("config") == mb.get("config")
    diff = {k: (sa.get(k), sb.get(k)) for k in set(sa) | set(sb) if sa.get(k) != sb.get(k)}
    return {"same_input": same_input, "same_config": same_cfg, "differences": diff,
            "reproduced": same_input and same_cfg and not diff}


def _md_table(df: pd.DataFrame, cols: list[str], limit: int = 30) -> str:
    if df is None or df.empty:
        return "_(없음)_\n"
    d = df[cols].head(limit)
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = "\n".join("| " + " | ".join("" if pd.isna(v) else (v.strftime("%Y-%m-%d") if isinstance(v, pd.Timestamp) else str(v))
                                       for v in r) + " |" for r in d.itertuples(index=False))
    more = f"\n\n_…{len(df) - limit:,}행 더 있음 (CSV 참고)_" if len(df) > limit else ""
    return head + body + more + "\n"


def write_markdown(res: AuditResult, path: Path, meta: dict) -> None:
    s = res.summary
    L = ["# Pulsentra 데이터 감사 보고서\n",
         "> 자동 생성. 입력 파일은 읽기만 했고 수정하지 않았습니다. 수치는 아래 입력·설정에서만 성립합니다.\n",
         f"- 입력: `{meta.get('data_dir')}` (형식 `{meta.get('format')}`), 기간 {meta.get('start')} ~ {meta.get('end') or '끝'}\n",
         f"- 출처: {meta.get('source_note')}\n",
         f"- 입력 지문 `{s.get('input_fingerprint')}` · 결과 지문 `{s.get('findings_fingerprint')}` · 사용 가능 행 지문 `{s.get('usable_fingerprint')}`\n",
         f"- 원본 행 {s['rows_raw']:,} → 백테스트 사용 가능 행 {s['rows_usable']:,} (제거 {s['rows_removed']:,}, 신호 제외 구간 {s['rows_signal_excluded']:,}, "
         f"자동 보정 {s['rows_repaired']:,}행)\n",
         f"- 종목 {s['symbols_total']:,}개 (일봉 있는 종목 {s['symbols_with_bars']:,}) · 감사 상태 {s['audit_status']}\n",
         f"- 권고별 건수 {s['by_recommendation']} · 기업행동 후보(신뢰도별) {s['corporate_action_candidates']}\n",
         "\n## 1. 검사별 요약\n\n`top_symbol_share`는 해당 검사 건수 중 가장 많은 종목의 비중입니다. 1에 가까우면 한 종목의 공급 문제일 가능성이 큽니다.\n\n"]
    L.append(_md_table(pd.DataFrame(s["by_check"]), ["check", "category", "severity", "rows", "symbols", "top_symbol", "top_symbol_rows", "top_symbol_share"], 100))
    F = res.findings
    if len(F):
        L.append("\n## 2. 원인 가설별 건수\n\n")
        cause = F.groupby(["check", "cause", "recommendation"]).size().reset_index(name="rows").sort_values("rows", ascending=False)
        L.append(_md_table(cause, ["check", "cause", "recommendation", "rows"], 60))
    L.append("\n## 3. 검토가 필요한 종목 (오류·검토 건수 순)\n\n")
    S = res.symbols
    if len(S):
        top = S[S["audit_status"] != "ok"].sort_values(["errors", "exclude_until_verified", "needs_review"], ascending=False)
        L.append(_md_table(top, ["symbol", "audit_status", "rows_raw", "rows_usable", "rows_removed", "errors", "warnings",
                                 "exclude_until_verified", "needs_review", "corporate_action_candidates"], 40))
    L.append("\n## 4. 기업행동 의심 구간 (OpenDART로 확인 필요)\n\n")
    K = res.candidates.sort_values(["confidence", "symbol", "date"]) if len(res.candidates) else res.candidates
    L.append(_md_table(K, ["symbol", "date", "kind", "implied_ratio", "price_ratio", "volume_ratio", "gap_sessions", "confidence"], 40))
    L.append("\n## 5. 제외 내역\n\n")
    X = res.exclusions
    if len(X):
        xs = X.groupby(["scope", "reason"]).agg(rows=("rows", "sum"), symbols=("symbol", "nunique"), ranges=("rows", "size")).reset_index()
        L.append(_md_table(xs, ["scope", "reason", "rows", "symbols", "ranges"]))
    else:
        L.append("_(없음)_\n")
    L.append("\n## 6. 자동 보정 (전후 값은 `corrections.csv`)\n\n")
    L.append(f"자동 보정은 수정주가 반올림 오차(허용 {res.summary['config']['rounding_tolerance']:.1%}) 한 가지뿐입니다. 보정 {len(res.corrections):,}개 필드.\n")
    L.append("\n## 7. 이 보고서로 답할 수 있는 질문\n\n"
             "| 질문 | 위치 |\n|---|---|\n"
             "| 어떤 종목에서 어떤 문제가 언제 발생했나 | `findings.csv` (symbol, date, check) |\n"
             "| 가격·거래량·기업행동·거래정지·수집 오류 중 무엇과 관련 있나 | `findings.csv` category, cause |\n"
             "| 자동 수정해도 되나 | `auto_fix` (repaired는 반올림 보정뿐), `corrections.csv` |\n"
             "| 공식 출처 대조가 필요한가 | `recommendation` = exclude_until_verified / review, `follow_up` |\n"
             "| 백테스트에 포함되나 | `backtest_use` (현재 파이프라인 동작), `symbol_summary.csv` audit_status |\n"
             "| 제외 이유와 행 수 | `exclusions.csv` (row_removed / signals_excluded) |\n"
             "| 재실행 시 재현되나 | `audit_manifest.json` fingerprints, `python -m app.audit --compare A B` |\n")
    L.append("\n## 8. 설정\n\n```json\n" + json.dumps(s["config"], ensure_ascii=False, indent=1) + "\n```\n")
    path.write_text("".join(L), encoding="utf-8")


__all__ = ["AuditConfig", "audit_frame", "load_raw_naver", "load_raw_standard", "write_audit", "compare_runs", "check_output_dir"]
