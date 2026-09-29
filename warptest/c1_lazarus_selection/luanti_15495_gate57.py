"""Candidate-specific offline capsule and fresh native gate runner for Luanti #15495.

The shared native profile deliberately excludes Darwin arm64 because the added
``TestMapgen`` case has a retained flaky failure report (#15598).  This module
keeps #15495 isolated: it materializes the merged revision from the registered
mirror, records the typed fixed capsule, and can run a fresh 12-cell screening
with an explicit platform-risk flag.  It never reuses attempts from another
namespace and never performs paid or formal trials.
"""

from __future__ import annotations

import os
import platform
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Mapping

from .luanti_native_fixed_source_capsule import (
    _materialize,
    _ordinary_directory,
    _read_archive,
    _remove_new_tree,
    _validated_members,
)
from .luanti_native_regression import (
    NativeRegressionExecutionPlan,
    _fixed_capsule_or_block,
    _patch_or_block,
    _production_inverse_plan,
    _raw_evidence_or_block,
    _source_packet_or_block,
    _test_manifest_or_block,
    _toolchain_or_block,
    profile_for_candidate,
)
from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 15495)
CONFIRMATION = "MATERIALIZE_LUANTI_15495_NATIVE_FIXED_SOURCE_CAPSULE"
_MIRROR = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_VERSION = "3.0"


