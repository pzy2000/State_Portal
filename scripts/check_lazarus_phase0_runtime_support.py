#!/usr/bin/env python3
"""Check whether every registered Phase-0 cell has a valid paid runtime path."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_catalog import validate_lazarus_catalog  # noqa: E402
from warptest.lazarus_runtime import phase0_runtime_support_report  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ROOT / "benchmarks/p5/c1_lazarus_phase0_tasks.yaml",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus/phase0-runtime-support.json",
    )
    return parser.parse_args(argv)


def _contained(workspace: Path, value: Path, label: str) -> Path:
    candidate = value.expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace")
    return path


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    catalog_path = _contained(workspace, args.catalog, "catalog")
    output = _contained(workspace, args.output, "output")
    payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Lazarus Phase-0 catalog is not a mapping")
    _, tasks = validate_lazarus_catalog(payload, source_path=catalog_path)
    report = phase0_runtime_support_report(tasks)
    _atomic_json(output, report)
    print(
        json.dumps(
            {
                "artifact": str(output.relative_to(workspace)),
                "complete": report["complete"],
                "provider_dispatch_permitted": report[
                    "provider_dispatch_permitted"
                ],
                "failures": report["failures"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["complete"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
