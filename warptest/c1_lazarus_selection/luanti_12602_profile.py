"""Two-layer candidate preflight for Luanti pull request 12602.

This module deliberately stops after the source/patch gates.  It reloads the
already persisted v2 source, patch, source-atomicity and test-manifest heads,
then performs a candidate-specific static inverse-patch and test-API check.
Version materialisation, repetitions, replay, oracle, construction and golden
work are represented as ``downstream_pending``; they are not grounds for
rejecting an otherwise closed candidate profile.

No provider, Git command, build, runtime, or candidate execution is used here.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 12602)
_PROFILE_ID = "luanti-12602-media-override-v1"
_AUTHORITY = "github:luanti-org/luanti"
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SCREENING_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/12602/current/v2", "screening_checkpoint", "2.0"
)
_PRODUCTION_PATHS = ("src/server/mods.cpp",)
_TEST_PATHS = ("src/unittest/test_servermodmanager.cpp",)
_TEST_MARKERS = (
    '#include "util/string.h"',
    "sm.getModsMediaPaths(result);",
    'sm.getModSpec("unittests")->path + DIR_DELIM + "textures"',
    'sm.getModSpec("basenodes")->path + DIR_DELIM + "textures"',
    "std::find(result.begin(), result.end(),",
    "UASSERT(it != result.end());",
)


def _reference(number: int, stage: str) -> ArtifactReference:
    values = {
        "source": (
            f"candidate_source_packet/luanti/{number}/source/v2",
            "candidate_source_packet",
        ),
        "patch": (
            f"candidate_test_patch/luanti/{number}/screening/v2",
            "candidate_test_patch",
        ),
        "atomicity": (
            f"source_atomicity/luanti/{number}/preannotation/v2",
            "source_atomicity",
        ),
        "manifest": (
            f"candidate_stage/luanti/{number}/test_manifest/v2",
            "candidate_stage",
        ),
    }
    artifact_id, artifact_kind = values[stage]
    return ArtifactReference(artifact_id, artifact_kind, "2.0")


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
    if not path.is_file() or info.st_mode & 0o170000 != 0o100000:
        raise RuntimeError(f"{label} is unsafe")
    return path


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise RuntimeError("raw source manifest has duplicate keys")
        result[key] = value
    return result


def _read_manifest(root: Path, relative: str) -> dict[str, object]:
    path = _regular_file(root, relative, "Luanti 12602 source manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Luanti 12602 source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("Luanti 12602 source manifest is invalid")
    return value


@dataclass(frozen=True)
class Luanti12602Profile:
    """Stable candidate metadata for the source/patch layer."""

    candidate: CandidateKey
    primary_issue_number: int
    profile_id: str
    production_paths: tuple[str, ...]
    patch_test_paths: tuple[str, ...]
    f2p_selector: str
    p2p_selector: str
    fixed_source_tree_path: str
    runtime_dependency_id: str
    tier: str = "on_path_candidate"

    def __post_init__(self) -> None:
        if self.candidate != _CANDIDATE:
            raise ValueError("Luanti 12602 profile candidate is invalid")
        if self.primary_issue_number != 12557 or not self.profile_id:
            raise ValueError("Luanti 12602 profile identity is invalid")
        if self.production_paths != _PRODUCTION_PATHS:
            raise ValueError("Luanti 12602 production paths are invalid")
        if self.patch_test_paths != _TEST_PATHS:
            raise ValueError("Luanti 12602 test paths are invalid")
        for value in (
            *self.production_paths,
            *self.patch_test_paths,
            self.fixed_source_tree_path,
        ):
            _safe_relative(value, "Luanti 12602 profile path")
        if not self.f2p_selector or not self.p2p_selector:
            raise ValueError("Luanti 12602 test selectors are invalid")

    @property
    def source_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "source")

    @property
    def patch_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "patch")

    @property
    def atomicity_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "atomicity")

    @property
    def manifest_reference(self) -> ArtifactReference:
        return _reference(self.candidate.change_number, "manifest")

    @property
    def profile_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_profile/luanti/12602/preflight/v2", "candidate_profile", "2.0"
        )

    @property
    def preparation_reference(self) -> ArtifactReference:
        return ArtifactReference(
            "candidate_preparation/luanti/12602/preannotation/v2",
            "candidate_preparation",
            "2.0",
        )


LUANTI_12602_PROFILE = Luanti12602Profile(
    candidate=_CANDIDATE,
    primary_issue_number=12557,
    profile_id=_PROFILE_ID,
    production_paths=_PRODUCTION_PATHS,
    patch_test_paths=_TEST_PATHS,
    f2p_selector="TestServerModManager.testGetModMediaPaths",
    p2p_selector="TestServerModManager.testGetModNames",
    fixed_source_tree_path="private/fixed-capsules/luanti/12602/captured-merged-tree",
    runtime_dependency_id="luanti-native-toolchain-v2",
)


@dataclass(frozen=True)
class _PatchSection:
    path: str
    content: bytes
    additions: tuple[str, ...]
    removals: tuple[str, ...]


@dataclass(frozen=True)
class Luanti12602CandidateChain:
    profile: Luanti12602Profile
    source: StableArtifact
    patch: StableArtifact
    atomicity: StableArtifact
    manifest: StableArtifact
    source_manifest: Mapping[str, Any]
    raw_patch: bytes
    sections: tuple[_PatchSection, ...]


@dataclass(frozen=True)
class Luanti12602Preflight:
    profile: Luanti12602Profile
    chain: Luanti12602CandidateChain
    production_inverse_patch: bytes
    test_api_markers: tuple[str, ...]
    capsule_status: str
    runtime_status: str
    downstream_status: str


def profile_for_candidate(candidate: CandidateKey) -> Luanti12602Profile:
    if candidate != _CANDIDATE:
        raise ValueError("candidate has no registered Luanti 12602 profile")
    return LUANTI_12602_PROFILE


def _parse_patch_sections(content: bytes) -> tuple[_PatchSection, ...]:
    if not content:
        raise RuntimeError("Luanti 12602 raw patch is empty")
    sections: list[tuple[str, list[str]]] = []
    current_path: str | None = None
    current: list[str] = []
    for line in content.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append((current_path, current))
            try:
                parts = shlex.split(line.rstrip("\r\n"))
            except ValueError as error:
                raise RuntimeError("Luanti 12602 patch header is invalid") from error
            if (
                len(parts) != 4
                or parts[:2] != ["diff", "--git"]
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError("Luanti 12602 patch header is invalid")
            current_path = _safe_relative(
                parts[2][2:], "Luanti 12602 patch path"
            ).as_posix()
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, current))
    if not sections or len({path for path, _ in sections}) != len(sections):
        raise RuntimeError("Luanti 12602 patch has duplicate or missing sections")
    result: list[_PatchSection] = []
    for path, lines in sections:
        additions = tuple(
            line[1:].rstrip("\r\n")
            for line in lines
            if line.startswith("+") and not line.startswith("+++")
        )
        removals = tuple(
            line[1:].rstrip("\r\n")
            for line in lines
            if line.startswith("-") and not line.startswith("---")
        )
        result.append(
            _PatchSection(
                path=path,
                content="".join(lines).encode("utf-8", "surrogateescape"),
                additions=additions,
                removals=removals,
            )
        )
    return tuple(result)


def _validate_source_authority(
    profile: Luanti12602Profile,
    source: StableArtifact,
    source_manifest: Mapping[str, Any],
    patch: StableArtifact,
) -> None:
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != profile.candidate.to_value()
        or source.payload.get("complete") is not True
        or type(source.payload.get("raw_evidence_path")) is not str
    ):
        raise RuntimeError("Luanti 12602 source packet is invalid")
    if (
        source_manifest.get("candidate") != profile.candidate.to_value()
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or source_manifest.get("issue_numbers") != [profile.primary_issue_number]
    ):
        raise RuntimeError("Luanti 12602 authority source is incomplete")
    authority = source_manifest.get("authority")
    if (
        type(authority) is not dict
        or authority.get("provider") != "github"
        or authority.get("repository") != "luanti-org/luanti"
    ):
        raise RuntimeError("Luanti 12602 authority identity is invalid")
    pull = source_manifest.get("pull")
    if (
        type(pull) is not dict
        or pull.get("number") != profile.candidate.change_number
        or pull.get("merged") is not True
    ):
        raise RuntimeError("Luanti 12602 merged pull source is invalid")
    if source_manifest.get("patch_raw_path") != patch.payload.get("raw_patch_path"):
        raise RuntimeError("Luanti 12602 source/patch raw path binding is invalid")


def _validate_patch_artifacts(
    profile: Luanti12602Profile,
    patch: StableArtifact,
    source: StableArtifact,
    sections: tuple[_PatchSection, ...],
) -> None:
    if (
        patch.inputs != (_ENUMERATION, source.reference)
        or patch.payload.get("candidate") != profile.candidate.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(profile.production_paths)
        or patch.payload.get("test_paths") != list(profile.patch_test_paths)
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
        or type(patch.payload.get("raw_patch_path")) is not str
    ):
        raise RuntimeError("Luanti 12602 patch artifact is invalid")
    expected = profile.production_paths + profile.patch_test_paths
    actual = tuple(section.path for section in sections)
    if actual != expected:
        raise RuntimeError("Luanti 12602 patch path inventory is invalid")
    for section in sections:
        if section.path in profile.production_paths and (
            not section.additions or not section.removals
        ):
            raise RuntimeError("Luanti 12602 production patch hunk is incomplete")


def _validate_single_defect(
    profile: Luanti12602Profile,
    atomicity: StableArtifact,
    source: StableArtifact,
    patch: StableArtifact,
) -> None:
    if (
        atomicity.inputs
        != (_ENUMERATION, source.reference, patch.reference, _SCREENING_CHECKPOINT)
        or atomicity.payload.get("candidate") != profile.candidate.to_value()
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != profile.primary_issue_number
        or atomicity.payload.get("authoritative_issue_numbers")
        != [profile.primary_issue_number]
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("linked_issue_numbers")
        != [profile.primary_issue_number]
        or atomicity.payload.get("reason") is not None
        or atomicity.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("Luanti 12602 single-defect mapping is invalid")


def _validate_test_manifest(
    profile: Luanti12602Profile,
    manifest: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
) -> None:
    if (
        not manifest.inputs
        or manifest.inputs[0] != patch.reference
        or (len(manifest.inputs) > 1 and manifest.inputs[1] != atomicity.reference)
        or manifest.payload.get("candidate") != profile.candidate.to_value()
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("outcome") is not None
    ):
        raise RuntimeError("Luanti 12602 test manifest is invalid")
    details = manifest.payload.get("details")
    if (
        type(details) is not dict
        or details.get("executable_test_paths") != list(profile.patch_test_paths)
        or details.get("test_count") != len(profile.patch_test_paths)
    ):
        raise RuntimeError("Luanti 12602 executable test manifest is invalid")


def _validate_test_api(
    profile: Luanti12602Profile, sections: tuple[_PatchSection, ...]
) -> tuple[str, ...]:
    test = next(
        section for section in sections if section.path == profile.patch_test_paths[0]
    )
    text = test.content.decode("utf-8", errors="surrogateescape")
    missing = tuple(marker for marker in _TEST_MARKERS if marker not in text)
    if missing:
        raise RuntimeError(
            "Luanti 12602 fixed test API markers are missing: " + ", ".join(missing)
        )
    return _TEST_MARKERS


def load_candidate_chain(
    artifact_root: str | Path, candidate: CandidateKey = _CANDIDATE
) -> Luanti12602CandidateChain:
    """Reload the current typed source/patch/atomicity/manifest chain."""

    root = Path(artifact_root).absolute()
    profile = profile_for_candidate(candidate)
    source, patch, atomicity, manifest = (
        _load(root, reference)
        for reference in (
            profile.source_reference,
            profile.patch_reference,
            profile.atomicity_reference,
            profile.manifest_reference,
        )
    )
    source_path = _safe_relative(
        source.payload.get("raw_evidence_path"), "Luanti 12602 source manifest"
    ).as_posix()
    source_manifest = _read_manifest(root, source_path)
    _validate_source_authority(profile, source, source_manifest, patch)
    raw_patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "Luanti 12602 raw patch"
    ).as_posix()
    raw_patch = _regular_file(
        root, raw_patch_path, "Luanti 12602 raw patch"
    ).read_bytes()
    sections = _parse_patch_sections(raw_patch)
    _validate_patch_artifacts(profile, patch, source, sections)
    _validate_single_defect(profile, atomicity, source, patch)
    _validate_test_manifest(profile, manifest, patch, atomicity)
    _validate_test_api(profile, sections)
    return Luanti12602CandidateChain(
        profile=profile,
        source=source,
        patch=patch,
        atomicity=atomicity,
        manifest=manifest,
        source_manifest=source_manifest,
        raw_patch=raw_patch,
        sections=sections,
    )


def prepare_candidate(
    artifact_root: str | Path, candidate: CandidateKey = _CANDIDATE
) -> Luanti12602Preflight:
    """Prepare gates 1--4 and leave all execution gates pending."""

    root = Path(artifact_root).absolute()
    chain = load_candidate_chain(root, candidate)
    production_inverse = b"".join(
        section.content
        for section in chain.sections
        if section.path in chain.profile.production_paths
    )
    if not production_inverse or any(
        path.encode("utf-8") in production_inverse
        for path in chain.profile.patch_test_paths
    ):
        raise RuntimeError("Luanti 12602 strict inverse patch contains a test path")
    capsule = root.joinpath(
        *_safe_relative(chain.profile.fixed_source_tree_path, "fixed source tree").parts
    )
    capsule_status = (
        "available" if capsule.is_dir() and not capsule.is_symlink() else "pending"
    )
    runtime_status = (
        "registered"
        if (root / "private/dependencies/luanti-native").is_dir()
        else "pending"
    )
    return Luanti12602Preflight(
        profile=chain.profile,
        chain=chain,
        production_inverse_patch=production_inverse,
        test_api_markers=_TEST_MARKERS,
        capsule_status=capsule_status,
        runtime_status=runtime_status,
        downstream_status="downstream_pending",
    )


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    value = max(parsed).astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value.replace("+00:00", "Z")


def _authorization_value(authorization: AuthorizationRecord) -> dict[str, object]:
    return {
        "authorization_id": authorization.authorization_id,
        "scope": authorization.scope,
        "granted_at": authorization.granted_at,
        "local_user_id": authorization.local_user_id,
        "revoked": authorization.revoked,
    }


def _gate_status() -> dict[str, str]:
    return {
        "source_authority": "passed",
        "single_defect_mapping": "passed",
        "strict_inverse_patch": "passed",
        "test_api_compatibility": "passed",
        "fixed_f2p_3x": "downstream_pending",
        "buggy_f2p_3x": "downstream_pending",
        "fixed_buggy_p2p_3x": "downstream_pending",
        "version_materialization": "downstream_pending",
        "replay_oracle": "downstream_pending",
        "construction_golden": "downstream_pending",
    }


def _screening_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti 12602 profile authorization is inactive")


def write_profile(
    artifact_root: str | Path,
    candidate: CandidateKey = _CANDIDATE,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and typed-reload the gates-1--4 candidate profile."""

    _screening_authorization(authorization)
    preparation = prepare_candidate(artifact_root, candidate)
    profile = preparation.profile
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "tier": profile.tier,
        "authority": _AUTHORITY,
        "primary_issue_number": profile.primary_issue_number,
        "production_paths": list(profile.production_paths),
        "test_paths": list(profile.patch_test_paths),
        "f2p_selector": profile.f2p_selector,
        "p2p_selector": profile.p2p_selector,
        "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        "strict_gate_status": _gate_status(),
        "fixed_source_tree_path": profile.fixed_source_tree_path,
        "fixed_source_tree_status": preparation.capsule_status,
        "runtime_dependency_id": profile.runtime_dependency_id,
        "runtime_status": preparation.runtime_status,
        "downstream_status": preparation.downstream_status,
        "paid_actions_authorized": False,
        "candidate_execution_performed": False,
    }
    created_at = _created_after(
        preparation.chain.source.created_at,
        preparation.chain.patch.created_at,
        preparation.chain.atomicity.created_at,
        preparation.chain.manifest.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        artifact_root,
        artifact_id=profile.profile_reference.artifact_id,
        artifact_kind=profile.profile_reference.artifact_kind,
        semantic_version=profile.profile_reference.semantic_version,
        created_at=created_at,
        inputs=(
            preparation.chain.source.reference,
            preparation.chain.patch.reference,
            preparation.chain.atomicity.reference,
            preparation.chain.manifest.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), profile.profile_reference)
    if artifact.payload != payload:
        raise RuntimeError("Luanti 12602 profile typed reload mismatch")
    return artifact


