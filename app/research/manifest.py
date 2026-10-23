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


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None


def git_info() -> dict:
    """Full commit, branch and the list of uncommitted code files (names only)."""
    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain", "--", *CODE_PATHS)
    changed = [line[3:] for line in dirty.splitlines()] if dirty else []
    return {"commit": sha, "commit_short": sha[:7] if sha else None, "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "describe": _git("describe", "--tags", "--always", "--dirty"), "uncommitted_code_files": changed,
            "reproducible_from_commit": bool(sha) and not changed}


def environment_info() -> dict:
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.platform(terse=True), "packages": package_versions(), "argv0": Path(sys.argv[0]).name}


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
