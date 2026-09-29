"""Current-v2, no-inheritance audit for Luanti changes 15495 and 14685.

This module deliberately stops before native configure/build/run.  It binds a
candidate-specific receipt to the current source/patch/atomicity/test chain and
records why a fresh Gate 5--7 epoch cannot be claimed yet.  In particular, the
old ``native_regression_*`` namespace is never loaded as evidence: its presence
is irrelevant to the receipt and no attempt result is inherited.

Luanti #15495 has a current fixed capsule and the registered native profile, but
the current host is Darwin arm64 and that profile's policy excludes this
platform because of the authoritative flaky-test report #15598.  Luanti #14685
has an accepted source/patch chain, but no candidate-specific profile or fixed
capsule; five non-production patch sections therefore cannot be typed as
support/fixture files.  Both receipts are resumable nonterminal blockers.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_CANDIDATES: Mapping[int, Mapping[str, Any]] = {
    15495: {
        "primary_issue": 15485,
        "linked_issues": (15485, 15598),
        "issue_title": "Grass generates 1 node below water",
        "profile_id": "luanti-15495-biome-boundary-v2",
        "production_paths": (
            "src/mapgen/cavegen.cpp",
            "src/mapgen/mapgen.cpp",
            "src/mapgen/mg_biome.cpp",
            "src/mapgen/mg_biome.h",
            "src/script/lua_api/l_mapgen.cpp",
        ),
        "support_paths": ("src/unittest/CMakeLists.txt",),
        "test_paths": ("src/unittest/test_mapgen.cpp",),
        "capsule_id": "fixed_source_capsule/luanti/15495/fixed/v2",
        "capsule_kind": "fixed_source_capsule",
        "capsule_version": "2.0",
        "platform_policy": "exclude_darwin_arm64",
        "diagnostic_issue": 15598,
        "candidate_profile_registered": True,
    },
    14685: {
        "primary_issue": 9673,
        "linked_issues": (9673,),
        "issue_title": "Support for more modern mesh format(s)  (.gltf)",
        "profile_id": None,
        "production_paths": (
            "games/devtest/mods/gltf/init.lua",
            "irr/include/IAnimatedMesh.h",
            "irr/include/IAnimatedMeshSceneNode.h",
            "irr/include/ISkinnedMesh.h",
            "irr/include/SAnimatedMesh.h",
            "irr/include/vector2d.h",
            "irr/src/CAnimatedMeshSceneNode.cpp",
            "irr/src/CAnimatedMeshSceneNode.h",
            "irr/src/CB3DMeshFileLoader.cpp",
            "irr/src/CGLTFMeshFileLoader.cpp",
            "irr/src/CGLTFMeshFileLoader.h",
            "irr/src/CMeshManipulator.cpp",
            "irr/src/CSkinnedMesh.cpp",
            "irr/src/CSkinnedMesh.h",
            "irr/src/CXMeshFileLoader.cpp",
            "src/client/content_cao.cpp",
            "src/client/content_cao.h",
            "src/gui/guiScene.cpp",
            "src/gui/guiScene.h",
            "src/network/clientpackethandler.cpp",
            "src/network/networkprotocol.cpp",
            "src/player.h",
            "src/remoteplayer.h",
            "src/script/lua_api/l_object.cpp",
            "src/server.cpp",
            "src/server.h",
        ),
        "support_paths": (),
        "test_paths": ("src/unittest/test_irr_gltf_mesh_loader.cpp",),
        "capsule_id": None,
        "capsule_kind": None,
        "capsule_version": None,
        "platform_policy": "unregistered",
        "diagnostic_issue": None,
        "candidate_profile_registered": False,
    },
}


def write_luanti_gate57_blocker(
    artifact_root: str | Path,
    change_number: int,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write one current-v2-bound, no-execution Gate 5--7 blocker.

    Only #15495 and #14685 are accepted.  The function does not read old
    attempts, aggregate receipts, build receipts, or historical runtime data.
    """

    spec = _CANDIDATES.get(change_number)
    if spec is None:
        raise ValueError("only Luanti #15495 and #14685 are supported")
    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_current_chain(root, change_number)
    enum, source, patch, screening, atomicity, manifest, checkpoint = chain
    candidate = {"repository_key": "luanti", "change_number": change_number}
    authority = _audit_authority(root, source, spec, change_number)
    patch_audit = _audit_patch(root, patch, spec)
    capsule_audit = _audit_capsule(root, source, spec, candidate)
    runtime_audit = _audit_runtime_assets(root, change_number, spec)
    platform_audit = _platform_audit(spec)

    if change_number == 15495:
        state = "infrastructure_blocked"
        outcome = "fresh_gate57_not_started_darwin_policy"
        blockers = [
            "registered_native_platform_policy_excludes_darwin_arm64",
            "fresh_12_cell_epoch_requires_explicit_non_darwin_worker_or_policy_override",
        ]
        replacement = (
            "retain #15495 pending only if an explicit Linux x86_64 worker or policy "
            "override is authorized; otherwise choose a current-v2 candidate with a "
            "portable profile and capsule, without inheriting #15495 attempts"
        )
        gate3 = "passed_static_production_inverse_scope"
        gate4 = "pending_fresh_api_preflight"
    else:
        state = "infrastructure_blocked"
        outcome = "fresh_gate57_not_started_missing_candidate_profile_capsule"
        blockers = [
            "candidate_specific_native_profile_not_registered",
            "fixed_source_capsule_not_registered",
            "five_nonproduction_patch_sections_are_unclassified_support_or_fixture_paths",
        ]
        replacement = (
            "do not use #14685 as an immediate replacement; retain it only after a "
            "fresh profile, exact support/fixture partition, and fixed capsule are "
            "registered, or replace with another current-v2 candidate that already "
            "has those assets"
        )
        gate3 = "pending_candidate_profile_partition"
        gate4 = "blocked_missing_candidate_profile_and_capsule"

    payload: dict[str, Any] = {
        "candidate": candidate,
        "state": state,
        "outcome": outcome,
        "terminal": False,
        "resumable": True,
        "candidate_pool_status": "pending_fresh_gate57_infrastructure_closure",
        "blockers": blockers,
        "replacement_suggestion": replacement,
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "new_runtime_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "old_aggregate_inherited": False,
        "old_build_receipt_inherited": False,
        "roster_modified": False,
        "paid_annotation": False,
        "formal_trial": False,
        "downstream_8_13_authorized": False,
        "current_gate_status": {
            "gate_1_source_authority": "passed_merged_pull_and_authoritative_issue",
            "gate_2_single_defect_mapping": "passed",
            "gate_3_strict_inverse_patch": gate3,
            "gate_4_fixed_buggy_api_compatibility": gate4,
            "gate_5_fixed_f2p_3x": "not_run_fresh",
            "gate_6_buggy_f2p_3x_failure": "not_run_fresh",
            "gate_7_fixed_buggy_p2p_3x": "not_run_fresh",
        },
        "authority": authority,
        "patch": patch_audit,
        "fixed_capsule": capsule_audit,
        "runtime_assets": runtime_audit,
        "platform_policy": platform_audit,
        "fresh_gate57_epoch": {
            "required_cell_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "candidate_specific_runner_created": False,
            "fresh_namespace": f"candidate_gate57_{change_number}_current_v2",
        },
        "current_chain": {
            "source": source.reference.artifact_id,
            "patch": patch.reference.artifact_id,
            "screening": screening.reference.artifact_id,
            "atomicity": atomicity.reference.artifact_id,
            "manifest": manifest.reference.artifact_id,
            "checkpoint": checkpoint.reference.artifact_id,
            "enumeration": enum.reference.artifact_id,
        },
        "next_action": (
            "resolve the typed infrastructure blocker, then create a new candidate-specific "
            "12-cell namespace and re-audit Gate 4 before any Gate 5--7 execution"
        ),
        "resource_policy": {
            "network": False,
            "build": False,
            "run": False,
            "paid": False,
            "formal_trials": False,
            "downstream_8_13": False,
        },
    }
    reference = ArtifactReference(
        f"candidate_gate57_{change_number}_current_v2_blocker/luanti/{change_number}/native/v1",
        f"candidate_gate57_{change_number}_current_v2_blocker",
        "1.0",
    )
    inputs = tuple(item.reference for item in chain)
    if capsule_audit["registered"]:
        inputs += (
            ArtifactReference(
                str(spec["capsule_id"]),
                str(spec["capsule_kind"]),
                str(spec["capsule_version"]),
            ),
        )
    existing = _try_load(root, reference)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("Luanti current-v2 blocker conflicts with exact state")
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
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("Luanti current-v2 blocker typed reload failed")
    return artifact


