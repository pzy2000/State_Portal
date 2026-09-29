"""Candidate-owned recovery-v3 downstream hand-off/build for Luanti #15408.

This adapter is deliberately isolated from the older ``application-v2``
namespace and from all historical native attempts.  It reloads the typed
recovery-v3 pair, the capsule-owned Irrlicht receipt, and the two recovery-v3
preflight receipts; then it registers a small native Start/replay/oracle
fixture and runs only the already-configured fixed/buggy server builds with
parallelism one.  A build failure is persisted as a resumable typed blocker.

No candidate runtime, replay, visual capture, paid annotation, or formal trial
is performed here.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_15408_downstream_closure import (
    _CANDIDATE,
    _CLOSURE,
    _PROFILE_ID,
    _try_load,
    _root,
)
from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


# Recovery-v3 only.  The old application-v2 and native build namespaces are
# intentionally not referenced by this module.
_RECOVERY_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15408/fixed-buggy/recovery-v3",
    "candidate_materialization",
    "3.0",
)
_RECOVERY_PREFLIGHT = ArtifactReference(
    "candidate_gate8_preflight/luanti/15408/recovery-v3",
    "candidate_gate8_preflight",
    "1.0",
)
_RECOVERY_CONFIGURE = ArtifactReference(
    "candidate_gate8_preflight/luanti/15408/recovery-v3-configure",
    "candidate_gate8_preflight",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15408/lz-luanti-15408-collision-acceleration/v1",
    "lazarus_task_mapping",
    "1.0",
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/15408/recovery-v3-native/v1",
    "candidate_downstream_plan",
    "1.0",
)
_FIXTURE_REL = PurePosixPath(
    "private/downstream-fixtures/luanti/15408/collision-acceleration-v1"
)
_PAIR_REL = PurePosixPath(
    "private/materialized/luanti/15408/application-recovery-v3"
)
_BUILD_REL = PurePosixPath(
    "private/bounded-builds/luanti/15408/capsule-owned-irrlicht-recovery-v3"
)
_TOOLCHAIN_REL = PurePosixPath("private/dependencies/luanti-native/3.0")
_BUILD_KIND = "candidate_gate8_15408_recovery_v3_build"
_BUILD_NAMESPACE = "recovery-v3"
_BUILD_AGGREGATE = ArtifactReference(
    "candidate_gate8_15408_recovery_v3_build/luanti/15408/recovery-v3",
    "candidate_gate8_15408_recovery_v3_build",
    "1.0",
)
_BUILD_BLOCKER = ArtifactReference(
    "candidate_gate8_15408_recovery_v3_build_blocker/luanti/15408/recovery-v3",
    "candidate_gate8_15408_recovery_v3_build_blocker",
    "1.0",
)

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/15408/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/15408/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/15408/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/15408/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/luanti/15408/native/v1",
    "candidate_gate57_transition",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "native_regression_aggregate/luanti/15408/fixed-buggy/fresh-20260814-15408-v1",
    "native_regression_aggregate",
    "3.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/15408/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _require_scope(authorization: AuthorizationRecord, scope: str) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != scope
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(f"#15408 recovery-v3 authorization {scope!r} is inactive")


def _load_ref(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write_or_check(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#15408 {reference.artifact_id} conflicts")
        return existing
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load_ref(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#15408 {reference.artifact_id} typed reload failed")
    return artifact


def _validate_upstream(root: Path) -> dict[str, StableArtifact]:
    refs = {
        "source": _SOURCE,
        "patch": _PATCH,
        "atomicity": _ATOMICITY,
        "manifest": _MANIFEST,
        "capsule": _CAPSULE,
        "transition": _TRANSITION,
        "aggregate": _AGGREGATE,
        "materialization": _RECOVERY_MATERIALIZATION,
        "closure": _CLOSURE,
        "preflight": _RECOVERY_PREFLIGHT,
        "configure": _RECOVERY_CONFIGURE,
    }
    loaded = {name: _load_ref(root, reference) for name, reference in refs.items()}
    source = loaded["source"]
    patch = loaded["patch"]
    atomicity = loaded["atomicity"]
    manifest = loaded["manifest"]
    transition = loaded["transition"]
    aggregate = loaded["aggregate"]
    materialization = loaded["materialization"]
    closure = loaded["closure"]
    preflight = loaded["preflight"]
    configure = loaded["configure"]
    for name in ("source", "patch", "atomicity", "manifest", "capsule", "transition", "aggregate"):
        if loaded[name].payload.get("candidate") != _CANDIDATE:
            raise RuntimeError(f"#15408 upstream {name} is not candidate-bound")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#15408 source packet is incomplete")
    if patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#15408 patch is not eligible")
    if atomicity.payload.get("outcome") != "accepted" or atomicity.payload.get("primary_issue_number") != 15027:
        raise RuntimeError("#15408 atomicity is not accepted for issue 15027")
    if manifest.payload.get("state") != "passed":
        raise RuntimeError("#15408 test manifest is not passed")
    if transition.payload.get("state") != "passed" or transition.payload.get("outcome") != "qualified":
        raise RuntimeError("#15408 Gate 1-7 transition is not qualified")
    if aggregate.payload.get("qualified") is not True or aggregate.payload.get("attempt_count") != 12:
        raise RuntimeError("#15408 Gate 1-7 aggregate is not qualified")
    if (
        materialization.payload.get("state") != "materialized"
        or materialization.payload.get("namespace") != "application-recovery-v3"
        or materialization.payload.get("complete") is not True
    ):
        raise RuntimeError("#15408 recovery-v3 materialization is not complete")
    if materialization.payload.get("production_inverse", {}).get("observed_changed_paths") != ["src/collision.cpp"]:
        raise RuntimeError("#15408 recovery-v3 inverse scope is not collision-only")
    if closure.payload.get("candidate") != _CANDIDATE or closure.payload.get("complete") is not True:
        raise RuntimeError("#15408 capsule-owned Irrlicht closure is not complete")
    if preflight.payload.get("candidate") != _CANDIDATE or preflight.payload.get("state") != "preflight_ready":
        raise RuntimeError("#15408 recovery-v3 typed preflight is not ready")
    configure_payload = configure.payload
    probe = configure_payload.get("configure_probe")
    if (
        configure_payload.get("candidate") != _CANDIDATE
        or configure_payload.get("state") != "preflight_ready"
        or not isinstance(probe, dict)
        or probe.get("state") != "passed"
        or probe.get("target_not_built") is not True
        or probe.get("variants", {}).get("fixed", {}).get("returncode") != 0
        or probe.get("variants", {}).get("buggy", {}).get("returncode") != 0
    ):
        raise RuntimeError("#15408 recovery-v3 configure preflight is not rc0 for both variants")
    for variant in ("fixed", "buggy"):
        relative = materialization.payload.get(f"{variant}_tree_path")
        if not isinstance(relative, str):
            raise RuntimeError(f"#15408 recovery-v3 {variant} tree path is invalid")
        tree = root / PurePosixPath(relative)
        if tree.is_symlink() or not tree.is_dir():
            raise RuntimeError(f"#15408 recovery-v3 {variant} tree is unavailable")
        if (tree / "src/collision.cpp").is_symlink() or not (tree / "src/collision.cpp").is_file():
            raise RuntimeError(f"#15408 recovery-v3 {variant} production file is unavailable")
    # Ensure the only pair loaded is the new namespace.  This assertion also
    # makes accidental use of the older module's pair path conspicuous.
    if materialization.payload.get("fixed_tree_path") != (_PAIR_REL / "fixed").as_posix():
        raise RuntimeError("#15408 recovery-v3 fixed path drifted")
    return loaded


def _safe_fixture_files() -> dict[str, str]:
    start = {
        "kind": "native_unit_test_start",
        "binary_relative": "bin/luantiserver",
        "argv": ["--run-unittests", "--test-module", "TestCollision"],
        "working_directory": ".",
        "isolated_home": "private/runtime-home/luanti-15408-recovery-v3",
        "start_observation": "capture Unit Test Results and bounded stdout/stderr",
    }
    replay = [
        {"type": "restore_registered_start"},
        {"type": "run_test_module", "selector": "TestCollision", "phase": "f2p"},
        {"type": "run_test_module", "selector": "TestSettings", "phase": "p2p"},
        {"type": "collect_registered_process_observation"},
    ]
    oracle = {
        "goal": "fixed TestCollision passes all registered collision assertions",
        "defect": "buggy TestCollision fails one or more glitched/falling/jumping ground assertions",
        "invariants": [
            "fixed and buggy use the same TestCollision test source",
            "fixed and buggy TestSettings P2P remains passing",
            "the process exits with a bounded result and no unknown/timeout state",
        ],
        "fixed_expected": {"TestCollision": "pass", "TestSettings": "pass"},
        "buggy_expected": {"TestCollision": "failure", "TestSettings": "pass"},
    }
    golden = {
        "common_before_fixed_after_buggy_after": True,
        "pairwise_before_visual_equivalence": True,
        "two_by_two_after_cross_matrix": True,
        "raw_semantic_observations_required": True,
        "same_lineage_required": True,
    }
    return {
        "start.json": json.dumps(start, sort_keys=True, indent=2) + "\n",
        "replay.json": json.dumps(replay, sort_keys=True, indent=2) + "\n",
        "oracle.json": json.dumps(oracle, sort_keys=True, indent=2) + "\n",
        "golden-contract.json": json.dumps(golden, sort_keys=True, indent=2) + "\n",
    }


def _write_fixture(root: Path, files: dict[str, str]) -> str:
    fixture = root / _FIXTURE_REL
    if fixture.exists() and (fixture.is_symlink() or not fixture.is_dir()):
        raise RuntimeError("#15408 recovery-v3 fixture root is unsafe")
    fixture.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, content in files.items():
        target = fixture / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file() or target.read_text(encoding="utf-8") != content:
                raise RuntimeError(f"#15408 recovery-v3 fixture drifted: {relative}")
        else:
            target.write_text(content, encoding="utf-8")
            target.chmod(0o600)
    actual = sorted(
        path.relative_to(fixture).as_posix()
        for path in fixture.rglob("*")
        if path.is_file()
    )
    if actual != sorted(files):
        raise RuntimeError("#15408 recovery-v3 fixture inventory drifted")
    for relative, expected in files.items():
        path = fixture / PurePosixPath(relative)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"#15408 recovery-v3 fixture file is unsafe: {relative}")
        if path.read_text(encoding="utf-8") != expected:
            raise RuntimeError(f"#15408 recovery-v3 fixture content drifted: {relative}")
    return _FIXTURE_REL.as_posix()


def register_mapping_and_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact]:
    """Strictly reload recovery-v3, write its mapping/fixture, and plan build."""

    _require_scope(authorization, "preannotation-planning")
    root = _root(artifact_root)
    _validate_upstream(root)
    files = _safe_fixture_files()
    fixture_root = _write_fixture(root, files)
    mapping_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "issue_number": 15027,
        "task_id": "lz-luanti-15408-collision-acceleration",
        "profile_id": _PROFILE_ID,
        "defect_locus": "at_goal",
        "closure_state": "mapping_registered_fixture_frozen",
        "start_definition": json.loads(files["start.json"]),
        "ordered_replay": json.loads(files["replay.json"]),
        "semantic_oracle": json.loads(files["oracle.json"]),
        "golden_contract": json.loads(files["golden-contract.json"]),
        "fixture_root": fixture_root,
        "fixture_files": sorted(files),
        "candidate_materialization": _RECOVERY_MATERIALIZATION.artifact_id,
        "capsule_owned_irrlicht_closure": _CLOSURE.artifact_id,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": True,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    mapping_inputs = (
        _TRANSITION,
        _RECOVERY_MATERIALIZATION,
        _CLOSURE,
        _RECOVERY_PREFLIGHT,
        _RECOVERY_CONFIGURE,
    )
    mapping = _write_or_check(
        root,
        _MAPPING,
        inputs=mapping_inputs,
        payload=mapping_payload,
        authorization=authorization,
    )
    plan_payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "state": "planned",
        "outcome": "gate8_recovery_v3_ready_for_candidate_build",
        "terminal": False,
        "resumable": True,
        "fresh_epoch": "candidate_gate8_15408_recovery_v3",
        "candidate_materialization": _RECOVERY_MATERIALIZATION.artifact_id,
        "candidate_owned_dependency": _CLOSURE.artifact_id,
        "mapping_reference": _MAPPING.artifact_id,
        "fixture_root": fixture_root,
        "fixed_source_tree_path": (_PAIR_REL / "fixed").as_posix(),
        "buggy_source_tree_path": (_PAIR_REL / "buggy").as_posix(),
        "configure_preflight_reference": _RECOVERY_CONFIGURE.artifact_id,
        "native_runtime": {
            "target": "luantiserver",
            "binary_relative": "bin/luantiserver",
            "fixed_argv": ["--run-unittests", "--test-module", "TestCollision"],
            "buggy_argv": ["--run-unittests", "--test-module", "TestCollision"],
            "p2p_selector": "TestSettings",
            "parallelism": 1,
        },
        "gate_status": {
            "gate8_source_pair": "passed_recovery_v3",
            "gate8_capsule_owned_irrlicht": "passed",
            "gate8_mapping_fixture": "passed",
            "gate8_configure": "passed_rc0_both_variants",
            "gate8_build": "pending",
            "gate9_registered_start_ordered_replay": "pending",
            "gate10_hidden_oracle_semantic_observations": "pending",
            "gate11_common_before_fixed_after_buggy_after_goldens": "pending",
            "gate12_before_equivalence_after_2x2_visual_cross_matrix": "pending",
            "gate13_same_lineage_construction": "pending",
        },
        "required_order": [
            "candidate_owned_mapping_and_fixture",
            "fixed_buggy_build_parallel_1",
            "registered_start_and_ordered_replay",
            "hidden_oracle_and_semantic_observations",
            "common_before_fixed_after_buggy_after_goldens",
            "visual_cross_matrix",
            "same_lineage_construction",
        ],
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
        "next_action": "run candidate-owned recovery-v3 fixed/buggy luantiserver build with --parallel 1",
        "complete": True,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    plan_inputs = (
        _SOURCE,
        _PATCH,
        _ATOMICITY,
        _MANIFEST,
        _CAPSULE,
        _TRANSITION,
        _AGGREGATE,
        _RECOVERY_MATERIALIZATION,
        _CLOSURE,
        _RECOVERY_PREFLIGHT,
        _RECOVERY_CONFIGURE,
        mapping.reference,
    )
    plan = _write_or_check(
        root,
        _PLAN,
        inputs=plan_inputs,
        payload=plan_payload,
        authorization=authorization,
    )
    return mapping, plan


def _build_reference(variant: str) -> ArtifactReference:
    return ArtifactReference(
        f"{_BUILD_KIND}/luanti/15408/{variant}/build/{_BUILD_NAMESPACE}",
        _BUILD_KIND,
        "1.0",
    )


def _process_payload(variant: str, argv: tuple[str, ...], receipt: NativeProcessReceipt) -> dict[str, Any]:
    return {
        "candidate": _CANDIDATE,
        "profile_id": _PROFILE_ID,
        "variant": variant,
        "step": "build",
        "argv": list(argv),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "output_encoding": "base64",
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }


def _load_completed_build_receipt(
    root: Path,
    *,
    variant: str,
    plan: StableArtifact,
) -> StableArtifact | None:
    """Resume only a successful receipt from this exact recovery-v3 lineage."""

    reference = _build_reference(variant)
    artifact = _try_load(root, reference)
    if artifact is None:
        return None
    expected_inputs = (
        plan.reference,
        _RECOVERY_MATERIALIZATION,
        _CLOSURE,
        _RECOVERY_CONFIGURE,
    )
    if artifact.inputs != expected_inputs:
        raise RuntimeError(f"#15408 recovery-v3 {variant} build receipt lineage drifted")
    payload = artifact.payload
    if (
        payload.get("candidate") != _CANDIDATE
        or payload.get("profile_id") != _PROFILE_ID
        or payload.get("variant") != variant
        or payload.get("step") != "build"
        or payload.get("process_outcome") != "completed"
        or payload.get("returncode") != 0
        or payload.get("candidate_execution_performed") is not False
        or payload.get("old_attempts_loaded") is not False
        or payload.get("paid_actions_authorized") is not False
        or payload.get("formal_trials_authorized") is not False
    ):
        raise RuntimeError(f"#15408 recovery-v3 {variant} build receipt is not reusable")
    executable = root / _PAIR_REL / variant / "bin/luantiserver"
    if executable.is_symlink() or not executable.is_file():
        raise RuntimeError(f"#15408 recovery-v3 {variant} executable is unavailable")
    return artifact


def build_recovery_v3(
    artifact_root: str | Path,
    *,
    plan: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Run fixed then buggy recovery-v3 builds, or write one typed blocker."""

    _require_scope(authorization, "infrastructure-audit")
    root = _root(artifact_root)
    _validate_upstream(root)
    if plan.reference != _PLAN or plan.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#15408 recovery-v3 downstream plan binding is invalid")
    mapping = _load_ref(root, _MAPPING)
    if plan.inputs[-1] != mapping.reference or mapping.payload.get("complete") is not True:
        raise RuntimeError("#15408 recovery-v3 mapping is not complete")
    existing = _try_load(root, _BUILD_AGGREGATE)
    if existing is not None:
        if existing.payload.get("outcome") != "bounded_configure_build_passed":
            raise RuntimeError("#15408 recovery-v3 build aggregate is not passed")
        return existing
    toolchain = root / _TOOLCHAIN_REL
    cmake = toolchain / "bin/cmake"
    ninja = toolchain / "bin/ninja"
    if toolchain.is_symlink() or not toolchain.is_dir() or cmake.is_symlink() or not cmake.is_file() or ninja.is_symlink() or not ninja.is_file():
        raise RuntimeError("#15408 recovery-v3 registered native toolchain is unavailable")
    build_root = root / _BUILD_REL
    build_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "HOME": str(root / "private/runtime-home/luanti-15408-recovery-v3"),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(toolchain / "bin"),
    }
    Path(environment["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    receipts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        tree = root / _PAIR_REL / variant
        build_dir = build_root / variant
        if tree.is_symlink() or not tree.is_dir() or build_dir.is_symlink() or not build_dir.is_dir():
            raise RuntimeError(f"#15408 recovery-v3 {variant} tree/build dir is unavailable")
        completed_artifact = _load_completed_build_receipt(
            root,
            variant=variant,
            plan=plan,
        )
        if completed_artifact is not None:
            receipts.append(completed_artifact)
            continue
        argv = (
            str(cmake),
            "--build",
            str(build_dir),
            "--target",
            "luantiserver",
            "--parallel",
            "1",
        )
        receipt = run_bounded_process(argv, cwd=tree, env=environment, timeout_seconds=1800)
        reference = _build_reference(variant)
        payload = _process_payload(variant, argv, receipt)
        artifact = _write_or_check(
            root,
            reference,
            inputs=(plan.reference, _RECOVERY_MATERIALIZATION, _CLOSURE, _RECOVERY_CONFIGURE),
            payload=payload,
            authorization=authorization,
        )
        receipts.append(artifact)
        executable = tree / "bin/luantiserver"
        if receipt.outcome != "completed" or receipt.returncode != 0 or executable.is_symlink() or not executable.is_file():
            blocker_payload: dict[str, Any] = {
                "candidate": _CANDIDATE,
                "state": "blocked",
                "outcome": "infrastructure_blocked",
                "terminal": False,
                "resumable": True,
                "reason": f"recovery_v3_{variant}_build_failed",
                "failed_variant": variant,
                "failed_receipt": artifact.reference.artifact_id,
                "candidate_downstream_plan": plan.reference.artifact_id,
                "candidate_materialization": _RECOVERY_MATERIALIZATION.artifact_id,
                "candidate_capsule_owned_irrlicht": _CLOSURE.artifact_id,
                "candidate_execution_performed": False,
                "old_attempts_loaded": False,
                "old_runtime_inheritance": False,
                "other_candidate_dependency_reused": False,
                "paid_actions_authorized": False,
                "formal_trials_authorized": False,
                "does_not_invalidate_gate_1_7": True,
                "next_action": "inspect this exact recovery-v3 build receipt; no retry without a new typed namespace",
                "complete": True,
                "resource_policy": {"network": False, "paid": False, "formal_trials": False},
            }
            blocker = _write_or_check(
                root,
                _BUILD_BLOCKER,
                inputs=(plan.reference, _RECOVERY_MATERIALIZATION, _CLOSURE, artifact.reference),
                payload=blocker_payload,
                authorization=authorization,
            )
            raise RuntimeError(f"#15408 recovery-v3 {variant} build failed; blocker={blocker.revision_path}")
    output_paths = [(_PAIR_REL / variant / "bin/luantiserver").as_posix() for variant in ("fixed", "buggy")]
    payload = {
        "candidate": _CANDIDATE,
        "state": "passed",
        "outcome": "bounded_configure_build_passed",
        "complete": True,
        "candidate_downstream_plan": plan.reference.artifact_id,
        "candidate_materialization": _RECOVERY_MATERIALIZATION.artifact_id,
        "candidate_capsule_owned_irrlicht": _CLOSURE.artifact_id,
        "variants": ["fixed", "buggy"],
        "steps_per_variant": ["configure_preflight_rc0", "build"],
        "target": "luantiserver",
        "parallelism": 1,
        "build_namespace": _BUILD_NAMESPACE,
        "output_paths": output_paths,
        "build_receipts": [item.reference.artifact_id for item in receipts],
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "other_candidate_dependency_reused": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "gate_1_7_unchanged": True,
        "gate9_to_13": "not_claimed",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    return _write_or_check(
        root,
        _BUILD_AGGREGATE,
        inputs=(plan.reference, _RECOVERY_MATERIALIZATION, _CLOSURE, _RECOVERY_CONFIGURE, *(item.reference for item in receipts)),
        payload=payload,
        authorization=authorization,
    )


def main() -> None:
    root = Path(".warptest-cache/c1_lazarus_selection_v1")
    planning = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-recovery-v3-planning-v1",
        scope="preannotation-planning",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    mapping, plan = register_mapping_and_plan(root, authorization=planning)
    print("mapping", mapping.revision_path)
    print("plan", plan.revision_path)
    infrastructure = AuthorizationRecord(
        authorization_id="authorization/luanti-15408-recovery-v3-build-v1",
        scope="infrastructure-audit",
        granted_at=_now(),
        local_user_id=os.getuid(),
    )
    print("build", build_recovery_v3(root, plan=plan, authorization=infrastructure).revision_path)


if __name__ == "__main__":
    main()


__all__ = ["register_mapping_and_plan", "build_recovery_v3"]
