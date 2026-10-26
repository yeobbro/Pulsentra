"""Reproducibility metadata shared by research and audit runs.

A manifest must let someone answer "which code, which packages, which data, which settings produced this file?"
without copying any data or credential into it. Only names, versions, hashes and counts are recorded.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
import warnings
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from typing import Iterator

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
# Packages whose versions can change numerical results or file formats.
KEY_PACKAGES = ("numpy", "pandas", "scikit-learn", "scipy", "joblib", "requests", "finance-datareader")
# Paths whose uncommitted edits make a run unreproducible from its commit.
CODE_PATHS = ("app", "scripts", "requirements.txt", "requirements-research.txt", "requirements-lock.txt")


def package_versions(names: tuple[str, ...] = KEY_PACKAGES) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except metadata.PackageNotFoundError:
            out[n] = None
    return out


def _git(*args: str, strip: bool = True) -> str | None:
    """Run git in ROOT. `strip=False` keeps the output byte-exact (porcelain status codes may start with a space)."""
    try:
        out = subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL).decode()
    except Exception:
        return None
    return out.strip() if strip else out


def _porcelain_paths(out: str | None) -> list[str]:
    """Paths from `git status --porcelain -z`: records are 'XY <path>' separated by NUL; a rename or copy (X in R/C)
    is followed by one extra record holding the original path, which is skipped. -z output is never quoted."""
    records = (out or "").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(records):
        rec = records[i]
        i += 1
        if len(rec) < 4:            # trailing empty record
            continue
        status, path = rec[:2], rec[3:]
        paths.append(path)
        if status[0] in "RC":
            i += 1
    return paths


def git_info() -> dict:
    """Full commit, branch and the list of uncommitted code files (names only)."""
    sha = _git("rev-parse", "HEAD")
    changed = _porcelain_paths(_git("status", "--porcelain", "-z", "--", *CODE_PATHS, strip=False))
    return {"commit": sha, "commit_short": sha[:7] if sha else None, "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "describe": _git("describe", "--tags", "--always", "--dirty"), "uncommitted_code_files": changed,
            "reproducible_from_commit": bool(sha) and not changed}


def environment_info() -> dict:
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.platform(terse=True), "packages": package_versions(), "argv0": Path(sys.argv[0]).name}


def guard_output_dir(out: Path, data_dir: Path, overwrite: bool) -> None:
    """Raise ValueError when writing to `out` could overwrite input data or a previous run's results.

    Refused: the input folder itself, a folder that contains it, anything under its raw/ files, and a non-empty
    folder unless `overwrite` (e.g. reports/v0.3, whose REPORT.md is the tracked v0.3 baseline).
    """
    out_r, data_r = Path(out).resolve(), Path(data_dir).resolve()
    if out_r == data_r or out_r in data_r.parents:
        raise ValueError(f"출력 폴더가 입력 데이터 폴더와 같거나 그것을 포함합니다: {out}")
    if data_r in out_r.parents and out_r.relative_to(data_r).parts[0] == "raw":
        raise ValueError(f"출력 폴더가 원본 일봉 폴더(raw/) 안에 있습니다: {out}")
    if out_r.exists() and any(out_r.iterdir()) and not overwrite:
        raise ValueError(f"출력 폴더에 이전 결과가 있습니다: {out} (새 폴더를 쓰거나 --overwrite)")


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def output_inventory(out_dir: Path, skip: tuple[str, ...] = ()) -> list[dict]:
    """Relative path, size and SHA-256 of every file a run wrote (the manifest itself is skipped)."""
    out = []
    for p in sorted(Path(out_dir).rglob("*")):
        if p.is_file() and p.name not in skip:
            out.append({"path": str(p.relative_to(out_dir)), "bytes": p.stat().st_size, "sha256": file_sha256(p)})
    return out


def frame_fingerprint(df: pd.DataFrame, cols: list[str]) -> str | None:
    """Order-sensitive hash of selected columns. Same input rows -> same fingerprint on any machine."""
    use = [c for c in cols if c in df.columns]
    if df.empty or not use:
        return None
    return hashlib.sha256(pd.util.hash_pandas_object(df[use], index=False).values.tobytes()).hexdigest()[:16]


@contextmanager
def capture_warnings() -> Iterator[list[dict]]:
    """Record warnings raised during a run (category, message, where) so the manifest can report them.

    Code inside the block that silences warnings with its own catch_warnings (e.g. the ML fit) is not counted.
    """
    seen: list[dict] = []
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        yield seen
    agg: dict[tuple[str, str, str], int] = {}
    for w in rec:
        key = (w.category.__name__, str(w.message)[:200], f"{Path(w.filename).name}:{w.lineno}")
        agg[key] = agg.get(key, 0) + 1
    seen.extend({"category": c, "message": m, "where": loc, "count": n} for (c, m, loc), n in sorted(agg.items()))
