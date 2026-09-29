#!/usr/bin/env python3
"""Drive one measure-edit-remeasure round on the P2P-only after rubrics.

Each invocation is one round: it lands the authored wording registry onto the
eleven specs, re-runs the guards that keep that wording legal, grades every after
frame with each judge, and appends the round to a ledger.  It returns success
only once some judge reaches the target accuracy.

The edit between rounds is deliberately not automated.  The thing being tuned is
a hidden oracle, and the only edits worth landing are the ones a failing verdict
actually justifies -- naming the part of the frame the judge could not find, or
replacing a condition no screenshot can settle.  A rewriter optimising the score
would instead learn to describe these two particular PNGs, which is the one
outcome that makes the rubric useless on a live trial.  So the loop reports what
each judge said it saw and stops; a round ends when the registry is edited and
this script is run again.

Usage:
    python scripts/tune_lazarus_p2p_only_after_rubrics.py [--target 0.80]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import yaml  # noqa: E402

from scripts.apply_lazarus_p2p_only_task_text import main as apply_task_text  # noqa: E402
from scripts.audit_lazarus_p2p_only_after_rubrics import (  # noqa: E402
    DEFAULT_MODELS,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_TARGET,
    DEFAULT_WORKERS,
    audit_model,
    format_digest,
    load_audit_tasks,
    write_report,
)
from warptest.c1_experiment import (  # noqa: E402
    DEFAULT_CREDENTIAL_SOURCE,
    relay_environment,
)
from warptest.lazarus_mining import (  # noqa: E402
    verify_public_text_has_no_hidden_tokens,
)
from warptest.task_descriptions import canonical_state_description  # noqa: E402

CATALOG_PATH = REPOSITORY_ROOT / "benchmarks/p5/c1_lazarus_p2p_only_tasks.yaml"
REGISTRY_PATH = (
    REPOSITORY_ROOT / "warptest/c1_lazarus_selection/p2p_only_task_text.py"
)
LEDGER_PATH = DEFAULT_OUTPUT_ROOT / "tuning-ledger.json"
DEFAULT_MAX_ITERATIONS = 6


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_registry() -> None:
    """Land the authored registry on the specs, catalog and golden sidecars.

    The applier revalidates the whole catalog against the Lazarus contract after
    writing, so a registry edit that breaks the schema fails here rather than
    surfacing as a judge miss.
    """

    status = apply_task_text([])
    if status != 0:
        raise SystemExit(f"applying the wording registry failed with status {status}")


def run_guards() -> dict[str, Any]:
    """Re-check that the landed wording is still legal and still separating.

    ``validate_lazarus_catalog`` already ran inside the applier.  The two checks
    here are the ones a rubric edit can break on its own: the public half must
    not have picked up a coordinator-only value, and the two after rubrics must
    still classify a frame between them, which is what ``merge_after_rubrics``
    inside ``load_audit_tasks`` enforces.
    """

    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    checked: list[str] = []
    for row in catalog["tasks"]:
        task_id = str(row["id"])
        spec_path = REPOSITORY_ROOT / str(row["oracle_spec"])
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        preconditions = str(row["preconditions"])
        reference_steps = str(row["reference_steps"])
        public_text = "\n".join(
            (
                preconditions,
                reference_steps,
                canonical_state_description(preconditions, reference_steps),
            )
        )
        touched = row["defect"]["upstream"].get("touched_files") or ()
        verify_public_text_has_no_hidden_tokens(
            public_text,
            spec=spec,
            touched_files=tuple(str(value) for value in touched),
        )
        checked.append(task_id)
    tasks = load_audit_tasks()
    return {
        "public_text_checked": checked,
        "after_rubrics_separating": [task.task_id for task in tasks],
    }


def read_ledger() -> dict[str, Any]:
    if not LEDGER_PATH.is_file():
        return {"schema": "c1-lazarus-p2p-only-after-rubric-tuning-v1", "rounds": []}
    payload = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("rounds"), list):
        raise SystemExit(f"{LEDGER_PATH}: ledger must hold a list of rounds")
    return payload


def write_ledger(ledger: dict[str, Any]) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def format_trend(ledger: dict[str, Any], *, target: float) -> str:
    lines = ["accuracy by round:"]
    for record in ledger["rounds"]:
        scores = "  ".join(
            f"{entry['model']} {entry['correct']}/{entry['trials']} "
            f"({entry['accuracy']:.1%})"
            for entry in record["models"]
        )
        marker = "MET" if record["best_accuracy"] >= target else "   "
        lines.append(f"  [{record['iteration']:>2}] {marker} {scores}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--target", type=float, default=DEFAULT_TARGET)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help="refuse to spend a further round once the ledger holds this many",
    )
    parser.add_argument(
        "--note",
        default="",
        help="what this round's registry edit was meant to fix",
    )
    parser.add_argument(
        "--guards-only",
        action="store_true",
        help="land the registry and run the guards without any judge request",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=REPOSITORY_ROOT / DEFAULT_CREDENTIAL_SOURCE,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    models = list(args.models or DEFAULT_MODELS)
    ledger = read_ledger()
    iteration = len(ledger["rounds"])
    if iteration >= args.max_iterations:
        print(
            f"[tune] the ledger already holds {iteration} round(s), at the "
            f"--max-iterations limit of {args.max_iterations}. Raise the limit "
            "deliberately or stop and report.",
            flush=True,
        )
        return 2

    print(f"[tune] round {iteration}: landing the wording registry", flush=True)
    apply_registry()
    guards = run_guards()
    print(
        f"[tune] guards passed for {len(guards['public_text_checked'])} task(s)",
        flush=True,
    )
    if args.guards_only:
        return 0

    tasks = load_audit_tasks()
    output_dir = DEFAULT_OUTPUT_ROOT / f"iter{iteration:02d}"
    fake = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
    credentials = (
        None if fake or os.environ.get("RELAY_API_KEY") else args.credentials
    )

    def _run() -> list[dict[str, Any]]:
        summaries = []
        for model in models:
            print(f"[tune] judging with {model}", flush=True)
            summaries.append(
                audit_model(model=model, tasks=tasks, workers=args.workers)
            )
        return summaries

    if credentials is None:
        summaries = _run()
    else:
        with relay_environment(credentials):
            summaries = _run()

    summary_path = write_report(
        summaries, output_dir=output_dir, target=args.target, tasks=tasks
    )
    best = max((summary["accuracy"] for summary in summaries), default=0.0)
    ledger["rounds"].append(
        {
            "iteration": iteration,
            "recorded_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "note": args.note,
            "registry_sha256": _sha256_file(REGISTRY_PATH),
            "report_dir": str(output_dir.relative_to(REPOSITORY_ROOT)),
            "target": args.target,
            "target_met": best >= args.target,
            "best_accuracy": best,
            "models": [
                {
                    "model": summary["model"],
                    "accuracy": summary["accuracy"],
                    "correct": summary["correct"],
                    "trials": summary["trials"],
                    "separated_tasks": summary["separated_tasks"],
                    "errors": summary["errors"],
                    "failures": [
                        {
                            "task_id": failure["task_id"],
                            "frame": failure["frame"],
                            "expected_label": failure["expected_label"],
                            "label": failure["label"],
                        }
                        for failure in summary["failures"]
                    ],
                }
                for summary in summaries
            ],
        }
    )
    write_ledger(ledger)

    print(format_digest(summaries, target=args.target), flush=True)
    print(format_trend(ledger, target=args.target), flush=True)
    print(f"[tune] wrote {summary_path} and {LEDGER_PATH}", flush=True)
    if best >= args.target:
        return 0
    print(
        "[tune] target not met. Edit readout/fixed_after/buggy_after in "
        f"{REGISTRY_PATH.relative_to(REPOSITORY_ROOT)} for the tasks above, then "
        "run this script again.",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