def write_luanti_gate57_blockers(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> dict[int, StableArtifact]:
    """Write both candidate receipts without starting any execution."""

    return {
        number: write_luanti_gate57_blocker(
            artifact_root, number, authorization=authorization
        )
        for number in (15495, 14685)
    }


def _load_current_chain(
    root: Path, change_number: int
) -> tuple[StableArtifact, ...]:
    prefix = f"luanti/{change_number}"
    refs = (
        _ENUMERATION,
        ArtifactReference(
            f"candidate_source_packet/{prefix}/source/v2",
            "candidate_source_packet",
            "2.0",
        ),
        ArtifactReference(
            f"candidate_test_patch/{prefix}/screening/v2", "candidate_test_patch", "2.0"
        ),
        ArtifactReference(
            f"screening_checkpoint/{prefix}/current/v2", "screening_checkpoint", "2.0"
        ),
        ArtifactReference(
            f"source_atomicity/{prefix}/preannotation/v2", "source_atomicity", "2.0"
        ),
        ArtifactReference(
            f"candidate_stage/{prefix}/test_manifest/v2", "candidate_stage", "2.0"
        ),
        ArtifactReference(
            f"preannotation_checkpoint/{prefix}/current/v2",
            "preannotation_checkpoint",
            "2.0",
        ),
    )
    chain = tuple(_load(root, reference) for reference in refs)
    enum, source, patch, screening, atomicity, manifest, checkpoint = chain
    candidate = {"repository_key": "luanti", "change_number": change_number}
    if enum.payload.get("repository_key") != "luanti" or enum.payload.get("complete") is not True:
        raise RuntimeError("Luanti enumeration is not complete")
    if source.inputs != (enum.reference,) or source.payload.get("candidate") != candidate:
        raise RuntimeError("Luanti source packet is not current-v2 bound")
    if source.payload.get("complete") is not True:
        raise RuntimeError("Luanti source packet is incomplete")
    if patch.inputs != (enum.reference, source.reference) or patch.payload.get("candidate") != candidate:
        raise RuntimeError("Luanti patch packet is not current-v2 bound")
    if patch.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("Luanti patch packet is not eligible")
    if screening.inputs != (enum.reference, patch.reference) or screening.payload.get("candidate") != candidate:
        raise RuntimeError("Luanti screening checkpoint is not current-v2 bound")
    if screening.payload.get("state") != "complete" or screening.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("Luanti screening checkpoint is not complete")
    spec = _CANDIDATES[change_number]
    if atomicity.inputs != (enum.reference, source.reference, patch.reference, screening.reference):
        raise RuntimeError("Luanti source atomicity inputs drifted")
    if (
        atomicity.payload.get("candidate") != candidate
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [spec["primary_issue"]]
        or atomicity.payload.get("linked_issue_numbers") != list(spec["linked_issues"])
    ):
        raise RuntimeError("Luanti source atomicity is not accepted")
    if manifest.inputs != (patch.reference,) or manifest.payload.get("candidate") != candidate:
        raise RuntimeError("Luanti test manifest is not current-v2 bound")
    if manifest.payload.get("complete") is not True or manifest.payload.get("state") != "passed":
        raise RuntimeError("Luanti test manifest is not passed")
    if checkpoint.inputs != (manifest.reference,) or checkpoint.payload.get("candidate") != candidate:
        raise RuntimeError("Luanti preannotation checkpoint is not current-v2 bound")
    if checkpoint.payload.get("state") != "active":
        raise RuntimeError("Luanti preannotation checkpoint is not active")
    return chain


def _audit_authority(
    root: Path,
    source: StableArtifact,
    spec: Mapping[str, Any],
    change_number: int,
) -> dict[str, Any]:
    relative = _relative_path(source.payload.get("raw_evidence_path"), "source manifest")
    manifest = _read_json(root, relative)
    authority = manifest.get("authority")
    pull = manifest.get("pull")
    candidate = {"repository_key": "luanti", "change_number": change_number}
    if (
        manifest.get("candidate") != candidate
        or manifest.get("complete") is not True
        or manifest.get("provider") != "github"
        or type(authority) is not dict
        or authority.get("provider") != "github"
        or authority.get("repository") != "luanti-org/luanti"
        or type(pull) is not dict
        or pull.get("number") != change_number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("Luanti authority manifest drifted")
    issue_numbers = manifest.get("issue_numbers")
    if issue_numbers != list(spec["linked_issues"]):
        raise RuntimeError("Luanti linked issue set drifted")
    issues = manifest.get("issues")
    if type(issues) is not list:
        raise RuntimeError("Luanti issue authority rows are missing")
    issue_rows: dict[int, dict[str, Any]] = {}
    for row in issues:
        if type(row) is not dict:
            continue
        number = row.get("number")
        nested = row.get("issue")
        if type(nested) is dict:
            number = nested.get("number", number)
            row = nested
        if type(number) is int:
            issue_rows[number] = row
    primary = issue_rows.get(int(spec["primary_issue"]))
    if type(primary) is not dict or primary.get("title") != spec["issue_title"]:
        raise RuntimeError("Luanti authoritative issue does not match candidate")
    body = primary.get("body") if type(primary.get("body")) is str else ""
    if not body.strip():
        raise RuntimeError("Luanti authoritative issue body is empty")
    pull_body = pull.get("body") if type(pull.get("body")) is str else ""
    if f"#{spec['primary_issue']}" not in pull_body and change_number != 14685:
        raise RuntimeError("Luanti pull does not bind the authoritative issue")
    if change_number == 14685 and "9673" not in pull_body:
        raise RuntimeError("Luanti #14685 pull does not close #9673")
    diagnostics: list[dict[str, Any]] = []
    for number in spec["linked_issues"]:
        row = issue_rows.get(int(number))
        if type(row) is not dict:
            raise RuntimeError(f"Luanti linked issue #{number} is missing")
        diagnostics.append({"number": number, "title": row.get("title")})
    return {
        "provider": "github",
        "repository": "luanti-org/luanti",
        "pull_number": change_number,
        "merged_pull": True,
        "primary_issue_number": spec["primary_issue"],
        "issue_title": spec["issue_title"],
        "linked_issues": diagnostics,
        "single_authoritative_issue": True,
        "real_bug_evidence": True,
        "source_manifest_path": relative,
    }


def _audit_patch(
    root: Path, patch: StableArtifact, spec: Mapping[str, Any]
) -> dict[str, Any]:
    relative = _relative_path(patch.payload.get("raw_patch_path"), "raw patch")
    content = _read_bytes(root, relative).decode("utf-8", "surrogateescape")
    paths: list[str] = []
    for line in content.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = shlex.split(line)
        if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
            raise RuntimeError("Luanti raw patch header is invalid")
        if parts[2][2:] != parts[3][2:]:
            raise RuntimeError("Luanti raw patch path pair is not identical")
        paths.append(parts[2][2:])
    production = tuple(spec["production_paths"])
    tests = tuple(spec["test_paths"])
    if tuple(patch.payload.get("production_paths", ())) != production:
        raise RuntimeError("Luanti typed production path list drifted")
    if tuple(patch.payload.get("test_paths", ())) != tests:
        raise RuntimeError("Luanti typed test path list drifted")
    if len(paths) != len(set(paths)) or any(path not in paths for path in production + tests):
        raise RuntimeError("Luanti raw patch is missing a typed production/test path")
    extras = [path for path in paths if path not in production + tests]
    support = tuple(spec["support_paths"])
    if support and tuple(extras) != support:
        raise RuntimeError("Luanti #15495 support path partition drifted")
    inverse_ready = bool(spec["candidate_profile_registered"]) and tuple(extras) == support
    return {
        "raw_patch_path": relative,
        "raw_diff_path_count": len(paths),
        "production_paths": list(production),
        "test_paths": list(tests),
        "unclassified_nonproduction_paths": extras,
        "support_paths": list(support),
        "production_only_inverse_static": inverse_ready,
        "strict_inverse_ready": inverse_ready,
        "preserved_test_hunks": True,
    }


def _audit_capsule(
    root: Path,
    source: StableArtifact,
    spec: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    capsule_id = spec.get("capsule_id")
    if not capsule_id:
        return {
            "registered": False,
            "status": "missing_candidate_specific_fixed_source_capsule",
            "source_tree_path": None,
            "ordinary_directory": False,
        }
    reference = ArtifactReference(
        str(capsule_id), str(spec["capsule_kind"]), str(spec["capsule_version"])
    )
    capsule = _load(root, reference)
    payload = capsule.payload
    if payload.get("candidate") != candidate or payload.get("profile_id") != spec["profile_id"]:
        raise RuntimeError("Luanti fixed capsule candidate binding drifted")
    source_manifest = _relative_path(source.payload.get("raw_evidence_path"), "source manifest")
    if payload.get("source_manifest_path") != source_manifest:
        raise RuntimeError("Luanti fixed capsule source manifest drifted")
    tree_relative = _relative_path(payload.get("source_tree_path"), "fixed capsule tree")
    tree = root / tree_relative
    if not _ordinary_directory(tree):
        raise RuntimeError("Luanti fixed capsule tree is not an ordinary directory")
    return {
        "registered": True,
        "status": "registered",
        "artifact_id": capsule.reference.artifact_id,
        "source_tree_path": tree_relative,
        "ordinary_directory": True,
        "profile_id": spec["profile_id"],
    }


def _audit_runtime_assets(
    root: Path, change_number: int, spec: Mapping[str, Any]
) -> dict[str, Any]:
    runtime_root = root / "private" / "dependencies" / "luanti-native" / "3.0"
    profile_dir = root / "artifacts" / "candidate_profile" / "luanti" / str(change_number) / "preflight" / "v2" / "2.0"
    preparation_dir = root / "artifacts" / "candidate_preparation" / "luanti" / str(change_number) / "preannotation" / "v2" / "2.0"
    fixed_build_dir = root / "artifacts" / "native_regression_build" / "luanti" / str(change_number) / "fixed" / "build" / "v3" / "3.0"
    buggy_build_dir = root / "artifacts" / "native_regression_build" / "luanti" / str(change_number) / "buggy" / "build" / "v3" / "3.0"
    return {
        "registered_toolchain_root": _ordinary_directory(runtime_root),
        "candidate_profile_artifact_registered": _ordinary_directory(profile_dir),
        "candidate_preparation_artifact_registered": _ordinary_directory(preparation_dir),
        "prior_fixed_build_receipt_present_but_ignored": _ordinary_directory(fixed_build_dir),
        "prior_buggy_build_receipt_present_but_ignored": _ordinary_directory(buggy_build_dir),
        "candidate_profile_code_registered": bool(spec["candidate_profile_registered"]),
        "build_or_run_started_this_epoch": False,
    }


def _platform_audit(spec: Mapping[str, Any]) -> dict[str, Any]:
    system = platform.system().casefold()
    machine = platform.machine().casefold()
    excluded = spec["platform_policy"] == "exclude_darwin_arm64" and system == "darwin" and machine in {"arm64", "aarch64"}
    return {
        "platform_system": system,
        "platform_machine": machine,
        "policy": spec["platform_policy"],
        "diagnostic_issue_number": spec["diagnostic_issue"],
        "policy_excludes_current_host": excluded,
        "fresh_run_requires_explicit_override": excluded,
        "override_applied": False,
    }


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _read_json(root: Path, relative: str) -> dict[str, Any]:
    path = root / PurePosixPath(relative)
    if not _ordinary_file(path):
        raise RuntimeError("Luanti raw authority path is not an ordinary file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("Luanti raw authority document is not an object")
    return value


def _read_bytes(root: Path, relative: str) -> bytes:
    path = root / PurePosixPath(relative)
    if not _ordinary_file(path):
        raise RuntimeError("Luanti raw patch path is not an ordinary file")
    return path.read_bytes()


def _relative_path(value: object, label: str) -> str:
    if type(value) is not str or not value or value.startswith("/"):
        raise RuntimeError(f"Luanti {label} path is invalid")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"Luanti {label} path escapes artifact root")
    return str(path)


def _ordinary_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _ordinary_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti Gate 5--7 blocker authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_gate57_blocker", "write_luanti_gate57_blockers"]