def write_preparation(
    artifact_root: str | Path,
    candidate: CandidateKey = _CANDIDATE,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the downstream-pending continuation receipt."""

    _screening_authorization(authorization)
    preparation = prepare_candidate(artifact_root, candidate)
    profile = preparation.profile
    profile_artifact = _load(Path(artifact_root).absolute(), profile.profile_reference)
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": profile.profile_id,
        "state": "prepared",
        "outcome": "downstream_pending",
        "profile_reference": profile.profile_reference.artifact_id,
        "fixed_source_tree_path": profile.fixed_source_tree_path,
        "fixed_source_tree_status": preparation.capsule_status,
        "production_inverse_paths": list(profile.production_paths),
        "preserved_test_paths": list(profile.patch_test_paths),
        "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        "f2p": {
            "selector": profile.f2p_selector,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
        },
        "p2p": {
            "selector": profile.p2p_selector,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
        },
        "strict_gate_status": _gate_status(),
        "runtime_dependency_id": profile.runtime_dependency_id,
        "runtime_status": preparation.runtime_status,
        "downstream_status": preparation.downstream_status,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    created_at = _created_after(profile_artifact.created_at, authorization.granted_at)
    write_stable_artifact(
        artifact_root,
        artifact_id=profile.preparation_reference.artifact_id,
        artifact_kind=profile.preparation_reference.artifact_kind,
        semantic_version=profile.preparation_reference.semantic_version,
        created_at=created_at,
        inputs=(profile_artifact.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), profile.preparation_reference)
    if artifact.payload != payload:
        raise RuntimeError("Luanti 12602 preparation typed reload mismatch")
    return artifact


__all__ = [
    "LUANTI_12602_PROFILE",
    "Luanti12602CandidateChain",
    "Luanti12602Preflight",
    "Luanti12602Profile",
    "load_candidate_chain",
    "prepare_candidate",
    "profile_for_candidate",
    "write_preparation",
    "write_profile",
]
