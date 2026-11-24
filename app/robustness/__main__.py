"""CLI: python -m app.robustness evaluate --factorlab DIR --factor NAME --out DIR
                                        [--ledger FILE --ledger-id ID] [--threshold name=value ...] [--overwrite]

Reads a Factor Lab output folder (and the ledger, read-only except for linking gate.json as an artifact when
--ledger-id is given). Writes gate.json and GATE.md. Exit code 0 when the gate was evaluated (whatever the status),
2 on invalid input, 3 when the ledger does not verify. The status itself is in gate.json, not in the exit code.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from app.research.ledger import Ledger, LedgerCorrupt, LedgerError
from app.research.robustness import GateConfig, evaluate, report_md


def _parse_thresholds(items: list[str], base: GateConfig) -> GateConfig:
    d = asdict(base)
    for it in items:
        if "=" not in it:
            raise ValueError(f"--threshold needs name=value, got {it!r}")
        k, v = it.split("=", 1)
        if k not in d:
            raise ValueError(f"unknown threshold {k!r} (known: {', '.join(d)})")
        d[k] = tuple(float(x) for x in v.split(",")) if k == "cost_stress_multipliers" else type(d[k])(float(v) if isinstance(d[k], float) else int(v))
    return GateConfig.from_dict(d)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.robustness", description="Robustness Gate MVP.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evaluate", help="evaluate one factor of a Factor Lab run")
    e.add_argument("--factorlab", required=True); e.add_argument("--factor", required=True); e.add_argument("--out", required=True)
    e.add_argument("--ledger", default="data/ledger/ledger.jsonl"); e.add_argument("--ledger-id")
    e.add_argument("--threshold", action="append", default=[], help="override a threshold (makes the thresholds check WATCH)")
    e.add_argument("--overwrite", action="store_true")
    sub.add_parser("thresholds", help="print the default thresholds")
    a = ap.parse_args(argv)
    if a.cmd == "thresholds":
        print(json.dumps(asdict(GateConfig()), indent=2)); return 0
    fl, out = Path(a.factorlab), Path(a.out)
    if not (fl / "run_manifest.json").is_file() or not (fl / "factor_ic_by_date.csv").is_file():
        print(f"not a Factor Lab output folder: {fl}", file=sys.stderr); return 2
    if out.exists() and any(out.iterdir()) and not a.overwrite:
        print(f"output folder is not empty: {out}", file=sys.stderr); return 2
    led, state, registered = None, None, None
    try:
        if a.ledger_id:
            led = Ledger(a.ledger)
            state = led.state()
            if a.ledger_id not in state:
                raise LedgerError(f"unknown experiment id {a.ledger_id!r}")
            registered = state[a.ledger_id]["spec"].get("gate")
        base = GateConfig.from_dict(registered)
        gate = _parse_thresholds(a.threshold, base)
        res = evaluate(fl, a.factor, gate, state, a.ledger_id, registered)
    except LedgerCorrupt as exc:
        print(f"ledger error: {exc}", file=sys.stderr); return 3
    except (LedgerError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out / "GATE.md").write_text(report_md(res), encoding="utf-8")
    if led:
        led.link_artifact(a.ledger_id, out / "gate.json", "robustness_gate")
    print(f"{a.factor}: {res['status']}  ({out / 'gate.json'})")
    for c in res["checks"]:
        print(f"  {c['status']:<17} {c['check']}: {c['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
