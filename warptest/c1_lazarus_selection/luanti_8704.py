"""Closed, non-executing preparation descriptor for Luanti change 8704.

The historical upstream regression is a Lua assertion which runs while the
minimal game loads.  This module never materializes a source tree, invokes
Git, starts a server, builds anything, contacts a provider, or writes a
candidate artifact.  It only validates the already-persisted v2 source packet
and the retained local mirror, then returns a future preparation descriptor.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .epoch import SELECTION_EPOCH_V2
from .models import CandidateKey
from .operations import evaluate_test_manifest
from .pipeline import StageDecision, V2PreannotationBatchContext
from .screening import resolve_raw_patch_evidence
from .source_atomicity import (
    SourceAtomicityReceipt,
    load_source_atomicity_artifact,
    load_source_atomicity_batch_context,
    write_source_atomicity_artifact,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


LUANTI_8704_PROFILE_ID = "luanti-8704-minimal-craft-result-v1"
_CANDIDATE = CandidateKey("luanti", 8704)
_SOURCE_PACKET_ID = "candidate_source_packet/luanti/8704/source/v2"
_SOURCE_PACKET_INPUT = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)
_MIRROR_PATH = PurePosixPath("private/repository-enumeration-git/luanti/mirror.git")
_SUPPORT_PATHS = ("games/minimal/mods/default/init.lua",)
_PATCH_TEST_PATHS = (
    "games/minimal/mods/test/init.lua",
    "games/minimal/mods/test/mod.conf",
)
_PRESERVED_TEST_PATHS = (*_SUPPORT_PATHS, *_PATCH_TEST_PATHS)
_TEST_PATHS = _PRESERVED_TEST_PATHS
_TRANSFORM_ID = "candidate_patch_transform/luanti/8704/screening/v2"
_TRANSFORM_KIND = "candidate_patch_transform"
_TRANSFORM_VERSION = "2.0"
_TEST_ENTRY_PATH = "games/minimal/mods/test/init.lua"
_TEST_MOD_CONF_PATH = "games/minimal/mods/test/mod.conf"
_TEST_FUNCTION = "test_get_craft_result"
_REQUIRED_MOD_DEPENDENCY = "default"
_PRODUCTION_PATHS = ("src/craftdef.cpp", "src/craftdef.h")
_EXPECTED_PATCH_PATHS = (*_PRESERVED_TEST_PATHS, *_PRODUCTION_PATHS)
_MERGED_TIP = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class Luanti8704PatchSplit:
    """The candidate-specific split for the test relocation in #8704."""

    production_paths: tuple[str, ...]
    support_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    relocation_source: str
    relocation_destination: str
    relocation_marker: str

    def __post_init__(self) -> None:
        if self.production_paths != _PRODUCTION_PATHS:
            raise ValueError("Luanti 8704 production split is invalid")
        if self.support_paths != _SUPPORT_PATHS:
            raise ValueError("Luanti 8704 support split is invalid")
        if self.test_paths != _PATCH_TEST_PATHS:
            raise ValueError("Luanti 8704 test split is invalid")
        if (
            self.relocation_source != _SUPPORT_PATHS[0]
            or self.relocation_destination != _TEST_ENTRY_PATH
            or self.relocation_marker != "local function test_get_craft_result()"
        ):
            raise ValueError("Luanti 8704 relocation binding is invalid")


@dataclass(frozen=True)
class Luanti8704RelocationReceipt:
    """Typed receipt proving that #8704's moved test stays fixed-side."""

    artifact: StableArtifact
    atomicity: SourceAtomicityReceipt
    split: Luanti8704PatchSplit

    def __post_init__(self) -> None:
        if self.artifact.reference != ArtifactReference(
            _TRANSFORM_ID, _TRANSFORM_KIND, _TRANSFORM_VERSION
        ):
            raise ValueError("Luanti 8704 relocation artifact identity is invalid")
        if self.atomicity.candidate != _CANDIDATE:
            raise ValueError("Luanti 8704 relocation atomicity candidate is invalid")
        expected_inputs = (
            ArtifactReference(
                "candidate_test_patch/luanti/8704/screening/v2",
                "candidate_test_patch",
                "2.0",
            ),
            self.atomicity.artifact.reference,
        )
        if self.artifact.inputs != expected_inputs:
            raise ValueError("Luanti 8704 relocation dependency closure is invalid")
        if self.artifact.payload != _relocation_payload(self.split):
            raise ValueError("Luanti 8704 relocation payload is invalid")


