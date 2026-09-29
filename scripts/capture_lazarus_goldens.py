#!/usr/bin/env python3
"""Capture and atomically publish real C1-Lazarus Phase-0 golden evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_formal_build import (  # noqa: E402
    LUANTI_FORMAL_TASK_IDS,
    create_formal_prebuild_callback,
)
from warptest.lazarus_luanti_goldens import (  # noqa: E402
    LUANTI_PHASE0_TASK_ID,
    LUANTI_GOLDEN_PROFILES,
    capture_luanti_goldens,
)
from warptest.lazarus_openra_hotkey_goldens import (  # noqa: E402
    capture_openra_pr21453_goldens,
)
from warptest.lazarus_openra_hotkey_probe import (  # noqa: E402
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    create_openra_pr21453_prebuild_callback,
)
from warptest.lazarus_versions import (  # noqa: E402
    LazarusVersionRequest,
    materialize_lazarus_versions,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=LUANTI_PHASE0_TASK_ID)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--cache-root", type=Path, default=ROOT / ".warptest-cache/lazarus")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--sandbox-root",
        type=Path,
        default=Path("/private/tmp/warptest-lazarus-goldens"),
    )
    return parser.parse_args(argv)


def _defaults(task_id: str) -> tuple[Path, Path]:
    if task_id == "lz-luanti-14757-math-round":
        return (
            ROOT
            / "benchmarks/p5/lazarus/authoring/lz-luanti-14757-math-round.version-request.json",
            ROOT / "specs/luanti/lazarus/visual_lz-luanti-14757-math-round-phase0",
        )
    if task_id == LUANTI_PHASE0_TASK_ID:
        return (
            ROOT / "benchmarks/p5/lazarus/authoring/lz-luanti-11919-vector-from-string.version-request.json",
            ROOT / "specs/luanti/lazarus/visual_pr11919",
        )
    if task_id == "lz-luanti-14396-metadata-set-string-nil":
        return (
            ROOT
            / "benchmarks/p5/lazarus/authoring/"
            "lz-luanti-14396-metadata-set-string-nil.version-request.json",
            ROOT
            / "specs/luanti/lazarus/visual_lz-luanti-14396-metadata-set-string-nil",
        )
    if task_id == OPENRA_HOTKEY_PHASE0_TASK_ID:
        return (
            ROOT / "benchmarks/p5/lazarus/authoring/lz-openra-21453-hotkey-ready-building.version-request.json",
            ROOT / "specs/openra/lazarus/visual",
        )
    raise ValueError("only registered Phase-0 golden captures are available")


def _request_from_file(path: Path) -> LazarusVersionRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Lazarus version request must be a JSON object")
    source_repo = ROOT / str(payload["source_repo"])
    patch = ROOT / str(payload["revert_patch"])
    return LazarusVersionRequest(
        task_id=str(payload["task_id"]),
        source_repo=source_repo,
        fixed_revision=str(payload["fixed_revision"]),
        revert_patch=patch,
        patch_sha256=str(payload["patch_sha256"]),
        declared_files=tuple(str(value) for value in payload["declared_files"]),
        build_class=str(payload["build_class"]),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    default_request, default_output = _defaults(args.task_id)
    request = _request_from_file((args.request or default_request).expanduser().resolve())
    if args.task_id == OPENRA_HOTKEY_PHASE0_TASK_ID:
        build = create_openra_pr21453_prebuild_callback()
    elif args.task_id in LUANTI_FORMAL_TASK_IDS:
        # A formal Luanti row is an ``incremental_cpp`` product: its client must
        # come from the registered recipe rather than an ad-hoc build command.
        build = create_formal_prebuild_callback(args.task_id)
    else:
        build = None
    pair = materialize_lazarus_versions(
        request,
        cache_root=args.cache_root.expanduser().resolve(),
        build=build,
    )
    if args.task_id in LUANTI_GOLDEN_PROFILES:
        result = capture_luanti_goldens(
            pair,
            profile=LUANTI_GOLDEN_PROFILES[args.task_id],
            repository_root=ROOT,
            output_dir=(args.output_dir or default_output).expanduser().resolve(),
            sandbox_root=args.sandbox_root.expanduser().resolve(),
            app_bundle_cache_root=args.cache_root.expanduser().resolve()
            / "app-bundles",
        )
    else:
        result = capture_openra_pr21453_goldens(
            pair,
            repository_root=ROOT,
            output_dir=(args.output_dir or default_output).expanduser().resolve(),
            sandbox_root=args.sandbox_root.expanduser().resolve(),
        )
    print(
        json.dumps(
            {
                "schema": "c1-lazarus-golden-capture-v1",
                "task_id": result.task_id,
                "before": str(result.before.relative_to(ROOT)),
                "buggy_before": str(result.buggy_before.relative_to(ROOT)),
                "fixed_after": str(result.fixed_after.relative_to(ROOT)),
                "buggy_after": str(result.buggy_after.relative_to(ROOT)),
                "metadata": str(result.metadata.relative_to(ROOT)),
                "before_metrics": result.before_metrics,
                "after_metrics": result.after_metrics,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
