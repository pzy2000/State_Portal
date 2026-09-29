"""Candidate-specific strict native audit for Luanti #14631.

The shared native allowlist intentionally stays unchanged while this backup
candidate is audited.  The module binds only the current-v2 chain for #14631,
materializes the merged revision from the retained mirror, and runs a fresh
12-cell fixed/buggy matrix in an immutable namespace.  No prior attempts are
loaded and no paid/formal trial is involved.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from . import luanti_native_regression as native
from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .luanti_native_fixed_source_capsule import (
    _materialize,
    _ordinary_directory,
    _read_archive,
    _remove_new_tree,
    _validated_members,
)
from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeRegressionExecutionResult,
    execute_luanti_native_regression,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    NativeRegressionProfile,
    _fixed_capsule_or_block,
    _patch_or_block,
    _production_inverse_plan,
    _raw_evidence_or_block,
    _source_packet_or_block,
    _test_manifest_or_block,
    _toolchain_or_block,
    _profile,
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


CANDIDATE = CandidateKey("luanti", 14631)
PROFILE: NativeRegressionProfile = _profile(
    14631,
    profile_id="luanti-14631-spatial-index-v1-native-gate57-backup",
    production_paths=(
        "irr/include/vector3d.h",
        "src/activeobjectmgr.h",
        "src/benchmark/benchmark_activeobjectmgr.cpp",
        "src/collision.cpp",
        "src/server/activeobjectmgr.cpp",
        "src/server/activeobjectmgr.h",
        "src/server/luaentity_sao.cpp",
        "src/server/player_sao.cpp",
        "src/server/player_sao.h",
        "src/server/serveractiveobject.cpp",
        "src/server/serveractiveobject.h",
        "src/serverenvironment.cpp",
        "src/serverenvironment.h",
    ),
    # The new helper header is retained in the buggy tree so the added test
    # translation unit still compiles while the pre-existing production
    # integration is reverted.  This is a support dependency, not a test
    # path, and is therefore excluded from the production-only inverse.
    support_paths=("src/unittest/CMakeLists.txt", "src/util/k_d_tree.h"),
    test_paths=(
        "src/unittest/test_k_d_tree.cpp",
        "src/unittest/test_serveractiveobjectmgr.cpp",
    ),
    p2p_test_paths=("src/unittest/test_settings.cpp",),
    patch_test_markers=(
        'TEST_CASE("k-d-tree")',
        'TEST_CASE("server active object manager")',
        "DynamicKdTrees",
        "compareObjectsInsideRadius",
    ),
    fixed_test_markers=(
        'TEST_CASE("k-d-tree")',
        'TEST_CASE("server active object manager")',
        'const char *getName() { return "TestSettings"; }',
    ),
    # The standalone k-d-tree test is independent of the regression.  The
    # active-object-manager case exercises the production integration and is
    # the F2P oracle that must fail on the inverse tree.
    f2p_selector="server active object manager",
    p2p_selector="TestSettings",
    platform_policy="portable",
)

_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_NAMESPACE = "fresh-20260814-14631-v2"
_CAPSULE_CONFIRMATION = "MATERIALIZE_LUANTI_14631_NATIVE_FIXED_SOURCE_CAPSULE"
_VERSION = "1.0"


def _install_profile() -> None:
    """Register the candidate only in this process, never in the roster."""

    native._PROFILES[CANDIDATE.change_number] = PROFILE


def materialize_fixed_capsule(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    if confirmation != _CAPSULE_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {_CAPSULE_CONFIRMATION!r}")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("14631 capsule authorization is invalid")
    if authorization.scope != "protocol-migration":
        raise PermissionError("14631 capsule authorization scope is invalid")
    _install_profile()
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _source_packet_or_block(root, PROFILE)
    patch = _patch_for_profile(root, source)
    manifest = _test_manifest_or_block(root, PROFILE, patch)
    if any(isinstance(item, StageDecision) for item in (source, patch, manifest)):
        raise RuntimeError("14631 current source chain is incomplete")
    revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=source.payload["raw_evidence_path"],
        candidate=CANDIDATE,
    )
    mirror = _ordinary_directory(root, _MIRROR, create=False)
    archive = _read_archive(mirror, revision)
    members = _validated_members(archive)
    destination = root / PurePosixPath(PROFILE.fixed_source_tree_path)
    materialized = False
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError("14631 fixed capsule destination is unsafe")
        # A previous candidate-specific capsule may contain the same merged
        # revision but an older binding payload.  Reuse its verified tree and
        # write a new typed binding for the corrected support-path closure.
        _ordinary_directory(root, PurePosixPath(PROFILE.fixed_source_tree_path), create=False)
    else:
        parent = _ordinary_directory(
            root,
            PurePosixPath(*PurePosixPath(PROFILE.fixed_source_tree_path).parts[:-1]),
            create=True,
        )
        _materialize(parent, destination, archive, members)
        materialized = True
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": PROFILE.profile_id,
        "source_tree_path": PROFILE.fixed_source_tree_path,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": list(PROFILE.test_paths),
        "p2p_test_paths": list(PROFILE.p2p_test_paths),
        "support_paths": list(PROFILE.support_paths),
    }
    inputs = (PROFILE.source_packet_reference, PROFILE.patch_reference, manifest.reference)
    try:
        write_stable_artifact(
            root,
            artifact_id=PROFILE.fixed_capsule_reference.artifact_id,
            artifact_kind=PROFILE.fixed_capsule_reference.artifact_kind,
            semantic_version=PROFILE.fixed_capsule_reference.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        artifact = load_stable_artifact(
            root,
            artifact_id=PROFILE.fixed_capsule_reference.artifact_id,
            artifact_kind=PROFILE.fixed_capsule_reference.artifact_kind,
            semantic_version=PROFILE.fixed_capsule_reference.semantic_version,
        )
    except BaseException:
        if materialized:
            _remove_new_tree(destination)
        raise
    if artifact.inputs != inputs or artifact.payload != payload:
        if materialized:
            _remove_new_tree(destination)
        raise RuntimeError("14631 fixed capsule typed reload is invalid")
    return artifact


def build_execution_plan(root: Path) -> NativeRegressionExecutionPlan | StageDecision:
    """Build the strict plan, with a fresh immutable execution namespace."""

    _install_profile()
    original_patch = native._patch_or_block
    native._patch_or_block = _patch_for_profile
    try:
        plan = native.plan_luanti_native_regression_execution(root, candidate=CANDIDATE)
    finally:
        native._patch_or_block = original_patch
    if isinstance(plan, StageDecision):
        return plan
    # The plan itself is already validated against the candidate-specific
    # source/test chain.  The namespace prevents any old attempt/build head
    # from being treated as a resumable result.
    return replace(plan, execution_namespace=_NAMESPACE)


def _patch_for_profile(
    root: Path,
    profile_or_source: NativeRegressionProfile | StableArtifact,
    source: StableArtifact | None = None,
) -> StableArtifact | StageDecision:
    """Bind the persisted screening patch while retyping its header helper.

    The current-v2 screening packet lists the newly added ``k_d_tree.h`` as a
    production path.  For a compilable production-only inverse it is retained
    as a support dependency; all 17 patch sections remain covered exactly.
    """

    # The shared planner supplies (root, profile, source); capsule
    # materialization calls this candidate adapter directly as (root, source).
    source = profile_or_source if source is None else source
    if not isinstance(source, StableArtifact):
        raise ValueError("14631 patch source binding is invalid")
    patch = load_stable_artifact(
        root,
        artifact_id=PROFILE.patch_reference.artifact_id,
        artifact_kind=PROFILE.patch_reference.artifact_kind,
        semantic_version=PROFILE.patch_reference.semantic_version,
    )
    expected_inputs = (
        native._SOURCE_PACKET_INPUT,
        PROFILE.source_packet_reference,
    )
    observed_production = [*PROFILE.production_paths, "src/util/k_d_tree.h"]
    if (
        patch.inputs != expected_inputs
        or source.reference != PROFILE.source_packet_reference
        or patch.payload.get("candidate") != PROFILE.candidate.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != observed_production
        or patch.payload.get("test_paths") != list(PROFILE.test_paths)
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("14631 native test patch is invalid")
    return patch


def execute_fresh(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> NativeRegressionExecutionResult:
    if authorization.scope != "preannotation-execution":
        raise PermissionError("14631 execution authorization scope is invalid")
    _install_profile()
    return execute_luanti_native_regression(
        artifact_root,
        candidate=CANDIDATE,
        authorization=authorization,
        confirmation=EXECUTION_CONFIRMATION,
        planner=lambda root, *, candidate: build_execution_plan(Path(root)),
    )


def persist_gate34(
    artifact_root: str | Path,
    *,
    plan: NativeRegressionExecutionPlan,
    result: NativeRegressionExecutionResult,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if plan.profile.candidate != CANDIDATE or result.aggregate.payload["candidate"] != CANDIDATE.to_value():
        raise ValueError("14631 gate34 identity is invalid")
    fixed_build = ArtifactReference(
        f"native_regression_build/luanti/14631/fixed/build/{_NAMESPACE}",
        "native_regression_build",
        "3.0",
    )
    buggy_build = ArtifactReference(
        f"native_regression_build/luanti/14631/buggy/build/{_NAMESPACE}",
        "native_regression_build",
        "3.0",
    )
    reference = ArtifactReference(
        "candidate_gate34_admission/luanti/14631/native/v1",
        "candidate_gate34_admission",
        _VERSION,
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
        "gate2": {
            "state": "passed",
            "authoritative_issue": 14613,
            "linked_issues": [14613, 14614, 16275],
            "incidental_cross_references": [14614, 16275],
        },
        "gate3": {
            "state": "passed",
            "production_only_inverse": True,
            "inverse_paths": list(plan.production_inverse.paths),
            "test_paths_excluded": list(plan.profile.test_paths),
            "support_paths_excluded": list(plan.profile.support_paths),
        },
        "gate4": {
            "state": "passed",
            "fixed_test_paths": list(plan.profile.test_paths),
            "buggy_api_compile": "passed",
            "fixed_build": fixed_build.artifact_id,
            "buggy_build": buggy_build.artifact_id,
            "fixed_test_selector": plan.profile.f2p_selector,
            "p2p_test_selector": plan.profile.p2p_selector,
        },
        "fresh_gate57_qualified": result.qualified,
        "execution_namespace": _NAMESPACE,
        "executed_attempts": result.executed_attempts,
        "resumed_attempts": result.resumed_attempts,
        "paid_annotation": False,
        "formal_trial": False,
    }
    inputs = (
        plan.profile.source_packet_reference,
        plan.profile.patch_reference,
        plan.profile.test_manifest_reference,
        plan.profile.fixed_capsule_reference,
        fixed_build,
        buggy_build,
        result.aggregate.reference,
    )
    root = Path(artifact_root).absolute().resolve(strict=True)
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
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("14631 gate34 typed reload is invalid")
    return artifact


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CANDIDATE",
    "PROFILE",
    "build_execution_plan",
    "execute_fresh",
    "materialize_fixed_capsule",
    "persist_gate34",
]
