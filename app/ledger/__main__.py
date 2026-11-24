"""CLI: python -m app.ledger [--ledger data/ledger/ledger.jsonl] <command> ...

  template                          print a spec skeleton (every field and its meaning)
  register SPEC.json                pre-register an experiment (the spec must contain "id")
  list                              one line per experiment
  show ID [--events]                current spec, versions, runs, links and decisions
  amend ID CHANGES.json --reason R  new spec version; the old version stays in the ledger
  run-start ID RUN_ID [--command C] / run-finish ID RUN_ID --status success|failure [--error E]
  link-manifest ID run_manifest.json [--run RUN_ID] / link-artifact ID FILE --kind K [--run RUN_ID]
  decide ID accept|hold|reject --reason R [--evidence PATH ...]
  verify [--files]                  check the hash chain and transitions (--files: re-hash linked files)

There is no delete, reset or rewrite command. Exit codes: 0 ok, 2 invalid request, 3 ledger does not verify.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.research.ledger import (DECISIONS, DEFAULT_LEDGER, RUN_STATUSES, Ledger, LedgerCorrupt, LedgerError,
                                 file_sha256, iter_events, summary, template, verify)


def _load_json(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise LedgerError(f"file not found: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LedgerError(f"not valid JSON: {p} ({exc})") from exc


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _verify_files(path: Path) -> list[str]:
    """Linked manifests/artifacts whose file is gone or whose SHA-256 changed since linking."""
    out = []
    for ev in iter_events(path):
        if ev.get("type") in ("manifest_linked", "artifact_linked"):
            p = Path(ev["path"])
            if not p.is_file():
                out.append(f"seq {ev['seq']}: linked file missing: {p}")
            elif file_sha256(p) != ev["sha256"]:
                out.append(f"seq {ev['seq']}: linked file changed since linking: {p}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ledger", description="Experiment ledger (append-only).")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help=f"ledger file (default {DEFAULT_LEDGER})")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("template", help="print a spec skeleton")
    s = sub.add_parser("register", help="pre-register an experiment"); s.add_argument("spec")
    sub.add_parser("list", help="list experiments")
    s = sub.add_parser("show", help="show one experiment"); s.add_argument("id"); s.add_argument("--events", action="store_true")
    s = sub.add_parser("amend", help="new spec version with a reason"); s.add_argument("id"); s.add_argument("changes")
    s.add_argument("--reason", required=True)
    s = sub.add_parser("run-start", help="record that a run started"); s.add_argument("id"); s.add_argument("run_id")
    s.add_argument("--command", default="")
    s = sub.add_parser("run-finish", help="record a run's outcome (failures are kept)"); s.add_argument("id"); s.add_argument("run_id")
    s.add_argument("--status", required=True, choices=RUN_STATUSES); s.add_argument("--error")
    s = sub.add_parser("link-manifest", help="link a run_manifest.json"); s.add_argument("id"); s.add_argument("manifest")
    s.add_argument("--run")
    s = sub.add_parser("link-artifact", help="link an output file"); s.add_argument("id"); s.add_argument("file")
    s.add_argument("--kind", required=True); s.add_argument("--run")
    s = sub.add_parser("decide", help="record a decision with its reason"); s.add_argument("id"); s.add_argument("decision", choices=DECISIONS)
    s.add_argument("--reason", required=True); s.add_argument("--evidence", nargs="*", default=[])
    s = sub.add_parser("verify", help="check ledger integrity"); s.add_argument("--files", action="store_true")
    a = ap.parse_args(argv)
    led = Ledger(a.ledger)
    try:
        if a.cmd == "template":
            _print(template())
        elif a.cmd == "register":
            ev = led.register(_load_json(a.spec)); print(f"registered {ev['experiment_id']} v1 spec_hash={ev['spec_hash'][:16]} seq={ev['seq']}")
        elif a.cmd == "list":
            st = led.state()
            if not st:
                print("(no experiments)")
            for eid, s_ in st.items():
                x = summary(s_)
                print(f"{eid}\tv{x['version']}\t{x['family']}\truns={x['runs']} ok={x['successful_runs']} fail={x['failed_runs']}"
                      f"\tdecision={x['decision']}\t{'AMENDED-AFTER-RESULTS ' if x['amended_after_results'] else ''}{x['title']}")
        elif a.cmd == "show":
            st = led.experiment(a.id)
            out = {"id": a.id, "summary": summary(st), "spec": st["spec"], "spec_hash": st["spec_hash"], "amendments": st["amendments"],
                   "runs": st["runs"], "manifests": [{k: m[k] for k in ("seq", "run_id", "path", "sha256", "facts")} for m in st["manifests"]],
                   "artifacts": [{k: m[k] for k in ("seq", "run_id", "kind", "path", "sha256")} for m in st["artifacts"]],
                   "decisions": st["decisions"]}
            if a.events:
                out["events"] = [e for e in iter_events(led.path) if e.get("experiment_id") == a.id]
            _print(out)
        elif a.cmd == "amend":
            ev = led.amend(a.id, _load_json(a.changes), a.reason)
            print(f"amended {a.id} -> v{ev['version']} fields={ev['changed_fields']} after_results={ev['after_results']}")
        elif a.cmd == "run-start":
            ev = led.start_run(a.id, a.run_id, a.command); print(f"run {a.run_id} started (spec v{ev['spec_version']}) seq={ev['seq']}")
        elif a.cmd == "run-finish":
            ev = led.finish_run(a.id, a.run_id, a.status, a.error); print(f"run {a.run_id} {a.status} seq={ev['seq']}")
        elif a.cmd == "link-manifest":
            ev = led.link_manifest(a.id, Path(a.manifest), a.run); print(f"manifest linked sha256={ev['sha256'][:16]} seq={ev['seq']}")
        elif a.cmd == "link-artifact":
            ev = led.link_artifact(a.id, Path(a.file), a.kind, a.run); print(f"artifact linked sha256={ev['sha256'][:16]} seq={ev['seq']}")
        elif a.cmd == "decide":
            ev = led.decide(a.id, a.decision, a.reason, a.evidence); print(f"decision {a.decision} recorded seq={ev['seq']}")
        elif a.cmd == "verify":
            rep = verify(led.path).as_dict()
            if a.files and rep["ok"]:
                rep["file_problems"] = _verify_files(led.path)
            rep["path"] = str(led.path); rep["exists"] = led.path.exists()
            _print(rep)
            return 0 if rep["ok"] and not rep.get("file_problems") else 3
    except LedgerCorrupt as exc:
        print(f"ledger error: {exc}", file=sys.stderr); return 3
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
