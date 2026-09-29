"""Typed no-execution blocker for Luanti #14423 Gate 5--7.

The current-v2 source chain for #14423 is present, but it is not a safe
candidate for a fresh Gate 5--7 epoch.  The pull request is a five-commit
bundle with several unrelated server fixes and unit-test/CI changes.  The
sole issue bound by the current source-atomicity receipt (#14432) is a client
``g_menuclouds`` assertion; the PR production diff has no client-side path and
does not repair that assertion.  Consequently the source-to-defect mapping
and the required production-only inverse cannot be proven as one defect.

This module records that exact, current-v2-bound stop.  It deliberately does
not materialize trees, build, run tests, load old attempts, or perform any
downstream/paid/formal activity.  A future authoritative re-enumeration (for
example, a single-defect commit candidate) must use a new candidate identity
and a new audit rather than inheriting this receipt.
"""

from __future__ import annotations

import json
import os
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14423}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14423/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14423/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14423/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14423/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14423_gate4_blocker/luanti/14423/native/v1",
    "candidate_gate57_14423_gate4_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/map.cpp",
    "src/mapblock.cpp",
    "src/script/scripting_server.cpp",
    "src/script/scripting_server.h",
    "src/server.cpp",
    "src/server/luaentity_sao.cpp",
    "src/server/serveractiveobject.h",
    "src/serverenvironment.cpp",
    "src/serverenvironment.h",
    "src/staticobject.cpp",
    "src/unittest/mock_server.h",
)
_TEST_PATHS = (
    "src/unittest/test_moveaction.cpp",
    "src/unittest/test_sao.cpp",
    "util/test_multiplayer.sh",
)
_UNACCOUNTED_PATHS = (
    ".github/workflows/linux.yml",
    "src/unittest/CMakeLists.txt",
)


