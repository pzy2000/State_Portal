"""Typed Gate 4 terminal for Luanti #11011.

The PR changes the public ``Schematic`` serialization signatures and updates
the preserved unit test to call those new signatures.  The production-only
inverse correctly restores the pre-PR signatures, while the test hunk remains
in the buggy tree.  Consequently the fixed test is not executable against
the buggy API.  This is a hard Gate 4 incompatibility, so the candidate is
terminal and no native capsule, build, or Gate 5--7 attempt is permitted.
"""

from __future__ import annotations

import os
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 11011}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11011/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11011/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11011/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11011/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate14_terminal/luanti/11011/preannotation/v1",
    "candidate_gate14_terminal",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/mapgen/mg_schematic.cpp",
    "src/mapgen/mg_schematic.h",
    "src/nodedef.cpp",
    "src/nodedef.h",
    "src/script/lua_api/l_mapgen.cpp",
)
_TEST_PATHS = (
    "src/unittest/test_noderesolver.cpp",
    "src/unittest/test_schematic.cpp",
)


def audit_luanti_11011_gate4(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Replay the exact current-v2 chain and persist the Gate 4 terminal."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11011 Gate 4 terminal authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    _validate_chain(source, patch, atomicity, manifest)

    raw_patch_path = _safe_relative(patch.payload.get("raw_patch_path"))
    raw_patch = _regular_file(root / raw_patch_path).read_bytes()
    sections = _parse_patch(raw_patch)
    if set(sections) != set(_PRODUCTION_PATHS + _TEST_PATHS):
        raise RuntimeError("#11011 raw patch paths drifted from current-v2 inventory")
    audit = _audit_fixed_only_api(sections)
    if not audit["incompatible"]:
        raise RuntimeError(
            "#11011 expected Gate 4 fixed-only API mismatch was not observed"
        )

    chain_revisions = {
        artifact.reference.artifact_id: artifact.created_at
        for artifact in (source, patch, atomicity, manifest)
    }
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-11011-schematic-node-resolver-v2",
        "state": "terminal",
        "outcome": "gate4_incompatible",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "preserved_fixed_test_calls_pr_introduced_signatures_absent_after_production_inverse",
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "failed_fixed_only_api",
            "fresh_gate57_epoch": "not_run_terminal",
        },
        "current_v2_chain_revisions": chain_revisions,
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": sorted(sections),
            "changed_signatures": audit["changed_signatures"],
            "buggy_signatures_required_by_inverse": audit[
                "buggy_signatures_required_by_inverse"
            ],
            "fixed_test_calls": audit["fixed_test_calls"],
            "fixed_only_api": audit["incompatible"],
            "buggy_compile_expected": True,
            "explanation": (
                "The production-only inverse restores the old names-parameter signatures; "
                "the preserved test hunk calls the new signatures, so the buggy tree cannot compile it."
            ),
        },
        "supersedes_nonterminal_blocker": (
            "candidate_gate57_11011_fresh_execution_blocker/luanti/11011/native/v1"
        ),
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "not_run": True,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
        },
        "next_action": "do_not_schedule_native_execution; remove_candidate_from_gate57_pool",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#11011 Gate 4 terminal conflicts with current-v2 state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_TERMINAL.artifact_id,
        artifact_kind=_TERMINAL.artifact_kind,
        semantic_version=_TERMINAL.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _TERMINAL)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#11011 Gate 4 terminal typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    chain = (source, patch, atomicity, manifest)
    if any(artifact.payload.get("candidate") != _CANDIDATE for artifact in chain):
        raise RuntimeError("#11011 current-v2 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#11011 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
        or patch.payload.get("source_manifest_path")
        != source.payload.get("raw_evidence_path")
    ):
        raise RuntimeError("#11011 test patch inventory is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 10995
    ):
        raise RuntimeError("#11011 source atomicity is not accepted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 2
    ):
        raise RuntimeError("#11011 test manifest is not passed")


def _audit_fixed_only_api(sections: dict[str, tuple[str, ...]]) -> dict[str, object]:
    production = tuple(line for path in _PRODUCTION_PATHS for line in sections[path])
    test = sections["src/unittest/test_schematic.cpp"]
    changed_signatures = {
        "deserializeFromMts": {
            "buggy": "bool deserializeFromMts(std::istream *is, std::vector<std::string> *names)",
            "fixed": "bool deserializeFromMts(std::istream *is)",
            "buggy_present_in_deleted_lines": any(
                line.startswith("-") and "deserializeFromMts(std::istream *is," in line
                for line in production
            ),
            "fixed_present_in_added_lines": any(
                line.startswith("+") and "deserializeFromMts(std::istream *is)" in line
                for line in production
            ),
        },
        "serializeToMts": {
            "buggy": "bool serializeToMts(std::ostream *os, const std::vector<std::string> &names) const",
            "fixed": "bool serializeToMts(std::ostream *os) const",
            "buggy_present_in_deleted_lines": any(
                line.startswith("-") and "serializeToMts(std::ostream *os," in line
                for line in production
            ),
            "fixed_present_in_added_lines": any(
                line.startswith("+")
                and "serializeToMts(std::ostream *os) const" in line
                for line in production
            ),
        },
        "serializeToLua": {
            "buggy": "bool serializeToLua(std::ostream *os, const std::vector<std::string> &names, bool use_comments, u32 indent_spaces) const",
            "fixed": "bool serializeToLua(std::ostream *os, bool use_comments, u32 indent_spaces) const",
            "buggy_present_in_deleted_lines": any(
                line.startswith("-") and "serializeToLua(std::ostream *os," in line
                for line in production
            ),
            "fixed_present_in_added_lines": any(
                line.startswith("+")
                and "serializeToLua(std::ostream *os, bool use_comments" in line
                for line in production
            ),
        },
    }
    fixed_test_calls = [
        line[1:].strip()
        for line in test
        if line.startswith("+")
        and any(
            marker in line
            for marker in (
                "serializeToMts(&ss)",
                "deserializeFromMts(&ss)",
                "serializeToLua(&ss, false, 0)",
            )
        )
    ]
    buggy_required = [
        "serializeToMts requires a second names argument",
        "deserializeFromMts requires a second names argument",
        "serializeToLua requires a names argument",
    ]
    incompatible = bool(fixed_test_calls) and all(
        value["buggy_present_in_deleted_lines"]
        and value["fixed_present_in_added_lines"]
        for value in changed_signatures.values()
    )
    return {
        "incompatible": incompatible,
        "changed_signatures": changed_signatures,
        "buggy_signatures_required_by_inverse": buggy_required,
        "fixed_test_calls": fixed_test_calls,
    }


def _parse_patch(raw_patch: bytes) -> dict[str, tuple[str, ...]]:
    text = raw_patch.decode("utf-8", errors="surrogateescape")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#11011 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#11011 raw patch path header is invalid")
            current = _safe_relative(left[2:])
            if current in sections:
                raise RuntimeError("#11011 raw patch has duplicate path")
            sections[current] = []
        if current is not None:
            sections[current].append(line)
    if not sections:
        raise RuntimeError("#11011 raw patch has no sections")
    return {path: tuple(lines) for path, lines in sections.items()}


def _safe_relative(value: object) -> str:
    if type(value) is not str or not value:
        raise RuntimeError("#11011 path is invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise RuntimeError("#11011 path is unsafe")
    return path.as_posix()


def _regular_file(path: Path) -> Path:
    try:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("#11011 raw patch is missing or unsafe")
    except OSError as error:
        raise RuntimeError("#11011 raw patch is unreadable") from error
    return path


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _TERMINAL)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["audit_luanti_11011_gate4"]
