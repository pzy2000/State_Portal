#!/usr/bin/env python3
"""Materialize one exact fixed/buggy Lazarus pair into the trusted CoW cache."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_build import create_prebuild_callback  # noqa: E402
from warptest.lazarus_formal_build import (  # noqa: E402
    LUANTI_FORMAL_TASK_IDS,
    create_formal_prebuild_callback,
)
from warptest.lazarus_openra_hotkey_probe import (  # noqa: E402
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    create_openra_pr21453_prebuild_callback,
)
from warptest.lazarus_versions import (  # noqa: E402
    LazarusVersionRequest,
    materialize_lazarus_versions,
)


def _contained(workspace: Path, value: str, label: str) -> Path:
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace: {value!r}")
    return path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--cache-root", type=Path, default=Path(".warptest-cache/lazarus")
    )
    parser.add_argument(
        "--build-command-json",
        help="JSON argv; required for jlpm/incremental_cpp/incremental_dotnet/unity",
    )
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--build-timeout-s", type=float, default=7_200)
    parser.add_argument("--formal-registered-build", action="store_true")
    return parser.parse_args(argv)


def _serialize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            key: _serialize(item) for key, item in dataclasses.asdict(value).items()
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    request_path = _contained(workspace, str(args.request), "request")
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("version request must be a JSON object")
    request = LazarusVersionRequest(
        task_id=str(payload["task_id"]),
        source_repo=_contained(workspace, str(payload["source_repo"]), "source_repo"),
        fixed_revision=str(payload["fixed_revision"]),
        revert_patch=_contained(
            workspace, str(payload["revert_patch"]), "revert_patch"
        ),
        patch_sha256=str(payload["patch_sha256"]),
        declared_files=tuple(str(value) for value in payload["declared_files"]),
        build_class=str(payload["build_class"]),
    )
    build = None
    if request.task_id == "lz-openra-21906-production-tabs":
        raise ValueError(
            "lz-openra-21906-production-tabs is a rejected construction candidate and cannot be materialized"
        )
    if request.task_id in LUANTI_FORMAL_TASK_IDS and not args.formal_registered_build:
        raise ValueError(
            f"{request.task_id} requires --formal-registered-build; "
            "generic build commands are not permitted for its authoritative "
            "Luanti construction gate"
        )
    if request.task_id == OPENRA_HOTKEY_PHASE0_TASK_ID:
        if args.build_command_json:
            raise ValueError(
                "the registered OpenRA Phase-0 task owns its hashable prebuild callback"
            )
        build = create_openra_pr21453_prebuild_callback(timeout_s=args.build_timeout_s)
    elif args.formal_registered_build:
        if args.build_command_json or args.artifact:
            raise ValueError(
                "registered formal builds do not accept command/artifact overrides"
            )
        build = create_formal_prebuild_callback(
            request.task_id, timeout_s=args.build_timeout_s
        )
    elif args.build_command_json:
        command = json.loads(args.build_command_json)
        if not isinstance(command, list):
            raise ValueError("--build-command-json must decode to an argv list")
        build = create_prebuild_callback(
            command,
            artifact_paths=tuple(args.artifact),
            timeout_s=args.build_timeout_s,
        )
    cache_root = _contained(workspace, str(args.cache_root), "cache_root")
    pair = materialize_lazarus_versions(request, cache_root=cache_root, build=build)
    print(json.dumps(_serialize(pair), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
