"""CLI: python -m app.research --data <dir> --format naver_raw|standard --out <dir> [--start --end]"""
from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

from app.research.data import load_naver_raw, load_standard_csv
from app.research.report import write_report
from app.research.study import Study, StudyConfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.research")
    ap.add_argument("--data", required=True)
    ap.add_argument("--format", choices=["naver_raw", "standard"], default="naver_raw")
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--min-turnover", type=float, default=None, help="KRW/day, estimated (default 1e9)")
    ap.add_argument("--random-pool", type=int, default=None)
    a = ap.parse_args(argv)
    t = time.time()
    log = lambda m: print(f"[{time.time() - t:7.1f}s] {m}", flush=True)  # noqa: E731
    loader = load_naver_raw if a.format == "naver_raw" else load_standard_csv
    ds = loader(a.data, a.start, a.end)
    log(f"loaded {ds.bars['symbol'].nunique() if len(ds.bars) else 0} instruments, {len(ds.bars):,} bars, {len(ds.issues):,} quality issues")
    if ds.bars.empty:
        print("REAL 데이터가 없습니다. 결과를 만들지 않았습니다.", file=sys.stderr)
        return 2
    cfg = StudyConfig()
    if a.min_turnover is not None:
        cfg.min_avg_turnover = a.min_turnover
    if a.random_pool is not None:
        cfg.random_pool = a.random_pool
    cmd = "python -m app.research " + " ".join(shlex.quote(x) for x in (argv if argv is not None else sys.argv[1:]))
    res = Study(ds, cfg, Path(a.out), log).run(cmd)
    write_report(res, cfg, Path(a.out))
    log(f"done -> {a.out}/REPORT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
