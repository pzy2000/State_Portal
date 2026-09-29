"""Offline server-unit continuation plan for Luanti on-path candidates.

This module records the smallest native runner contract for a candidate whose
changed code and regression test are both in the server unit-test target.  It
only reloads the already accepted source/patch/test chain and writes a typed,
downstream-pending plan.  It never materialises a tree, configures CMake,
starts Luanti, contacts a provider, or performs a paid operation.
"""

from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 14586)
_TOOLCHAIN_ID = "luanti-native-toolchain"
_TOOLCHAIN_VERSION = "3.0"
_TOOLCHAIN_ROOT = "private/dependencies/luanti-native/3.0"
_FIXED_CAPSULE_ROOT = "private/fixed-capsules/luanti/14586/fixed-source"
_PLAN_REFERENCE = ArtifactReference(
    "candidate_native_runner_plan/luanti/14586/preannotation/v1",
    "candidate_native_runner_plan",
    "1.0",
)


@dataclass(frozen=True)
class _ServerUnitSpec:
    candidate: CandidateKey
    issue: int
    profile_id: str
    production_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    test_markers: tuple[str, ...]
    f2p_selector: str
    p2p_selector: str

    @property
    def source_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_source_packet/luanti/14586/source/v2",
            "candidate_source_packet",
            "2.0",
        )

    @property
    def patch_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_test_patch/luanti/14586/screening/v2",
            "candidate_test_patch",
            "2.0",
        )

    @property
    def atomicity_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "source_atomicity/luanti/14586/preannotation/v2",
            "source_atomicity",
            "2.0",
        )

    @property
    def manifest_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_stage/luanti/14586/test_manifest/v2",
            "candidate_stage",
            "2.0",
        )

    @property
    def profile_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_profile/luanti/14586/preflight/v2",
            "candidate_profile",
            "2.0",
        )

    @property
    def preparation_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_preparation/luanti/14586/preannotation/v2",
            "candidate_preparation",
            "2.0",
        )


_SPEC = _ServerUnitSpec(
    candidate=_CANDIDATE,
    issue=14572,
    profile_id="luanti-14586-partial-move-v2",
    production_paths=("src/inventory.cpp",),
    test_paths=("src/unittest/test_moveaction.cpp",),
    test_markers=(
        "testMoveSomewherePartial",
        'apply_action("MoveSomewhere 111 player:p1 main 1 player:p2 main",',
        "default:stone 110",
    ),
    f2p_selector="TestMoveAction",
    p2p_selector="TestSettings",
)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)


@dataclass(frozen=True)
class _PatchSection:
    path: str
    content: bytes


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise RuntimeError(f"{label} is unsafe")
    return path


def _regular_file(root: Path, relative: str, label: str) -> Path:
    path = root.joinpath(*_safe_relative(relative, label).parts)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"{label} is unsafe")
    return path