@dataclass(frozen=True)
class Luanti8704V2RewriteResult:
    """All current v2 heads rewritten after the candidate split correction."""

    patch: StableArtifact
    screening_checkpoint: StableArtifact
    atomicity: SourceAtomicityReceipt
    relocation: Luanti8704RelocationReceipt
    manifest: StableArtifact
    preannotation_checkpoint: StableArtifact


def inspect_luanti_8704_patch_split(content: bytes) -> Luanti8704PatchSplit:
    """Validate the exact #8704 production/test/support relocation shape."""

    sections = _patch_sections(content)
    by_path = {section.path: section for section in sections}
    if len(by_path) != len(sections) or set(by_path) != set(_EXPECTED_PATCH_PATHS):
        raise RuntimeError("Luanti 8704 patch path inventory is invalid")
    support = by_path[_SUPPORT_PATHS[0]]
    test = by_path[_TEST_ENTRY_PATH]
    mod_conf = by_path[_TEST_MOD_CONF_PATH]
    marker = "local function test_get_craft_result()"
    source_block = _relocation_block(support.removals, marker)
    destination_block = _relocation_block(test.additions, marker)
    if source_block != destination_block:
        raise RuntimeError("Luanti 8704 relocation body is inconsistent")
    if not any(
        line.strip() == "optional_depends = default" for line in mod_conf.additions
    ):
        raise RuntimeError("Luanti 8704 relocation dependency is missing")
    if not support.removals or support.additions:
        raise RuntimeError("Luanti 8704 relocation support hunk is invalid")
    if not test.additions:
        raise RuntimeError("Luanti 8704 relocation test hunk is missing")
    for path in _PRODUCTION_PATHS:
        section = by_path[path]
        if not section.additions or not section.removals:
            raise RuntimeError("Luanti 8704 production patch hunk is invalid")
    return Luanti8704PatchSplit(
        production_paths=_PRODUCTION_PATHS,
        support_paths=_SUPPORT_PATHS,
        test_paths=_PATCH_TEST_PATHS,
        relocation_source=_SUPPORT_PATHS[0],
        relocation_destination=_TEST_ENTRY_PATH,
        relocation_marker=marker,
    )


