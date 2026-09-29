#!/usr/bin/env python3
"""Apply the authored P2P-only wording to the three files that carry it.

:mod:`scripts.publish_lazarus_p2p_only_catalog` is the writer of record, but
re-running it needs the selection cache, the pinned game submodules and a fresh
Gate 1--6 run. This applier writes the identical text offline, so the wording
registry in :mod:`warptest.c1_lazarus_selection.p2p_only_task_text` can be
edited and landed on its own. It reuses the publisher's dump settings, so a
later publish run rewrites the same bytes.

Usage:
    python scripts/apply_lazarus_p2p_only_task_text.py [--check] [--task-id ID ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.publish_lazarus_p2p_only_catalog import (  # noqa: E402
    BlockText,
    CATALOG_PATH,
)
from warptest.c1_lazarus_selection.c1_lazarus_task_text import TaskText  # noqa: E402
from warptest.c1_lazarus_selection.p2p_only_task_text import (  # noqa: E402
    TASK_TEXT,
    raw_initial,
)
from warptest.lazarus_catalog import validate_lazarus_catalog  # noqa: E402
from warptest.task_descriptions import canonical_state_description  # noqa: E402

CATALOG = REPO_ROOT / CATALOG_PATH
# Each named Lazarus group grades one frame, so the registry owns its expected
# text the same way it owns that frame's rubric.
CHECK_FRAMES: tuple[tuple[str, str], ...] = (
    ("goal_checks", "fixed"),
    ("defect_signature", "buggy"),
    ("invariant_checks", "before"),
)


def _render_catalog(catalog: Mapping[str, Any]) -> str:
    return yaml.dump(catalog, allow_unicode=True, sort_keys=False, width=100)


def _render_spec(document: Any) -> str:
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=100)


def _apply_visual_text(document: dict[str, Any], text: TaskText) -> None:
    """Rewrite the before rubric, both after rubrics and the graded frames."""

    evaluation = document["evaluation"]
    visual = evaluation["visual_oracle"]
    # judge_lazarus_visual_evidence grades the before frame with the registered
    # visual oracle, and the publisher states the scene there as well.
    visual["description"] = f"{text.scene} {text.before}"
    visual["rubric"] = text.rubric("before")
    lazarus = evaluation["lazarus"]
    for group, frame in CHECK_FRAMES:
        # The goldens sidecar carries the frames without the named groups.
        for check in lazarus.get(group) or ():
            check["expected"] = text.description(frame)
    goldens = lazarus["goldens"]
    for frame in ("fixed", "buggy"):
        branch = goldens[frame]
        branch["after_description"] = text.description(frame)
        branch["after_rubric"] = text.rubric(frame)


def planned_documents(task_ids: list[str] | None = None) -> dict[Path, str]:
    """Return every file the registry owns, mapped to its intended content."""

    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    rows = {str(row["id"]): row for row in catalog["tasks"]}
    selected = sorted(set(task_ids) if task_ids else set(rows) & set(TASK_TEXT))
    unregistered = [task_id for task_id in selected if task_id not in TASK_TEXT]
    unpublished = [task_id for task_id in selected if task_id not in rows]
    if unregistered or unpublished:
        raise SystemExit(
            "unknown task id(s): "
            f"unregistered={unregistered} not-in-catalog={unpublished}"
        )

    planned: dict[Path, str] = {}
    for task_id in selected:
        text = TASK_TEXT[task_id]
        row = rows[task_id]
        row["raw_initial"] = raw_initial(task_id)
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
        row["state_description"] = BlockText(row["state_description"])
    planned[CATALOG] = _render_catalog(catalog)
    return planned


def _validate() -> None:
    # validate_lazarus_catalog re-reads every row's oracle spec and grades it
    # against the Lazarus evaluation contract, which is the only spec schema this
    # track publishes against.
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    validate_lazarus_catalog(catalog, source_path=CATALOG)


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
            print(f"[p2p-only-text] drifted: {path.relative_to(REPO_ROOT)}", flush=True)
        if drifted:
            print(
                "[p2p-only-text] run scripts/apply_lazarus_p2p_only_task_text.py "
                "to rewrite",
                flush=True,
            )
            return 1
        print("[p2p-only-text] every registered file matches the registry", flush=True)
        return 0

    for path in drifted:
        path.write_text(planned[path], encoding="utf-8")
        print(f"[p2p-only-text] wrote {path.relative_to(REPO_ROOT)}", flush=True)
    _validate()
    if not drifted:
        print("[p2p-only-text] nothing to write", flush=True)
    print("[p2p-only-text] catalog and specs revalidated", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