def _parse_patch(content: bytes) -> tuple[_PatchSection, ...]:
    sections: list[_PatchSection] = []
    current_path: str | None = None
    current: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append(
                    _PatchSection(
                        current_path,
                        "".join(current).encode("utf-8", "surrogateescape"),
                    )
                )
            try:
                parts = shlex.split(line.rstrip("\r\n"))
            except ValueError as error:
                raise RuntimeError("server-unit patch header is invalid") from error
            if (
                len(parts) != 4
                or parts[:2] != ["diff", "--git"]
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("server-unit patch header is invalid")
            current_path = _safe_relative(
                parts[2][2:], "server-unit patch path"
            ).as_posix()
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append(
            _PatchSection(
                current_path,
                "".join(current).encode("utf-8", "surrogateescape"),
            )
        )
    if not sections or len({section.path for section in sections}) != len(sections):
        raise RuntimeError("server-unit patch inventory is invalid")
    return tuple(sections)


def _load_candidate_chain(
    root: Path,
) -> tuple[
    StableArtifact,
    StableArtifact,
    StableArtifact,
    StableArtifact,
    tuple[_PatchSection, ...],
]:
    source = _load(root, _SPEC.source_reference)
    patch = _load(root, _SPEC.patch_reference)
    atomicity = _load(root, _SPEC.atomicity_reference)
    manifest = _load(root, _SPEC.manifest_reference)
    candidate = _SPEC.candidate.to_value()
    source_path = _safe_relative(
        source.payload.get("raw_evidence_path"), "server-unit source manifest"
    ).as_posix()
    source_manifest = _regular_file(
        root, source_path, "server-unit source manifest"
    ).read_text(encoding="utf-8")
    import json

    parsed_manifest = json.loads(source_manifest)
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != candidate
        or source.payload.get("complete") is not True
        or parsed_manifest.get("candidate") != candidate
        or parsed_manifest.get("complete") is not True
        or parsed_manifest.get("provider") != "github"
        or parsed_manifest.get("issue_numbers") != [_SPEC.issue]
        or parsed_manifest.get("patch_raw_path") != patch.payload.get("raw_patch_path")
    ):
        raise RuntimeError("server-unit source authority is invalid")
    pull = parsed_manifest.get("pull")
    if (
        type(pull) is not dict
        or pull.get("number") != 14586
        or pull.get("merged") is not True
    ):
        raise RuntimeError("server-unit merged pull source is invalid")
    patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "server-unit raw patch"
    ).as_posix()
    sections = _parse_patch(
        _regular_file(root, patch_path, "server-unit raw patch").read_bytes()
    )
    expected_paths = _SPEC.production_paths + _SPEC.test_paths
    if (
        patch.inputs != (_ENUMERATION, source.reference)
        or patch.payload.get("candidate") != candidate
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(_SPEC.production_paths)
        or patch.payload.get("test_paths") != list(_SPEC.test_paths)
        or tuple(section.path for section in sections) != expected_paths
    ):
        raise RuntimeError("server-unit patch binding is invalid")
    if (
        atomicity.payload.get("candidate") != candidate
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != _SPEC.issue
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("authoritative_issue_numbers") != [_SPEC.issue]
    ):
        raise RuntimeError("server-unit single-defect mapping is invalid")
    if atomicity.inputs != (
        _ENUMERATION,
        source.reference,
        patch.reference,
        ArtifactReference(
            "screening_checkpoint/luanti/14586/current/v2",
            "screening_checkpoint",
            "2.0",
        ),
    ):
        raise RuntimeError("server-unit atomicity inputs are invalid")
    details = manifest.payload.get("details")
    if (
        manifest.inputs != (patch.reference,)
        or manifest.payload.get("candidate") != candidate
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("complete") is not True
        or type(details) is not dict
        or details.get("executable_test_paths") != list(_SPEC.test_paths)
    ):
        raise RuntimeError("server-unit test manifest is invalid")
    test = next(section for section in sections if section.path == _SPEC.test_paths[0])
    if not all(marker.encode() in test.content for marker in _SPEC.test_markers):
        raise RuntimeError("server-unit test markers are missing")
    return source, patch, atomicity, manifest, sections


def _ordinary_directory_status(root: Path, relative: str) -> bool:
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _receipt_contract(spec: object) -> list[dict[str, object]]:
    """Return the 12-cell fake-runner contract without starting a process."""

    # The object is intentionally duck-typed here: only the two stable
    # selectors are copied into the contract, and no subprocess is reachable.
    f2p = getattr(spec, "f2p_selector")
    p2p = getattr(spec, "p2p_selector")
    cells: list[dict[str, object]] = []
    for phase, selector, buggy_expected in (
        ("f2p", f2p, "failure"),
        ("p2p", p2p, "pass"),
    ):
        for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected)):
            for repetition in range(1, 4):
                cells.append(
                    {
                        "phase": phase,
                        "variant": variant,
                        "repetition": repetition,
                        "selector": selector,
                        "expected": expected,
                    }
                )
    return cells