def rewrite_luanti_8704_v2_patch_chain(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> Luanti8704V2RewriteResult:
    """Rewrite #8704's v2 heads after correcting its test relocation split.

    This is a local, candidate-specific migration.  It never fetches source or
    executes a candidate.  The old patch/manifest/checkpoint conclusions are
    not reused: each current head is written again from the corrected split
    and then reloaded through the v2 typed loaders.
    """

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "candidate-screening"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("Luanti 8704 rewrite authorization is inactive")
    root = Path(artifact_root).resolve(strict=True)
    context = load_source_atomicity_batch_context(root, repository_key="luanti")
    batch_context = V2PreannotationBatchContext.from_source_atomicity(context)
    enumeration = load_stable_artifact(
        root,
        artifact_id=_SOURCE_PACKET_INPUT.artifact_id,
        artifact_kind=_SOURCE_PACKET_INPUT.artifact_kind,
        semantic_version=_SOURCE_PACKET_INPUT.semantic_version,
    )
    source = load_stable_artifact(
        root,
        artifact_id=_SOURCE_PACKET_ID,
        artifact_kind="candidate_source_packet",
        semantic_version="2.0",
    )
    if source.inputs != (enumeration.reference,):
        raise RuntimeError("Luanti 8704 source packet enumeration binding is invalid")
    raw_evidence = resolve_raw_patch_evidence(
        root,
        candidate=_CANDIDATE,
        source_manifest_path=source.payload.get("raw_evidence_path"),
    )
    split = inspect_luanti_8704_patch_split(raw_evidence.patch_path.read_bytes())
    patch_reference = ArtifactReference(
        "candidate_test_patch/luanti/8704/screening/v2",
        "candidate_test_patch",
        "2.0",
    )
    patch_payload = {
        "candidate": _CANDIDATE.to_value(),
        "outcome": "patch_eligible",
        "production_paths": list(split.production_paths),
        "test_paths": list(split.test_paths),
        "source_manifest_path": raw_evidence.source_manifest_path,
        "raw_patch_path": raw_evidence.raw_patch_path,
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=patch_reference.artifact_id,
        artifact_kind=patch_reference.artifact_kind,
        semantic_version=patch_reference.semantic_version,
        created_at=_now(),
        inputs=(enumeration.reference, source.reference),
        payload=patch_payload,
        authorization=authorization,
    )
    patch = load_stable_artifact(
        root,
        artifact_id=patch_reference.artifact_id,
        artifact_kind=patch_reference.artifact_kind,
        semantic_version=patch_reference.semantic_version,
    )
    screening_reference = ArtifactReference(
        "screening_checkpoint/luanti/8704/current/v2",
        "screening_checkpoint",
        "2.0",
    )
    screening_payload = {
        "candidate": _CANDIDATE.to_value(),
        "state": "complete",
        "outcome": "patch_eligible",
        "stage_artifact_id": patch.reference.artifact_id,
    }
    write_stable_artifact(
        root,
        artifact_id=screening_reference.artifact_id,
        artifact_kind=screening_reference.artifact_kind,
        semantic_version=screening_reference.semantic_version,
        created_at=_now(),
        inputs=(enumeration.reference, patch.reference),
        payload=screening_payload,
        authorization=authorization,
    )
    screening = load_stable_artifact(
        root,
        artifact_id=screening_reference.artifact_id,
        artifact_kind=screening_reference.artifact_kind,
        semantic_version=screening_reference.semantic_version,
    )
    atomicity = write_source_atomicity_artifact(
        batch_context=context,
        candidate=_CANDIDATE,
        created_at=_now(),
        authorization=authorization,
    )
    transform_reference = ArtifactReference(
        _TRANSFORM_ID,
        _TRANSFORM_KIND,
        _TRANSFORM_VERSION,
    )
    write_stable_artifact(
        root,
        artifact_id=transform_reference.artifact_id,
        artifact_kind=transform_reference.artifact_kind,
        semantic_version=transform_reference.semantic_version,
        created_at=_now(),
        inputs=(patch.reference, atomicity.artifact.reference),
        payload=_relocation_payload(split),
        authorization=authorization,
    )
    relocation = load_luanti_8704_relocation_receipt(root)
    decision = evaluate_test_manifest(
        root,
        _CANDIDATE,
        selection_epoch=SELECTION_EPOCH_V2,
        batch_context=batch_context,
    )
    if decision.state != "passed":
        raise RuntimeError("Luanti 8704 corrected test manifest did not pass")
    manifest_reference = ArtifactReference(
        "candidate_stage/luanti/8704/test_manifest/v2",
        "candidate_stage",
        "2.0",
    )
    manifest_payload = {
        "candidate": _CANDIDATE.to_value(),
        "stage": "test_manifest",
        "state": "passed",
        "outcome": None,
        "details": dict(decision.details),
        "complete": True,
    }
    write_stable_artifact(
        root,
        artifact_id=manifest_reference.artifact_id,
        artifact_kind=manifest_reference.artifact_kind,
        semantic_version=manifest_reference.semantic_version,
        created_at=_now(),
        inputs=(patch.reference, atomicity.artifact.reference),
        payload=manifest_payload,
        authorization=authorization,
    )
    manifest = load_stable_artifact(
        root,
        artifact_id=manifest_reference.artifact_id,
        artifact_kind=manifest_reference.artifact_kind,
        semantic_version=manifest_reference.semantic_version,
    )
    preannotation_reference = ArtifactReference(
        "preannotation_checkpoint/luanti/8704/current/v2",
        "preannotation_checkpoint",
        "2.0",
    )
    preannotation_payload = {
        "candidate": _CANDIDATE.to_value(),
        "completed_stages": ["test_manifest"],
        "state": "active",
        "outcome": None,
        "current_artifact_id": manifest.reference.artifact_id,
    }
    write_stable_artifact(
        root,
        artifact_id=preannotation_reference.artifact_id,
        artifact_kind=preannotation_reference.artifact_kind,
        semantic_version=preannotation_reference.semantic_version,
        created_at=_now(),
        inputs=(manifest.reference,),
        payload=preannotation_payload,
        authorization=authorization,
    )
    preannotation = load_stable_artifact(
        root,
        artifact_id=preannotation_reference.artifact_id,
        artifact_kind=preannotation_reference.artifact_kind,
        semantic_version=preannotation_reference.semantic_version,
    )
    result = Luanti8704V2RewriteResult(
        patch=patch,
        screening_checkpoint=screening,
        atomicity=atomicity,
        relocation=relocation,
        manifest=manifest,
        preannotation_checkpoint=preannotation,
    )
    _validate_rewrite_result(root, result)
    return result


def load_luanti_8704_relocation_receipt(
    artifact_root: str | Path,
) -> Luanti8704RelocationReceipt:
    """Reload the current #8704 relocation receipt and all of its inputs."""

    root = Path(artifact_root).resolve(strict=True)
    context = load_source_atomicity_batch_context(root, repository_key="luanti")
    atomicity = load_source_atomicity_artifact(
        batch_context=context,
        candidate=_CANDIDATE,
    )
    if atomicity.outcome != "accepted" or atomicity.primary_issue_number != 8607:
        raise RuntimeError("Luanti 8704 relocation source atomicity is not accepted")
    patch = load_stable_artifact(
        root,
        artifact_id="candidate_test_patch/luanti/8704/screening/v2",
        artifact_kind="candidate_test_patch",
        semantic_version="2.0",
    )
    if (
        patch.inputs
        != (
            _SOURCE_PACKET_INPUT,
            ArtifactReference(
                _SOURCE_PACKET_ID,
                "candidate_source_packet",
                "2.0",
            ),
        )
        or patch.payload.get("candidate") != _CANDIDATE.to_value()
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(_PRODUCTION_PATHS)
        or patch.payload.get("test_paths") != list(_PATCH_TEST_PATHS)
        or patch.payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 8704 corrected patch split is not current")
    raw_evidence = resolve_raw_patch_evidence(
        root,
        candidate=_CANDIDATE,
        source_manifest_path=patch.payload.get("source_manifest_path"),
    )
    split = inspect_luanti_8704_patch_split(raw_evidence.patch_path.read_bytes())
    reference = ArtifactReference(_TRANSFORM_ID, _TRANSFORM_KIND, _TRANSFORM_VERSION)
    artifact = load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )
    receipt = Luanti8704RelocationReceipt(
        artifact=artifact,
        atomicity=atomicity,
        split=split,
    )
    if artifact.inputs != (patch.reference, atomicity.artifact.reference):
        raise RuntimeError("Luanti 8704 relocation inputs are not current")
    return receipt


def _relocation_payload(split: Luanti8704PatchSplit) -> dict[str, object]:
    return {
        "candidate": _CANDIDATE.to_value(),
        "transform": "support_test_relocation",
        "production_paths": list(split.production_paths),
        "support_paths": list(split.support_paths),
        "test_paths": list(split.test_paths),
        "relocation_source": split.relocation_source,
        "relocation_destination": split.relocation_destination,
        "relocation_marker": split.relocation_marker,
        "preserve_support_side": True,
        "complete": True,
    }


def _relocation_block(lines: tuple[str, ...], marker: str) -> tuple[str, ...]:
    starts = [index for index, line in enumerate(lines) if line.strip() == marker]
    if len(starts) != 1:
        raise RuntimeError("Luanti 8704 relocation marker is not unique")
    start = starts[0]
    ends = [
        index
        for index in range(start + 1, len(lines))
        if lines[index].strip() == f"{_TEST_FUNCTION}()"
    ]
    if len(ends) != 1:
        raise RuntimeError("Luanti 8704 relocation invocation is not unique")
    return tuple(lines[start : ends[0] + 1])


def _validate_rewrite_result(
    root: Path,
    result: Luanti8704V2RewriteResult,
) -> None:
    if result.patch.payload.get("production_paths") != list(_PRODUCTION_PATHS):
        raise RuntimeError("Luanti 8704 rewritten production split is invalid")
    if result.patch.payload.get("test_paths") != list(_PATCH_TEST_PATHS):
        raise RuntimeError("Luanti 8704 rewritten test split is invalid")
    if result.screening_checkpoint.inputs != (
        _SOURCE_PACKET_INPUT,
        result.patch.reference,
    ):
        raise RuntimeError("Luanti 8704 rewritten screening checkpoint is invalid")
    if result.manifest.inputs != (
        result.patch.reference,
        result.atomicity.artifact.reference,
    ):
        raise RuntimeError("Luanti 8704 rewritten manifest closure is invalid")
    if result.preannotation_checkpoint.inputs != (result.manifest.reference,):
        raise RuntimeError("Luanti 8704 rewritten preannotation checkpoint is invalid")
    loaded = load_luanti_8704_relocation_receipt(root)
    if loaded.artifact.reference != result.relocation.artifact.reference:
        raise RuntimeError("Luanti 8704 rewritten relocation reload drifted")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Luanti8704MinimalGamePrerequisites:
    """Local inputs required by a later noninteractive minimal-game runner."""

    game_id: str
    server_binary_relative_path: str
    required_mod_paths: tuple[str, ...]
    test_entry_path: str
    test_function: str
    required_mod_dependency: str
    requires_isolated_writable_world: bool
    requires_external_bounded_supervisor: bool
    client_interaction_required: bool
    server_start_argv_template: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.game_id != "minimal":
            raise ValueError("Luanti 8704 must use the minimal game")
        if self.server_binary_relative_path != "bin/minetestserver":
            raise ValueError("Luanti 8704 server binary is invalid")
        if self.required_mod_paths != (
            "games/minimal/mods/default",
            "games/minimal/mods/test",
        ):
            raise ValueError("Luanti 8704 minimal mod prerequisites are invalid")
        if (
            self.test_entry_path != _TEST_ENTRY_PATH
            or self.test_function != _TEST_FUNCTION
            or self.required_mod_dependency != _REQUIRED_MOD_DEPENDENCY
        ):
            raise ValueError("Luanti 8704 Lua regression binding is invalid")
        if (
            self.requires_isolated_writable_world is not True
            or self.requires_external_bounded_supervisor is not True
            or self.client_interaction_required is not False
        ):
            raise ValueError(
                "Luanti 8704 noninteractive runtime prerequisites are invalid"
            )
        if self.server_start_argv_template != (
            "./bin/minetestserver",
            "--gameid",
            "minimal",
            "--world",
            "{isolated_world}",
            "--server",
        ):
            raise ValueError("Luanti 8704 future server descriptor is invalid")


@dataclass(frozen=True)
class Luanti8704PreparationDescriptor:
    """A non-persistable description for a later materialization operation."""

    candidate: CandidateKey
    profile_id: str
    source_manifest_path: str
    raw_patch_path: str
    merged_tip: str
    mirror: Path
    fixed_archive_argv: tuple[str, ...]
    production_reverse_patch: bytes
    buggy_apply_argv: tuple[str, ...]
    preserved_test_paths: tuple[str, ...]
    minimal_game_prerequisites: Luanti8704MinimalGamePrerequisites

    def __post_init__(self) -> None:
        if self.candidate != _CANDIDATE or self.profile_id != LUANTI_8704_PROFILE_ID:
            raise ValueError("Luanti 8704 preparation descriptor identity is invalid")
        if not _MERGED_TIP.fullmatch(self.merged_tip):
            raise ValueError("Luanti 8704 merged tip is invalid")
        if self.fixed_archive_argv != (
            "git",
            f"--git-dir={self.mirror}",
            "archive",
            "--format=tar",
            self.merged_tip,
        ):
            raise ValueError("Luanti 8704 fixed archive descriptor is invalid")
        if self.buggy_apply_argv != (
            "git",
            "apply",
            "-R",
            "--whitespace=nowarn",
            "-",
        ):
            raise ValueError("Luanti 8704 reverse patch descriptor is invalid")
        if self.preserved_test_paths != _TEST_PATHS:
            raise ValueError("Luanti 8704 regression test preservation is invalid")
        if not self.production_reverse_patch:
            raise ValueError("Luanti 8704 production reverse patch is empty")
        if any(
            path.encode("utf-8") in self.production_reverse_patch
            for path in _TEST_PATHS
        ):
            raise ValueError("Luanti 8704 reverse patch would alter the Lua regression")
        if not isinstance(
            self.minimal_game_prerequisites, Luanti8704MinimalGamePrerequisites
        ):
            raise ValueError("Luanti 8704 minimal-game prerequisites are invalid")


@dataclass(frozen=True)
class _PatchSection:
    path: str
    lines: tuple[str, ...]
    additions: tuple[str, ...]
    removals: tuple[str, ...]


def describe_luanti_8704_minimal_game_preparation(
    artifact_root: str | Path,
    *,
    candidate: CandidateKey,
) -> Luanti8704PreparationDescriptor | StageDecision:
    """Describe the sole registered #8704 preparation without executing it.

    Missing retained source evidence or a missing local mirror is an
    infrastructure terminal.  Structural source evidence drift remains an
    error: it is not safe to substitute a different test or patch shape.
    """

    if candidate != _CANDIDATE:
        raise ValueError("only registered candidate Luanti 8704 has this adapter")
    root = Path(artifact_root).resolve(strict=True)
    source_packet = _source_packet_or_block(root)
    if isinstance(source_packet, StageDecision):
        return source_packet
    raw_evidence_path = source_packet.payload.get("raw_evidence_path")
    try:
        raw_evidence = resolve_raw_patch_evidence(
            root,
            candidate=candidate,
            source_manifest_path=raw_evidence_path,
        )
    except RuntimeError as error:
        if _is_missing_raw_evidence(error):
            return _infrastructure_blocker(
                "persisted_source_evidence_unavailable",
                {"source_packet_id": _SOURCE_PACKET_ID},
            )
        raise
    manifest = _read_raw_manifest(root, raw_evidence.source_manifest_path)
    merged_tip = _captured_merged_tip(root, manifest, candidate)
    mirror = _registered_mirror(root)
    if mirror is None:
        return _infrastructure_blocker(
            "local_luanti_mirror_unavailable",
            {"mirror_path": _MIRROR_PATH.as_posix()},
        )
    sections = _patch_sections(raw_evidence.patch_path.read_bytes())
    prerequisite_decision = _validate_minimal_game_regression(sections)
    if prerequisite_decision is not None:
        return prerequisite_decision
    production_reverse_patch = _production_only_patch(sections)
    return Luanti8704PreparationDescriptor(
        candidate=candidate,
        profile_id=LUANTI_8704_PROFILE_ID,
        source_manifest_path=raw_evidence.source_manifest_path,
        raw_patch_path=raw_evidence.raw_patch_path,
        merged_tip=merged_tip,
        mirror=mirror,
        fixed_archive_argv=(
            "git",
            f"--git-dir={mirror}",
            "archive",
            "--format=tar",
            merged_tip,
        ),
        production_reverse_patch=production_reverse_patch,
        buggy_apply_argv=("git", "apply", "-R", "--whitespace=nowarn", "-"),
        preserved_test_paths=_TEST_PATHS,
        minimal_game_prerequisites=Luanti8704MinimalGamePrerequisites(
            game_id="minimal",
            server_binary_relative_path="bin/minetestserver",
            required_mod_paths=(
                "games/minimal/mods/default",
                "games/minimal/mods/test",
            ),
            test_entry_path=_TEST_ENTRY_PATH,
            test_function=_TEST_FUNCTION,
            required_mod_dependency=_REQUIRED_MOD_DEPENDENCY,
            requires_isolated_writable_world=True,
            requires_external_bounded_supervisor=True,
            client_interaction_required=False,
            server_start_argv_template=(
                "./bin/minetestserver",
                "--gameid",
                "minimal",
                "--world",
                "{isolated_world}",
                "--server",
            ),
        ),
    )


def _source_packet_or_block(root: Path):
    try:
        packet = load_stable_artifact(
            root,
            artifact_id=_SOURCE_PACKET_ID,
            artifact_kind="candidate_source_packet",
            semantic_version="2.0",
        )
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return _infrastructure_blocker(
                "persisted_v2_source_packet_unavailable",
                {"source_packet_id": _SOURCE_PACKET_ID},
            )
        raise
    if (
        packet.payload.get("candidate") != _CANDIDATE.to_value()
        or packet.payload.get("complete") is not True
        or type(packet.payload.get("raw_evidence_path")) is not str
        or packet.inputs != (_SOURCE_PACKET_INPUT,)
    ):
        raise RuntimeError("Luanti 8704 persisted v2 source packet is invalid")
    return packet


def _infrastructure_blocker(
    reason: str,
    details: dict[str, object],
) -> StageDecision:
    return StageDecision.terminal(
        "infrastructure_blocked",
        {"profile_id": LUANTI_8704_PROFILE_ID, "reason": reason, **details},
    )


def _is_missing_raw_evidence(error: RuntimeError) -> bool:
    return str(error) in {
        "source manifest file is missing",
        "source patch file is missing",
    }


def _read_raw_manifest(root: Path, relative_path: str) -> dict[str, object]:
    path = _safe_raw_file(root, relative_path, "source manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Luanti 8704 raw source manifest is invalid") from error
    if type(value) is not dict:
        raise RuntimeError("Luanti 8704 raw source manifest is invalid")
    return value


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError("Luanti 8704 raw JSON has duplicate keys")
        result[key] = value
    return result


def _captured_merged_tip(
    root: Path,
    manifest: dict[str, object],
    candidate: CandidateKey,
) -> str:
    raw_paths = manifest.get("raw_paths")
    if (
        manifest.get("candidate") != candidate.to_value()
        or manifest.get("complete") is not True
        or type(raw_paths) is not list
    ):
        raise RuntimeError("Luanti 8704 raw source manifest binding is invalid")
    pull_path = _pull_response_path(raw_paths)
    pull = _read_raw_object(_safe_raw_file(root, pull_path, "pull"), "pull")
    if pull.get("number") != candidate.change_number or pull.get("merged") is not True:
        raise RuntimeError("Luanti 8704 captured pull binding is invalid")
    merged_tip = pull.get("merge_commit_sha")
    if type(merged_tip) is not str or not _MERGED_TIP.fullmatch(merged_tip):
        raise RuntimeError("Luanti 8704 captured pull has no merged tip")
    return merged_tip


def _pull_response_path(raw_paths: list[object]) -> str:
    matches: list[str] = []
    for value in raw_paths:
        if type(value) is not str or not value:
            raise RuntimeError(
                "Luanti 8704 raw source manifest pull inventory is invalid"
            )
        path = PurePosixPath(value)
        if (
            len(path.parts) >= 6
            and path.parts[:4] == ("raw", "github", "luanti", "8704")
            and path.parts[4] == "pull"
        ):
            matches.append(value)
    if len(matches) != 1:
        raise RuntimeError("Luanti 8704 raw source manifest pull capture is invalid")
    return matches[0]


def _read_raw_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Luanti 8704 raw {label} is invalid") from error
    if type(value) is not dict:
        raise RuntimeError(f"Luanti 8704 raw {label} is invalid")
    return value


def _safe_raw_file(root: Path, relative_path: str, label: str) -> Path:
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
        or relative_path != relative.as_posix()
    ):
        raise RuntimeError(f"Luanti 8704 {label} path is unsafe")
    path = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as error:
            raise RuntimeError(f"Luanti 8704 {label} is missing") from error
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"Luanti 8704 {label} path contains a symlink")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise RuntimeError(f"Luanti 8704 {label} is unsafe")
    return path


