#!/usr/bin/env python3
"""Wire golden_before/golden_after into core hidden specs' visual_oracle blocks.

For each selected core task (from ``benchmarks/core/hidden/oracles.yaml``) this
inserts ``golden_before``/``golden_after`` lines right after the existing
``golden_image`` line in the spec's ``visual_oracle`` block, referencing
``visual/<task-id>.before.golden.png`` / ``visual/<task-id>.after.golden.png``.

The edit is text-based (to preserve formatting/comments) and idempotent: a spec
that already declares the pair keys is left unchanged. Only specs whose pair
images actually exist on disk are wired, unless --allow-missing is given.

Usage:
    python3 scripts/wire_visual_pairs.py --benchmark 2048 DatingSim
    python3 scripts/wire_visual_pairs.py --benchmark SpaceRPG --check
    python3 scripts/wire_visual_pairs.py --c1 --benchmark luanti --task luanti-water-source
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warptest.task_schema import load_hidden_oracles, resolve_hidden_spec  # noqa: E402


def selected_specs(
    benchmarks: list[str],
    task_filter: list[str] | None,
    *,
    c1_tasks: bool,
) -> list[tuple[str, Path]]:
    """Resolve (task_id, hidden spec path) pairs from the requested task universe.

    Most of the C1 task universe is not registered in the core hidden oracles, so
    ``--c1`` reads ``benchmarks/p5/c1_tasks.yaml`` instead, mirroring the task
    selection used by ``scripts/generate_visual_pairs.py --c1``.
    """

    selected: list[tuple[str, Path]] = []
    if c1_tasks:
        payload = yaml.safe_load(
            (REPO_ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )
        for task in payload.get("tasks", []):
            task_id = str(task.get("id", ""))
            if str(task.get("benchmark", "")) not in benchmarks:
                continue
            if task_filter and task_id not in task_filter:
                continue
            spec_path = (REPO_ROOT / str(task["oracle_spec"])).resolve()
            if spec_path.exists():
                selected.append((task_id, spec_path))
        return selected

    for task_id, oracle in load_hidden_oracles(REPO_ROOT).items():
        spec_path = resolve_hidden_spec(REPO_ROOT, oracle)
        if not spec_path.exists():
            continue
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        if (spec or {}).get("benchmark") not in benchmarks:
            continue
        if task_filter and task_id not in task_filter:
            continue
        selected.append((task_id, spec_path))
    return selected


def wire_spec(task_id: str, spec_path: Path, *, allow_missing: bool, check: bool) -> str:
    text = spec_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    before_rel = f"visual/{task_id}.before.golden.png"
    after_rel = f"visual/{task_id}.after.golden.png"
    before_file = spec_path.parent / before_rel
    after_file = spec_path.parent / after_rel

    if "golden_before:" in text and "golden_after:" in text:
        return "already-wired"
    if not (before_file.exists() and after_file.exists()) and not allow_missing:
        return f"missing-images ({before_file.name}, {after_file.name})"

    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        stripped = line.strip()
        if not inserted and stripped.startswith("golden_image:"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}golden_before: {before_rel}")
            out.append(f"{indent}golden_after: {after_rel}")
            inserted = True
    if not inserted:
        return "no-golden_image-line"
    if check:
        return "would-wire"
    spec_path.write_text("\n".join(out) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return "wired"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark", nargs="+", required=True, help="Benchmarks to wire.")
    parser.add_argument("--task", action="append", dest="tasks", help="Limit to specific task ids. Repeatable.")
    parser.add_argument("--allow-missing", action="store_true", help="Wire even if the pair images are not on disk yet.")
    parser.add_argument("--check", action="store_true", help="Report what would change without writing.")
    parser.add_argument(
        "--c1",
        action="store_true",
        help="Read selected tasks from benchmarks/p5/c1_tasks.yaml instead of core hidden oracles.",
    )
    args = parser.parse_args()

    results: dict[str, list[str]] = {}
    for task_id, spec_path in selected_specs(args.benchmark, args.tasks, c1_tasks=args.c1):
        status = wire_spec(task_id, spec_path, allow_missing=args.allow_missing, check=args.check)
        results.setdefault(status, []).append(task_id)

    for status in sorted(results):
        print(f"{status}: {len(results[status])}")
        for task_id in results[status]:
            print(f"  {task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
