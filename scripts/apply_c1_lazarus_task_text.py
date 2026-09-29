#!/usr/bin/env python3
"""Apply the authored C1-Lazarus wording to the three files that carry it.

The manifest writer in :mod:`warptest.c1_lazarus_selection.c1_lazarus_task_goldens_manifest`
and the catalog writer in :mod:`warptest.c1_lazarus_selection.c1_lazarus_formal_catalog`
both read the same registry, but re-running them needs the 48 GB selection
artifact root and an infrastructure-audit authorization. This applier writes the
identical text offline, so the registry can be edited and landed on its own.

Usage:
    python scripts/apply_c1_lazarus_task_text.py [--check] [--task-id ID ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warptest.c1_lazarus_selection.c1_lazarus_formal_catalog import (  # noqa: E402
    _BlockText,
    _render_catalog,
)
from warptest.c1_lazarus_selection.c1_lazarus_task_text import (  # noqa: E402
    TASK_TEXT,
    TaskText,
)
from warptest.lazarus_catalog import validate_lazarus_catalog  # noqa: E402
from warptest.spec_schema import ensure_valid_spec_schema  # noqa: E402
from warptest.task_descriptions import canonical_state_description  # noqa: E402

CATALOG_PATH = REPO_ROOT / "benchmarks" / "p5" / "c1_lazarus_tasks.yaml"


def _render_spec(document: Any) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _apply_visual_text(document: dict[str, Any], text: TaskText) -> None:
    """Rewrite the before rubric and both after rubrics of one oracle document."""

    evaluation = document["evaluation"]
    visual = evaluation["visual_oracle"]
    # judge_lazarus_visual_evidence grades the before frame with the registered
    # visual oracle, so this description belongs to the common start.
    visual["description"] = text.description("before")
    visual["rubric"] = text.rubric("before")
    goldens = evaluation["lazarus"]["goldens"]
    for frame in ("fixed", "buggy"):
        branch = goldens[frame]
        branch["after_description"] = text.description(frame)
        branch["after_rubric"] = text.rubric(frame)


def planned_documents(task_ids: list[str] | None = None) -> dict[Path, str]:
    """Return every file the registry owns, mapped to its intended content."""

    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    rows = {str(row["id"]): row for row in catalog["tasks"]}
    selected = sorted(set(task_ids) if task_ids else set(TASK_TEXT))
    unknown = [task_id for task_id in selected if task_id not in rows]
    if unknown:
        raise SystemExit(f"unknown task id(s): {', '.join(unknown)}")

    planned: dict[Path, str] = {}
    for task_id in selected:
        text = TASK_TEXT.get(task_id)
        if text is None:
            continue
        row = rows[task_id]
        spec_path = REPO_ROOT / str(row["oracle_spec"])
        goldens_path = spec_path.with_name(f"{task_id}.goldens.yaml")
        for path in (spec_path, goldens_path):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            _apply_visual_text(document, text)
            planned[path] = _render_spec(document)
        row["preconditions"] = text.preconditions
        row["reference_steps"] = text.reference_steps
        row["state_description"] = canonical_state_description(
            text.preconditions, text.reference_steps
        )

    for row in catalog["tasks"]:
        row["state_description"] = _BlockText(row["state_description"])
    planned[CATALOG_PATH] = _render_catalog(catalog)
    return planned


def _validate(paths: list[Path]) -> None:
    catalog = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    validate_lazarus_catalog(catalog, source_path=CATALOG_PATH)
    for path in paths:
        if path == CATALOG_PATH or path.name.endswith(".goldens.yaml"):
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        ensure_valid_spec_schema(document, path, allow_lazarus=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift only")
    parser.add_argument("--task-id", action="append", dest="task_ids")
    args = parser.parse_args(argv)

    planned = planned_documents(args.task_ids)
    drifted = [
        path
        for path, content in sorted(planned.items())
        if path.read_text(encoding="utf-8") != content
    ]
    if args.check:
        for path in drifted:
            print(f"[lazarus-text] drifted: {path.relative_to(REPO_ROOT)}", flush=True)
        if drifted:
            print(
                "[lazarus-text] run scripts/apply_c1_lazarus_task_text.py to rewrite",
                flush=True,
            )
            return 1
        print("[lazarus-text] every registered file matches the registry", flush=True)
        return 0

    for path in drifted:
        path.write_text(planned[path], encoding="utf-8")
        print(f"[lazarus-text] wrote {path.relative_to(REPO_ROOT)}", flush=True)
    _validate(sorted(planned))
    if not drifted:
        print("[lazarus-text] nothing to write", flush=True)
    print("[lazarus-text] catalog and specs revalidated", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
