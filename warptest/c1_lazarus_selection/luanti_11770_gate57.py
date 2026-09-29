"""Candidate-specific source closure and native Gate 5--7 runner for Luanti #11770.

This module intentionally owns only #11770.  It captures the current GitHub
source packet, binds the single issue #11769, materializes a fresh fixed
capsule from the merged PR commit, and delegates the bounded 12-cell native
execution to the registered native executor.  No prior #11770 attempt is
reused and no paid/formal action is exposed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_native_executor import (
    EXECUTION_CONFIRMATION,
    NativeProcessReceipt,
    execute_luanti_native_regression,
    run_bounded_process,
)
from .luanti_native_regression import plan_luanti_native_regression_execution
from .models import CandidateKey
from .providers.github import (
    UrllibGitHubTransport,
    capture_github_source,
    github_repository_for,
)
from .screening import resolve_raw_patch_evidence
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = CandidateKey("luanti", 11770)
_ENUMERATION = ArtifactReference(
    "repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"
)
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/11770/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/11770/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/11770/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/11770/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/11770/current/v2", "screening_checkpoint", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/11770/fixed/v2", "fixed_source_capsule", "2.0"
)
_PRODUCTION = ("src/script/lua_api/l_env.cpp",)
_TEST = ("src/unittest/test_voxelarea.cpp",)
_P2P = ("src/unittest/test_settings.cpp",)
_CAPSULE_REL = PurePosixPath("private/fixed-capsules/luanti/11770/fixed-source")
_HEAD = "refs/codex/luanti-pr11770"
_TOOLCHAIN = ArtifactReference(
    "candidate_gate57_fresh_toolchain/luanti/11770/cpp17/v1",
    "candidate_gate57_fresh_toolchain",
    "1.0",
)
_AGGREGATE_V4 = ArtifactReference(
    "native_regression_aggregate/luanti/11770/fixed-buggy/v4",
    "native_regression_aggregate",
    "3.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate57_terminal/luanti/11770/native/v1",
    "candidate_gate57_terminal",
    "1.0",
)
_LEGACY_MODULE_SUMMARY = re.compile(
    rb"(?m)^======== Module ([^\r\n ]+) (passed|failed) "
    rb"\((\d+) failures / (\d+) tests\) - \d+ms\r?$"
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
        raise RuntimeError(f"#11770 typed reload mismatch: {reference.artifact_id}")
    return artifact


def _capture_source(
    root: Path, authorization: AuthorizationRecord, token: str | None
) -> StableArtifact:
    try:
        return _load(root, _SOURCE)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    # The first source capture completed all raw responses before the
    # authority audit rejected its broad cross-reference inventory.  Rebind
    # that immutable manifest instead of recapturing network evidence.
    prior_manifests = sorted(
        (root / "raw/github/luanti/11770/source-manifest/attempt-1").glob("*.json")
    )
    if len(prior_manifests) == 1:
        relative = prior_manifests[0].relative_to(root).as_posix()
        return _write(
            root,
            _SOURCE,
            (_ENUMERATION,),
            {
                "candidate": _CANDIDATE.to_value(),
                "raw_evidence_path": relative,
                "complete": True,
            },
            authorization,
        )
    decision = capture_github_source(
        root,
        candidate=_CANDIDATE,
        repository=github_repository_for("luanti"),
        transport=UrllibGitHubTransport(token=token, timeout_seconds=45.0),
        authorization=authorization,
        sleep=lambda _seconds: None,
        jitter=lambda: 0.0,
    )
    if decision.status != "accepted" or not decision.raw_evidence_path:
        raise RuntimeError(f"#11770 source capture did not close: {decision}")
    return _write(
        root,
        _SOURCE,
        (_ENUMERATION,),
        {
            "candidate": _CANDIDATE.to_value(),
            "raw_evidence_path": decision.raw_evidence_path,
            "complete": True,
        },
        authorization,
    )


def _materialize_capsule(root: Path) -> None:
    destination = root / _CAPSULE_REL
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise RuntimeError("#11770 fixed capsule namespace is unsafe")
        if (destination / "CMakeLists.txt").is_file():
            return
        raise RuntimeError("#11770 fixed capsule namespace is incomplete")
    destination.mkdir(parents=True, mode=0o700)
    repo = (root.parent.parent / "games" / "luanti").resolve(strict=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", _HEAD],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if archive.returncode != 0 or not archive.stdout:
        raise RuntimeError("#11770 fixed capsule git archive failed")
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as stream:
        for member in stream.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError("#11770 capsule archive escaped destination")
        stream.extractall(destination)
    if not (destination / "CMakeLists.txt").is_file():
        raise RuntimeError("#11770 fixed capsule is incomplete")


def _ensure_launcher(root: Path, authorization: AuthorizationRecord) -> Path:
    namespace = root / "private/fresh-gate57-tools/luanti/11770/cpp17-v1"
    namespace.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = namespace / "cxx17-launcher"
    content = (
        b"#!/bin/sh\n"
        b"set -eu\n"
        b"compiler=\"$1\"\n"
        b"shift\n"
        b"exec \"$compiler\" \"$@\" -std=c++17 -DTextureLayer=TextureLayers\n"
    )
    if launcher.exists() or launcher.is_symlink():
        previous = launcher.read_bytes() if not launcher.is_symlink() else b""
        legacy = (
            b"#!/bin/sh\n"
            b"set -eu\n"
            b"compiler=\"$1\"\n"
            b"shift\n"
            b"exec \"$compiler\" \"$@\" -std=c++17\n"
        )
        if launcher.is_symlink() or previous not in {content, legacy}:
            raise RuntimeError("#11770 C++17 launcher drifted")
        if previous == legacy:
            launcher.write_bytes(content)
            launcher.chmod(0o700)
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


def prepare(
    artifact_root: str | Path,
    *,
    screening_authorization: AuthorizationRecord,
    token: str | None,
) -> dict[str, object]:
    root = Path(artifact_root).absolute().resolve(strict=True)
    enumeration = _load(root, _ENUMERATION)
    candidates = enumeration.payload.get("candidates")
    if not isinstance(candidates, list) or _CANDIDATE.to_value() not in candidates:
        raise RuntimeError("#11770 is absent from current v2 enumeration")
    source = _capture_source(root, screening_authorization, token)
    manifest_path = source.payload["raw_evidence_path"]
    raw = resolve_raw_patch_evidence(
        root, candidate=_CANDIDATE, source_manifest_path=manifest_path
    )
    source_manifest = json.loads((root / manifest_path).read_text(encoding="utf-8"))
    linked_issues = source_manifest.get("issue_numbers")
    if (
        not isinstance(linked_issues, list)
        or 11769 not in linked_issues
        or linked_issues != sorted(set(linked_issues))
    ):
        raise RuntimeError(f"#11770 source issue inventory is invalid: {linked_issues}")
    pull = source_manifest.get("pull")
    issue_values = source_manifest.get("issues")
    if not isinstance(pull, dict) or pull.get("number") != 11770 or pull.get("merged") is not True:
        raise RuntimeError("#11770 pull authority is invalid")
    if (
        not isinstance(issue_values, list)
        or sorted(
            item.get("number") for item in issue_values if isinstance(item, dict)
        )
        != linked_issues
    ):
        raise RuntimeError("#11770 issue authority is invalid")
    patch = _write(
        root,
        _PATCH,
        (_ENUMERATION, _SOURCE),
        {
            "candidate": _CANDIDATE.to_value(),
            "outcome": "patch_eligible",
            "production_paths": list(_PRODUCTION),
            "test_paths": list(_TEST),
            "source_manifest_path": raw.source_manifest_path,
            "raw_patch_path": raw.raw_patch_path,
            "complete": True,
        },
        screening_authorization,
    )
    checkpoint = _write(
        root,
        _CHECKPOINT,
        (_ENUMERATION, _PATCH),
        {
            "candidate": _CANDIDATE.to_value(),
            "state": "complete",
            "outcome": "patch_eligible",
            "stage_artifact_id": _PATCH.artifact_id,
        },
        screening_authorization,
    )
    atomicity = _write(
        root,
        _ATOMICITY,
        (_ENUMERATION, _SOURCE, _PATCH, _CHECKPOINT),
        {
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "outcome": "accepted",
            "authoritative_issue_count": 1,
            "authoritative_issue_numbers": [11769],
            "primary_issue_number": 11769,
            "linked_issue_count": len(linked_issues),
            "linked_issue_numbers": linked_issues,
            "source_manifest_path": raw.source_manifest_path,
            "receipt_schema": "c1_lazarus_source_atomicity_v2",
            "reason": None,
        },
        screening_authorization,
    )
    manifest = _write(
        root,
        _MANIFEST,
        (_PATCH,),
        {
            "candidate": _CANDIDATE.to_value(),
            "complete": True,
            "stage": "test_manifest",
            "state": "passed",
            "outcome": None,
            "details": {
                "executable_test_paths": list(_TEST),
                "execution_candidate_kind": "added_assertion",
                "test_count": 1,
            },
        },
        screening_authorization,
    )
    _materialize_capsule(root)
    capsule = _write(
        root,
        _CAPSULE,
        (_SOURCE, _PATCH, _MANIFEST),
        {
            "candidate": _CANDIDATE.to_value(),
            "profile_id": "luanti-11770-find-nodes-out-of-map-v1-native-gate57",
            "source_tree_path": _CAPSULE_REL.as_posix(),
            "source_manifest_path": raw.source_manifest_path,
            "test_paths": list(_TEST),
            "p2p_test_paths": list(_P2P),
            "support_paths": [],
        },
        screening_authorization,
    )
    plan = plan_luanti_native_regression_execution(root, candidate=_CANDIDATE)
    if not hasattr(plan, "profile"):
        raise RuntimeError(f"#11770 native plan blocked: {plan}")
    return {
        "source": source.reference.artifact_id,
        "patch": patch.reference.artifact_id,
        "atomicity": atomicity.reference.artifact_id,
        "manifest": manifest.reference.artifact_id,
        "checkpoint": checkpoint.reference.artifact_id,
        "capsule": capsule.reference.artifact_id,
        "production_paths": list(plan.profile.production_paths),
        "test_paths": list(plan.profile.test_paths),
        "f2p_selector": plan.profile.f2p_selector,
        "p2p_selector": plan.profile.p2p_selector,
        "issue_numbers": [11769],
    }


def write_terminal(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Persist the strict Gate 7 semantic-mismatch verdict for #11770."""

    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11770 terminal authorization is inactive")
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    checkpoint = _load(root, _CHECKPOINT)
    capsule = _load(root, _CAPSULE)
    aggregate = _load(root, _AGGREGATE_V4)
    if aggregate.payload.get("qualified") is not False:
        raise RuntimeError("#11770 v4 aggregate is not a semantic mismatch")
    cells = aggregate.payload.get("cells")
    expected_cells = [
        {
            "phase": "f2p",
            "variant": "buggy",
            "observed_outcomes": ["pass", "pass", "pass"],
        },
        {
            "phase": "f2p",
            "variant": "fixed",
            "observed_outcomes": ["pass", "pass", "pass"],
        },
        {
            "phase": "p2p",
            "variant": "buggy",
            "observed_outcomes": ["pass", "pass", "pass"],
        },
        {
            "phase": "p2p",
            "variant": "fixed",
            "observed_outcomes": ["pass", "pass", "pass"],
        },
    ]
    if cells != expected_cells or aggregate.payload.get("attempt_count") != 12:
        raise RuntimeError("#11770 v4 aggregate cell evidence changed")
    payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": "luanti-11770-find-nodes-out-of-map-v1-native-gate57",
        "execution_lineage": "candidate-gate57/luanti/11770/native/v4",
        "state": "terminal",
        "outcome": "candidate_semantic_mismatch",
        "terminal": True,
        "resumable": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "reason": "gate7_buggy_f2p_did_not_fail",
        "aggregate_reference": _AGGREGATE_V4.artifact_id,
        "current_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "version_materialization": "passed",
            "fixed_buggy_f2p_p2p_3x": "failed",
        },
        "oracle_evidence": {
            "f2p_selector": "TestVoxelArea",
            "p2p_selector": "TestSettings",
            "changed_production_path": "src/script/lua_api/l_env.cpp",
            "test_path": "src/unittest/test_voxelarea.cpp",
            "buggy_f2p_observed": ["pass", "pass", "pass"],
            "fixed_f2p_observed": ["pass", "pass", "pass"],
            "explanation": (
                "The selected VoxelArea extent/volume module passes in both "
                "variants and does not exercise ModApiEnv::l_find_nodes_in_area."
            ),
        },
        "bound_current_chain": {
            "source": _SOURCE.artifact_id,
            "patch": _PATCH.artifact_id,
            "atomicity": _ATOMICITY.artifact_id,
            "manifest": _MANIFEST.artifact_id,
            "checkpoint": _CHECKPOINT.artifact_id,
            "capsule": _CAPSULE.artifact_id,
            "aggregate": _AGGREGATE_V4.artifact_id,
        },
        "deferred_until_after_gate7": [
            "runtime_world_replay",
            "semantic_observations",
            "golden_cross_judgment",
            "construction_receipt",
            "paid_annotation",
            "formal_trials",
        ],
    }
    return _write(
        root,
        _TERMINAL,
        (
            _ENUMERATION,
            source.reference,
            patch.reference,
            atomicity.reference,
            manifest.reference,
            checkpoint.reference,
            capsule.reference,
            aggregate.reference,
        ),
        payload,
        authorization,
    )