def _registered_mirror(root: Path) -> Path | None:
    mirror = _safe_directory(root, _MIRROR_PATH)
    if mirror is None:
        return None
    if _safe_directory(mirror, PurePosixPath("objects")) is None:
        return None
    if _safe_directory(mirror, PurePosixPath("refs")) is None:
        return None
    head = _safe_regular_file(mirror, PurePosixPath("HEAD"))
    packed_refs = _safe_regular_file(mirror, PurePosixPath("packed-refs"))
    if head is None or packed_refs is None:
        return None
    try:
        head_value = head.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not head_value.startswith("ref: refs/"):
        return None
    return mirror


def _safe_directory(root: Path, relative: PurePosixPath) -> Path | None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
    return current


def _safe_regular_file(root: Path, relative: PurePosixPath) -> Path | None:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
    return current if stat.S_ISREG(current.lstat().st_mode) else None


def _patch_sections(content: bytes) -> tuple[_PatchSection, ...]:
    if not content:
        raise RuntimeError("Luanti 8704 raw patch is empty")
    text = content.decode("utf-8", errors="surrogateescape")
    sections: list[_PatchSection] = []
    path: str | None = None
    lines: list[str] = []
    additions: list[str] = []
    removals: list[str] = []

    def finish() -> None:
        if path is not None:
            sections.append(
                _PatchSection(path, tuple(lines), tuple(additions), tuple(removals))
            )

    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            finish()
            path = _diff_path(line)
            lines = [line]
            additions = []
            removals = []
        elif path is not None:
            lines.append(line)
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                additions.append(line[1:])
            elif line.startswith("-"):
                removals.append(line[1:])
    finish()
    if tuple(section.path for section in sections) != _EXPECTED_PATCH_PATHS:
        raise RuntimeError("Luanti 8704 raw patch is outside the registered profile")
    return tuple(sections)