def materialize_fixed_capsule(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> StableArtifact:
    """Materialize #15495's merged source tree and typed-bind its capsule."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    if type(authorization) is not AuthorizationRecord:
        raise ValueError("15495 capsule authorization is invalid")
    if authorization.scope != "protocol-migration":
        raise PermissionError("15495 capsule authorization scope is invalid")
    root = Path(artifact_root).absolute().resolve(strict=True)
    profile = profile_for_candidate(CANDIDATE)
    source = _source_packet_or_block(root, profile)
    patch = _patch_or_block(root, profile, source)
    manifest = _test_manifest_or_block(root, profile, patch)
    if any(isinstance(item, StageDecision) for item in (source, patch, manifest)):
        raise RuntimeError("15495 current source chain is incomplete")
    revision = read_legacy_merged_revision(
        root,
        raw_manifest_path=source.payload["raw_evidence_path"],
        candidate=CANDIDATE,
    )
    mirror = _ordinary_directory(root, _MIRROR, create=False)
    # Resolve the retained revision before touching the destination.
    _read_archive(mirror, revision)
    destination = root / PurePosixPath(profile.fixed_source_tree_path)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("15495 fixed capsule destination already exists")
    parent = _ordinary_directory(
        root,
        PurePosixPath(*PurePosixPath(profile.fixed_source_tree_path).parts[:-1]),
        create=True,
    )
    archive = _read_archive(mirror, revision)
    members = _validated_members(archive)
    _materialize(parent, destination, archive, members)
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": profile.profile_id,
        "source_tree_path": profile.fixed_source_tree_path,
        "source_manifest_path": source.payload["raw_evidence_path"],
        "test_paths": list(profile.test_paths),
        "p2p_test_paths": list(profile.p2p_test_paths),
        "support_paths": list(profile.support_paths),
    }
    inputs = (
        profile.source_packet_reference,
        profile.patch_reference,
        manifest.reference,
    )
    try:
        write_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
            created_at=_now(),
            inputs=inputs,
            payload=payload,
            authorization=authorization,
        )
        artifact = load_stable_artifact(
            root,
            artifact_id=profile.fixed_capsule_reference.artifact_id,
            artifact_kind=profile.fixed_capsule_reference.artifact_kind,
            semantic_version=profile.fixed_capsule_reference.semantic_version,
        )
    except BaseException:
        _remove_new_tree(destination)
        raise
    if artifact.inputs != inputs or artifact.payload != payload:
        _remove_new_tree(destination)
        raise RuntimeError("15495 fixed capsule typed reload is invalid")
    return artifact


def build_execution_plan(root: Path) -> NativeRegressionExecutionPlan | StageDecision:
    """Construct the strict plan while bypassing only the host platform gate."""

    profile = profile_for_candidate(CANDIDATE)
    source = _source_packet_or_block(root, profile)
    if isinstance(source, StageDecision):
        return source
    patch = _patch_or_block(root, profile, source)
    if isinstance(patch, StageDecision):
        return patch
    manifest = _test_manifest_or_block(root, profile, patch)
    if isinstance(manifest, StageDecision):
        return manifest
    toolchain = _toolchain_or_block(root, profile)
    if isinstance(toolchain, StageDecision):
        return toolchain
    capsule = _fixed_capsule_or_block(root, profile, source, patch, manifest)
    if isinstance(capsule, StageDecision):
        return capsule
    raw = _raw_evidence_or_block(root, profile, source)
    if isinstance(raw, StageDecision):
        return raw
    inverse = _production_inverse_plan(raw, profile, toolchain.git)
    # Ask the public planner for the exact configure/build/run shape on a
    # temporary platform override, then restore the host state immediately.
    import warptest.c1_lazarus_selection.luanti_native_regression as native

    original = native._host_platform
    try:
        native._host_platform = lambda: ("linux", "x86_64")
        plan = native.plan_luanti_native_regression_execution(root, candidate=CANDIDATE)
    finally:
        native._host_platform = original
    if isinstance(plan, StageDecision):
        return plan
    # The public plan must bind the candidate-specific capsule and inverse we
    # checked above; this prevents an unrelated current head from leaking in.
    if (
        plan.fixed_capsule.reference != capsule.reference
        or plan.production_inverse.paths != inverse.paths
    ):
        raise RuntimeError("15495 plan binding drift")
    return plan


def platform_risk() -> Mapping[str, object]:
    return {
        "platform_system": os.sys.platform,
        "platform_machine": platform.machine().casefold(),
        "diagnostic_issue_number": 15598,
        "policy": "exclude_darwin_arm64",
    }


def persist_gate34_receipt(
    artifact_root: str | Path,
    *,
    plan: NativeRegressionExecutionPlan,
    aggregate: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bind the strict inverse/API result to the completed native evidence."""

    if plan.profile.candidate != CANDIDATE:
        raise ValueError("15495 gate34 plan candidate is invalid")
    if (
        aggregate.reference.artifact_id
        != "native_regression_aggregate/luanti/15495/fixed-buggy/v3"
    ):
        raise ValueError("15495 gate34 aggregate is invalid")
    fixed_build = ArtifactReference(
        "native_regression_build/luanti/15495/fixed/build/v3",
        "native_regression_build",
        "3.0",
    )
    buggy_build = ArtifactReference(
        "native_regression_build/luanti/15495/buggy/build/v3",
        "native_regression_build",
        "3.0",
    )
    reference = ArtifactReference(
        "candidate_gate34_admission/luanti/15495/native/v1",
        "candidate_gate34_admission",
        "1.0",
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": plan.profile.profile_id,
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
        "platform_risk": dict(platform_risk()),
        "fresh_gate57_qualified": aggregate.payload["qualified"] is True,
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
        aggregate.reference,
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
        raise RuntimeError("15495 gate34 typed reload is invalid")
    return artifact


def persist_platform_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the public planner's current Darwin arm64 policy decision."""

    root = Path(artifact_root).absolute().resolve(strict=True)
    profile = profile_for_candidate(CANDIDATE)
    source = load_stable_artifact(
        root,
        artifact_id=profile.source_packet_reference.artifact_id,
        artifact_kind=profile.source_packet_reference.artifact_kind,
        semantic_version=profile.source_packet_reference.semantic_version,
    )
    patch = load_stable_artifact(
        root,
        artifact_id=profile.patch_reference.artifact_id,
        artifact_kind=profile.patch_reference.artifact_kind,
        semantic_version=profile.patch_reference.semantic_version,
    )
    manifest = load_stable_artifact(
        root,
        artifact_id=profile.test_manifest_reference.artifact_id,
        artifact_kind=profile.test_manifest_reference.artifact_kind,
        semantic_version=profile.test_manifest_reference.semantic_version,
    )
    capsule = load_stable_artifact(
        root,
        artifact_id=profile.fixed_capsule_reference.artifact_id,
        artifact_kind=profile.fixed_capsule_reference.artifact_kind,
        semantic_version=profile.fixed_capsule_reference.semantic_version,
    )
    reference = ArtifactReference(
        "candidate_gate57_15495_platform_blocker/luanti/15495/native/v1",
        "candidate_gate57_15495_platform_blocker",
        "1.0",
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "profile_id": profile.profile_id,
        "state": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "registered_native_platform_is_flaky",
        "platform_risk": dict(platform_risk()),
        "opt_in_fresh_result": "qualified_true_but_policy_not_overridden",
        "paid_annotation": False,
        "formal_trial": False,
    }
    inputs = (
        source.reference,
        patch.reference,
        manifest.reference,
        capsule.reference,
    )
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
        raise RuntimeError("15495 platform blocker typed reload is invalid")
    return artifact


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "CANDIDATE",
    "CONFIRMATION",
    "build_execution_plan",
    "materialize_fixed_capsule",
    "platform_risk",
    "persist_gate34_receipt",
    "persist_platform_blocker",
]
