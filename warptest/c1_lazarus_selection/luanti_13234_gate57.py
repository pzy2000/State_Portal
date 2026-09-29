"""Fresh, candidate-scoped Gate 5--7 execution for Luanti #13234.

This historical Luanti revision has no ``--test-module`` CLI switch.  The
adapter therefore runs its real complete unit-test suite and derives exactly
one selected module result from the native ``======== Module ...`` record.
It preserves raw output and refuses missing, duplicate, empty, timed-out, or
truncated summaries.  No previous #13234 attempts are read.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .luanti_native_executor import (
    EXECUTION_CONFIRMATION as GENERIC_CONFIRMATION,
    NativeProcessReceipt,
    NativeRegressionExecutionResult,
    execute_luanti_native_regression,
    run_bounded_process,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    plan_luanti_native_regression_execution,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_13234_GATE57_CPP17_V1"
_CANDIDATE = CandidateKey("luanti", 13234)
_CANDIDATE_VALUE = _CANDIDATE.to_value()
_SOURCE = ArtifactReference("candidate_source_packet/luanti/13234/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/13234/screening/v2", "candidate_test_patch", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/13234/test_manifest/v2", "candidate_stage", "2.0")
_CAPSULE = ArtifactReference("fixed_source_capsule/luanti/13234/fixed/v2", "fixed_source_capsule", "2.0")
_PROFILE = ArtifactReference("candidate_profile/luanti/13234/preflight/v2", "candidate_profile", "2.0")
_SHARED_IRRLICHT = ArtifactReference("candidate_gate57_11866_irrlicht_dependency/luanti/11866/native/v1", "candidate_gate57_11866_irrlicht_dependency", "1.0")
_DEPENDENCY = ArtifactReference("candidate_gate57_dependency/luanti/13234/irrlichtmt/v1", "candidate_gate57_dependency", "1.0")
_SELECTOR = ArtifactReference("candidate_gate57_selector_adapter/luanti/13234/native/v1", "candidate_gate57_selector_adapter", "1.0")
_TOOLCHAIN = ArtifactReference("candidate_gate57_toolchain/luanti/13234/cpp17/v1", "candidate_gate57_toolchain", "1.0")
_AGGREGATE = ArtifactReference("native_regression_aggregate/luanti/13234/fixed-buggy/cpp17-v1", "native_regression_aggregate", "3.0")
_TERMINAL = ArtifactReference("candidate_gate57_terminal/luanti/13234/native/v1", "candidate_gate57_terminal", "1.0")
_LAUNCHER = "private/fresh-gate57-tools/luanti/13234/cpp17-v1/cxx17-texturelayer-launcher"
_LAUNCHER_CONTENT = (
    b'#!/bin/sh\nset -eu\ncompiler="$1"\nshift\n'
    b'exec "$compiler" "$@" -std=c++17 -DTextureLayer=TextureLayers\n'
)
_MODULE = re.compile(
    rb"(?m)^======== Module ([^\r\n ]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\) - \d+ms\r?$"
)
_NAMESPACE = "cpp17-v1"


def register_luanti_13234_gate57_prerequisites(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Persist only candidate-local dependency, selector, and launcher proof."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, manifest, capsule, profile, shared = tuple(
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _MANIFEST, _CAPSULE, _PROFILE, _SHARED_IRRLICHT)
    )
    _validate_chain(source, patch, manifest, capsule, profile, shared)
    include = root / shared.payload["include_path"]
    if not include.is_dir() or not (include / "irrlicht.h").is_file():
        raise RuntimeError("#13234 registered IrrlichtMt include closure is unavailable")
    dependency_payload = {
        "candidate": _CANDIDATE_VALUE,
        "state": "registered",
        "outcome": "candidate_scoped_reused_irrlichtmt_headers",
        "terminal": False,
        "resumable": True,
        "include_path": shared.payload["include_path"],
        "dependency_version": "1.9.0.15",
        "source_dependency_reference": _SHARED_IRRLICHT.artifact_id,
        "headers_present": True,
        "network_used": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    dependency = _write(root, _DEPENDENCY, (capsule.reference, shared.reference), dependency_payload, authorization)

    fixed_tree = root / capsule.payload["source_tree_path"]
    main = (fixed_tree / "src/main.cpp").read_text(encoding="utf-8")
    craft = (fixed_tree / "src/unittest/test_craft.cpp").read_text(encoding="utf-8")
    settings = (fixed_tree / "src/unittest/test_settings.cpp").read_text(encoding="utf-8")
    if "--test-module" in main or 'getName() { return "TestCraft"; }' not in craft or 'getName() { return "TestSettings"; }' not in settings:
        raise RuntimeError("#13234 historical suite selector closure drifted")
    selector_payload = {
        "candidate": _CANDIDATE_VALUE,
        "state": "registered",
        "outcome": "legacy_full_suite_named_module_summary_adapter",
        "terminal": False,
        "resumable": True,
        "module_listing_source": "fixed_source_test_modules",
        "cli_test_module_supported": False,
        "suite_argv_suffix": ["--run-unittests"],
        "f2p_selector": "TestCraft",
        "p2p_selector": "TestSettings",
        "raw_summary_format": "======== Module NAME passed_or_failed (N failures / N tests) - Nms",
        "selected_module_requires_exactly_one_summary": True,
        "selected_module_requires_positive_test_count": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    selector = _write(root, _SELECTOR, (capsule.reference, profile.reference), selector_payload, authorization)

    launcher = root / _LAUNCHER
    launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if launcher.exists():
        if launcher.is_symlink() or launcher.read_bytes() != _LAUNCHER_CONTENT:
            raise RuntimeError("#13234 C++17 launcher drifted")
    else:
        launcher.write_bytes(_LAUNCHER_CONTENT)
        launcher.chmod(0o700)
    toolchain_payload = {
        "candidate": _CANDIDATE_VALUE,
        "state": "registered",
        "outcome": "source_preserving_cxx17_texturelayer_launcher",
        "terminal": False,
        "resumable": True,
        "wrapper_path": _LAUNCHER,
        "language_standard": "c++17",
        "compatibility_macro": "TextureLayer=TextureLayers",
        "production_source_bytes_modified": False,
        "test_source_bytes_modified": False,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    toolchain = _write(root, _TOOLCHAIN, (dependency.reference, selector.reference), toolchain_payload, authorization)
    return dependency, selector, toolchain


def plan_luanti_13234_gate57(
    artifact_root: str | Path, *, candidate: CandidateKey
) -> NativeRegressionExecutionPlan | StageDecision:
    """Use only the current #13234 chain and its fresh namespace."""

    if candidate != _CANDIDATE:
        raise ValueError("#13234 Gate 5--7 candidate is invalid")
    root = Path(artifact_root).absolute().resolve(strict=True)
    plan = plan_luanti_native_regression_execution(root, candidate=candidate)
    if isinstance(plan, StageDecision):
        return plan
    dependency, selector, toolchain = tuple(_load(root, reference) for reference in (_DEPENDENCY, _SELECTOR, _TOOLCHAIN))
    include = root / dependency.payload["include_path"]
    launcher = root / toolchain.payload["wrapper_path"]
    if not include.is_dir() or not (include / "irrlicht.h").is_file() or not launcher.is_file() or launcher.is_symlink() or launcher.read_bytes() != _LAUNCHER_CONTENT:
        raise RuntimeError("#13234 fresh prerequisite closure is unavailable")
    configure = (
        *plan.configure_argv,
        "-DCMAKE_CXX_STANDARD=17",
        "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
        "-DCMAKE_CXX_EXTENSIONS=OFF",
        f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
        f"-DIRRLICHT_INCLUDE_DIR={include}",
    )
    return replace(plan, configure_argv=configure, execution_namespace=_NAMESPACE, dependency_references=(dependency.reference, selector.reference, toolchain.reference))


