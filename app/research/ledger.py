"""Experiment Ledger: pre-registration and an append-only event history for research experiments.

Storage: one JSON object per line (JSONL), like data/real/manifest.jsonl. Every event carries `seq`, the hash of the
previous event (`prev_hash`) and its own `hash` (SHA-256 of the canonical JSON of the event without `hash`), so
rewriting, reordering or deleting a past line breaks the chain and `verify()` reports it. Appends take an exclusive
file lock, write one line and fsync. There is no delete, reset or rewrite operation.

Why JSONL and not SQLite: the ledger is small, written rarely, must be readable and diffable by a person, and must
not share a database with the app store (data/*.sqlite3), which holds market data and can be reset by the app.

What the ledger does NOT do: it does not run experiments, compute results or judge them. It records what was
declared before a run, what ran, which manifest and files the run produced, and the decision a person made.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

LEDGER_VERSION = 1
DEFAULT_LEDGER = Path("data/ledger/ledger.jsonl")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")

# Spec fields. REQUIRED must be present and non-empty at registration; OPTIONAL are recorded when given.
# Each field is stored as declared; the ledger does not check that a declared rule is what the code actually runs —
# that is what linking the run manifest (and the robustness gate) is for.
REQUIRED_FIELDS: dict[str, str] = {
    "title": "short name",
    "hypothesis": "what is expected and in which direction",
    "rationale": "economic reason the effect should exist",
    "family": "strategy or factor family (e.g. momentum, reversal, volume)",
    "universe": "instruments and eligibility filters",
    "data": "data source and version (e.g. naver_raw data/real)",
    "periods": "dev / validation / test date ranges",
    "primary_metrics": "metrics declared before the run, in priority order",
    "decision_criteria": "what counts as accept / hold / reject",
    "cost_assumptions": "commission, sell tax, slippage and whether per side or round trip",
    "search_space": "parameters and values that will be tried",
    "planned_experiments": "number of configurations that will be tried",
}
OPTIONAL_FIELDS: dict[str, str] = {
    "data_fingerprint": "fingerprint of the data the run must use (checked against the run manifest)",
    "signal_rule": "signal definition", "entry_rule": "entry timing", "exit_rule": "exit rule",
    "benchmark": "benchmark the result is compared with",
    "gate": "robustness gate thresholds declared before the run",
    "parent_id": "earlier experiment this one follows", "hypothesis_family_id": "group of experiments testing one idea",
    "notes": "anything else",
}
SPEC_FIELDS = (*REQUIRED_FIELDS, *OPTIONAL_FIELDS)
DECISIONS = ("accept", "hold", "reject")
RUN_STATUSES = ("success", "failure")
EVENT_TYPES = ("registered", "amended", "run_started", "run_finished", "manifest_linked", "artifact_linked", "decision")


class LedgerError(RuntimeError):
    """Invalid request (unknown id, missing field, forbidden transition) — nothing was written."""


class LedgerCorrupt(LedgerError):
    """The file does not verify; no event is appended to a ledger that is not intact."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_hash(spec: dict) -> str:
    return sha256_text(canonical(spec))


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _event_hash(ev: dict) -> str:
    return sha256_text(canonical({k: v for k, v in ev.items() if k != "hash"}))


def validate_spec(spec: Any) -> dict:
    """Return the spec as stored (only known fields). Raise LedgerError listing every problem."""
    if not isinstance(spec, dict):
        raise LedgerError("spec must be a JSON object")
    problems = []
    unknown = sorted(set(spec) - set(SPEC_FIELDS) - {"id"})
    if unknown:
        problems.append(f"unknown fields {unknown} (allowed: {', '.join(SPEC_FIELDS)})")
    for f in REQUIRED_FIELDS:
        v = spec.get(f)
        if v is None or (isinstance(v, (str, list, dict)) and len(v) == 0):
            problems.append(f"required field missing or empty: {f}")
    p = spec.get("periods")
    if isinstance(p, dict) and p:
        for name, rng in p.items():
            if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
                problems.append(f"periods.{name} must be [start, end]")
    elif p is not None and not isinstance(p, dict):
        problems.append("periods must be an object {name: [start, end]}")
    unfilled = sorted(k for k, v in spec.items() if k in SPEC_FIELDS and _has_placeholder(v))
    if unfilled:
        problems.append(f"template placeholders left in {unfilled}")
    pe = spec.get("planned_experiments")
    if pe is not None and not (isinstance(pe, int) and not isinstance(pe, bool) and pe >= 1):
        problems.append("planned_experiments must be an integer >= 1")
    pm = spec.get("primary_metrics")
    if pm is not None and not (isinstance(pm, list) and all(isinstance(x, str) and x for x in pm)):
        problems.append("primary_metrics must be a non-empty list of names")
    if problems:
        raise LedgerError("; ".join(problems))
    return {k: spec[k] for k in SPEC_FIELDS if k in spec}


