"""scripts/compare_baseline.py: table comparison rules (the two-run end-to-end needs git history and is run by hand)."""
import importlib.util
import json
from pathlib import Path

import pandas as pd

spec = importlib.util.spec_from_file_location("compare_baseline", Path(__file__).resolve().parents[1] / "scripts" / "compare_baseline.py")
cb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cb)


def _run_dir(path: Path, tables: dict, manifest: dict) -> Path:
    path.mkdir(parents=True)
    for name, df in tables.items():
        df.to_csv(path / name, index=False)
    (path / "run_manifest.json").write_text(json.dumps(manifest))
    return path


M = {"data_fingerprint": "f", "chosen_exit_on_dev": {"A": "x"}, "sample": {"bars": 1}, "configs_tested_per_strategy": 48}


def test_new_columns_and_float_noise_are_not_differences(tmp_path):
    g = pd.DataFrame({"n": [1, 2], "mean": [0.1, 0.2]})
    a = _run_dir(tmp_path / "a", {"grid.csv": g, "sig.csv": g, "ml.csv": g}, M)
    b = _run_dir(tmp_path / "b", {"grid.csv": g, "sig.csv": g.assign(exit_date="2024-01-01"),
                                  "ml.csv": g.assign(mean=g["mean"] + 2e-16)}, M)
    r = cb.compare_outputs(a, b)
    assert r["same_results"] and r["identical"] == ["grid.csv"]
    assert r["identical_plus_new_columns"] == {"sig.csv": ["exit_date"]} and "ml.csv" in r["numerically_equal"]


def test_real_differences_and_missing_tables_are_reported(tmp_path):
    g = pd.DataFrame({"n": [1, 2], "mean": [0.1, 0.2]})
    a = _run_dir(tmp_path / "a", {"grid.csv": g, "gone.csv": g}, M)
    b = _run_dir(tmp_path / "b", {"grid.csv": g.assign(mean=[0.1, 0.3])}, {**M, "data_fingerprint": "other"})
    r = cb.compare_outputs(a, b)
    assert not r["same_results"] and "grid.csv" in r["different"] and r["missing"] == ["gone.csv"]
    assert r["manifest"]["data_fingerprint"] is False