def execute_luanti_13234_gate57(
    artifact_root: str | Path, *, authorization: AuthorizationRecord, confirmation: str = CONFIRMATION
) -> NativeRegressionExecutionResult:
    """Build both inverse variants and execute a new twelve-cell epoch."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")

    def planner(root: Path, *, candidate: CandidateKey):
        return plan_luanti_13234_gate57(root, candidate=candidate)

    def process_runner(argv: tuple[str, ...], *, cwd: Path, env: dict[str, str], timeout_seconds: int) -> NativeProcessReceipt:
        if len(argv) != 4 or argv[1:3] != ("--run-unittests", "--test-module"):
            return run_bounded_process(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
        selected = argv[3]
        raw = run_bounded_process((argv[0], "--run-unittests"), cwd=cwd, env=env, timeout_seconds=timeout_seconds)
        if raw.outcome != "completed" or raw.stdout_truncated or raw.stderr_truncated:
            return raw
        summaries = [
            result
            for stream in (raw.stdout, raw.stderr)
            for result in _MODULE.finditer(stream)
            if result.group(1).decode("utf-8", errors="strict") == selected
        ]
        if len(summaries) != 1 or int(summaries[0].group(4)) <= 0:
            return raw
        failed = int(summaries[0].group(3))
        canonical = (
            b"\nUnit Test Results: " + (b"FAILED" if failed else b"PASSED")
            + b"\n    " + str(1 if failed else 0).encode("ascii")
            + b" / 1 failed tests.\n"
        )
        return NativeProcessReceipt("completed", 1 if failed else 0, raw.stdout, raw.stderr + canonical)

    return execute_luanti_native_regression(
        artifact_root,
        candidate=_CANDIDATE,
        authorization=authorization,
        confirmation=GENERIC_CONFIRMATION,
        planner=planner,
        process_runner=process_runner,
    )


def write_luanti_13234_gate57_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist the observed semantic no-go after the complete fresh epoch."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, manifest, capsule, profile, dependency, selector, toolchain, aggregate = tuple(
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _MANIFEST, _CAPSULE, _PROFILE, _DEPENDENCY, _SELECTOR, _TOOLCHAIN, _AGGREGATE)
    )
    if any(value.payload.get("candidate") != _CANDIDATE_VALUE for value in (source, patch, manifest, capsule, profile, dependency, selector, toolchain, aggregate)):
        raise RuntimeError("#13234 terminal chain candidate binding drifted")
    expected_cells = {
        ("f2p", "fixed"): ["pass", "pass", "pass"],
        ("f2p", "buggy"): ["pass", "pass", "pass"],
        ("p2p", "fixed"): ["pass", "pass", "pass"],
        ("p2p", "buggy"): ["pass", "pass", "pass"],
    }
    cells = {
        (item["phase"], item["variant"]): item["observed_outcomes"]
        for item in aggregate.payload.get("cells", [])
    }
    if cells != expected_cells or aggregate.payload.get("attempt_count") != 12 or aggregate.payload.get("stable_repetitions") is not True or aggregate.payload.get("qualified") is not False:
        raise RuntimeError("#13234 fresh aggregate is not the expected semantic no-go")
    payload = {
        "candidate": _CANDIDATE_VALUE,
        "profile_id": "luanti-13234-shapeless-hopcroft-karp-v1-native-gate57",
        "state": "terminal",
        "outcome": "candidate_semantic_mismatch",
        "terminal": True,
        "resumable": False,
        "reason": "buggy_f2p_testcraft_passed_three_of_three",
        "candidate_materialization_performed": True,
        "candidate_execution_performed": True,
        "new_build_started": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "aggregate_reference": _AGGREGATE.artifact_id,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization": "passed",
            "fixed_f2p_3x": "passed",
            "buggy_f2p_3x_failure": "failed_buggy_passed_three_of_three",
            "fixed_buggy_p2p_3x": "passed",
            "gate_8_13": "not_claimed",
        },
        "fresh_epoch": {
            "attempt_count": 12,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "cells": [
                {"phase": phase, "variant": variant, "observed_outcomes": values}
                for (phase, variant), values in sorted(expected_cells.items())
            ],
        },
        "semantic_observation": {
            "f2p_selector": "TestCraft",
            "p2p_selector": "TestSettings",
            "production_paths": ["src/craftdef.cpp", "src/craftdef.h"],
            "explanation": "The preserved TestCraft assertions validate craft-result correctness but do not make the factorial-performance regression fail on the inverse production implementation.",
        },
        "next_action": "do_not_rebuild_or_retry_without_new_candidate_specific_evidence_that_changes_the_buggy_f2p_oracle",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    return _write(
        root,
        _TERMINAL,
        (source.reference, patch.reference, manifest.reference, capsule.reference, profile.reference, dependency.reference, selector.reference, toolchain.reference, aggregate.reference),
        payload,
        authorization,
    )


def _validate_chain(source: StableArtifact, patch: StableArtifact, manifest: StableArtifact, capsule: StableArtifact, profile: StableArtifact, shared: StableArtifact) -> None:
    if any(value.payload.get("candidate") != _CANDIDATE_VALUE for value in (source, patch, manifest, capsule, profile)):
        raise RuntimeError("#13234 prerequisite chain candidate binding drifted")
    if source.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible" or manifest.payload.get("state") != "passed":
        raise RuntimeError("#13234 current source/patch/manifest chain is invalid")
    if profile.payload.get("strict_gate_status", {}).get("test_api_compatibility") != "passed_static_buggy_parent":
        raise RuntimeError("#13234 Gate 4 profile is invalid")
    if shared.payload.get("complete") is not True or shared.payload.get("dependency_version") != "1.9.0.15":
        raise RuntimeError("#13234 reusable header closure is invalid")


def _write(root: Path, reference: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#13234 {reference.artifact_kind} conflicts with current state")
        return existing
    write_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#13234 {reference.artifact_kind} typed reload failed")
    return artifact


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope != "preannotation-execution" or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#13234 Gate 5--7 authorization is inactive")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "execute_luanti_13234_gate57", "plan_luanti_13234_gate57", "register_luanti_13234_gate57_prerequisites", "write_luanti_13234_gate57_terminal"]
