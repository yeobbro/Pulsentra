"""Run the research pipeline with the v0.3 baseline code and with the current code on the SAME data, then compare.

Separates "the code changed the numbers" from "the data changed the numbers": if both runs agree here, any gap to
reports/v0.3 comes from the data (or from the audit/cost options), not from the code.

  python scripts/compare_baseline.py --data data/real --out reports/compare_v03_v04 [--baseline-ref v0.3.0] [--start 2016-01-01]

The baseline code is exported with `git archive` into a temporary folder outside the repository; nothing in the
working tree changes. Both runs use their own default settings. Expect two full research runs (about 13 min each
at v0.3 scale).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def compare_outputs(a: Path, b: Path) -> dict:
    """Table-by-table comparison of two research output folders (columns new in `b` are listed, not counted as a difference)."""
    res = {"identical": [], "identical_plus_new_columns": {}, "numerically_equal": {}, "both_empty": [], "different": {}, "missing": []}
    for p in sorted(a.rglob("*.csv")):
        f = str(p.relative_to(a))
        q = b / f
        if not q.exists():
            res["missing"].append(f)
            continue
        if p.stat().st_size <= 2 and q.stat().st_size <= 2:
            res["both_empty"].append(f)
            continue
        x, y = pd.read_csv(p), pd.read_csv(q)
        extra = [c for c in y.columns if c not in x.columns]
        y2 = y[[c for c in y.columns if c in x.columns]]
        if x.equals(y2):
            (res["identical_plus_new_columns"].__setitem__(f, extra) if extra else res["identical"].append(f))
            continue
        try:
            # summation order can move the last bit (seen: brier_skill differing by 2e-16); report the size, not a difference
            pd.testing.assert_frame_equal(x, y2, check_exact=False, rtol=1e-9, atol=1e-12)
            num = [c for c in x.columns if x[c].dtype.kind == "f" and not x[c].equals(y2[c])]
            res["numerically_equal"][f] = {c: float((x[c] - y2[c]).abs().max()) for c in num}
        except AssertionError as exc:
            res["different"][f] = str(exc).splitlines()[0][:200]
    ma = json.loads((a / "run_manifest.json").read_text(encoding="utf-8"))
    mb = json.loads((b / "run_manifest.json").read_text(encoding="utf-8"))
    res["manifest"] = {k: ma.get(k) == mb.get(k) for k in ("data_fingerprint", "chosen_exit_on_dev", "sample", "configs_tested_per_strategy")}
    res["same_results"] = not res["different"] and not res["missing"] and all(res["manifest"].values())
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--format", default="naver_raw", choices=["naver_raw", "standard"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline-ref", default="v0.3.0")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from app.research.manifest import guard_output_dir
    out = Path(a.out)
    try:
        guard_output_dir(out, Path(a.data), overwrite=False)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    data = str(Path(a.data).resolve())
    common = ["--data", data, "--format", a.format, "--start", a.start] + (["--end", a.end] if a.end else [])
    with tempfile.TemporaryDirectory(prefix="pulsentra-baseline-") as tmp:
        blob = subprocess.check_output(["git", "archive", a.baseline_ref], cwd=ROOT)
        tarfile.open(fileobj=BytesIO(blob)).extractall(tmp, filter="data")
        runs = {"baseline": (Path(tmp), out / "baseline"), "current": (ROOT, out / "current")}
        for name, (code, dest) in runs.items():
            print(f"[{name}] python -m app.research ({code})", flush=True)
            rc = subprocess.call([sys.executable, "-m", "app.research", *common, "--out", str(dest.resolve())], cwd=code)
            if rc != 0:
                print(f"[{name}] exit {rc} — comparison stopped", file=sys.stderr)
                return rc
    res = compare_outputs(out / "baseline", out / "current")
    res["baseline_ref"] = a.baseline_ref
    (out / "comparison.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("same_results", "manifest", "numerically_equal", "different", "missing")}, ensure_ascii=False, indent=2))
    return 0 if res["same_results"] else 1


if __name__ == "__main__":
    sys.exit(main())
