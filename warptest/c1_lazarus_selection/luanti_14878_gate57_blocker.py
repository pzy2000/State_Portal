"""Typed Gate 5--7 blocker for Luanti change 14878.

The current-v2 source, patch, atomicity and test-manifest receipts are
registered for this security regression, but their execution contract is not
safe to advance.  The pull request introduces ``sanitize_untrusted`` in the
production string API and adds tests that call that new symbol.  Reversing
the production-only patch therefore leaves the preserved test calling an API
which does not exist in the buggy tree.  The raw patch also contains a
documentation hunk which is absent from the typed path inventory.

This receipt is deliberately resumable: it performs no materialization,
build, candidate execution, paid annotation, or formal trial.  A future
corrected current-v2 test contract must replace the blocker and be re-audited
before a fresh twelve-cell Gate 5--7 epoch is allowed.
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


_CANDIDATE = {"repository_key": "luanti", "change_number": 14878}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14878/source/v2",
    "candidate_source_packet",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14878/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14878/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14878/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14878_gate4_blocker/luanti/14878/native/v1",
    "candidate_gate57_14878_gate4_blocker",
    "1.0",
)

_PRODUCTION_PATHS = (
    "src/network/serverpackethandler.cpp",
    "src/util/string.cpp",
    "src/util/string.h",
)
_TEST_PATHS = ("src/unittest/test_utilities.cpp",)
_FIXED_API = "sanitize_untrusted"


def write_luanti_14878_gate57_blocker(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write a current-chain-bound, no-execution Gate 4 blocker."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14878 Gate 5--7 blocker authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = tuple(
        _load(root, reference) for reference in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    )
    source, patch, atomicity, manifest = chain
    _validate_chain(source, patch, atomicity, manifest)

    raw_patch_path = _safe_relative(
        patch.payload.get("raw_patch_path"), "#14878 raw patch path"
    )
    raw_patch = _regular_file(root / raw_patch_path, "#14878 raw patch").read_bytes()
    sections = _parse_patch(raw_patch)
    declared_paths = set(_PRODUCTION_PATHS + _TEST_PATHS)
    undeclared_paths = sorted(set(sections) - declared_paths)
    missing_paths = sorted(declared_paths - set(sections))
    test_additions = sections.get(_TEST_PATHS[0], ())
    fixed_api_additions = sorted(
        path
        for path, lines in sections.items()
        if path in _PRODUCTION_PATHS and any(_FIXED_API in line for line in lines)
    )
    test_fixed_api_calls = [line for line in test_additions if _FIXED_API + "(" in line]
    fixed_only_api = bool(fixed_api_additions and test_fixed_api_calls)
    if not fixed_only_api:
        raise RuntimeError(
            "#14878 expected fixed-only sanitize_untrusted API incompatibility was not observed"
        )
    if not undeclared_paths:
        raise RuntimeError(
            "#14878 expected undeclared documentation path was not observed"
        )

    # Probe only exact candidate namespaces.  A future corrected profile or
    # execution receipt must force a fresh audit rather than being masked by
    # this blocker.
    required = {
        "candidate_profile": root / "artifacts/candidate_profile/luanti/14878",
        "candidate_preparation": root / "artifacts/candidate_preparation/luanti/14878",
        "fixed_source_capsule": root / "artifacts/fixed_source_capsule/luanti/14878",
        "gate57_namespace": root
        / "artifacts/candidate_gate57_14878_gate4_blocker/luanti/14878",
    }
    present = {name: path.exists() for name, path in required.items()}
    # This namespace is the receipt currently being written, not an existing
    # execution prerequisite.  Keep the probe stable across idempotent reloads.
    present["gate57_namespace"] = False
    if any(present[name] for name in ("candidate_profile", "fixed_source_capsule")):
        raise RuntimeError("#14878 Gate 4 blocker inputs changed; re-audit exact chain")

    chain_revisions = {
        artifact.reference.artifact_id: artifact.created_at for artifact in chain
    }
    payload: dict[str, Any] = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14878-formspec-sanitize-v2",
        "state": "blocked",
        "outcome": "gate4_compatibility_blocked",
        "terminal": False,
        "resumable": True,
        "reason": "preserved_test_calls_fixed_only_sanitize_untrusted_api",
        "candidate_execution_performed": False,
        "candidate_materialization_performed": False,
        "new_build_started": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "blocked_undeclared_patch_path",
            "test_api_compatibility": "blocked_fixed_only_api",
            "fresh_gate57_epoch": "blocked_before_first_attempt",
        },
        "current_v2_chain_revisions": chain_revisions,
        "patch_audit": {
            "declared_production_paths": list(_PRODUCTION_PATHS),
            "declared_test_paths": list(_TEST_PATHS),
            "raw_patch_paths": sorted(sections),
            "undeclared_paths": undeclared_paths,
            "missing_declared_paths": missing_paths,
            "fixed_api": _FIXED_API,
            "fixed_api_added_in_production_paths": fixed_api_additions,
            "preserved_test_fixed_api_call_count": len(test_fixed_api_calls),
            "preserved_test_fixed_api_call_examples": test_fixed_api_calls[:8],
            "buggy_compile_expected": True,
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
            "corrected_current_v2_test_contract_without_fixed_only_api",
            "typed_inventory_accounting_for_doc_lua_api_md",
            "candidate_profile_and_selector_binding",
            "fixed_source_capsule_and_production_only_buggy_inverse",
            "candidate_specific_native_toolchain_and_runner",
            "fresh_12_cell_gate57_aggregate_and_transition",
        ],
        "next_action": (
            "replace_or_reclassify_the_current_v2_test_contract_to_use_only_buggy_visible_API_and_account_for_doc_lua_api_md_then_rerun_gate1_4_before_any_build"
        ),
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST)
    existing = _try_load(root)
    if existing is not None:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError("#14878 Gate 4 blocker conflicts with current-v2 state")
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
        raise RuntimeError("#14878 Gate 4 blocker typed reload failed")
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
        raise RuntimeError("#14878 current-v2 chain candidate binding is invalid")
    if source.payload.get("complete") is not True:
        raise RuntimeError("#14878 source packet is not complete")
    if (
        patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION_PATHS
        or tuple(patch.payload.get("test_paths", ())) != _TEST_PATHS
    ):
        raise RuntimeError("#14878 test patch inventory is not current-v2 eligible")
    if (
        atomicity.payload.get("complete") is not True
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("authoritative_issue_count") != 1
        or atomicity.payload.get("primary_issue_number") != 14875
    ):
        raise RuntimeError("#14878 source atomicity is not accepted")
    details = manifest.payload.get("details")
    if (
        manifest.payload.get("complete") is not True
        or manifest.payload.get("stage") != "test_manifest"
        or manifest.payload.get("state") != "passed"
        or not isinstance(details, dict)
        or details.get("executable_test_paths") != list(_TEST_PATHS)
        or details.get("test_count") != 1
    ):
        raise RuntimeError("#14878 test manifest is not passed")


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


def _parse_patch(raw_patch: bytes) -> dict[str, tuple[str, ...]]:
    text = raw_patch.decode("utf-8", errors="surrogateescape")
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = shlex.split(line)
            if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
                raise RuntimeError("#14878 raw patch header is invalid")
            left, right = parts[2], parts[3]
            if (
                not left.startswith("a/")
                or not right.startswith("b/")
                or left[2:] != right[2:]
            ):
                raise RuntimeError("#14878 raw patch path header is invalid")
            path = _safe_relative(left[2:], "#14878 patch path").as_posix()
            if path in result:
                raise RuntimeError("#14878 raw patch has duplicate path")
            current = path
            result[current] = []
            continue
        if current is not None and line.startswith("+") and not line.startswith("+++"):
            result[current].append(line[1:])
    if not result:
        raise RuntimeError("#14878 raw patch has no sections")
    return {path: tuple(lines) for path, lines in result.items()}


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["write_luanti_14878_gate57_blocker"]
