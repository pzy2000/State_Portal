"""Current-v2 Gate 2--7 closure for Luanti #16200.

The typed screening chain already exists for this candidate.  This module
materializes only the merged PR fixed capsule, registers a candidate-local
C++17 launcher receipt, and delegates one fresh native fixed/buggy 12-cell
execution.  It deliberately never loads an older #16200 attempt and exposes
no paid or formal-trial action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    execute_luanti_native_regression,
)
from .luanti_native_regression import plan_luanti_native_regression_execution
from .models import CandidateKey
from .screening import resolve_raw_patch_evidence
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 16200)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/16200/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/16200/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/16200/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/16200/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/16200/current/v2", "screening_checkpoint", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/16200/fixed/v2", "fixed_source_capsule", "2.0"
)
_TOOLCHAIN = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/16200/cpp17/v1",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_CAPSULE_REL = PurePosixPath("private/fixed-capsules/luanti/16200/fixed-source")
_PROFILE_ID = "luanti-16200-rotation-conventions-v1-native-gate57"
_MERGE_COMMIT = "23bf50a07c7f21408f9738d6fc53b5f37f5ca4b1"
_PRODUCTION = (
    "irr/include/matrix4.h",
    "src/script/lua_api/l_object.cpp",
    "src/util/numeric.h",
)
_SUPPORT = ("doc/lua_api.md",)
_TEST = (
    "builtin/common/tests/vector_spec.lua",
    "src/unittest/test_irr_matrix4.cpp",
    "src/unittest/test_irr_rotation.cpp",
)
_P2P = ("src/unittest/test_k_d_tree.cpp",)


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, Any],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    write_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, reference)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError(f"#16200 typed reload mismatch: {reference.artifact_id}")
    return artifact


def _materialize_capsule(root: Path) -> Path:
    destination = root / _CAPSULE_REL
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError("#16200 fixed capsule namespace is unsafe")
        if not (destination / "CMakeLists.txt").is_file():
            raise RuntimeError("#16200 fixed capsule namespace is incomplete")
    else:
        repo = (root.parent.parent / "games" / "luanti").resolve(strict=True)
        check = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{_MERGE_COMMIT}^{{commit}}"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if check.returncode != 0:
            raise RuntimeError("#16200 merged PR commit is unavailable locally")
        archive = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", _MERGE_COMMIT],
            capture_output=True,
            check=False,
            timeout=180,
        )
        if archive.returncode != 0 or not archive.stdout:
            raise RuntimeError("#16200 fixed capsule git archive failed")
        destination.mkdir(parents=True, mode=0o700)
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as stream:
            for member in stream.getmembers():
                target = (destination / member.name).resolve()
                if target != destination and destination not in target.parents:
                    raise RuntimeError("#16200 capsule archive escaped destination")
            stream.extractall(destination)
    for relative in (*_PRODUCTION, *_SUPPORT, *_TEST, *_P2P):
        path = destination / relative
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"#16200 capsule missing {relative}")
    return destination


def prepare(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> dict[str, object]:
    root = Path(artifact_root).absolute().resolve(strict=True)
    enumeration = _load(root, _ENUMERATION)
    if _CANDIDATE.to_value() not in enumeration.payload.get("candidates", []):
        raise RuntimeError("#16200 is absent from current v2 enumeration")
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    checkpoint = _load(root, _CHECKPOINT)
    if source.payload.get("candidate") != _CANDIDATE.to_value() or source.payload.get("complete") is not True:
        raise RuntimeError("#16200 source packet is not complete")
    if patch.payload.get("candidate") != _CANDIDATE.to_value() or patch.payload.get("outcome") != "patch_eligible":
        raise RuntimeError("#16200 patch packet is not eligible")
    if atomicity.payload.get("outcome") != "accepted" or atomicity.payload.get("primary_issue_number") != 15195:
        raise RuntimeError("#16200 source atomicity is not accepted")
    if manifest.payload.get("state") != "passed" or manifest.payload.get("details", {}).get("test_count") != 3:
        raise RuntimeError("#16200 test manifest is not closed")
    if checkpoint.payload.get("state") != "complete":
        raise RuntimeError("#16200 screening checkpoint is not complete")
    source_manifest_path = source.payload.get("raw_evidence_path")
    if not isinstance(source_manifest_path, str):
        raise RuntimeError("#16200 source manifest path is invalid")
    raw = json.loads((root / source_manifest_path).read_text(encoding="utf-8"))
    if raw.get("pull", {}).get("merged") is not True or raw.get("pull", {}).get("merge_commit_sha") != _MERGE_COMMIT:
        raise RuntimeError("#16200 merged PR authority changed")
    evidence = resolve_raw_patch_evidence(
        root, candidate=_CANDIDATE, source_manifest_path=source_manifest_path
    )
    _materialize_capsule(root)
    capsule = _write(
        root,
        _CAPSULE,
        (source.reference, patch.reference, manifest.reference),
        {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "source_tree_path": _CAPSULE_REL.as_posix(),
            "source_manifest_path": source_manifest_path,
            "test_paths": list(_TEST),
            "p2p_test_paths": list(_P2P),
            "support_paths": list(_SUPPORT),
        },
        authorization,
    )
    plan = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if not hasattr(plan, "profile"):
        raise RuntimeError(f"#16200 native plan blocked: {plan}")
    return {
        "source": source.reference.artifact_id,
        "patch": patch.reference.artifact_id,
        "atomicity": atomicity.reference.artifact_id,
        "manifest": manifest.reference.artifact_id,
        "checkpoint": checkpoint.reference.artifact_id,
        "capsule": capsule.reference.artifact_id,
        "raw_patch": evidence.raw_patch_path,
        "production_paths": list(plan.profile.production_paths),
        "support_paths": list(plan.profile.support_paths),
        "test_paths": list(plan.profile.test_paths),
        "p2p_test_paths": list(plan.profile.p2p_test_paths),
        "f2p_selector": plan.profile.f2p_selector,
        "p2p_selector": plan.profile.p2p_selector,
        "issue_numbers": [15195],
    }


def _ensure_launcher(root: Path, authorization: AuthorizationRecord) -> Path:
    namespace = root / "private/fresh-gate57-tools/luanti/16200/cpp17-v1"
    namespace.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = namespace / "cxx17-launcher"
    content = (
        b"#!/bin/sh\n"
        b"set -eu\n"
        b"compiler=\"$1\"\n"
        b"shift\n"
        b"exec \"$compiler\" \"$@\" -std=c++17\n"
    )
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or launcher.read_bytes() != content:
            raise RuntimeError("#16200 C++17 launcher drifted")
    else:
        launcher.write_bytes(content)
        launcher.chmod(0o700)
    _write(
        root,
        _TOOLCHAIN,
        (_CAPSULE,),
        {
            "candidate": _CANDIDATE.to_value(),
            "fresh_epoch": "cpp17-v1",
            "tool_kind": "candidate_cxx17_launcher",
            "wrapper_path": launcher.relative_to(root).as_posix(),
            "content_fingerprint": hashlib.sha256(content).hexdigest(),
            "language_standard": "c++17",
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return launcher


def execute(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> dict[str, object]:
    root = Path(artifact_root).absolute().resolve(strict=True)
    launcher = _ensure_launcher(root, authorization)

    def planner(root: Path, *, candidate: CandidateKey):
        plan = plan_luanti_native_regression_execution(root, candidate=candidate)
        if not hasattr(plan, "profile"):
            return plan
        return plan.__class__(
            profile=plan.profile,
            toolchain=plan.toolchain,
            fixed_capsule=plan.fixed_capsule,
            source_manifest_path=plan.source_manifest_path,
            raw_patch_path=plan.raw_patch_path,
            production_inverse=plan.production_inverse,
            configure_argv=plan.configure_argv
            + (
                "-DCMAKE_CXX_STANDARD=17",
                "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
                "-DCMAKE_CXX_EXTENSIONS=OFF",
                "-DCMAKE_CXX_FLAGS=-w",
                f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
            ),
            build_argv=plan.build_argv,
            runs=plan.runs,
            closed_environment=plan.closed_environment,
            execution_namespace="v1",
            dependency_references=(_TOOLCHAIN,),
        )

    result = execute_luanti_native_regression(
        artifact_root,
        candidate=_CANDIDATE,
        authorization=authorization,
        confirmation=EXECUTION_CONFIRMATION,
        planner=planner,
    )
    return {
        "aggregate": result.aggregate.reference.artifact_id,
        "executed_build_steps": result.executed_build_steps,
        "executed_attempts": result.executed_attempts,
        "resumed_attempts": result.resumed_attempts,
        "qualified": result.qualified,
        "cells": result.aggregate.payload["cells"],
    }


def _authorization(scope: str, authorization_id: str) -> AuthorizationRecord:
    return AuthorizationRecord(
        authorization_id=authorization_id,
        scope=scope,
        granted_at=_now(),
        local_user_id=os.getuid(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path(".warptest-cache/c1_lazarus_selection_v1"),
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    result: dict[str, object] = {
        "prepared": prepare(
            args.artifact_root,
            authorization=_authorization(
                "candidate-screening", "authorization/luanti-16200-screening-v1"
            ),
        )
    }
    if args.execute:
        result["execution"] = execute(
            args.artifact_root,
            authorization=_authorization(
                "preannotation-execution", "authorization/luanti-16200-gate57-v1"
            ),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
