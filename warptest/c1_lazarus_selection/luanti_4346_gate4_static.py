"""Strict static Gate 4 proof for Luanti #4346.

The frozen test added by #4346 directly constructs ``voxalgo::VoxelLineIterator``
and calls its new members.  Those declarations are introduced by the PR in
``src/voxelalgorithms.h``.  Keeping the test while applying a production-only
inverse therefore leaves it outside the buggy API contract.  This adapter
records that conclusion without source materialization, native builds, test
attempts, paid work, or formal trials.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 4346}
_ROOT = Path("games/luanti")
_PARENT = "8aadc62856cc3789ed345ddf3870e311af60afe9"
_MERGE = "3f8261830e0503cd59d8713d5c9aab12fc1491db"
_TEST = "src/unittest/test_voxelalgorithms.cpp"
_NEW_API_HEADER = "src/voxelalgorithms.h"
_NEW_API_CPP = "src/voxelalgorithms.cpp"
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/4346/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/4346/screening/v2", "candidate_test_patch", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/4346/current/v2", "screening_checkpoint", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/4346/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/4346/test_manifest/v2", "candidate_stage", "2.0"
)
_OUTPUT = ArtifactReference(
    "candidate_gate4_api_compatibility/luanti/4346/current-v1/v1",
    "candidate_gate4_api_compatibility",
    "1.0",
)


def audit_luanti_4346_gate4_static(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write and reload the candidate-specific no-build terminal receipt."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source, patch, checkpoint, atomicity, manifest = tuple(
        _load(root, reference)
        for reference in (_SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    )
    _validate_chain(source, patch, checkpoint, atomicity, manifest)

    source_path = _safe_path(source.payload.get("raw_evidence_path"))
    patch_path = _safe_path(patch.payload.get("raw_patch_path"))
    source_manifest = _read_json(root / source_path)
    raw_patch = _read_bytes(root / patch_path)
    _validate_raw_source(source_manifest)

    mirror = (root.parent.parent / _ROOT).resolve(strict=True)
    _git(mirror, "cat-file", "-e", f"{_PARENT}^{{commit}}")
    _git(mirror, "cat-file", "-e", f"{_MERGE}^{{commit}}")
    if _git(mirror, "merge-base", _PARENT, _MERGE).decode().strip() != _PARENT:
        raise RuntimeError("#4346 expected buggy parent is not the merged ancestor")

    fixed_test = _git_show(mirror, _MERGE, _TEST)
    buggy_test = _git_show(mirror, _PARENT, _TEST)
    fixed_header = _git_show(mirror, _MERGE, _NEW_API_HEADER)
    buggy_header = _git_show(mirror, _PARENT, _NEW_API_HEADER)
    fixed_cpp = _git_show(mirror, _MERGE, _NEW_API_CPP)
    buggy_cpp = _git_show(mirror, _PARENT, _NEW_API_CPP)
    evidence = _fixed_only_evidence(
        fixed_test, buggy_test, fixed_header, buggy_header, fixed_cpp, buggy_cpp
    )
    if not evidence:
        raise RuntimeError("#4346 fixed-only API proof is empty")

    raw_paths = _patch_paths(raw_patch)
    if _TEST not in raw_paths:
        raise RuntimeError("#4346 raw diff lost its frozen test")
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "state": "terminal_gate4_api_incompatible",
        "outcome": "gate4_fixed_test_api_absent_on_buggy",
        "terminal": True,
        "resumable": False,
        "candidate_pool_status": "terminal_rejected",
        "gate_status": {
            "gate_1_source_authority": "passed_source_atomicity_issue_4973",
            "gate_2_single_defect_mapping": "passed_primary_issue_4973",
            "gate_3_strict_inverse_patch": "not_executed_gate4_static_terminal",
            "gate_4_test_api_compatibility": "failed_fixed_only_voxel_line_iterator",
            "gate_5_7": "not_run",
            "gate_8_13": "not_claimed",
        },
        "authority": {
            "primary_issue_number": 4973,
            "authoritative_issue_numbers": [4973],
            "linked_issue_numbers": [4349, 4388, 4973, 4992],
        },
        "frozen_test": {
            "path": _TEST,
            "fixed_sha256": hashlib.sha256(fixed_test).hexdigest(),
            "buggy_parent_sha256": hashlib.sha256(buggy_test).hexdigest(),
            "production_only_inverse_requires_fixed_test_bytes": True,
        },
        "fixed_only_api_evidence": evidence,
        "git_evidence": {
            "mirror": _ROOT.as_posix(),
            "buggy_parent_revision": _PARENT,
            "merged_revision": _MERGE,
            "raw_patch_sha256": hashlib.sha256(raw_patch).hexdigest(),
            "raw_patch_paths": raw_paths,
            "read_only_git_commands": ["cat-file", "merge-base", "show"],
        },
        "preflight": {
            "capsule_created": False,
            "profile_created": False,
            "new_build_started": False,
            "candidate_execution_performed": False,
            "attempts_created": 0,
            "reason": "Gate 4 rejected before materialization; no native build is useful.",
        },
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "roster_modified": False,
        "complete": True,
    }
    inputs = (_SOURCE, _PATCH, _CHECKPOINT, _ATOMICITY, _MANIFEST)
    existing = _try_load(root, _OUTPUT)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError("#4346 Gate 4 terminal receipt drifted")
        return existing
    write_stable_artifact(
        root,
        artifact_id=_OUTPUT.artifact_id,
        artifact_kind=_OUTPUT.artifact_kind,
        semantic_version=_OUTPUT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    result = _load(root, _OUTPUT)
    if result.inputs != inputs or result.payload != payload:
        raise RuntimeError("#4346 Gate 4 terminal typed reload failed")
    return result


def _validate_chain(
    source: StableArtifact,
    patch: StableArtifact,
    checkpoint: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
) -> None:
    if source.inputs != (_ENUMERATION,) or source.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#4346 source chain drifted")
    if patch.inputs != (_ENUMERATION, _SOURCE) or patch.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#4346 patch chain drifted")
    if checkpoint.inputs != (_ENUMERATION, _PATCH) or checkpoint.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#4346 checkpoint chain drifted")
    if atomicity.inputs != (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT):
        raise RuntimeError("#4346 atomicity chain drifted")
    if atomicity.payload.get("authoritative_issue_numbers") != [4973]:
        raise RuntimeError("#4346 authority mapping drifted")
    if manifest.inputs != (_PATCH,) or manifest.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#4346 manifest chain drifted")
    details = manifest.payload.get("details")
    if not isinstance(details, dict) or details.get("executable_test_paths") != [_TEST]:
        raise RuntimeError("#4346 frozen test manifest drifted")


def _validate_raw_source(value: dict[str, Any]) -> None:
    pull = value.get("pull")
    if not isinstance(pull, dict) or pull.get("number") != 4346 or not pull.get("merged"):
        raise RuntimeError("#4346 raw source pull identity drifted")
    if pull.get("merge_commit_sha") != _MERGE:
        raise RuntimeError("#4346 raw source merge drifted")
    if value.get("issue_numbers") != [4349, 4388, 4973, 4992]:
        raise RuntimeError("#4346 raw linked issue inventory drifted")


def _fixed_only_evidence(
    fixed_test: bytes,
    buggy_test: bytes,
    fixed_header: bytes,
    buggy_header: bytes,
    fixed_cpp: bytes,
    buggy_cpp: bytes,
) -> list[dict[str, object]]:
    fixed_test_text = fixed_test.decode("utf-8", errors="surrogateescape")
    buggy_test_text = buggy_test.decode("utf-8", errors="surrogateescape")
    fixed_production = (fixed_header + b"\n" + fixed_cpp).decode(
        "utf-8", errors="surrogateescape"
    )
    buggy_production = (buggy_header + b"\n" + buggy_cpp).decode(
        "utf-8", errors="surrogateescape"
    )
    tokens = (
        ("voxalgo::VoxelLineIterator", "VoxelLineIterator", "VoxelLineIterator"),
        (
            "VoxelLineIterator::hasNext",
            "iterator.hasNext()",
            "bool hasNext() const { return m_has_next;",
        ),
        ("VoxelLineIterator::next", "iterator.next()", "VoxelLineIterator::next"),
        (
            "VoxelLineIterator::m_current_node_pos",
            "iterator.m_current_node_pos",
            "m_current_node_pos",
        ),
    )
    rows: list[dict[str, object]] = []
    for label, test_token, production_token in tokens:
        if test_token not in fixed_test_text:
            raise RuntimeError(f"#4346 frozen test lost {test_token}")
        if production_token not in fixed_production:
            raise RuntimeError(f"#4346 fixed production lost {production_token}")
        if production_token in buggy_production:
            raise RuntimeError(
                f"#4346 buggy production unexpectedly exposes {production_token}"
            )
        rows.append(
            {
                "symbol": label,
                "fixed_test_calls": [
                    line.strip()
                    for line in fixed_test_text.splitlines()
                    if test_token in line
                ],
                "buggy_parent_test_mentions": [
                    line.strip()
                    for line in buggy_test_text.splitlines()
                    if test_token in line
                ],
                "fixed_production_present": True,
                "buggy_production_present": False,
            }
        )
    return rows


def _patch_paths(raw_patch: bytes) -> list[str]:
    paths: list[str] = []
    for line in raw_patch.decode("utf-8", errors="surrogateescape").splitlines():
        if not line.startswith("diff --git "):
            continue
        pieces = shlex.split(line)
        if len(pieces) != 4 or not pieces[2].startswith("a/") or not pieces[3].startswith("b/"):
            raise RuntimeError("#4346 raw patch header is malformed")
        if pieces[2][2:] != pieces[3][2:]:
            raise RuntimeError("#4346 raw patch has a rename/unexpected path")
        paths.append(pieces[2][2:])
    if not paths or len(paths) != len(set(paths)):
        raise RuntimeError("#4346 raw patch path inventory is invalid")
    return paths


def _git(mirror: Path, *args: str) -> bytes:
    completed = subprocess.run(("git", "-C", str(mirror), *args), capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout


def _git_show(mirror: Path, revision: str, path: str) -> bytes:
    return _git(mirror, "show", f"{revision}:{path}")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _safe_path(value: object) -> PurePosixPath:
    if type(value) is not str or not value:
        raise RuntimeError("#4346 raw evidence path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        raise RuntimeError("#4346 raw evidence path is unsafe")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#4346 raw source manifest is missing")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("#4346 raw source manifest is not an object")
    return value


def _read_bytes(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("#4346 raw patch is missing")
    return path.read_bytes()


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope != "candidate-screening" or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#4346 static Gate 4 audit requires candidate-screening authorization")


def _now() -> str:
    return ((datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z"))


__all__ = ["audit_luanti_4346_gate4_static"]
