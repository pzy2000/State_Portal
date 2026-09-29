#!/usr/bin/env python3
"""Build a non-paid, hash-bound Lazarus calibration artifact for Phase 0."""

from __future__ import annotations

import argparse
import hashlib
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

from warptest.lazarus_calibration import (  # noqa: E402
    LazarusBackendBinding,
    build_lazarus_calibration,
    lazarus_backend_binding_from_artifact,
    lazarus_harness_hash,
    lazarus_provider_identity_sha256,
    validate_lazarus_calibration,
)
from warptest.c1_experiment import relay_environment  # noqa: E402
from warptest.lazarus_catalog import validate_lazarus_catalog  # noqa: E402
from warptest.lazarus_manifest import validate_lazarus_phase0_manifest  # noqa: E402
from warptest.lazarus_openra_hotkey_probe import (  # noqa: E402
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    create_openra_pr21453_prebuild_callback,
)
from warptest.lazarus_versions import (  # noqa: E402
    LazarusVersionArtifact,
    LazarusVersionRequest,
    materialize_lazarus_versions,
)


_BACKENDS = {
    "luanti": {
        "capture_backend": "luanti_irrlicht_create_screenshot_v1",
        "input_backend": "luanti_irrlicht_sevent_v1",
        "binary_paths": ("build-app/out/luanti.app/Contents/MacOS/luanti",),
    },
    "openra": {
        "capture_backend": "openra_renderer_save_screenshot_sync_v1",
        "input_backend": "openra_default_input_handler_v1",
        "binary_paths": (
            "bin/OpenRA.Game.dll",
            "bin/OpenRA.Mods.Common.dll",
            "bin/OpenRA.Test.dll",
        ),
    },
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "benchmarks/p5/lazarus_phase0_manifest.yaml",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ROOT / "benchmarks/p5/c1_lazarus_phase0_tasks.yaml",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/lazarus/phase0-calibration.json",
    )
    parser.add_argument("--cache-root", type=Path, default=ROOT / ".warptest-cache/lazarus")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("test_model_batch.py"),
    )
    return parser.parse_args(argv)


def _contained(workspace: Path, value: Path, label: str) -> Path:
    path = value.expanduser()
    path = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace")
    return path


def _request_from_task(workspace: Path, task_id: str) -> LazarusVersionRequest:
    path = workspace / "benchmarks/p5/lazarus/authoring" / f"{task_id}.version-request.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        raise ValueError(f"Lazarus version request is invalid for {task_id}")
    return LazarusVersionRequest(
        task_id=task_id,
        source_repo=workspace / str(payload["source_repo"]),
        fixed_revision=str(payload["fixed_revision"]),
        revert_patch=workspace / str(payload["revert_patch"]),
        patch_sha256=str(payload["patch_sha256"]),
        declared_files=tuple(str(value) for value in payload["declared_files"]),
        build_class=str(payload["build_class"]),
    )


def _build_callback(task_id: str):
    return create_openra_pr21453_prebuild_callback() if task_id == OPENRA_HOTKEY_PHASE0_TASK_ID else None


def _gate_canary_passed(
    workspace: Path, task: Mapping[str, Any], artifact: LazarusVersionArtifact
) -> bool:
    defect = task.get("defect")
    if not isinstance(defect, Mapping):
        return False
    path = workspace / str(defect.get("gate_report") or "")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(report, Mapping)
        or report.get("status") != "passed"
        or report.get("task_id") != artifact.task_id
    ):
        return False
    evidence = report.get("evidence")
    versions = evidence.get("versions") if isinstance(evidence, Mapping) else None
    recorded = versions.get(artifact.version) if isinstance(versions, Mapping) else None
    return (
        isinstance(recorded, Mapping)
        and recorded.get("tree_sha256") == artifact.tree_sha256
        and recorded.get("patch_sha256") == artifact.patch_sha256
    )


def _binding(
    workspace: Path, task: Mapping[str, Any], artifact: LazarusVersionArtifact
) -> LazarusBackendBinding:
    benchmark = str(task.get("benchmark") or "")
    backend = _BACKENDS.get(benchmark)
    if backend is None:
        raise ValueError(f"Phase-0 calibration has no backend identity for {benchmark!r}")
    return lazarus_backend_binding_from_artifact(
        artifact,
        benchmark=benchmark,
        capture_backend=str(backend["capture_backend"]),
        input_backend=str(backend["input_backend"]),
        binary_paths=tuple(str(path) for path in backend["binary_paths"]),
        canary_passed=_gate_canary_passed(workspace, task, artifact),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    catalog_path = _contained(workspace, args.catalog, "catalog")
    output = _contained(workspace, args.output, "output")
    cache_root = _contained(workspace, args.cache_root, "cache_root")
    credentials = _contained(workspace, args.credentials, "credentials")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(catalog, dict):
        raise ValueError("Lazarus calibration inputs must be mappings")
    _, tasks = validate_lazarus_catalog(catalog, source_path=catalog_path)
    contract = validate_lazarus_phase0_manifest(
        manifest,
        catalog_task_ids=[str(task["id"]) for task in tasks],
    )
    task_by_id = {str(task["id"]): task for task in tasks}
    bindings: list[LazarusBackendBinding] = []
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for task_id in contract.task_ids:
        request = _request_from_task(workspace, task_id)
        pair = materialize_lazarus_versions(
            request,
            cache_root=cache_root,
            build=_build_callback(task_id),
        )
        for artifact in (pair.buggy, pair.fixed):
            binding = _binding(workspace, task_by_id[task_id], artifact)
            bindings.append(binding)
            expected[(binding.task_id, binding.version)] = {
                "benchmark": binding.benchmark,
                "source_tree_sha256": binding.source_tree_sha256,
                "prebuilt_binary_sha256": binding.prebuilt_binary_sha256,
                "capture_backend": binding.capture_backend,
                "input_backend": binding.input_backend,
            }
    manifest_sha = _sha256(manifest_path)
    harness_sha = lazarus_harness_hash(workspace)
    with relay_environment(credentials):
        provider_sha = lazarus_provider_identity_sha256(manifest)
    artifact = build_lazarus_calibration(
        bindings,
        manifest_sha256=manifest_sha,
        harness_sha256=harness_sha,
        provider_identity_sha256=provider_sha,
        observer_model=contract.observer_model,
        grounder_model=contract.grounder_model,
        judge_model=contract.judge_model,
    )
    errors = validate_lazarus_calibration(
        artifact,
        expected_versions=expected,
        manifest_sha256=manifest_sha,
        harness_sha256=harness_sha,
        provider_identity_sha256=provider_sha,
    )
    if errors:
        raise RuntimeError("Lazarus calibration did not self-validate: " + "; ".join(errors))
    _atomic_json(output, artifact)
    print(
        json.dumps(
            {
                "artifact": str(output.relative_to(workspace)),
                "passed": artifact["passed"],
                "bindings": len(bindings),
                "manifest_sha256": manifest_sha,
                "harness_sha256": harness_sha,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if artifact["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