def _diff_path(line: str) -> str:
    parts = line.rstrip("\r\n").split(" ")
    if (
        len(parts) != 4
        or not parts[2].startswith("a/")
        or not parts[3].startswith("b/")
        or parts[2][2:] != parts[3][2:]
    ):
        raise RuntimeError("Luanti 8704 raw patch header is invalid")
    path = parts[3][2:]
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or pure.as_posix() != path:
        raise RuntimeError("Luanti 8704 raw patch path is invalid")
    return path


def _validate_minimal_game_regression(
    sections: tuple[_PatchSection, ...],
) -> StageDecision | None:
    by_path = {section.path: section for section in sections}
    default_removals = "".join(by_path[_TEST_PATHS[0]].removals)
    test_additions = "".join(by_path[_TEST_ENTRY_PATH].additions)
    mod_conf_additions = "".join(by_path[_TEST_MOD_CONF_PATH].additions)
    required_lua = (
        f"local function {_TEST_FUNCTION}()",
        "minetest.get_craft_result",
        'method = "normal"',
        'method = "fuel"',
        'method = "cooking"',
        "default:torch",
        "default:stone",
        f"{_TEST_FUNCTION}()",
    )
    if (
        f"local function {_TEST_FUNCTION}()" not in default_removals
        or f"{_TEST_FUNCTION}()" not in default_removals
        or any(token not in test_additions for token in required_lua)
    ):
        return _infrastructure_blocker(
            "minimal_game_regression_test_unavailable",
            {
                "test_entry_path": _TEST_ENTRY_PATH,
                "test_function": _TEST_FUNCTION,
            },
        )
    if f"optional_depends = {_REQUIRED_MOD_DEPENDENCY}" not in mod_conf_additions:
        return _infrastructure_blocker(
            "minimal_game_test_dependency_unavailable",
            {
                "required_mod_dependency": _REQUIRED_MOD_DEPENDENCY,
                "test_mod_conf_path": _TEST_MOD_CONF_PATH,
            },
        )
    return None


def _production_only_patch(sections: tuple[_PatchSection, ...]) -> bytes:
    return "".join(
        line
        for section in sections
        if section.path in _PRODUCTION_PATHS
        for line in section.lines
    ).encode("utf-8", errors="surrogateescape")


__all__ = [
    "LUANTI_8704_PROFILE_ID",
    "Luanti8704PatchSplit",
    "Luanti8704RelocationReceipt",
    "Luanti8704MinimalGamePrerequisites",
    "Luanti8704PreparationDescriptor",
    "describe_luanti_8704_minimal_game_preparation",
    "inspect_luanti_8704_patch_split",
    "load_luanti_8704_relocation_receipt",
    "rewrite_luanti_8704_v2_patch_chain",
]