def _authorization_ok(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti server-unit plan authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _next_created_at(root: Path, authorization: AuthorizationRecord) -> str:
    """Choose a monotonic revision time for an idempotent stable-head update."""

    values = [datetime.fromisoformat(authorization.granted_at.replace("Z", "+00:00"))]
    try:
        current = _load(root, _PLAN_REFERENCE)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        values.append(datetime.fromisoformat(current.created_at.replace("Z", "+00:00")))
    value = max(values).astimezone(timezone.utc) + timedelta(microseconds=1)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def write_server_unit_plan(
    artifact_root: str | Path,
    candidate: CandidateKey = _CANDIDATE,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write and typed-reload the bounded #14586 native runner plan."""

    _authorization_ok(authorization)
    if candidate != _CANDIDATE:
        raise ValueError("Luanti server-unit plan is only registered for luanti/14586")

    root = Path(artifact_root).absolute()
    source, patch, atomicity, manifest, _sections = _load_candidate_chain(root)
    spec = _SPEC

    toolchain_present = _ordinary_directory_status(root, _TOOLCHAIN_ROOT)
    capsule_present = _ordinary_directory_status(root, _FIXED_CAPSULE_ROOT)
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": spec.profile_id,
        "state": "planned",
        "terminal": False,
        "downstream_status": "downstream_pending",
        "source_packet": spec.source_reference.artifact_id,
        "test_patch": spec.patch_reference.artifact_id,
        "test_manifest": spec.manifest_reference.artifact_id,
        "strict_inverse": {
            "production_paths": list(spec.production_paths),
            "preserved_test_paths": list(spec.test_paths),
            "reverse_patch_argv": [
                "git",
                "apply",
                "-R",
                "--whitespace=nowarn",
                "-",
            ],
        },
        "runner_contract": {
            "kind": "luanti_native_server_unit",
            "dependency_id": _TOOLCHAIN_ID,
            "dependency_version": _TOOLCHAIN_VERSION,
            "executable": "bin/luantiserver",
            "configure": [
                "-DBUILD_CLIENT=FALSE",
                "-DBUILD_SERVER=TRUE",
                "-DBUILD_UNITTESTS=TRUE",
                "-DENABLE_CURL=FALSE",
                "-DENABLE_GETTEXT=FALSE",
            ],
            "build": ["--target", "luantiserver", "--parallel", "1"],
            "f2p": {
                "module": "TestMoveAction",
                "selector": spec.f2p_selector,
                "repetitions": 3,
                "fixed_expected": "pass",
                "buggy_expected": "failure",
            },
            "p2p": {
                "module": "TestSettings",
                "selector": spec.p2p_selector,
                "repetitions": 3,
                "fixed_expected": "pass",
                "buggy_expected": "pass",
            },
            "receipt_contract": _receipt_contract(spec),
        },
        "required_order": [
            "fixed_source_capsule",
            "fixed_buggy_materialization",
            "fixed_buggy_server_build",
            "fixed_buggy_f2p_p2p_repetitions",
            "ordered_replay_oracle",
            "construction_golden_closure",
        ],
        "observed_assets": {
            "toolchain_root": _TOOLCHAIN_ROOT,
            "toolchain_present": toolchain_present,
            "fixed_capsule_root": _FIXED_CAPSULE_ROOT,
            "fixed_capsule_present": capsule_present,
        },
        "missing_assets": [
            "fixed_source_capsule",
            "fixed_buggy_materialized_trees",
            "candidate_specific_build_receipts",
            "candidate_specific_fixed_buggy_server_binary",
            "fixed_buggy_f2p_p2p_repetition_receipts",
            "ordered_replay_oracle_and_golden_closure",
        ],
        "resource_policy": {
            "network": False,
            "paid": False,
            "build_started": False,
            "candidate_started": False,
            "execution_requires_separate_preannotation_authorization": True,
        },
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_PLAN_REFERENCE.artifact_id,
        artifact_kind=_PLAN_REFERENCE.artifact_kind,
        semantic_version=_PLAN_REFERENCE.semantic_version,
        created_at=_next_created_at(root, authorization),
        inputs=(
            source.reference,
            patch.reference,
            atomicity.reference,
            manifest.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _PLAN_REFERENCE)
    if artifact.inputs != (
        source.reference,
        patch.reference,
        atomicity.reference,
        manifest.reference,
    ):
        raise RuntimeError("Luanti server-unit plan inputs are invalid")
    if artifact.payload != payload:
        raise RuntimeError("Luanti server-unit plan typed reload mismatch")
    return artifact


__all__ = ["write_server_unit_plan"]
