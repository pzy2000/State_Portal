"""Two-layer source/patch preflight for Luanti on-path candidates.

The profile layer is intentionally bounded.  It reloads the typed v2 source,
patch, atomicity and test-manifest artifacts that already exist in the local
cache and performs a candidate-specific static inverse/test-API audit.  It
does not contact a provider, build Luanti, execute a candidate, or start a
paid operation.  Version materialisation, repetitions, replay, oracle and
golden work remain ``downstream_pending`` and are recorded by the preparation
receipt instead of being used as rejection gates.
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


_AUTHORITY = "github:luanti-org/luanti"
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)


@dataclass(frozen=True)
class LuantiOnPathSpec:
    number: int
    issue: int
    profile_id: str
    production_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    test_markers: tuple[str, ...]
    f2p_selector: str
    p2p_selector: str
    inventory_note: str | None = None

    @property
    def candidate(self) -> CandidateKey:
        return CandidateKey("luanti", self.number)

    @property
    def source_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_source_packet/luanti/{self.number}/source/v2",
            "candidate_source_packet",
            "2.0",
        )

    @property
    def patch_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_test_patch/luanti/{self.number}/screening/v2",
            "candidate_test_patch",
            "2.0",
        )

    @property
    def atomicity_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"source_atomicity/luanti/{self.number}/preannotation/v2",
            "source_atomicity",
            "2.0",
        )

    @property
    def manifest_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_stage/luanti/{self.number}/test_manifest/v2",
            "candidate_stage",
            "2.0",
        )

    @property
    def checkpoint_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"screening_checkpoint/luanti/{self.number}/current/v2",
            "screening_checkpoint",
            "2.0",
        )

    @property
    def profile_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_profile/luanti/{self.number}/preflight/v2",
            "candidate_profile",
            "2.0",
        )

    @property
    def preparation_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_preparation/luanti/{self.number}/preannotation/v2",
            "candidate_preparation",
            "2.0",
        )

    @property
    def downstream_plan_reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_downstream_plan/luanti/{self.number}/preannotation/v2",
            "candidate_downstream_plan",
            "2.0",
        )


_SPECS: dict[int, LuantiOnPathSpec] = {
    12602: LuantiOnPathSpec(
        number=12602,
        issue=12557,
        profile_id="luanti-12602-media-override-v1",
        production_paths=("src/server/mods.cpp",),
        test_paths=("src/unittest/test_servermodmanager.cpp",),
        test_markers=(
            "Test media overriding:",
            'sm.getModSpec("unittests")->path + DIR_DELIM + "textures"',
            'sm.getModSpec("basenodes")->path + DIR_DELIM + "textures"',
            "std::find(result.begin(), result.end(),",
            "UASSERT(it != result.end());",
        ),
        f2p_selector="TestServerModManager.testGetModMediaPaths",
        p2p_selector="TestServerModManager.testGetModNames",
    ),
    14464: LuantiOnPathSpec(
        number=14464,
        issue=14463,
        profile_id="luanti-14464-iconv-state-v2",
        production_paths=(
            "src/content_nodemeta.cpp",
            "src/nodemetadata.cpp",
            "src/object_properties.cpp",
            "src/script/common/c_converter.cpp",
            "src/serialization.cpp",
            "src/util/serialize.h",
            "src/util/string.cpp",
        ),
        test_paths=("src/unittest/test_utilities.cpp",),
        test_markers=(
            "try to check that the conversion function does not accidentally keep",
            'utf8_to_wide("\\xC4");',
            'UASSERT(utf8_to_wide("\\x81") != L"\\u0101");',
        ),
        f2p_selector="TestUtilities.testUTF8",
        p2p_selector="TestUtilities.testRemoveEscapes",
    ),
    14072: LuantiOnPathSpec(
        number=14072,
        issue=13552,
        profile_id="luanti-14072-oversized-stack-v2",
        production_paths=(
            "src/inventory.cpp",
            "src/unittest/helpers/helper_moveaction.lua",
        ),
        test_paths=("src/unittest/test_moveaction.cpp",),
        test_markers=(
            "testMoveFillStack",
            'apply_action("Move 209 player:p1 main 0 player:p1 main 1",',
            "default:stone 200",
            "default:stone 99",
            'apply_action("Move 200 player:p1 main 0 player:p1 main 1",',
        ),
        f2p_selector="TestMoveAction.testMoveFillStack",
        p2p_selector="TestMoveAction.testMovePartial",
        inventory_note=(
            "The typed patch inventory lists helper_moveaction.lua as a "
            "production path; this classification is preserved for the "
            "strict inverse receipt and requires no reclassification here."
        ),
    ),
    15687: LuantiOnPathSpec(
        number=15687,
        issue=11991,
        profile_id="luanti-15687-matrix-scale-v2",
        production_paths=("irr/include/matrix4.h", "src/client/content_cao.cpp"),
        test_paths=("src/unittest/test_irr_matrix4.cpp",),
        test_markers=(
            'SECTION("getScale")',
            "correctly gets the length of each row of the 3x3 submatrix",
            "v3f scale = A.getScale();",
            "getLength()",
            "CHECK(scale.equals(v3f(",
        ),
        f2p_selector="TestIrrMatrix4.getScale",
        p2p_selector="TestIrrMatrix4.setRotationRadians",
    ),
}


def profile_for_candidate(candidate: CandidateKey) -> LuantiOnPathSpec:
    """Return the registered on-path spec without contacting a provider."""

    if candidate.repository_key != "luanti" or candidate.change_number not in _SPECS:
        raise ValueError("candidate has no registered Luanti on-path profile")
    return _SPECS[candidate.change_number]


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


@dataclass(frozen=True)
class _PatchSection:
    path: str
    content: bytes
    additions: tuple[str, ...]
    removals: tuple[str, ...]


@dataclass(frozen=True)
class LuantiOnPathChain:
    spec: LuantiOnPathSpec
    source: StableArtifact
    patch: StableArtifact
    atomicity: StableArtifact
    manifest: StableArtifact
    checkpoint: StableArtifact
    source_manifest: Mapping[str, Any]
    sections: tuple[_PatchSection, ...]


@dataclass(frozen=True)
class LuantiOnPathPreflight:
    chain: LuantiOnPathChain
    inverse_paths: tuple[str, ...]
    test_api_markers: tuple[str, ...]
    gate_status: Mapping[str, str]
    downstream_status: str = "downstream_pending"


def _read_json(root: Path, relative: str, label: str) -> dict[str, Any]:
    path = _regular_file(root, relative, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} is invalid")
    return value


def _parse_patch(raw_patch: bytes, spec: LuantiOnPathSpec) -> tuple[_PatchSection, ...]:
    if not raw_patch:
        raise RuntimeError(f"Luanti {spec.number} raw patch is empty")
    sections: list[tuple[str, list[str]]] = []
    current_path: str | None = None
    current: list[str] = []
    for line in raw_patch.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append((current_path, current))
            try:
                parts = shlex.split(line.rstrip("\r\n"))
            except ValueError as error:
                raise RuntimeError(
                    f"Luanti {spec.number} patch header is invalid"
                ) from error
            if (
                len(parts) != 4
                or parts[:2] != ["diff", "--git"]
                or not parts[2].startswith("a/")
                or not parts[3].startswith("b/")
                or parts[2][2:] != parts[3][2:]
            ):
                raise RuntimeError(f"Luanti {spec.number} patch header is invalid")
            current_path = _safe_relative(
                parts[2][2:], f"Luanti {spec.number} patch path"
            ).as_posix()
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, current))
    if not sections or len({path for path, _ in sections}) != len(sections):
        raise RuntimeError(
            f"Luanti {spec.number} patch has duplicate or missing sections"
        )
    return tuple(
        _PatchSection(
            path=path,
            content="".join(lines).encode("utf-8", "surrogateescape"),
            additions=tuple(
                line[1:].rstrip("\r\n")
                for line in lines
                if line.startswith("+") and not line.startswith("+++")
            ),
            removals=tuple(
                line[1:].rstrip("\r\n")
                for line in lines
                if line.startswith("-") and not line.startswith("---")
            ),
        )
        for path, lines in sections
    )


def _validate_chain(root: Path, spec: LuantiOnPathSpec) -> LuantiOnPathChain:
    source, patch, atomicity, manifest, checkpoint = (
        _load(root, reference)
        for reference in (
            spec.source_reference,
            spec.patch_reference,
            spec.atomicity_reference,
            spec.manifest_reference,
            spec.checkpoint_reference,
        )
    )
    candidate_value = spec.candidate.to_value()
    source_path = _safe_relative(
        source.payload.get("raw_evidence_path"), f"Luanti {spec.number} source manifest"
    ).as_posix()
    source_manifest = _read_json(
        root, source_path, f"Luanti {spec.number} source manifest"
    )
    if (
        source.inputs != (_ENUMERATION,)
        or source.payload.get("candidate") != candidate_value
        or source.payload.get("complete") is not True
        or source_manifest.get("candidate") != candidate_value
        or source_manifest.get("complete") is not True
        or source_manifest.get("provider") != "github"
        or source_manifest.get("issue_numbers") != [spec.issue]
        or source_manifest.get("authority")
        != {
            "api_base": "https://api.github.com",
            "provider": "github",
            "repository": "luanti-org/luanti",
        }
        or source_manifest.get("patch_raw_path") != patch.payload.get("raw_patch_path")
    ):
        raise RuntimeError(f"Luanti {spec.number} source authority is invalid")
    pull = source_manifest.get("pull")
    if (
        type(pull) is not dict
        or pull.get("number") != spec.number
        or pull.get("merged") is not True
    ):
        raise RuntimeError(f"Luanti {spec.number} merged pull source is invalid")

    patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), f"Luanti {spec.number} raw patch"
    ).as_posix()
    raw_patch = _regular_file(
        root, patch_path, f"Luanti {spec.number} raw patch"
    ).read_bytes()
    sections = _parse_patch(raw_patch, spec)
    expected_paths = spec.production_paths + spec.test_paths
    if (
        patch.inputs != (_ENUMERATION, source.reference)
        or patch.payload.get("candidate") != candidate_value
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != spec.production_paths
        or tuple(patch.payload.get("test_paths", ())) != spec.test_paths
        or patch.payload.get("source_manifest_path") != source_path
        or tuple(sorted(section.path for section in sections))
        != tuple(sorted(expected_paths))
    ):
        raise RuntimeError(f"Luanti {spec.number} patch inventory is invalid")
    if any(
        not section.additions and not section.removals
        for section in sections
        if section.path in spec.production_paths
    ):
        raise RuntimeError(f"Luanti {spec.number} patch contains an empty hunk")

    if (
        atomicity.inputs
        != (_ENUMERATION, source.reference, patch.reference, spec.checkpoint_reference)
        or atomicity.payload.get("candidate") != candidate_value
        or atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != spec.issue
        or atomicity.payload.get("authoritative_issue_numbers") != [spec.issue]
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("linked_issue_numbers") != [spec.issue]
        or atomicity.payload.get("reason") is not None
        or atomicity.payload.get("source_manifest_path") != source_path
    ):
        raise RuntimeError(f"Luanti {spec.number} single-defect mapping is invalid")

    details = manifest.payload.get("details")
    if (
        manifest.inputs != (patch.reference,)
        or manifest.payload.get("candidate") != candidate_value
        or manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("outcome") is not None
        or type(details) is not dict
        or details.get("executable_test_paths") != list(spec.test_paths)
        or details.get("test_count") != len(spec.test_paths)
    ):
        raise RuntimeError(f"Luanti {spec.number} test manifest is invalid")
    if (
        checkpoint.payload.get("candidate") != candidate_value
        or checkpoint.payload.get("state") != "complete"
        or checkpoint.payload.get("stage_artifact_id") != patch.reference.artifact_id
    ):
        raise RuntimeError(f"Luanti {spec.number} screening checkpoint is invalid")

    test_section = next(
        section for section in sections if section.path in spec.test_paths
    )
    test_text = test_section.content.decode("utf-8", errors="surrogateescape")
    missing = tuple(marker for marker in spec.test_markers if marker not in test_text)
    if missing:
        raise RuntimeError(
            f"Luanti {spec.number} fixed test API markers are missing: {', '.join(missing)}"
        )
    return LuantiOnPathChain(
        spec=spec,
        source=source,
        patch=patch,
        atomicity=atomicity,
        manifest=manifest,
        checkpoint=checkpoint,
        source_manifest=source_manifest,
        sections=sections,
    )


def prepare_candidate(
    artifact_root: str | Path, candidate: CandidateKey
) -> LuantiOnPathPreflight:
    """Reload and validate gates 1--4; leave gates 5--7 pending."""

    spec = profile_for_candidate(candidate)
    chain = _validate_chain(Path(artifact_root).absolute(), spec)
    inverse_sections = tuple(
        section for section in chain.sections if section.path in spec.production_paths
    )
    if not inverse_sections or any(
        section.path in spec.test_paths for section in inverse_sections
    ):
        raise RuntimeError(f"Luanti {spec.number} strict inverse contains a test path")
    return LuantiOnPathPreflight(
        chain=chain,
        inverse_paths=tuple(section.path for section in inverse_sections),
        test_api_markers=spec.test_markers,
        gate_status={
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
        },
    )


def _screening_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti on-path profile authorization is inactive")


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values]
    value = max(parsed).astimezone(timezone.utc).isoformat(timespec="microseconds")
    return value.replace("+00:00", "Z")


def write_profile(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and reload the source/patch-qualified candidate profile."""

    _screening_authorization(authorization)
    preflight = prepare_candidate(artifact_root, candidate)
    spec = preflight.chain.spec
    pull = preflight.chain.source_manifest["pull"]
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": spec.profile_id,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "authority": _AUTHORITY,
        "primary_issue_number": spec.issue,
        "source_issue_url": f"https://github.com/luanti-org/luanti/issues/{spec.issue}",
        "pull_title": pull.get("title"),
        "production_paths": list(spec.production_paths),
        "test_paths": list(spec.test_paths),
        "test_markers": list(spec.test_markers),
        "inventory_note": spec.inventory_note,
        "f2p_selector": spec.f2p_selector,
        "p2p_selector": spec.p2p_selector,
        "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        "strict_gate_status": dict(preflight.gate_status),
        "downstream_status": preflight.downstream_status,
        "paid_actions_authorized": False,
        "candidate_execution_performed": False,
    }
    created_at = _created_after(
        preflight.chain.source.created_at,
        preflight.chain.patch.created_at,
        preflight.chain.atomicity.created_at,
        preflight.chain.manifest.created_at,
        preflight.chain.checkpoint.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        artifact_root,
        artifact_id=spec.profile_reference.artifact_id,
        artifact_kind=spec.profile_reference.artifact_kind,
        semantic_version=spec.profile_reference.semantic_version,
        created_at=created_at,
        inputs=(
            preflight.chain.source.reference,
            preflight.chain.patch.reference,
            preflight.chain.atomicity.reference,
            preflight.chain.manifest.reference,
        ),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), spec.profile_reference)
    if artifact.payload != payload:
        raise RuntimeError(f"Luanti {spec.number} profile typed reload mismatch")
    return artifact


