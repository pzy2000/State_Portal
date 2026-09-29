"""Fresh v3 native Gate 5--7 execution for Luanti #16200.

The v2 fixed build exposed a reproducible CMake/archive defect: the bundled
Lua target puts header files first in its source list and compiles them as C++;
Clang's default ``.h`` compilation emits precompiled-header data, so ``ar``
creates a symbol-only archive.  v3 keeps the fixed source capsule byte-for-byte
unchanged and uses a candidate-local C++ launcher that forces ``-x c++`` for
all CXX inputs, producing real Mach-O header objects.  The execution namespace
and dependency receipts are new, so no v1/v2 build or attempt can be reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .luanti_16200_gate57 import _CANDIDATE, _CAPSULE, prepare
from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    execute_luanti_native_regression,
)
from .luanti_native_regression import plan_luanti_native_regression_execution
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_DIAGNOSTIC = ArtifactReference(
    "candidate_gate57_link_diagnostic/luanti/16200/native/v1",
    "candidate_gate57_link_diagnostic",
    "1.0",
)
_TOOLCHAIN = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/16200/cpp17/v3",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_PROFILE_ID = "luanti-16200-rotation-conventions-v1-native-gate57"
_LAUNCHER_REL = Path(
    "private/fresh-gate57-tools/luanti/16200/cpp17-v3/cxx17-lua-header-launcher"
)


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
        raise RuntimeError(f"#16200 v3 typed reload mismatch: {reference.artifact_id}")
    return artifact


def _ensure_v3_toolchain(
    root: Path, *, authorization: AuthorizationRecord
) -> Path:
    diagnostic = _load(root, _DIAGNOSTIC)
    capsule = _load(root, _CAPSULE)
    if diagnostic.payload.get("outcome") != "source_preserving_lua_archive_repair_proved":
        raise RuntimeError("#16200 v3 diagnostic proof is missing")
    if capsule.payload.get("candidate") != _CANDIDATE.to_value():
        raise RuntimeError("#16200 v3 capsule is not candidate-bound")
    launcher = root / _LAUNCHER_REL
    content = (
        b"#!/bin/sh\n"
        b"set -eu\n"
        b"compiler=\"$1\"\n"
        b"shift\n"
        b"exec \"$compiler\" -x c++ \"$@\" -std=c++17\n"
    )
    launcher.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or launcher.read_bytes() != content:
            raise RuntimeError("#16200 v3 launcher drifted")
    else:
        launcher.write_bytes(content)
        launcher.chmod(0o700)
    _write(
        root,
        _TOOLCHAIN,
        (_CAPSULE, _DIAGNOSTIC),
        {
            "candidate": _CANDIDATE.to_value(),
            "fresh_epoch": "cpp17-v3",
            "tool_kind": "candidate_cxx17_lua_header_launcher",
            "wrapper_path": _LAUNCHER_REL.as_posix(),
            "content_fingerprint": hashlib.sha256(content).hexdigest(),
            "language_standard": "c++17",
            "forced_language": "c++",
            "source_preserving": True,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "diagnostic_reference": _DIAGNOSTIC.artifact_id,
        },
        authorization,
    )
    return launcher


def execute(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> dict[str, object]:
    root = Path(artifact_root).absolute().resolve(strict=True)
    launcher = _ensure_v3_toolchain(root, authorization=authorization)

    def planner(root: Path, *, candidate):
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
            execution_namespace="v3",
            dependency_references=(_TOOLCHAIN, _DIAGNOSTIC),
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
                "candidate-screening", "authorization/luanti-16200-screening-v3"
            ),
        )
    }
    if args.execute:
        result["execution"] = execute(
            args.artifact_root,
            authorization=_authorization(
                "preannotation-execution", "authorization/luanti-16200-gate57-v3"
            ),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