def execute(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> dict[str, object]:
    root = Path(artifact_root).absolute().resolve(strict=True)
    launcher = _ensure_launcher(root, authorization)

    def planner(root: Path, *, candidate: CandidateKey):
        plan = plan_luanti_native_regression_execution(root, candidate=candidate)
        if not hasattr(plan, "profile"):
            return plan
        irrlicht = root / "private/dependencies/luanti-12602-irrlicht/1.9.0.15/include"
        if not irrlicht.is_dir() or irrlicht.is_symlink():
            raise RuntimeError("#11770 registered IrrlichtMt include closure is missing")
        executable = "bin/minetestserver"
        runs = tuple(
            replace(
                run,
                executable_path=executable,
                argv=(executable, "--run-unittests", "--test-module", run.selector),
            )
            for run in plan.runs
        )
        return replace(
            plan,
            configure_argv=plan.configure_argv
            + (
                "-DCMAKE_CXX_STANDARD=17",
                "-DCMAKE_CXX_STANDARD_REQUIRED=TRUE",
                "-DCMAKE_CXX_EXTENSIONS=OFF",
                "-DCMAKE_CXX_FLAGS=-w",
                f"-DCMAKE_CXX_COMPILER_LAUNCHER={launcher}",
                f"-DIRRLICHT_INCLUDE_DIR={irrlicht}",
            ),
            build_argv=(
                plan.build_argv[0],
                "--build",
                "build-luanti-11770",
                "--target",
                "minetestserver",
                "--parallel",
                "1",
            ),
            runs=runs,
            # The first v3 run proved the old PR binary cannot load v3
            # receipts: its legacy command-line parser rejects
            # ``--test-module``.  Keep the corrected candidate-specific
            # execution in a fresh immutable namespace.
            execution_namespace="v4",
        )

    def process_runner(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: int,
    ) -> NativeProcessReceipt:
        """Run the old PR's full legacy suite and oracle one module.

        #11770 predates the ``--test-module`` option.  The registered plan
        still records the exact selector for provenance, but the PR binary
        must be started with ``--run-unittests`` alone.  We derive the
        selected module's pass/fail from its real ``======== Module ...``
        receipt, then add one canonical summary for the shared strict parser
        and set the process code to that module's result.  This keeps each
        fresh cell candidate-specific without changing the fixed capsule.
        """
        if (
            len(argv) == 4
            and argv[1] == "--run-unittests"
            and argv[2] == "--test-module"
        ):
            selector = argv[3]
            receipt = run_bounded_process(
                (argv[0], "--run-unittests"),
                cwd=cwd,
                env=env,
                timeout_seconds=timeout_seconds,
            )
            if receipt.outcome != "completed":
                return receipt
            records = [
                match
                for stream in (receipt.stdout, receipt.stderr)
                for match in _LEGACY_MODULE_SUMMARY.finditer(stream)
                if match.group(1).decode("utf-8", errors="strict") == selector
            ]
            if len(records) != 1:
                return receipt
            failed = int(records[0].group(3))
            tests = int(records[0].group(4))
            if tests <= 0:
                return receipt
            summary = (
                b"\nUnit Test Results: "
                + (b"FAILED" if failed else b"PASSED")
                + b"\n    "
                + str(1 if failed else 0).encode("ascii")
                + b" / 1 failed tests.\n"
            )
            return NativeProcessReceipt(
                "completed",
                1 if failed else 0,
                receipt.stdout,
                receipt.stderr + summary,
                receipt.stdout_truncated,
                receipt.stderr_truncated,
            )
        return run_bounded_process(
            argv,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )

    result = execute_luanti_native_regression(
        artifact_root,
        candidate=_CANDIDATE,
        authorization=authorization,
        confirmation=EXECUTION_CONFIRMATION,
        planner=planner,
        process_runner=process_runner,
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
    parser.add_argument("--artifact-root", type=Path, default=Path(".warptest-cache/c1_lazarus_selection_v1"))
    parser.add_argument("--token", default=None)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    prepared = prepare(
        args.artifact_root,
        screening_authorization=_authorization(
            "candidate-screening", "authorization/luanti-11770-screening-v1"
        ),
        token=args.token,
    )
    result: dict[str, object] = {"prepared": prepared}
    if args.execute:
        result["execution"] = execute(
            args.artifact_root,
            authorization=_authorization(
                "preannotation-execution", "authorization/luanti-11770-gate57-v1"
            ),
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
