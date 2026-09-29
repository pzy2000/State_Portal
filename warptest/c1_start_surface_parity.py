"""Offline equivalence gate for the two C1 Start semantic surfaces.

The GUI arms grade the Start target through the production surface (rendered DOM
and production save keys) because they run without state hooks; the state arm
runs with hooks and calls the profile validator directly. The two arms therefore
grade the same predicate through different interfaces, which is a real
difference between two Start rates that are compared head to head.

Forcing one interface is not an option: the GUI arms have no hooks to call, and
the DatingSim production grader clicks the real Save button and restores
``localStorage`` afterwards, so running it per trial would perturb the state the
state arm is about to transition. Instead this gate measures the equivalence
offline. For every benchmark that has a production-surface grader it restores a
known state in a hook-enabled session and grades it with *both* surfaces, then
reports how often they agree. That number is the citable evidence that the two
arms' Start predicates coincide, and any disagreement names the task.

Two cases are graded per task:

``entry``
    The application entry state, which does not satisfy the oracle target. Both
    surfaces must reject it.
``restored_oracle``
    The registered oracle target restored on the live session. Both surfaces must
    accept it.

Run as ``python3 -m warptest.c1_start_surface_parity --output <path>``.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from . import run_context
from .c1_public_semantics import grade_public_start, public_semantic_source
from .c1_start_semantics import profile_for_repo, semantic_start_grade
from .runner import load_spec
from .state_loop import load_state_loop_tasks
from .state_store import StateSession
from .task_schema import resolve_task_repo


SCHEMA = "c1-start-surface-parity-v1"
# The formal C1 task set, not the state-loop set: this gate exists to bound a
# difference between two C1 arms.
C1_TASKS = "benchmarks/p5/c1_tasks.yaml"


def compare_start_semantic_surfaces(
    profile: Any, runtime: Any, oracle_spec: dict[str, Any]
) -> dict[str, Any]:
    """Grade one live Start with both surfaces and report whether they agree.

    The hook validator runs first because the DatingSim production grader
    writes to the page before restoring what it found.
    """

    hook_ok, hook_status, hook_detail = semantic_start_grade(
        profile, runtime, oracle_spec, prefer_public_surface=False
    )
    public = grade_public_start(profile, runtime, oracle_spec)
    if public is None:
        return {
            "hook": {"ok": hook_ok, "status": hook_status},
            "public": None,
            "agree": None,
            "comparable": False,
        }
    public_ok, public_status, public_detail = public
    return {
        "hook": {"ok": bool(hook_ok), "status": hook_status, "detail": hook_detail},
        "public": {
            "ok": bool(public_ok),
            "status": public_status,
            "detail": public_detail,
        },
        "agree": bool(hook_ok) == bool(public_ok),
        "comparable": True,
    }


def _public_surface_tasks(root: Path, tasks_path: str | Path) -> list[Any]:
    tasks = load_state_loop_tasks(root, tasks_path)
    selected = []
    for task in tasks:
        shim = type("_Shim", (), {"benchmark": task.benchmark})
        if public_semantic_source(shim) is not None:
            selected.append(task)
    return selected


def _grade_task(
    task: Any,
    *,
    root: Path,
    report_dir: Path,
    headless: bool,
) -> dict[str, Any]:
    repo = resolve_task_repo(root, task.issue)
    oracle_path = Path(task.oracle_spec)
    if not oracle_path.is_absolute():
        oracle_path = (root / oracle_path).resolve()
    oracle_spec = load_spec(oracle_path)
    row: dict[str, Any] = {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "surface": public_semantic_source(
            type("_Shim", (), {"benchmark": task.benchmark})
        ),
        "cases": {},
    }
    session = StateSession(repo, report_dir / task.id, headless=headless, experiment_seed=0)
    try:
        session.start()
    except Exception as error:  # noqa: BLE001 - recorded, not raised
        row["error"] = f"{type(error).__name__}: {error}"
        return row
    try:
        profile = getattr(session, "_profile", None) or profile_for_repo(repo)
        row["cases"]["entry"] = {
            "expected_ok": False,
            **compare_start_semantic_surfaces(profile, session.runtime, oracle_spec),
        }
        restored = session.restore_target_only(
            oracle_spec, oracle_path, screenshot_name=str(report_dir / task.id / "before.png")
        )
        row["restore_status"] = restored.status
        row["cases"]["restored_oracle"] = {
            "expected_ok": True,
            **compare_start_semantic_surfaces(profile, session.runtime, oracle_spec),
        }
    except Exception as error:  # noqa: BLE001 - recorded, not raised
        row["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            session.close()
        except Exception:
            pass
    return row


def run_surface_parity_gate(
    root: str | Path = Path.cwd(),
    *,
    tasks_path: str | Path = C1_TASKS,
    report_dir: str | Path | None = None,
    headless: bool = True,
    task_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Grade every production-surface benchmark with both surfaces."""

    base = Path(root).expanduser().resolve()
    started = time.perf_counter()
    tasks = _public_surface_tasks(base, tasks_path)
    if task_ids:
        wanted = set(task_ids)
        tasks = [task for task in tasks if task.id in wanted]
    sessions_dir = Path(report_dir).expanduser().resolve() if report_dir else (
        base / "reports" / "warptest" / "c1-start-surface-parity"
    )
    rows = [
        _grade_task(task, root=base, report_dir=sessions_dir, headless=headless)
        for task in tasks
    ]

    comparable = 0
    agreeing = 0
    expected_matches = 0
    disagreements: list[str] = []
    for row in rows:
        for case_name, case in row.get("cases", {}).items():
            if not case.get("comparable"):
                continue
            comparable += 1
            if case.get("agree"):
                agreeing += 1
            else:
                disagreements.append(f"{row['task_id']}/{case_name}")
            hook = case.get("hook") or {}
            if bool(hook.get("ok")) is bool(case.get("expected_ok")):
                expected_matches += 1
    errors = [row["task_id"] for row in rows if row.get("error")]
    return {
        "schema": SCHEMA,
        "git_commit": run_context.git_commit(base),
        "tasks_path": str(tasks_path),
        "task_count": len(rows),
        "comparable_cases": comparable,
        "agreeing_cases": agreeing,
        "agreement_rate": (agreeing / comparable) if comparable else None,
        "expected_verdict_matches": expected_matches,
        "disagreements": disagreements,
        "errored_tasks": errors,
        # Fail closed: a task that could not be graded is not evidence of parity.
        "passed": bool(comparable) and not disagreements and not errors,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path.cwd()))
    parser.add_argument("--tasks", default=C1_TASKS)
    parser.add_argument("--report-dir", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument("--task-ids", default="")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    payload = run_surface_parity_gate(
        args.root,
        tasks_path=args.tasks,
        report_dir=args.report_dir,
        headless=not args.headed,
        task_ids=[
            task_id.strip()
            for task_id in args.task_ids.split(",")
            if task_id.strip()
        ],
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"[surface-parity] agreement={payload['agreement_rate']} "
        f"comparable={payload['comparable_cases']} "
        f"disagreements={payload['disagreements']} -> {output}",
        flush=True,
    )
    return 0 if payload["passed"] else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
