"""Local data and credentials stay out of Git: ignore rules, tracked files, example settings."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HAS_GIT = shutil.which("git") is not None and (ROOT / ".git").exists()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


@pytest.mark.skipif(not HAS_GIT, reason="not a git checkout")
@pytest.mark.parametrize("path", [".env", ".env.local", ".env.production", "data/real/raw/005930.csv", "data/real/universe.csv",
                                  "data/raw/x.csv", "data/cache/x.json", "data/audit/run/findings.csv", "data/crosscheck/run/a.csv",
                                  "data/market_signal_lab.sqlite3", "data/models/m.joblib", "local.db",
                                  "reports/v0.4/strategy_exit_grid.csv", "reports/v0.4/trades/portfolio_a.csv", "reports/x/a.parquet"])
def test_sensitive_paths_are_ignored(path):
    assert _git("check-ignore", "--no-index", path).strip() == path


@pytest.mark.skipif(not HAS_GIT, reason="not a git checkout")
def test_example_settings_are_not_ignored():
    assert _git("check-ignore", "--no-index", ".env.example").strip() == ""


@pytest.mark.skipif(not HAS_GIT, reason="not a git checkout")
def test_no_secrets_or_data_are_tracked():
    tracked = [p for p in _git("ls-files").splitlines() if p]
    assert ".env" not in tracked
    assert not [p for p in tracked if p.startswith("data/") and p != "data/.gitkeep"]
    assert not [p for p in tracked if re.search(r"\.(sqlite3|db|joblib|parquet)$", p)]
    big = [p for p in tracked if (ROOT / p).exists() and (ROOT / p).stat().st_size > 2_000_000]
    assert not big, big


def test_env_example_has_no_values_for_keys():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("KIS_APP_KEY", "KIS_APP_SECRET", "DART_API_KEY"):
        m = re.search(rf"^{key}=(.*)$", text, re.M)
        assert m and m.group(1).strip() == "", key
