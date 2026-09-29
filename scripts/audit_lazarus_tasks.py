#!/usr/bin/env python3
"""Audit either the explicit Phase-0 catalog or a future formal Lazarus catalog."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_audit import (  # noqa: E402
    audit_lazarus_distribution,
    audit_lazarus_phase0,
)
from warptest.lazarus_catalog import validate_lazarus_catalog  # noqa: E402
from warptest.lazarus_roster import unbound_formal_task_ids  # noqa: E402
from warptest.lazarus_manifest import (  # noqa: E402
    validate_lazarus_manifest,
    validate_lazarus_phase0_manifest,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/p5/lazarus_phase0_manifest.yaml",
    )
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def _contained(workspace: Path, value: Path, label: str) -> Path:
    path = value.expanduser()
    path = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace")
    return path


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    manifest_path = _contained(workspace, args.manifest, "manifest")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Lazarus manifest must be a mapping")
    source_value = args.catalog or Path(str(manifest.get("tasks") or ""))
    if not str(source_value):
        raise ValueError("Lazarus audit needs a catalog")
    catalog_path = _contained(workspace, source_value, "catalog")
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict):
        raise ValueError("Lazarus catalog must be a mapping")
    _, tasks = validate_lazarus_catalog(catalog, source_path=catalog_path)
    task_ids = [str(task["id"]) for task in tasks]
    experiment = manifest.get("experiment_id")
    if experiment == "c1_lazarus_phase0":
        validate_lazarus_phase0_manifest(manifest, catalog_task_ids=task_ids)
        result = audit_lazarus_phase0(tasks)
        default_output = workspace / "reports/lazarus/phase0-task-audit.json"
    elif experiment == "c1_lazarus":
        contract = validate_lazarus_manifest(
            manifest,
            catalog_task_ids=task_ids,
            unbound_task_ids=unbound_formal_task_ids(
                tasks, repository_root=workspace
            ),
        )
        result = audit_lazarus_distribution(tasks, pilot_task_ids=contract.phase0_task_ids)
        default_output = workspace / "reports/lazarus/task-audit.json"
    else:
        raise ValueError("unknown Lazarus experiment_id")
    output = _contained(workspace, args.output or default_output, "output")
    _atomic_json(output, result)
    print(
        json.dumps(
            {
                "artifact": str(output.relative_to(workspace)),
                "complete": result["complete"],
                "task_count": result["task_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
