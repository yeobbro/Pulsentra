"""Experiment ledger: pre-registration, append-only history, integrity checks and the CLI."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.ledger.__main__ import main as cli
from app.research.ledger import (REQUIRED_FIELDS, Ledger, LedgerCorrupt, LedgerError, iter_events, spec_hash, template,
                                 verify)

ROOT = Path(__file__).resolve().parents[1]


def spec(eid="EXP-20261025-mom60", **over):
    s = {"id": eid, "title": "60-day momentum", "hypothesis": "higher 60d return -> higher next 20d return",
         "rationale": "under-reaction", "family": "momentum", "universe": "KOSPI+KOSDAQ, turnover >= 1e9, price >= 1000",
         "data": "naver_raw data/real", "periods": {"dev": ["2016-01-01", "2021-12-31"], "validation": ["2022-01-01", "2023-12-31"],
                                                    "test": ["2024-01-01", "2099-12-31"]},
         "primary_metrics": ["rank_ic_mean"], "decision_criteria": "gate PASS on validation, then test once",
         "cost_assumptions": "commission 1.5bp + slippage 10bp per side, sell tax 20bp", "search_space": {"lookback": [20, 60, 120]},
         "planned_experiments": 3}
    s.update(over)
    return s


def manifest(tmp_path: Path, **over) -> Path:
    m = {"manifest_version": 2, "git": {"commit": "a" * 40, "reproducible_from_commit": True, "uncommitted_code_files": []},
         "data_fingerprint": "abcd1234abcd1234", "config": {"x": 1}, "cost_assumptions": {"commission_bps": 1.5},
         "configs_tested_per_strategy": 3, "experiments_total": 3, "validity": {"status": "PROVISIONAL"},
         "outputs": [{"path": "a.csv", "sha256": "f" * 64}]}
    m.update(over)
    p = tmp_path / "run_manifest.json"
    p.write_text(json.dumps(m), encoding="utf-8")
    return p


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "ledger" / "ledger.jsonl")


def test_register_and_show(led):
    ev = led.register(spec())
    assert ev["seq"] == 1 and ev["prev_hash"] is None and ev["spec_hash"] == spec_hash(ev["spec"])
    st = led.experiment("EXP-20261025-mom60")
    assert st["version"] == 1 and st["spec"]["family"] == "momentum" and st["registered_at"]
    assert verify(led.path).ok


@pytest.mark.parametrize("field", list(REQUIRED_FIELDS))
def test_required_fields(led, field):
    s = spec()
    del s[field]
    with pytest.raises(LedgerError, match=field):
        led.register(s)
    assert not led.path.exists() or led.path.read_text() == ""


def test_bad_values_and_template_placeholders(led):
    for bad in (spec(planned_experiments=0), spec(primary_metrics="ic"), spec(periods={"dev": ["2016-01-01"]}),
                spec(surprise=1), spec(hypothesis="")):
        with pytest.raises(LedgerError):
            led.register(bad)
    with pytest.raises(LedgerError):
        led.register(template())
    t = {**template(), "id": "EXP-real-id"}
    with pytest.raises(LedgerError, match="placeholders"):
        led.register(t)
    with pytest.raises(LedgerError, match="id"):
        led.register(spec(eid="x"))


def test_duplicate_id_refused_and_nothing_overwritten(led):
    led.register(spec())
    before = led.path.read_bytes()
    with pytest.raises(LedgerError, match="registered twice"):
        led.register(spec(title="other"))
    assert led.path.read_bytes() == before
    assert led.experiment("EXP-20261025-mom60")["spec"]["title"] == "60-day momentum"


def test_amendment_keeps_old_version_and_reason(led):
    led.register(spec())
    v1 = led.experiment("EXP-20261025-mom60")["spec_hash"]
    with pytest.raises(LedgerError, match="reason"):
        led.amend("EXP-20261025-mom60", {"title": "x"}, "")
    with pytest.raises(LedgerError, match="changes nothing"):
        led.amend("EXP-20261025-mom60", {"title": "60-day momentum"}, "noop")
    ev = led.amend("EXP-20261025-mom60", {"search_space": {"lookback": [60]}, "planned_experiments": 1}, "narrowed before any run")
    assert ev["version"] == 2 and ev["previous_spec_hash"] == v1 and ev["after_results"] is False
    assert ev["changed_fields"] == ["planned_experiments", "search_space"]
    evs = list(iter_events(led.path))
    assert evs[0]["spec"]["search_space"] == {"lookback": [20, 60, 120]}       # version 1 still in the file
    st = led.experiment("EXP-20261025-mom60")
    assert st["version"] == 2 and st["amendments"][0]["reason"] == "narrowed before any run"


def test_amendment_after_results_is_flagged(led):
    led.register(spec())
    led.start_run("EXP-20261025-mom60", "r1")
    with pytest.raises(LedgerError, match="open"):
        led.amend("EXP-20261025-mom60", {"title": "t"}, "during run")
    led.finish_run("EXP-20261025-mom60", "r1", "success")
    ev = led.amend("EXP-20261025-mom60", {"planned_experiments": 9}, "added configs after seeing results")
    assert ev["after_results"] is True
    from app.research.ledger import summary
    assert summary(led.experiment("EXP-20261025-mom60"))["amended_after_results"] is True


def test_runs_order_and_failures_kept(led):
    eid = "EXP-20261025-mom60"
    led.register(spec())
    with pytest.raises(LedgerError, match="not running"):
        led.finish_run(eid, "r1", "success")
    led.start_run(eid, "r1", "python -m app.factorlab run ...")
    with pytest.raises(LedgerError, match="open"):
        led.start_run(eid, "r2")                                # one open run per experiment
    led.finish_run(eid, "r1", "failure", "KeyError: close")
    with pytest.raises(LedgerError, match="reused"):
        led.start_run(eid, "r1")
    led.start_run(eid, "r2")
    led.finish_run(eid, "r2", "success")
    st = led.experiment(eid)
    assert st["runs"]["r1"]["status"] == "failure" and st["runs"]["r1"]["error"] == "KeyError: close"
    assert st["runs"]["r2"]["status"] == "success"
    types = [e["type"] for e in iter_events(led.path)]
    assert types == ["registered", "run_started", "run_finished", "run_started", "run_finished"]
    assert [e["seq"] for e in iter_events(led.path)] == [1, 2, 3, 4, 5]
    with pytest.raises(LedgerError):
        led.finish_run(eid, "r2", "maybe")


def test_manifest_link_records_only_real_fields(led, tmp_path):
    eid = "EXP-20261025-mom60"
    led.register(spec())
    led.start_run(eid, "r1"); led.finish_run(eid, "r1", "success")
    p = manifest(tmp_path)
    ev = led.link_manifest(eid, p, run_id="r1")
    f = ev["facts"]
    assert f["git_commit"] == "a" * 40 and f["data_fingerprint"] == "abcd1234abcd1234" and f["experiments_total"] == 3
    assert f["validity_status"] == "PROVISIONAL" and f["config_sha256"] and f["outputs"][0]["path"] == "a.csv"
    assert f["kind"] is None and f["primary_metrics"] is None          # absent in the manifest -> not invented
    with pytest.raises(LedgerError, match="not found"):
        led.link_manifest(eid, tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(LedgerError, match="not valid JSON"):
        led.link_manifest(eid, tmp_path / "bad.json")
    with pytest.raises(LedgerError, match="unknown run"):
        led.link_manifest(eid, p, run_id="r9")
    with pytest.raises(LedgerError, match="unknown experiment"):
        led.link_manifest("EXP-nope", p)


def test_decisions(led, tmp_path):
    eid = "EXP-20261025-mom60"
    led.register(spec())
    with pytest.raises(LedgerError, match="accept needs"):
        led.decide(eid, "accept", "looks good")
    with pytest.raises(LedgerError, match="reason"):
        led.decide(eid, "reject", "  ")
    with pytest.raises(LedgerError):
        led.decide(eid, "maybe", "x")
    led.decide(eid, "hold", "no run yet")
    led.start_run(eid, "r1"); led.finish_run(eid, "r1", "success")
    led.link_manifest(eid, manifest(tmp_path), "r1")
    led.decide(eid, "accept", "gate PASS", ["data/robustness/x/gate.json"])
    st = led.experiment(eid)
    assert [d["decision"] for d in st["decisions"]] == ["hold", "accept"]


def test_artifact_link(led, tmp_path):
    led.register(spec())
    f = tmp_path / "factor_summary.csv"
    f.write_text("a,b\n1,2\n")
    ev = led.link_artifact("EXP-20261025-mom60", f, "factor_summary")
    assert ev["bytes"] == f.stat().st_size and len(ev["sha256"]) == 64
    with pytest.raises(LedgerError):
        led.link_artifact("EXP-20261025-mom60", tmp_path / "nope.csv", "x")


def _two_events(led):
    led.register(spec())
    led.register(spec(eid="EXP-20261025-rev5", family="reversal"))
    return led.path.read_text().splitlines()


def test_verify_detects_edit(led):
    lines = _two_events(led)
    ev = json.loads(lines[0]); ev["spec"]["title"] = "edited"
    led.path.write_text(json.dumps(ev) + "\n" + lines[1] + "\n")
    rep = verify(led.path)
    assert not rep.ok and any("hash mismatch" in p for p in rep.problems)
    with pytest.raises(LedgerCorrupt):
        led.decide("EXP-20261025-mom60", "hold", "x")        # no append to a ledger that does not verify
    with pytest.raises(LedgerCorrupt):
        led.state()


def test_verify_detects_deletion_and_reorder(led):
    lines = _two_events(led)
    led.path.write_text(lines[1] + "\n")
    assert not verify(led.path).ok
    led.path.write_text(lines[1] + "\n" + lines[0] + "\n")
    assert not verify(led.path).ok


def test_verify_detects_garbage_and_torn_tail(led):
    lines = _two_events(led)
    led.path.write_text(lines[0] + "\nnot json\n")
    rep = verify(led.path)
    assert not rep.ok and any("not valid JSON" in p for p in rep.problems)
    led.path.write_text(lines[0] + "\n" + lines[1][:20])
    rep = verify(led.path)
    assert not rep.ok and rep.torn_tail and rep.events == 1
    with pytest.raises(LedgerCorrupt):
        led.register(spec(eid="EXP-third"))


def test_verify_detects_forged_event_with_valid_hash(led):
    """A forged line that is re-hashed correctly still fails the transition rules (e.g. accept without a run)."""
    from app.research.ledger import _event_hash, canonical
    lines = _two_events(led)
    last = json.loads(lines[-1])
    forged = {"ledger_version": 1, "seq": 3, "at": "2026-10-25T00:00:00+00:00", "prev_hash": last["hash"], "type": "decision",
              "experiment_id": "EXP-20261025-mom60", "decision": "accept", "reason": "forged", "evidence": []}
    forged["hash"] = _event_hash(forged)
    led.path.write_text("\n".join(lines) + "\n" + canonical(forged) + "\n")
    rep = verify(led.path)
    assert not rep.ok and any("accept needs" in p for p in rep.problems)


def test_no_delete_or_reset_api():
    names = {n for n in dir(Ledger) if not n.startswith("_")}
    assert not names & {"delete", "remove", "reset", "clear", "rewrite", "truncate", "overwrite"}


def test_cli_flow(tmp_path, capsys):
    L = str(tmp_path / "l.jsonl")
    sp = tmp_path / "s.json"; sp.write_text(json.dumps(spec()))
    assert cli(["--ledger", L, "register", str(sp)]) == 0
    assert cli(["--ledger", L, "register", str(sp)]) == 2                       # duplicate
    assert cli(["--ledger", L, "register", str(tmp_path / "missing.json")]) == 2
    assert cli(["--ledger", L, "show", "EXP-unknown"]) == 2
    ch = tmp_path / "c.json"; ch.write_text(json.dumps({"planned_experiments": 1}))
    assert cli(["--ledger", L, "amend", "EXP-20261025-mom60", str(ch), "--reason", "narrow"]) == 0
    assert cli(["--ledger", L, "run-start", "EXP-20261025-mom60", "r1"]) == 0
    assert cli(["--ledger", L, "run-finish", "EXP-20261025-mom60", "r1", "--status", "success"]) == 0
    mp = manifest(tmp_path)
    assert cli(["--ledger", L, "link-manifest", "EXP-20261025-mom60", str(mp), "--run", "r1"]) == 0
    assert cli(["--ledger", L, "decide", "EXP-20261025-mom60", "hold", "--reason", "needs test period"]) == 0
    capsys.readouterr()
    assert cli(["--ledger", L, "list"]) == 0
    assert "EXP-20261025-mom60\tv2" in capsys.readouterr().out
    assert cli(["--ledger", L, "show", "EXP-20261025-mom60", "--events"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert len(shown["events"]) == 6 and shown["decisions"][0]["decision"] == "hold"
    assert cli(["--ledger", L, "verify", "--files"]) == 0
    mp.write_text(mp.read_text() + " ")                                          # linked file changed afterwards
    capsys.readouterr()
    assert cli(["--ledger", L, "verify", "--files"]) == 3
    assert "changed since linking" in capsys.readouterr().out
    Path(L).write_text(Path(L).read_text().replace("narrow", "NARROW"))
    assert cli(["--ledger", L, "verify"]) == 3
    assert cli(["--ledger", L, "list"]) == 3


def test_cli_help_runs():
    r = subprocess.run([sys.executable, "-m", "app.ledger", "--help"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "verify" in r.stdout and "delete" not in r.stdout.split("There is no")[0]
