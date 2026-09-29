#!/usr/bin/env python3
"""Verify the committed one-time C1 evaluation-v1 migration.

The migration itself is represented by the 61 reviewed YAML changes.  Runtime
task-shape tables deliberately no longer exist, so this post-migration command
only validates that the tree is complete and cannot regress to the legacy root
layout.  ``--check`` is retained for CI/readme command stability.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "benchmarks/p5/c1_tasks.yaml"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def verify() -> int:
    from warptest.c1_evaluation import (
        evaluation_contract,
        validate_evaluation_contract,
    )

    payload = yaml.safe_load(TASKS_PATH.read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or len(tasks) != 61:
        raise ValueError("formal C1 migration requires exactly 61 registered tasks")

    task_ids: set[str] = set()
    spec_paths: set[Path] = set()
    failures: list[str] = []
    for row in tasks:
        if not isinstance(row, dict):
            failures.append("formal task row is not a mapping")
            continue
        task_id = str(row.get("id") or "")
        raw_path = row.get("oracle_spec")
        if not task_id or task_id in task_ids:
            failures.append(f"invalid or duplicate task id {task_id!r}")
            continue
        task_ids.add(task_id)
        if not isinstance(raw_path, str):
            failures.append(f"{task_id}: oracle_spec is missing")
            continue
        spec_path = (ROOT / raw_path).resolve()
        if spec_path in spec_paths:
            failures.append(f"{task_id}: duplicate oracle path {raw_path}")
            continue
        spec_paths.add(spec_path)
        try:
            spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            failures.append(f"{task_id}: cannot load {raw_path}: {error}")
            continue
        if not isinstance(spec, dict):
            failures.append(f"{task_id}: oracle is not a mapping")
            continue
        if evaluation_contract(spec) is None:
            failures.append(f"{task_id}: evaluation-v1 is missing")
            continue
        errors = validate_evaluation_contract(spec, spec_path)
        failures.extend(f"{task_id}: {error}" for error in errors)

    if failures:
        raise ValueError("C1 evaluation-v1 verification failed:\n- " + "\n- ".join(failures))
    print("verified 61 formal C1 evaluation-v1 contracts")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.parse_args()
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
