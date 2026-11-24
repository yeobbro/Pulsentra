"""requirements.txt and pyproject.toml must declare the same runtime dependencies.

Vercel installs from pyproject.toml when it has a [project] table; Render and local setup use
requirements.txt. If the lists drift, one deployment boots without a module (e.g. joblib).
"""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _requirements() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def test_pyproject_dependencies_match_requirements_txt():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == _requirements()
