#!/usr/bin/env python3
"""Append the C1-Lazarus expansion rows to the formal catalog.

The frozen 24 rows must stay byte-identical, so this appends serialized text
rather than re-dumping the document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_expansion_specs import (  # noqa: E402
    EXPANSION_ROWS,
    build_catalog_row,
)


_CATALOG = "benchmarks/p5/c1_lazarus_tasks.yaml"
_FROZEN_HEADER = "# Formal C1-Lazarus catalog for the frozen 24-task roster."
_EXPANDED_HEADER = (
    "# Formal C1-Lazarus catalog: the frozen 24-task roster plus the live-run\n"
    "# expansion batch."
)
_EXPANSION_NOTE = """
# The expansion rows appended after the frozen roster were established
# differently. Their revert.patch is still the production-only difference
# between the materialized versions, but their upstream.json is the mined
# authority record as retrieved and their gate.json is a live receipt: it
# reports a construction run that executed in this repository rather than a
# projection of the freeze sprint's Gate 1-13 verdicts. Those reports therefore
# declare derived_from_frozen_artifacts: false and fresh_gate_run: true.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    catalog = args.workspace / _CATALOG
    text = catalog.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    existing = {str(task["id"]) for task in document["tasks"]}

    appended: list[dict[str, str]] = []
    chunks: list[str] = []
    for row in EXPANSION_ROWS:
        if row.task_id in existing:
            continue
        patch = args.workspace / row.defect_dir / "formal-v1/revert.patch"
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        payload = build_catalog_row(row, patch_sha256=digest)
        chunks.append(
            yaml.safe_dump(
                [payload], sort_keys=False, allow_unicode=True, width=100
            )
        )
        appended.append({"task_id": row.task_id, "patch_sha256": digest})

    if not chunks:
        print(json.dumps({"appended": [], "task_count": len(existing)}, indent=2))
        return 0

    if _FROZEN_HEADER in text:
        text = text.replace(_FROZEN_HEADER, _EXPANDED_HEADER, 1)
    if _EXPANSION_NOTE.strip() not in text:
        marker = "schema_version: 1"
        text = text.replace(marker, _EXPANSION_NOTE.lstrip("\n") + marker, 1)
    if not text.endswith("\n"):
        text += "\n"
    catalog.write_text(text + "".join(chunks), encoding="utf-8")

    reloaded = yaml.safe_load(catalog.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {"appended": appended, "task_count": len(reloaded["tasks"])},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