def write_luanti_14423_gate57_blocker(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist and reload the no-execution current-v2 blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14423 Gate 5--7 blocker authorization is inactive")

    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    source, patch, atomicity, manifest = chain
    _validate_chain(source, patch, atomicity, manifest)

    source_manifest_path = _safe_relative(
        source.payload.get("raw_evidence_path"), "#14423 source manifest path"
    )
    source_manifest_file = _regular_file(
        root / source_manifest_path, "#14423 source manifest"
    )
    source_manifest = _load_json(source_manifest_file, "#14423 source manifest")
    pull = source_manifest.get("pull")
    issues = source_manifest.get("issues")
    if type(pull) is not dict or type(issues) is not list or len(issues) != 1:
        raise RuntimeError("#14423 source manifest lacks typed pull/issue evidence")
    issue = issues[0].get("issue") if type(issues[0]) is dict else None
    if type(issue) is not dict:
        raise RuntimeError("#14423 source manifest issue evidence is invalid")

    raw_patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "#14423 raw patch path"
    )
    raw_patch = _regular_file(root / raw_patch_path, "#14423 raw patch").read_text(
        encoding="utf-8", errors="surrogateescape"
    )
    raw_paths = tuple(sorted(_parse_patch_paths(raw_patch)))
    declared_paths = tuple(sorted(_PRODUCTION_PATHS + _TEST_PATHS))
    expected_raw_paths = tuple(sorted(declared_paths + _UNACCOUNTED_PATHS))
    if raw_paths != expected_raw_paths:
        raise RuntimeError(
            "#14423 raw patch inventory changed; current-v2 blocker requires re-audit"
        )

    issue_body = str(issue.get("body") or "")
    issue_title = str(issue.get("title") or "")
    issue_number = issue.get("number")
    pull_commits = pull.get("commits")
    pull_changed_files = pull.get("changed_files")
    pull_body = str(pull.get("body") or "")
    commit_markers = tuple(
        marker
        for marker in ("[commit 1]", "[commit 2]", "[commit 3]", "[commit 4]")
        if marker in pull_body
    )
    client_production_paths = tuple(
        path
        for path in _PRODUCTION_PATHS
        if path.startswith("src/client/") or "clientlauncher" in path
    )
    issue_client_indicators = tuple(
        token
        for token in ("g_menuclouds", "clientlauncher.cpp", "@ Client / Audiovisuals")
        if token in issue_title
        or token in issue_body
        or any(
            token in str(label.get("name"))
            for label in issue.get("labels", ())
            if type(label) is dict
        )
    )
    if issue_number != 14432 or pull_commits != 5 or pull_changed_files != 16:
        raise RuntimeError("#14423 source manifest identity changed; re-audit required")
    if client_production_paths or not {"g_menuclouds", "clientlauncher.cpp"}.issubset(
        set(issue_client_indicators)
    ):
        raise RuntimeError(
            "#14423 expected client-mapping mismatch is no longer present"
        )

    # Do not silently allow a later profile/capsule/runner to coexist with
    # this receipt.  Any such state requires a fresh, candidate-specific audit.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/14423",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/14423",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/14423",
        "candidate_gate57_profile": root
        / "artifacts/candidate_gate57_profile/luanti/14423",
        "candidate_gate57_aggregate": root
        / "artifacts/candidate_gate57_aggregate/luanti/14423",
        "candidate_gate57_transition": root
        / "artifacts/candidate_gate57_transition/luanti/14423",
    }
    present = {name: path.exists() for name, path in required.items()}
    if any(present.values()):
        raise RuntimeError(
            "#14423 Gate 4 blocker prerequisites changed; re-audit exact chain"
        )

    chain_revisions = {
        artifact.reference.artifact_id: artifact.created_at for artifact in chain
    }
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14423-server-bundle-v2",
        "state": "blocked",
        "outcome": "candidate_mapping_and_gate4_blocked",
        "terminal": True,
        "resumable": False,
        "reason": (
            "current_v2 binds client issue #14432 to a five-commit multi-defect server bundle; "
            "production-only inverse cannot represent one authoritative defect"
        ),
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "source_authority": "passed_but_mapped_issue_is_incompatible",
            "single_defect_mapping": "blocked_client_issue_vs_server_bundle",
            "strict_inverse_patch": "blocked_multi_commit_multi_defect",
            "test_api_compatibility": "not_evaluated",
            "fresh_gate57_epoch": "blocked_before_materialization",
        },
        "current_v2_chain_revisions": chain_revisions,
        "source_mapping_audit": {
            "bound_issue_number": issue_number,
            "bound_issue_title": issue_title,
            "bound_issue_client_indicators": list(issue_client_indicators),
            "pull_title": pull.get("title"),
            "pull_commit_count": pull_commits,
            "pull_changed_files": pull_changed_files,
            "pull_body_commit_markers": list(commit_markers),
            "production_paths": list(_PRODUCTION_PATHS),
            "test_paths": list(_TEST_PATHS),
            "unaccounted_paths": list(_UNACCOUNTED_PATHS),
            "client_production_paths": list(client_production_paths),
            "raw_patch_paths": list(raw_paths),
            "issue_to_production_path_overlap": False,
            "authoritative_rebind_required": True,
        },
        "required_current_v2_chain": {
            name: {"registered": present[name], "probe": path.as_posix()}
            for name, path in required.items()
        },
        "fresh_epoch": {
            "required_attempt_count": 12,
            "attempt_count": 0,
            "inherit_prior_attempts": False,
            "old_attempts_referenced": False,
            "cells": [
                {
                    "phase": "f2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "f2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "failure",
                },
                {
                    "phase": "p2p",
                    "variant": "fixed",
                    "repetitions": 3,
                    "expected": "pass",
                },
                {
                    "phase": "p2p",
                    "variant": "buggy",
                    "repetitions": 3,
                    "expected": "pass",
                },
            ],
        },
        "missing_for_fresh_gate5_7": [
            "authoritative_single_defect_source_rebinding",
            "single_defect_production_only_inverse_patch",
            "candidate_profile_and_selector_binding",
            "fixed_source_capsule_and_native_toolchain_closure",
            "fresh_12_cell_gate57_aggregate_and_transition",
        ],
        "next_action": (
            "do_not_relaunch_14423; re-enumerate a new candidate identity from one defect/commit "
            "with an issue whose scope overlaps the production inverse"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14423 Gate 4 blocker conflicts with current-v2 state")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, _BLOCKER)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14423 Gate 4 blocker typed reload failed")
    return artifact


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if any(
        artifact.payload.get("candidate") != _CANDIDATE
        for artifact in (source, patch, atomicity, manifest)
    ):
        raise RuntimeError("#14423 current-v2 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14423 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
    ):
        raise RuntimeError("#14423 test patch inventory is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 14432
    ):
        raise RuntimeError(
            "#14423 source atomicity is not the registered single-issue receipt"
        )
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or type(details) is not dict
        or details.get("executable_test_paths")
        != ["src/unittest/test_moveaction.cpp", "src/unittest/test_sao.cpp"]
        or details.get("test_count") != 2
    ):
        raise RuntimeError("#14423 test manifest is not current-v2 passed")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path) -> StableArtifact | None:
    try:
        return _load(root, _BLOCKER)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _safe_relative(value: object, label: str) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or "." in path.parts or ".." in path.parts:
        raise RuntimeError(f"{label} is unsafe")
    return path


def _regular_file(path: Path, label: str) -> Path:
    try:
        if not path.is_file():
            raise RuntimeError(f"{label} is missing")
    except OSError as error:
        raise RuntimeError(f"{label} is unreadable") from error
    return path


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is not valid JSON") from error
    if type(value) is not dict:
        raise RuntimeError(f"{label} root is not an object")
    return value


def _parse_patch_paths(raw_patch: str) -> set[str]:
    paths: set[str] = set()
    for line in raw_patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        fields = line.split()
        if len(fields) != 4 or fields[:2] != ["diff", "--git"]:
            raise RuntimeError("#14423 raw patch header is invalid")
        left, right = fields[2], fields[3]
        if (
            not left.startswith("a/")
            or not right.startswith("b/")
            or left[2:] != right[2:]
        ):
            raise RuntimeError("#14423 raw patch path header is invalid")
        path = _safe_relative(left[2:], "#14423 patch path").as_posix()
        if path in paths:
            raise RuntimeError("#14423 raw patch has duplicate path")
        paths.add(path)
    if not paths:
        raise RuntimeError("#14423 raw patch has no sections")
    return paths


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14423_gate57_blocker"]