def write_preparation(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist the downstream-pending continuation receipt."""

    _screening_authorization(authorization)
    preflight = prepare_candidate(artifact_root, candidate)
    spec = preflight.chain.spec
    profile_artifact = _load(Path(artifact_root).absolute(), spec.profile_reference)
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": spec.profile_id,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "state": "prepared",
        "outcome": "downstream_pending",
        "profile_reference": spec.profile_reference.artifact_id,
        "primary_issue_number": spec.issue,
        "production_inverse_paths": list(preflight.inverse_paths),
        "preserved_test_paths": list(spec.test_paths),
        "test_markers": list(spec.test_markers),
        "reverse_patch_argv": ["git", "apply", "-R", "--whitespace=nowarn", "-"],
        "f2p": {
            "selector": spec.f2p_selector,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
        },
        "p2p": {
            "selector": spec.p2p_selector,
            "repetitions": 3,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
        },
        "strict_gate_status": dict(preflight.gate_status),
        "downstream_status": preflight.downstream_status,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    created_at = _created_after(profile_artifact.created_at, authorization.granted_at)
    write_stable_artifact(
        artifact_root,
        artifact_id=spec.preparation_reference.artifact_id,
        artifact_kind=spec.preparation_reference.artifact_kind,
        semantic_version=spec.preparation_reference.semantic_version,
        created_at=created_at,
        inputs=(profile_artifact.reference,),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), spec.preparation_reference)
    if artifact.payload != payload:
        raise RuntimeError(f"Luanti {spec.number} preparation typed reload mismatch")
    return artifact


def write_downstream_plan(
    artifact_root: str | Path,
    candidate: CandidateKey,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist a bounded, non-executing plan for the missing downstream work.

    This receipt is deliberately not a stage result.  It records the exact
    missing candidate-specific assets and the order in which a future
    authorized runner must create them, while preserving the active candidate
    lifecycle and leaving every execution gate pending.
    """

    _screening_authorization(authorization)
    preflight = prepare_candidate(artifact_root, candidate)
    spec = preflight.chain.spec
    profile_artifact = _load(Path(artifact_root).absolute(), spec.profile_reference)
    preparation_artifact = _load(
        Path(artifact_root).absolute(), spec.preparation_reference
    )
    root = Path(artifact_root).absolute()
    fixed_capsule = root / "private" / "fixed-capsules" / "luanti" / str(spec.number)
    native_root = root / "private" / "dependencies" / "luanti-native" / "3.0"
    binary_candidates = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.joinpath("private").rglob("*")
            if path.is_file()
            and path.name in {"minetest", "luanti"}
            and str(spec.number) in path.as_posix()
        )
    )
    missing_assets = [
        "fixed_source_capsule",
        "fixed_buggy_materialized_trees",
        "candidate_specific_build_receipts",
        "candidate_specific_fixed_buggy_binaries",
        "registered_f2p_p2p_runner",
        "three_repetitions_per_f2p_p2p_cell",
        "ordered_replay_oracle_and_golden_closure",
    ]
    if fixed_capsule.is_dir():
        missing_assets.remove("fixed_source_capsule")
    if native_root.is_dir():
        missing_assets.remove("registered_f2p_p2p_runner")
    payload: dict[str, object] = {
        "candidate": candidate.to_value(),
        "profile_id": spec.profile_id,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "state": "planned",
        "terminal": False,
        "paid_actions_authorized": False,
        "candidate_execution_performed": False,
        "completed_stage": "test_manifest",
        "next_stage": "fixed_source_capsule",
        "downstream_status": "downstream_pending",
        "preparation_reference": preparation_artifact.reference.artifact_id,
        "production_paths": list(spec.production_paths),
        "preserved_test_paths": list(spec.test_paths),
        "f2p": {"selector": spec.f2p_selector, "repetitions": 3},
        "p2p": {"selector": spec.p2p_selector, "repetitions": 3},
        "required_order": [
            "fixed_source_capsule",
            "fixed_buggy_materialization",
            "fixed_buggy_build",
            "f2p_p2p_repetitions",
            "ordered_replay_oracle",
            "construction_golden_closure",
        ],
        "missing_assets": missing_assets,
        "observed": {
            "fixed_capsule_directory": fixed_capsule.relative_to(root).as_posix(),
            "fixed_capsule_present": fixed_capsule.is_dir(),
            "native_toolchain_root": native_root.relative_to(root).as_posix(),
            "native_toolchain_present": native_root.is_dir(),
            "candidate_binary_paths": list(binary_candidates),
        },
        "resource_policy": {
            "network": False,
            "paid": False,
            "build_started": False,
            "candidate_started": False,
            "large_build_requires_separate_authorization": True,
        },
        "strict_gate_status": dict(preflight.gate_status),
    }
    created_at = _created_after(
        profile_artifact.created_at,
        preparation_artifact.created_at,
        authorization.granted_at,
    )
    write_stable_artifact(
        artifact_root,
        artifact_id=spec.downstream_plan_reference.artifact_id,
        artifact_kind=spec.downstream_plan_reference.artifact_kind,
        semantic_version=spec.downstream_plan_reference.semantic_version,
        created_at=created_at,
        inputs=(profile_artifact.reference, preparation_artifact.reference),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(Path(artifact_root).absolute(), spec.downstream_plan_reference)
    if artifact.inputs != (profile_artifact.reference, preparation_artifact.reference):
        raise RuntimeError(f"Luanti {spec.number} downstream plan inputs mismatch")
    if artifact.payload != payload:
        raise RuntimeError(
            f"Luanti {spec.number} downstream plan typed reload mismatch"
        )
    return artifact


__all__ = [
    "LuantiOnPathChain",
    "LuantiOnPathPreflight",
    "LuantiOnPathSpec",
    "prepare_candidate",
    "profile_for_candidate",
    "write_downstream_plan",
    "write_preparation",
    "write_profile",
]
