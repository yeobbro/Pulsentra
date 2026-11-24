"""Regression: `git_info()` must record uncommitted code paths exactly (no first character lost)."""
import subprocess

import pytest

from app.research import manifest

GIT_INFO_KEYS = {"commit", "commit_short", "branch", "describe", "uncommitted_code_files", "reproducible_from_commit"}


def _run(repo, *args):
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
                   cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    (tmp_path / "app" / "factorlab").mkdir(parents=True)
    (tmp_path / "app" / "factorlab" / "__main__.py").write_text("x = 1\n")
    (tmp_path / "app" / "with space.py").write_text("y = 1\n")
    (tmp_path / "README.md").write_text("docs\n")
    _run(tmp_path, "init", "-q")
    _run(tmp_path, "add", "-A")
    _run(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.setattr(manifest, "ROOT", tmp_path)
    return tmp_path


def test_clean_tree_contract(repo):
    g = manifest.git_info()
    assert set(g) == GIT_INFO_KEYS
    assert g["uncommitted_code_files"] == [] and g["reproducible_from_commit"] is True and len(g["commit"]) == 40


def test_modified_first_entry_keeps_its_first_character(repo):
    # porcelain line is " M app/factorlab/__main__.py"; the old code stripped the leading space, then cut 3 chars
    (repo / "app" / "factorlab" / "__main__.py").write_text("x = 2\n")
    (repo / "app" / "example.py").write_text("new\n")                   # untracked
    (repo / "README.md").write_text("docs changed\n")                   # outside CODE_PATHS: not listed
    g = manifest.git_info()
    files = g["uncommitted_code_files"]
    assert "pp/factorlab/__main__.py" not in files
    assert files == ["app/factorlab/__main__.py", "app/example.py"]
    assert set(g) == GIT_INFO_KEYS and g["reproducible_from_commit"] is False


def test_staged_and_space_and_rename_paths(repo):
    (repo / "app" / "with space.py").write_text("y = 2\n")
    _run(repo, "mv", "app/factorlab/__main__.py", "app/factorlab/main2.py")
    files = manifest.git_info()["uncommitted_code_files"]
    assert sorted(files) == ["app/factorlab/main2.py", "app/with space.py"]


def test_parser_on_literal_porcelain_output():
    out = " M app/a.py\0?? app/example.py\0M  app/b c.py\0R  app/new.py\0app/old.py\0"
    assert manifest._porcelain_paths(out) == ["app/a.py", "app/example.py", "app/b c.py", "app/new.py"]
    assert manifest._porcelain_paths("") == [] and manifest._porcelain_paths(None) == []