def _has_placeholder(v: Any) -> bool:
    if isinstance(v, str):
        return v.startswith("<") and v.endswith(">")
    if isinstance(v, dict):
        return any(_has_placeholder(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_placeholder(x) for x in v)
    return False


def template() -> dict:
    """A spec skeleton listing every field with its meaning (fill in, then register)."""
    return {"id": "EXP-YYYYMMDD-short-name", **{k: f"<{v}>" for k, v in REQUIRED_FIELDS.items()},
            "periods": {"dev": ["2016-01-01", "2021-12-31"], "validation": ["2022-01-01", "2023-12-31"], "test": ["2024-01-01", "2099-12-31"]},
            "primary_metrics": ["rank_ic_mean"], "planned_experiments": 1,
            **{k: f"<optional: {v}>" for k, v in OPTIONAL_FIELDS.items()}}


# ---------------------------------------------------------------------------------------------------- reading

@dataclass
class VerifyReport:
    ok: bool
    events: int
    problems: list[str] = field(default_factory=list)
    torn_tail: bool = False                   # last line incomplete (crash during a write)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "events": self.events, "problems": self.problems, "torn_tail": self.torn_tail}


def _read_lines(path: Path) -> tuple[list[str], bool]:
    if not path.exists():
        return [], False
    data = path.read_bytes()
    torn = bool(data) and not data.endswith(b"\n")
    return data.decode("utf-8").split("\n")[:-1] if not torn else data.decode("utf-8").split("\n"), torn


def verify(path: Path) -> VerifyReport:
    """Check the hash chain, sequence numbers, event types and per-experiment state transitions."""
    path = Path(path)
    lines, torn = _read_lines(path)
    problems: list[str] = []
    if torn:
        problems.append("last line is incomplete (write interrupted); the ledger needs manual repair before new events")
        lines = lines[:-1]
    prev, state = None, {}
    n = 0
    for i, line in enumerate(lines, 1):
        try:
            ev = json.loads(line)
        except ValueError:
            problems.append(f"line {i}: not valid JSON")
            continue
        n += 1
        if not isinstance(ev, dict):
            problems.append(f"line {i}: not an object")
            continue
        if ev.get("seq") != i:
            problems.append(f"line {i}: seq {ev.get('seq')} (expected {i})")
        if ev.get("prev_hash") != prev:
            problems.append(f"line {i}: prev_hash does not match the previous event")
        if ev.get("hash") != _event_hash(ev):
            problems.append(f"line {i}: hash mismatch (event edited)")
        prev = ev.get("hash")
        err = _apply(state, ev)
        if err:
            problems.append(f"line {i}: {err}")
    return VerifyReport(ok=not problems, events=n, problems=problems, torn_tail=torn)


def _apply(state: dict, ev: dict) -> str | None:
    """Advance the per-experiment state; return a message when the event is not a legal transition."""
    t, eid = ev.get("type"), ev.get("experiment_id")
    if t not in EVENT_TYPES:
        return f"unknown event type {t!r}"
    st = state.get(eid)
    if t == "registered":
        if st is not None:
            return f"experiment {eid} registered twice"
        if ev.get("spec_hash") != spec_hash(ev.get("spec") or {}):
            return f"{eid}: spec_hash does not match the stored spec"
        state[eid] = {"version": 1, "spec": ev.get("spec"), "spec_hash": ev.get("spec_hash"), "registered_seq": ev.get("seq"),
                      "registered_at": ev.get("at"), "runs": {}, "open_run": None, "results_seen": False,
                      "amendments": [], "manifests": [], "artifacts": [], "decisions": []}
        return None
    if st is None:
        return f"{t} for unregistered experiment {eid}"
    if t == "amended":
        if ev.get("version") != st["version"] + 1:
            return f"{eid}: amendment version {ev.get('version')} (expected {st['version'] + 1})"
        if ev.get("previous_spec_hash") != st["spec_hash"]:
            return f"{eid}: amendment does not follow the current spec"
        if ev.get("spec_hash") != spec_hash(ev.get("spec") or {}):
            return f"{eid}: spec_hash does not match the stored spec"
        st["amendments"].append({"seq": ev.get("seq"), "version": ev.get("version"), "reason": ev.get("reason"),
                                 "after_results": bool(ev.get("after_results")), "changed_fields": ev.get("changed_fields")})
        st.update(version=ev["version"], spec=ev["spec"], spec_hash=ev["spec_hash"])
        return None
    if t == "run_started":
        if st["open_run"] is not None:
            return f"{eid}: run {ev.get('run_id')} started while run {st['open_run']} is open"
        if ev.get("run_id") in st["runs"]:
            return f"{eid}: run id {ev.get('run_id')} reused"
        if ev.get("spec_version") != st["version"] or ev.get("spec_hash") != st["spec_hash"]:
            return f"{eid}: run started against a spec that is not the current version"
        st["runs"][ev["run_id"]] = {"started_seq": ev.get("seq"), "spec_version": ev.get("spec_version"),
                                    "spec_hash": ev.get("spec_hash"), "status": "running"}
        st["open_run"] = ev["run_id"]
        return None
    if t == "run_finished":
        r = st["runs"].get(ev.get("run_id"))
        if r is None or r["status"] != "running":
            return f"{eid}: run_finished for a run that is not running"
        if ev.get("status") not in RUN_STATUSES:
            return f"{eid}: run status {ev.get('status')!r}"
        r.update(status=ev["status"], finished_seq=ev.get("seq"), error=ev.get("error"))
        st["open_run"] = None
        st["results_seen"] = True
        return None
    if t in ("manifest_linked", "artifact_linked"):
        rid = ev.get("run_id")
        if rid is not None and rid not in st["runs"]:
            return f"{eid}: {t} names unknown run {rid}"
        (st["manifests"] if t == "manifest_linked" else st["artifacts"]).append(ev)
        return None
    if t == "decision":
        if ev.get("decision") not in DECISIONS:
            return f"{eid}: decision {ev.get('decision')!r}"
        if not str(ev.get("reason") or "").strip():
            return f"{eid}: decision without a reason"
        if ev["decision"] == "accept" and not (any(r["status"] == "success" for r in st["runs"].values()) and st["manifests"]):
            return f"{eid}: accept needs a successful run and a linked manifest"
        st["decisions"].append({"seq": ev.get("seq"), "decision": ev["decision"], "reason": ev.get("reason"), "at": ev.get("at")})
        return None
    return None


def load_state(path: Path) -> dict:
    """Experiment states replayed from a ledger that verifies. Raises LedgerCorrupt otherwise."""
    rep = verify(path)
    if not rep.ok:
        raise LedgerCorrupt("ledger does not verify: " + "; ".join(rep.problems[:5]))
    state: dict = {}
    for ev in iter_events(path):
        _apply(state, ev)
    return state


def iter_events(path: Path) -> Iterable[dict]:
    lines, torn = _read_lines(Path(path))
    for line in lines[:-1] if torn else lines:
        yield json.loads(line)


# ---------------------------------------------------------------------------------------------------- writing

class Ledger:
    def __init__(self, path: Path | str = DEFAULT_LEDGER) -> None:
        self.path = Path(path)

    # every write goes through here: lock, verify the file as it is, check the transition, append one line, fsync
    def _append(self, ev: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            rep = verify(self.path)
            if not rep.ok:
                raise LedgerCorrupt("ledger does not verify; nothing appended: " + "; ".join(rep.problems[:5]))
            events = list(iter_events(self.path))
            state: dict = {}
            for old in events:
                _apply(state, old)
            ev = {"ledger_version": LEDGER_VERSION, "seq": len(events) + 1, "at": utc_now(),
                  "prev_hash": events[-1]["hash"] if events else None, **ev}
            err = _apply(state, ev)
            if err:
                raise LedgerError(err)
            ev["hash"] = _event_hash(ev)
            os.write(fd, (canonical(ev) + "\n").encode("utf-8"))
            os.fsync(fd)
            return ev
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def state(self) -> dict:
        return load_state(self.path) if self.path.exists() else {}

    def experiment(self, eid: str) -> dict:
        st = self.state().get(eid)
        if st is None:
            raise LedgerError(f"unknown experiment id {eid!r}")
        return st

    def register(self, spec: dict) -> dict:
        eid = spec.get("id") if isinstance(spec, dict) else None
        if not isinstance(eid, str) or not ID_RE.match(eid):
            raise LedgerError("id must be 3-64 characters: letters, digits, '.', '_', '-' (starting with a letter or digit)")
        if "YYYYMMDD" in eid:
            raise LedgerError("replace the template id with a real one")
        clean = validate_spec(spec)
        return self._append({"type": "registered", "experiment_id": eid, "version": 1, "spec": clean, "spec_hash": spec_hash(clean)})

    def amend(self, eid: str, changes: dict, reason: str) -> dict:
        """New spec version = current spec updated with `changes`. The old version stays in the ledger."""
        if not str(reason or "").strip():
            raise LedgerError("an amendment needs a reason")
        st = self.experiment(eid)
        if st["open_run"] is not None:
            raise LedgerError(f"{eid}: cannot amend while run {st['open_run']} is open")
        if not isinstance(changes, dict) or not changes:
            raise LedgerError("changes must be a non-empty object")
        new = validate_spec({**st["spec"], **{k: v for k, v in changes.items() if k != "id"}})
        changed = sorted(k for k in new if st["spec"].get(k) != new.get(k))
        if not changed:
            raise LedgerError("the amendment changes nothing")
        return self._append({"type": "amended", "experiment_id": eid, "version": st["version"] + 1, "spec": new,
                             "spec_hash": spec_hash(new), "previous_spec_hash": st["spec_hash"], "changed_fields": changed,
                             "reason": reason, "after_results": st["results_seen"]})

    def start_run(self, eid: str, run_id: str, command: str = "") -> dict:
        st = self.experiment(eid)
        return self._append({"type": "run_started", "experiment_id": eid, "run_id": run_id, "spec_version": st["version"],
                             "spec_hash": st["spec_hash"], "command": command})

    def finish_run(self, eid: str, run_id: str, status: str, error: str | None = None) -> dict:
        if status not in RUN_STATUSES:
            raise LedgerError(f"status must be one of {RUN_STATUSES}")
        return self._append({"type": "run_finished", "experiment_id": eid, "run_id": run_id, "status": status,
                             "error": (str(error)[:500] if error else None)})

    def link_manifest(self, eid: str, manifest_path: Path, run_id: str | None = None) -> dict:
        """Record facts read from a run_manifest.json (only fields the manifest really has) plus its SHA-256."""
        p = Path(manifest_path)
        if not p.is_file():
            raise LedgerError(f"manifest not found: {p}")
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LedgerError(f"manifest is not valid JSON: {p}") from exc
        self.experiment(eid)
        return self._append({"type": "manifest_linked", "experiment_id": eid, "run_id": run_id, "path": str(p),
                             "sha256": file_sha256(p), "facts": manifest_facts(m)})

    def link_artifact(self, eid: str, artifact_path: Path, kind: str, run_id: str | None = None) -> dict:
        p = Path(artifact_path)
        if not p.is_file():
            raise LedgerError(f"artifact not found: {p}")
        self.experiment(eid)
        return self._append({"type": "artifact_linked", "experiment_id": eid, "run_id": run_id, "kind": kind, "path": str(p),
                             "sha256": file_sha256(p), "bytes": p.stat().st_size})

    def decide(self, eid: str, decision: str, reason: str, evidence: list[str] | None = None) -> dict:
        if decision not in DECISIONS:
            raise LedgerError(f"decision must be one of {DECISIONS}")
        if not str(reason or "").strip():
            raise LedgerError("a decision needs a reason")
        return self._append({"type": "decision", "experiment_id": eid, "decision": decision, "reason": reason,
                             "evidence": evidence or []})


def manifest_facts(m: dict) -> dict:
    """The run facts a ledger keeps from a manifest. Missing keys stay missing (None); nothing is inferred."""
    git = m.get("git") or {}
    validity = m.get("validity") or {}
    return {
        "manifest_version": m.get("manifest_version"), "kind": m.get("kind"),
        "git_commit": git.get("commit") or m.get("git_commit"),
        "reproducible_from_commit": git.get("reproducible_from_commit"),
        "uncommitted_code_files": git.get("uncommitted_code_files"),
        "data_fingerprint": m.get("data_fingerprint"),
        "config_sha256": sha256_text(canonical(m["config"])) if "config" in m else None,
        "cost_assumptions": m.get("cost_assumptions"),
        "configs_tested_per_strategy": m.get("configs_tested_per_strategy"),
        "experiments_total": m.get("experiments_total"),
        "validity_status": validity.get("status"),
        "outputs": [{"path": o.get("path"), "sha256": o.get("sha256")} for o in (m.get("outputs") or [])],
        "primary_metrics": m.get("primary_metrics"),
        "started_utc": m.get("started_utc"), "finished_utc": m.get("finished_utc"),
        "ledger_experiment": m.get("ledger_experiment"),
        "data_eligibility": None if not isinstance(m.get("data_eligibility"), dict) else {
            k: m["data_eligibility"].get(k) for k in ("rules_version", "status", "label", "data_scope", "real_data_verified", "assessed_utc")},
    }


def summary(st: dict) -> dict:
    """One line per experiment for listings."""
    runs = st["runs"].values()
    return {"version": st["version"], "title": st["spec"].get("title"), "family": st["spec"].get("family"),
            "registered_at": st["registered_at"], "runs": len(st["runs"]),
            "successful_runs": sum(r["status"] == "success" for r in runs),
            "failed_runs": sum(r["status"] == "failure" for r in runs), "open_run": st["open_run"],
            "amended_after_results": any(a["after_results"] for a in st["amendments"]),
            "planned_experiments": st["spec"].get("planned_experiments"),
            "experiments_total_reported": [m["facts"].get("experiments_total") for m in st["manifests"]],
            "decision": st["decisions"][-1]["decision"] if st["decisions"] else None}
