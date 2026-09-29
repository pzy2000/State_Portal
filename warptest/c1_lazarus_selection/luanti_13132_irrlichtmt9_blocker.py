"""Strict current-v2 Gate 1--7 reload and Gate 8 blocker for Luanti #13132.

The candidate's retained source pins IrrlichtMt ``1.9.0mt9``.  This audit
recursively reloads the fresh timeout-repair receipts and records the exact
missing dependency without borrowing another candidate's Irrlicht tree.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 13132}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/13132/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/13132/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/13132/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/13132/test_manifest/v2", "candidate_stage", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/13132/fixed/v2", "fixed_source_capsule", "2.0"
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_aggregate/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_transition/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_transition",
    "1.0",
)
_BUILD = ArtifactReference(
    "candidate_build_receipt/luanti/13132/fixed-buggy/v1",
    "candidate_build_receipt",
    "1.0",
)
_COMPARISON = ArtifactReference(
    "candidate_downstream_readiness_comparison/luanti/13132-9722/current-v2/v1",
    "candidate_downstream_readiness_comparison",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate8_13132_irrlichtmt9_blocker/luanti/13132/current-v2/v1",
    "candidate_gate8_13132_irrlichtmt9_blocker",
    "1.0",
)
_SOURCE_TREE = PurePosixPath("private/candidate-source/luanti/13132/fixed")
_DEPENDENCY_TREE = PurePosixPath(
    "private/dependencies/luanti-13132-irrlicht/1.9.0mt9/source"
)


def write_luanti_13132_irrlichtmt9_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist an exact resumable blocker after a strict recursive reload."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "infrastructure-audit"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#13132 IrrlichtMt audit authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, ref)
        for ref in (
            _SOURCE,
            _PATCH,
            _ATOMICITY,
            _MANIFEST,
            _CAPSULE,
            _AGGREGATE,
            _TRANSITION,
            _BUILD,
            _COMPARISON,
        )
    )
    source, patch, atomicity, manifest, capsule, aggregate, transition, build, comparison = chain
    _validate_gate14(source, patch, atomicity, manifest, capsule)
    attempts = _validate_gate57(root, aggregate, transition)
    _validate_downstream(build, comparison)

    source_tree = _ordinary_dir(root / _SOURCE_TREE, root)
    tag_file = _ordinary_file(source_tree / "misc/irrlichtmt_tag.txt", root)
    required_tag = tag_file.read_text(encoding="utf-8").strip()
    if required_tag != "1.9.0mt9":
        raise RuntimeError("#13132 retained source IrrlichtMt tag drifted")
    if (source_tree / "lib/irrlichtmt").exists():
        raise RuntimeError("#13132 vendored IrrlichtMt appeared; re-audit required")
    dependency_tree = root / _DEPENDENCY_TREE
    if dependency_tree.exists() or dependency_tree.is_symlink():
        raise RuntimeError("#13132 exact IrrlichtMt closure appeared; re-audit required")

    payload = {
        "candidate": _CANDIDATE,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "gate_1_7": {
            "status": "qualified",
            "attempt_count": 12,
            "fixed_f2p": "pass_3x",
            "buggy_f2p": "clean_failure_3x",
            "fixed_p2p": "pass_3x",
            "buggy_p2p": "pass_3x",
            "no_timeout_or_unknown": True,
            "no_attempt_inheritance": True,
            "recursively_reloaded_attempts": len(attempts),
        },
        "required_dependency": {
            "name": "IrrlichtMt",
            "version": required_tag,
            "version_evidence": f"{_SOURCE_TREE.as_posix()}/misc/irrlichtmt_tag.txt",
            "candidate_owned_destination": _DEPENDENCY_TREE.as_posix(),
            "registered": False,
            "vendored_in_candidate_source": False,
        },
        "gate8_status": "blocked_missing_provenance_bound_irrlichtmt_1.9.0mt9",
        "gate9_to_13": "pending_not_rejection",
        "other_candidate_dependency_reused": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "new_build_started": False,
        "network_used": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "does_not_invalidate_gate_1_7": True,
        "next_action": (
            "register a provenance-bound IrrlichtMt 1.9.0mt9 source closure, "
            "materialize the fixed/buggy pair, then build each side single-threaded "
            "with only a C++17 compiler-dialect override"
        ),
        "complete": True,
    }
    inputs = tuple(item.reference for item in chain) + tuple(
        item.reference for item in attempts
    )
    existing = _try_load(root, _BLOCKER)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#13132 IrrlichtMt blocker conflicts with current state")
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
        raise RuntimeError("#13132 IrrlichtMt blocker typed reload failed")
    return artifact


def _validate_gate14(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    capsule: StableArtifact,
) -> None:
    if any(item.payload.get("candidate") != _CANDIDATE for item in (source, patch, atomicity, manifest, capsule)):
        raise RuntimeError("#13132 Gate 1--4 candidate binding drifted")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#13132 source closure is incomplete")
    if (
        patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != ["builtin/common/misc_helpers.lua"]
        or patch.payload.get("test_paths") != ["builtin/common/tests/misc_helpers_spec.lua"]
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_numbers") != [13128]
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("test_count") != 1
        or capsule.payload.get("profile_id") != "luanti-13132-empty-separator-v1"
    ):
        raise RuntimeError("#13132 Gate 1--4 evidence drifted")


def _validate_gate57(
    root: Path, aggregate: StableArtifact, transition: StableArtifact
) -> tuple[StableArtifact, ...]:
    if (
        aggregate.payload.get("candidate") != _CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("no_timeout_or_unknown") is not True
        or aggregate.payload.get("old_attempts_loaded") is not False
        or transition.payload.get("candidate") != _CANDIDATE
        or transition.payload.get("qualified") is not True
        or transition.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#13132 strict Gate 5--7 aggregate drifted")
    attempt_refs = tuple(
        ref
        for ref in aggregate.inputs
        if ref.artifact_kind == "candidate_gate57_13132_timeout_repair_attempt"
    )
    if len(attempt_refs) != 12 or len(set(attempt_refs)) != 12:
        raise RuntimeError("#13132 Gate 5--7 attempt lineage is incomplete")
    attempts = tuple(_load(root, ref) for ref in attempt_refs)
    expected = {
        ("f2p", "fixed"): "pass",
        ("f2p", "buggy"): "failure",
        ("p2p", "fixed"): "pass",
        ("p2p", "buggy"): "pass",
    }
    coverage: set[tuple[str, str, int]] = set()
    for attempt in attempts:
        payload = attempt.payload
        key = (payload.get("phase"), payload.get("variant"))
        repetition = payload.get("repetition")
        if (
            payload.get("candidate") != _CANDIDATE
            or key not in expected
            or repetition not in {1, 2, 3}
            or payload.get("observed_outcome") != expected[key]
            or payload.get("process_outcome") != "completed"
            or payload.get("matched_expected") is not True
            or payload.get("fresh_receipt") is not True
            or payload.get("old_attempt_loaded") is not False
            or payload.get("observed_outcome") in {"timed_out", "unknown"}
        ):
            raise RuntimeError("#13132 Gate 5--7 attempt is not strict/fresh")
        coverage.add((str(key[0]), str(key[1]), int(repetition)))
    if len(coverage) != 12:
        raise RuntimeError("#13132 Gate 5--7 cell coverage is incomplete")
    return attempts


def _validate_downstream(
    build: StableArtifact, comparison: StableArtifact
) -> None:
    if (
        build.payload.get("candidate") != _CANDIDATE
        or build.payload.get("outcome") != "infrastructure_blocked"
        or build.payload.get("configure", {}).get("required_dependency") != "IrrlichtMt"
        or comparison.payload.get("recommended_candidate") != 13132
        or comparison.payload.get("terminal") is not False
    ):
        raise RuntimeError("#13132 downstream blocker evidence drifted")


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#13132 audited directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#13132 audited file is unsafe")
    return resolved


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


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_13132_irrlichtmt9_blocker"]
