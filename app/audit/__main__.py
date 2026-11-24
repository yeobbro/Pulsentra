"""CLI: python -m app.audit --data <dir> [--format naver_raw|standard] [--out <dir>] [--start --end] [--symbols ...]
         python -m app.audit --compare <audit dir A> <audit dir B>

Reads the input files only. Writes findings, corrections, exclusions, corporate-action candidates, a per-symbol
summary, AUDIT_REPORT.md and audit_manifest.json to --out (default data/audit/<UTC timestamp>, ignored by Git).
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

import pandas as pd

from app.audit import check_output_dir, compare_runs, load_raw_naver, load_raw_standard, write_audit
from app.data.audit import AuditConfig, audit_frame

SOURCE_NOTES = {
    "naver_raw": "scripts/collect_kr_daily.py 수집본 — 네이버 금융 일봉(비공식, 가격 수정·거래량 미수정), FinanceDataReader 상장·폐지 목록, KRX 지수 캐시",
    "standard": "사용자 제공 표준 CSV (출처는 각 파일의 source 열)",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.audit")
    ap.add_argument("--data")
    ap.add_argument("--format", choices=["naver_raw", "standard"], default="naver_raw")
    ap.add_argument("--out", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", nargs="*", default=None, help="audit only these codes")
    ap.add_argument("--overwrite", action="store_true", help="replace a previous audit in --out")
    ap.add_argument("--jump-threshold", type=float, default=None)
    ap.add_argument("--volume-break-factor", type=float, default=None)
    ap.add_argument("--no-gap-volume-shift", action="store_true", help="skip the (slow, info-only) no-gap volume shift scan")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"), help="compare two audit output folders and exit")
    a = ap.parse_args(argv)
    if a.compare:
        res = compare_runs(Path(a.compare[0]), Path(a.compare[1]))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["reproduced"] else 1
    if not a.data:
        ap.error("--data is required (or use --compare)")
    data = Path(a.data)
    out = Path(a.out) if a.out else Path("data/audit") / pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    try:
        check_output_dir(out, data, a.overwrite)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    cfg = AuditConfig()
    if a.jump_threshold is not None:
        cfg.jump_threshold = a.jump_threshold
    if a.volume_break_factor is not None:
        cfg.volume_break_factor = a.volume_break_factor
    if a.no_gap_volume_shift:
        cfg.no_gap_volume_shift = False
    if a.format == "standard":
        cfg.remove_halt_markers = False     # the standard loader keeps every row; halt markers then fail the price checks
    t = time.time()
    loader = load_raw_naver if a.format == "naver_raw" else load_raw_standard
    bars, inst, cal = loader(data, a.start, a.end, a.symbols)
    print(f"[{time.time() - t:6.1f}s] loaded {len(bars):,} raw rows, {bars['symbol'].nunique() if len(bars) else 0} symbols", flush=True)
    if bars.empty:
        print("감사할 일봉이 없습니다.", file=sys.stderr)
        return 2
    res = audit_frame(bars, cfg, cal, inst)
    cmd = "python -m app.audit " + " ".join(shlex.quote(x) for x in (argv if argv is not None else sys.argv[1:]))
    meta = {"command": cmd, "data_dir": str(data), "format": a.format, "start": a.start, "end": a.end, "symbols": a.symbols,
            "source_note": SOURCE_NOTES[a.format], "started_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "seconds": round(time.time() - t, 1), "config": res.summary["config"]}
    write_audit(res, out, meta)
    s = res.summary
    print(f"[{time.time() - t:6.1f}s] {s['findings']:,} findings, usable rows {s['rows_usable']:,}/{s['rows_raw']:,} -> {out}/AUDIT_REPORT.md", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
