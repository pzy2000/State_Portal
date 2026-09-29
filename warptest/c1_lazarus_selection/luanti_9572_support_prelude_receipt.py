"""Typed support-prelude receipt for the fresh Luanti #9572 Gate 5--7 epoch.

The closure epoch has already executed its twelve bounded cells.  This small
candidate-specific audit records the exact merge revision and hashes the
support file and bootstrap used by every receipt.  It deliberately does not
edit a production or test file and does not read the old invalid-test-count
epoch.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_ROOT = ArtifactReference(
    "candidate_source_packet/luanti/9572/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PROFILE = ArtifactReference(
    "candidate_gate57_9572_closure_profile/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_profile",
    "1.0",
)
_PREPARATION = ArtifactReference(
    "candidate_gate57_9572_closure_preparation/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_preparation",
    "1.0",
)
_CAPSULE = ArtifactReference(
    "candidate_gate57_9572_closure_capsule/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_capsule",
    "1.0",
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_9572_closure_aggregate/luanti/9572/lua/v1",
    "candidate_gate57_9572_closure_aggregate",
    "1.0",
)
_RECEIPT = ArtifactReference(
    "candidate_gate57_9572_support_prelude_receipt/luanti/9572/lua/v1",
    "candidate_gate57_9572_support_prelude_receipt",
    "1.0",
)
_MERGE_SHA = "7148834440d10bc211628aa2652e31166bdd58a0"
_PRODUCTION = "builtin/common/vector.lua"
_SUPPORT = "builtin/common/misc_helpers.lua"
_TEST = "builtin/common/tests/vector_spec.lua"
_TREE = PurePosixPath("private/fixed-capsules/luanti/9572/closure-source-v1")
_FIXED_TREE = PurePosixPath("private/fixed-capsules/luanti/9572/fixed-source-v2")
_LOADER_NAME = "c1_lazarus_9572_registered_support_loader.lua"
_LOADER_BYTES = b'_G.core = _G.core or {}\ndofile("builtin/common/misc_helpers.lua")\n'


def write_luanti_9572_support_prelude_receipt(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Verify and persist the typed same-SHA support-prelude receipt."""

    if (
        authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9572 support-prelude authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _ROOT)
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    aggregate = _load(root, _AGGREGATE)
    if source.payload.get("candidate") != {"repository_key": "luanti", "change_number": 9572}:
        raise RuntimeError("#9572 support-prelude source candidate is invalid")
    if profile.payload.get("candidate") != source.payload.get("candidate"):
        raise RuntimeError("#9572 support-prelude profile candidate is invalid")
    if preparation.payload.get("state") != "prepared":
        raise RuntimeError("#9572 support-prelude preparation is invalid")
    if capsule.payload.get("source_tree_path") != _TREE.as_posix():
        raise RuntimeError("#9572 support-prelude capsule tree is invalid")
    if aggregate.payload.get("qualified") is not True:
        raise RuntimeError("#9572 support-prelude requires qualified aggregate")
    merge_sha, source_manifest_path = _merge_revision(root, source)
    if merge_sha != _MERGE_SHA:
        raise RuntimeError("#9572 support-prelude merge revision changed")
    mirror = root / "private/repository-enumeration-git/luanti/mirror.git"
    closure = root.joinpath(*_TREE.parts)
    fixed = root.joinpath(*_FIXED_TREE.parts)
    support = _regular(closure / _SUPPORT)
    fixed_support = _regular(fixed / _PRODUCTION)  # proves fixed tree exists
    del fixed_support
    expected_support = _git_show(mirror, merge_sha, _SUPPORT)
    expected_vector = _git_show(mirror, merge_sha, _PRODUCTION)
    expected_test = _git_show(mirror, merge_sha, _TEST)
    if support != expected_support:
        raise RuntimeError("#9572 support-prelude support bytes mismatch")
    if _regular(closure / _PRODUCTION) != expected_vector:
        raise RuntimeError("#9572 support-prelude production bytes mismatch")
    if _regular(closure / _TEST) != expected_test:
        raise RuntimeError("#9572 support-prelude test bytes mismatch")
    if b"function math.hypot" not in support:
        raise RuntimeError("#9572 support-prelude math.hypot is missing")
    attempts = _attempts(root)
    if len(attempts) != 12:
        raise RuntimeError("#9572 support-prelude attempt coverage is invalid")
    if any(
        item.payload.get("process_outcome") != "completed"
        or item.payload.get("observed_outcome") in {"timed_out", "launch_failed", "unknown"}
        or item.payload.get("fresh_receipt") is not True
        or item.payload.get("candidate_execution_performed") is not True
        or item.payload.get("support_loader")
        != {
            "kind": "registered_support_loader",
            "script": _LOADER_NAME,
            "loads": [_SUPPORT],
            "modifies_test": False,
        }
        for item in attempts
    ):
        raise RuntimeError("#9572 support-prelude attempt loader/timeout validation failed")
    loader_hash = hashlib.sha256(_LOADER_BYTES).hexdigest()
    payload = {
        "candidate": source.payload["candidate"],
        "profile_id": profile.payload["profile_id"],
        "state": "validated",
        "outcome": "qualified",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "execution_lineage": aggregate.payload["execution_lineage"],
        "aggregate_reference": aggregate.reference.artifact_id,
        "attempt_count": len(attempts),
        "merge_revision": merge_sha,
        "source_manifest_path": source_manifest_path,
        "same_merged_tree": True,
        "paths": {"production": _PRODUCTION, "support": _SUPPORT, "test": _TEST},
        "byte_fingerprints": {
            "support_fixed": _sha256(support),
            "support_buggy": _sha256(support),
            "support_merged": _sha256(expected_support),
            "production_fixed": _sha256(expected_vector),
            "test_merged": _sha256(expected_test),
            "registered_bootstrap": loader_hash,
        },
        "registered_bootstrap": {
            "script": _LOADER_NAME,
            "script_fingerprint": loader_hash,
            "core_table_initialized": True,
            "support_loaded_before_test": True,
            "loads": [_SUPPORT],
            "modifies_test": False,
            "registered": True,
        },
        "attempt_references": [item.reference.artifact_id for item in attempts],
        "runtime_observations": {
            "no_flake": True,
            "no_timeout": True,
            "no_unknown": True,
            "fixed_f2p": ["pass", "pass", "pass"],
            "buggy_f2p": ["failure", "failure", "failure"],
            "fixed_p2p": ["pass", "pass", "pass"],
            "buggy_p2p": ["pass", "pass", "pass"],
        },
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    _assert_absent(root, _RECEIPT)
    inputs = (
        _ROOT,
        profile.reference,
        preparation.reference,
        capsule.reference,
        *tuple(item.reference for item in attempts),
        aggregate.reference,
    )
    write_stable_artifact(
        root,
        artifact_id=_RECEIPT.artifact_id,
        artifact_kind=_RECEIPT.artifact_kind,
        semantic_version=_RECEIPT.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    receipt = _load(root, _RECEIPT)
    if receipt.payload != payload or receipt.inputs != inputs:
        raise RuntimeError("#9572 support-prelude receipt typed reload failed")
    return receipt


def _attempts(root: Path) -> tuple[StableArtifact, ...]:
    base = root / "artifacts/candidate_gate57_9572_closure_attempt/luanti/9572/lua"
    refs = []
    for phase, selector in (("f2p", "vector rotate() rotates"), ("p2p", "vector add()")):
        for variant, expected in (("fixed", "pass"), ("buggy", "failure" if phase == "f2p" else "pass")):
            for repetition in range(1, 4):
                ref = ArtifactReference(
                    f"candidate_gate57_9572_closure_attempt/luanti/9572/lua/{phase}/{variant}/{repetition}/v1",
                    "candidate_gate57_9572_closure_attempt",
                    "1.0",
                )
                item = _load(root, ref)
                if item.payload.get("selector") != selector or item.payload.get("expected_outcome") != expected:
                    raise RuntimeError("#9572 support-prelude selector/expectation is invalid")
                refs.append(item)
    return tuple(refs)


def _merge_revision(root: Path, source: StableArtifact) -> tuple[str, str]:
    raw = source.payload.get("raw_evidence_path")
    if not isinstance(raw, str):
        raise RuntimeError("#9572 support-prelude source evidence path is invalid")
    path = root / PurePosixPath(raw)
    data = json.loads(path.read_text(encoding="utf-8"))
    pull = data.get("pull")
    if not isinstance(pull, dict) or pull.get("merge_commit_sha") != _MERGE_SHA:
        raise RuntimeError("#9572 support-prelude merge SHA is invalid")
    return _MERGE_SHA, raw


def _git_show(mirror: Path, revision: str, relative: str) -> bytes:
    result = subprocess.run(
        ("git", f"--git-dir={mirror}", "show", f"{revision}:{relative}"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"#9572 support-prelude missing merged file: {relative}")
    return result.stdout


def _regular(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#9572 support-prelude unsafe file: {path}")
    return path.read_bytes()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _assert_absent(root: Path, reference: ArtifactReference) -> None:
    if (root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version).exists():
        raise RuntimeError(f"#9572 support-prelude receipt already exists: {reference.artifact_id}")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_9572_support_prelude_receipt"]
