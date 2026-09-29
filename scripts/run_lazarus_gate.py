#!/usr/bin/env python3
"""Run the real non-paid construction gate for a registered Lazarus task."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warptest.lazarus_luanti_gate import (  # noqa: E402
    LUANTI_GATE_PROFILES,
    LUANTI_GATE_SANDBOX_ROOT,
    LUANTI_PHASE0_TASK_ID,
    run_luanti_construction_gate,
)
from warptest.lazarus_openra_hotkey_gate import (  # noqa: E402
    OPENRA_HOTKEY_GATE_SANDBOX_ROOT,
    run_openra_pr21453_construction_gate,
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
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--upstream-record", type=Path)
    parser.add_argument("--golden-metadata", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--cache-root", type=Path, default=ROOT / ".warptest-cache/lazarus")
    parser.add_argument("--sandbox-root", type=Path)
    return parser.parse_args(argv)


def _defaults(task_id: str) -> tuple[Path, Path, Path, Path, Path]:
    if task_id == "lz-luanti-14757-math-round":
        defects = ROOT / "benchmarks/p5/lazarus/defects/lz-luanti-14757-math-round"
        return (
            ROOT
            / "benchmarks/p5/lazarus/authoring/lz-luanti-14757-math-round.version-request.json",
            ROOT / "specs/luanti/lazarus/lz-luanti-14757-math-round.phase0.yaml",
            defects / "upstream.json",
            ROOT
            / "specs/luanti/lazarus/visual_lz-luanti-14757-math-round-phase0/goldens.meta.json",
            defects / "gate.json",
        )
    if task_id == LUANTI_PHASE0_TASK_ID:
        return (
            ROOT / "benchmarks/p5/lazarus/authoring/lz-luanti-11919-vector-from-string.version-request.json",
            ROOT / "specs/luanti/lazarus/lz-luanti-11919-vector-from-string.yaml",
            ROOT / "benchmarks/p5/lazarus/defects/lz-luanti-11919-vector-from-string/upstream.json",
            ROOT / "specs/luanti/lazarus/visual_pr11919/goldens.meta.json",
            ROOT / "benchmarks/p5/lazarus/defects/lz-luanti-11919-vector-from-string/gate.json",
        )
    if task_id == OPENRA_HOTKEY_PHASE0_TASK_ID:
        return (
            ROOT / "benchmarks/p5/lazarus/authoring/lz-openra-21453-hotkey-ready-building.version-request.json",
            ROOT / "specs/openra/lazarus/lz-openra-21453-hotkey-ready-building.yaml",
            ROOT / "benchmarks/p5/lazarus/defects/lz-openra-21453-hotkey-ready-building/upstream.json",
            ROOT / "specs/openra/lazarus/visual/goldens.meta.json",
            ROOT / "benchmarks/p5/lazarus/defects/lz-openra-21453-hotkey-ready-building/gate.json",
        )
    raise ValueError("only registered Phase-0 construction gates are available")


def _request_from_file(path: Path) -> LazarusVersionRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Lazarus version request must be a JSON object")
    return LazarusVersionRequest(
        task_id=str(payload["task_id"]),
        source_repo=ROOT / str(payload["source_repo"]),
        fixed_revision=str(payload["fixed_revision"]),
        revert_patch=ROOT / str(payload["revert_patch"]),
        patch_sha256=str(payload["patch_sha256"]),
        declared_files=tuple(str(value) for value in payload["declared_files"]),
        build_class=str(payload["build_class"]),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    default_request, default_spec, default_upstream, default_golden, default_report = _defaults(
        args.task_id
    )
    request_path = (args.request or default_request).expanduser().resolve()
    spec_path = (args.spec or default_spec).expanduser().resolve()
    upstream_path = (args.upstream_record or default_upstream).expanduser().resolve()
    golden_path = (args.golden_metadata or default_golden).expanduser().resolve()
    report_path = (args.report or default_report).expanduser().resolve()
    sandbox_root = (
        args.sandbox_root
        or (
            LUANTI_GATE_SANDBOX_ROOT
            if args.task_id in LUANTI_GATE_PROFILES
            else OPENRA_HOTKEY_GATE_SANDBOX_ROOT
        )
    ).expanduser().resolve()
    pair = materialize_lazarus_versions(
        _request_from_file(request_path),
        cache_root=args.cache_root.expanduser().resolve(),
        build=(
            create_openra_pr21453_prebuild_callback()
            if args.task_id == OPENRA_HOTKEY_PHASE0_TASK_ID
            else None
        ),
    )
    if args.task_id in LUANTI_GATE_PROFILES:
        gate = run_luanti_construction_gate(
            pair,
            profile=LUANTI_GATE_PROFILES[args.task_id],
            repository_root=ROOT,
            spec_path=spec_path,
            upstream_record_path=upstream_path,
            golden_metadata_path=golden_path,
            report_path=report_path,
            sandbox_root=sandbox_root,
        )
    else:
        gate = run_openra_pr21453_construction_gate(
            pair,
            repository_root=ROOT,
            spec_path=spec_path,
            upstream_record_path=upstream_path,
            golden_metadata_path=golden_path,
            report_path=report_path,
            sandbox_root=sandbox_root,
        )
    print(
        json.dumps(
            {
                "task_id": gate.result.task_id,
                "passed": gate.result.passed,
                "complete": gate.result.complete,
                "failures": list(gate.result.failures),
                "report": str(gate.report_path.relative_to(ROOT)),
                "fixed_upstream_ms": gate.fixed_upstream.duration_ms,
                "buggy_upstream_ms": gate.buggy_upstream.duration_ms,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if gate.result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
