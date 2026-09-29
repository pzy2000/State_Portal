"""Candidate-owned strict Gate 1--7 closure for JupyterLab #17700."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from .luanti_native_executor import NativeProcessReceipt, run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_JUPYTERLAB_17700_FRESH_GATE57_V1"
_VERSION = "1.0"
_CANDIDATE = {"repository_key": "jupyterlab", "change_number": 17700}
_FIXED_REVISION = "80fda5f0004b7ac79141619fb566f6d93c54d85e"
_BASE_REVISION = "7bca584707aee4ec29cba40a814bdd787f566219"
_RAW = PurePosixPath("raw/github/jupyterlab/17700/current-v1")
_PAIR = PurePosixPath("private/materialized/jupyterlab/17700/current-v1")
_MIRROR = PurePosixPath("private/repository-enumeration-git/jupyterlab/mirror.git")
_RUNTIME_HOME = PurePosixPath("private/runtime-home/jupyterlab-17700-gate57-v1")
_NODE = Path("/opt/homebrew/Cellar/node@22/22.22.0_1/bin/node")
_PRODUCTION = PurePosixPath("packages/statusbar/src/statusbar.ts")
_TEST = PurePosixPath("packages/statusbar/test/statusbar.spec.ts")
_PACKAGE = PurePosixPath("packages/statusbar")
_BUILD_OUTPUT = PurePosixPath("packages/statusbar/lib/statusbar.js")
_JEST = PurePosixPath("node_modules/jest/bin/jest.js")
_TSC = PurePosixPath("node_modules/typescript/bin/tsc")
_F2P = "should put higher rank right items closer to the middle"
_P2P = "should put higher rank left items closer to the middle"
_RAW_HASHES = {
    "files.json": "0187f773fb7cb2576aa0a5d82714042111212f6c76d42ea5af41c6ca3d685158",
    "issue-17680.json": "cb349e542033885b4554d953e931e2e282573995376f2503e3b532b87b850212",
    "issue-comments.json": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    "pr-comments.json": "95ca77781daa2cf81d88fb9ea70bb96848cabc8e99b3123bcad0f3c70fadab57",
    "pr-commits.json": "ecd52b83d6614e3c88abc57e1738b8b4f2e6c809b4e9e69b528cd9ea72df57f8",
    "pr-reviews.json": "98ee7aa430c63c5a93289740bf1dd96fcc4d2cc09f06ece2dd98cb8ae4ba58e1",
    "pull.diff": "48f951f6d1743784168bee37a6c4ab881b840178b238738bf445085ad9b1a6f6",
    "pull.json": "b48a0987e60326f8de7e97a6898ae4b23e98f51ce1a53c228132804ec4803f48",
}
_SOURCE_HASHES = {
    "fixed": "fb31934f4ddb0dd35154cf17c4bbb2de51ac70b0c69db563a219d69087681dbc",
    "buggy": "b93c630afbd85cb51e114c06a13f347144bca1589560c3102f1becc2e233b8c2",
}
_TEST_HASH = "b1a2216b82915869778b5b12e1744daf4b36e8f2eff20f778b60653ac4eabef1"

_SOURCE = ArtifactReference(
    "candidate_source_packet/jupyterlab/17700/source/current-v1",
    "candidate_source_packet",
    _VERSION,
)
_PATCH = ArtifactReference(
    "candidate_test_patch/jupyterlab/17700/screening/current-v1",
    "candidate_test_patch",
    _VERSION,
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/jupyterlab/17700/preannotation/current-v1",
    "source_atomicity",
    _VERSION,
)
_MANIFEST = ArtifactReference(
    "candidate_stage/jupyterlab/17700/test-manifest/current-v1",
    "candidate_stage",
    _VERSION,
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/jupyterlab/17700/fixed/current-v1",
    "fixed_source_capsule",
    _VERSION,
)
_PROFILE = ArtifactReference(
    "candidate_profile/jupyterlab/17700/gate1-4/current-v1",
    "candidate_profile",
    _VERSION,
)
_RUNTIME = ArtifactReference(
    "candidate_runtime/jupyterlab/17700/node22/current-v1",
    "candidate_runtime",
    _VERSION,
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/jupyterlab/17700/fixed-buggy/current-v1",
    "candidate_materialization",
    _VERSION,
)
_AGGREGATE = ArtifactReference(
    "candidate_gate57_aggregate/jupyterlab/17700/node22/current-v1",
    "candidate_gate57_aggregate",
    _VERSION,
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_transition/jupyterlab/17700/node22/current-v1",
    "candidate_gate57_transition",
    _VERSION,
)
_ADMISSION = ArtifactReference(
    "candidate_pool_admission/jupyterlab/17700/strict/current-v1",
    "candidate_pool_admission",
    _VERSION,
)
_TERMINAL = ArtifactReference(
    "candidate_sprint_terminal/jupyterlab/17700/gate57/current-v1",
    "candidate_sprint_terminal",
    _VERSION,
)


def execute_jupyterlab_17700_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Validate Gate 1--4, rebuild both variants, and run twelve fresh cells."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    _assert_fresh(root)
    chain = _register_static(root, authorization)
    builds = tuple(
        _run_build(root, variant, chain, authorization)
        for variant in ("fixed", "buggy")
    )
    attempts = tuple(
        _run_attempt(root, phase, variant, repetition, chain + builds, authorization)
        for phase in ("f2p", "p2p")
        for variant in ("fixed", "buggy")
        for repetition in (1, 2, 3)
    )
    aggregate = _register_aggregate(root, chain + builds, attempts, authorization)
    transition = _register_transition(
        root, chain + builds, attempts, aggregate, authorization
    )
    if aggregate.payload.get("qualified") is not True:
        terminal = _write(
            root,
            _TERMINAL,
            (aggregate.reference, transition.reference),
            {
                "candidate": _CANDIDATE,
                "state": "terminal",
                "complete": True,
                "reason": "fresh Gate 5-7 matrix did not match the strict expected outcomes",
                "gate8_13_performed": False,
                "paid_actions_authorized": False,
                "formal_trials_authorized": False,
            },
            authorization,
        )
        return aggregate, transition, terminal
    admission = _write(
        root,
        _ADMISSION,
        tuple(item.reference for item in chain + builds)
        + (aggregate.reference, transition.reference),
        {
            "candidate": _CANDIDATE,
            "state": "admitted",
            "complete": True,
            "outcome": "strict_gate1_7_pool_admission_passed",
            "defect_locus": "at_goal",
            "strict_gate_1_7": True,
            "no_timeout_unknown_or_inheritance": True,
            "fresh_attempt_count": 12,
            "catalog_admission_ready": False,
            "gate8_to_13": "pending_post_admission",
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return aggregate, transition, admission


def _register_static(
    root: Path, auth: AuthorizationRecord
) -> tuple[StableArtifact, ...]:
    raw = _directory(root / _RAW, root)
    for name, digest in _RAW_HASHES.items():
        if _sha(_file(raw / name, root)) != digest:
            raise RuntimeError(f"#17700 raw capture drifted: {name}")
    pull = _json(raw / "pull.json")
    issue = _json(raw / "issue-17680.json")
    files = _json(raw / "files.json")
    commits = _json(raw / "pr-commits.json")
    reviews = _json(raw / "pr-reviews.json")
    if not isinstance(pull, dict) or (
        pull.get("number") != 17700
        or pull.get("merged") is not True
        or pull.get("merge_commit_sha") != "31d45658d408fc4170ef91cce67c9c89645b1038"
        or pull.get("head", {}).get("sha") != _FIXED_REVISION
        or pull.get("base", {}).get("sha") != _BASE_REVISION
    ):
        raise RuntimeError("#17700 authoritative PR identity drifted")
    references = {
        int(value) for value in re.findall(r"#([0-9]+)", str(pull.get("body", "")))
    }
    if references != {17680} or not re.search(
        r"(?i)fixes\s+#17680\b", str(pull.get("body", ""))
    ):
        raise RuntimeError("#17700 does not map to exactly one authoritative issue")
    labels = (
        {item.get("name") for item in issue.get("labels", []) if isinstance(item, dict)}
        if isinstance(issue, dict)
        else set()
    )
    if not isinstance(issue, dict) or (
        issue.get("number") != 17680
        or issue.get("pull_request") is not None
        or issue.get("state") != "closed"
        or "bug" not in labels
        or "tab order" not in str(issue.get("title", "")).lower()
        or "expected behavior" not in str(issue.get("body", "")).lower()
    ):
        raise RuntimeError("#17680 is not the captured real bug")
    if not isinstance(files, list) or len(files) != 2:
        raise RuntimeError("#17700 changed-file boundary drifted")
    by_name = {item.get("filename"): item for item in files if isinstance(item, dict)}
    if set(by_name) != {_PRODUCTION.as_posix(), _TEST.as_posix()}:
        raise RuntimeError("#17700 production/test boundary drifted")
    production_patch = str(by_name[_PRODUCTION.as_posix()].get("patch", ""))
    test_patch = str(by_name[_TEST.as_posix()].get("patch", ""))
    if not all(
        token in production_patch
        for token in ("flexDirection = 'row'", "item.rank < rankItem.rank")
    ):
        raise RuntimeError("#17700 production patch semantics drifted")
    if (
        "item2.node.nextSibling" not in test_patch
        or not isinstance(commits, list)
        or len(commits) != 3
    ):
        raise RuntimeError("#17700 test/commit closure drifted")
    if not isinstance(reviews, list) or not any(
        item.get("state") == "APPROVED" for item in reviews if isinstance(item, dict)
    ):
        raise RuntimeError("#17700 review closure drifted")

    mirror = _directory(root / _MIRROR, root)
    fixed = _directory(root / _PAIR / "fixed", root)
    buggy = _directory(root / _PAIR / "buggy", root)
    entries = _git(mirror, "ls-tree", "-r", _FIXED_REVISION).splitlines()
    fixed_inventory = hashlib.sha256()
    buggy_inventory = hashlib.sha256()
    differences: list[str] = []
    for entry in entries:
        metadata, relative = entry.split("\t", 1)
        mode = metadata.split(" ", 1)[0]
        left = _tree_bytes(fixed / PurePosixPath(relative), mode, root)
        right = _tree_bytes(buggy / PurePosixPath(relative), mode, root)
        for value, content in ((fixed_inventory, left), (buggy_inventory, right)):
            value.update(
                relative.encode()
                + b"\0"
                + mode.encode()
                + b"\0"
                + hashlib.sha256(content).digest()
            )
        if left != right:
            differences.append(relative)
    if differences != [_PRODUCTION.as_posix()]:
        raise RuntimeError(f"#17700 inverse is not production-only: {differences!r}")
    for variant, expected in _SOURCE_HASHES.items():
        if _sha(_file(root / _PAIR / variant / _PRODUCTION, root)) != expected:
            raise RuntimeError(f"#17700 {variant} production hash drifted")
    fixed_test = _file(fixed / _TEST, root)
    buggy_test = _file(buggy / _TEST, root)
    if (
        _sha(fixed_test) != _TEST_HASH
        or fixed_test.read_bytes() != buggy_test.read_bytes()
    ):
        raise RuntimeError("#17700 fixed test was not preserved on the buggy API")
    test_text = fixed_test.read_text(encoding="utf-8")
    if _F2P not in test_text or _P2P not in test_text:
        raise RuntimeError("#17700 registered selectors are absent")

    source = _write(
        root,
        _SOURCE,
        (),
        {
            "candidate": _CANDIDATE,
            "state": "captured",
            "complete": True,
            "authority": "github:jupyterlab/jupyterlab",
            "authoritative_pr": 17700,
            "primary_bug_issue": 17680,
            "authoritative_bug_count": 1,
            "issue_labels": sorted(labels),
            "fixed_revision": _FIXED_REVISION,
            "base_revision": _BASE_REVISION,
            "raw_capture_root": _RAW.as_posix(),
            "raw_capture_hashes": dict(_RAW_HASHES),
            "source_complete": True,
            "incidental_reproduction_pr": 17591,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    patch = _write(
        root,
        _PATCH,
        (source.reference,),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "complete": True,
            "outcome": "patch_eligible",
            "production_paths": [_PRODUCTION.as_posix()],
            "test_paths": [_TEST.as_posix()],
            "production_hunks": 2,
            "production_hunks_same_defect": True,
            "production_only_inverse": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    atomicity = _write(
        root,
        _ATOMICITY,
        (source.reference, patch.reference),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "complete": True,
            "outcome": "accepted",
            "classification": "real_bug_accessibility",
            "primary_issue_number": 17680,
            "authoritative_issue_count": 1,
            "authoritative_issue_numbers": [17680],
            "incidental_pr_numbers": [17591],
            "production_only_inverse": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    manifest = _write(
        root,
        _MANIFEST,
        (source.reference, patch.reference, atomicity.reference),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "complete": True,
            "stage": "test_manifest",
            "runner": "jest_node22",
            "test_count": 2,
            "executable_test_paths": [_TEST.as_posix()],
            "f2p_selector": _F2P,
            "p2p_selector": _P2P,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    capsule = _write(
        root,
        _CAPSULE,
        (source.reference, patch.reference, atomicity.reference, manifest.reference),
        {
            "candidate": _CANDIDATE,
            "state": "materialized",
            "complete": True,
            "source_revision": _FIXED_REVISION,
            "source_tree_path": f"{_PAIR.as_posix()}/fixed",
            "tracked_file_count": len(entries),
            "tree_fingerprint": fixed_inventory.hexdigest(),
            "production_paths": [_PRODUCTION.as_posix()],
            "test_paths": [_TEST.as_posix()],
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    profile = _write(
        root,
        _PROFILE,
        (
            source.reference,
            patch.reference,
            atomicity.reference,
            manifest.reference,
            capsule.reference,
        ),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "complete": True,
            "profile_id": "jupyterlab-17700-node22-statusbar-order-v1",
            "defect_locus": "at_goal",
            "gate_status": {
                "source_authority": "passed",
                "single_bug_mapping": "passed",
                "strict_production_inverse": "passed",
                "fixed_test_buggy_api_compatibility": "passed",
            },
            "buggy_api_surface_unchanged": True,
            "fixed_only_identifiers": [],
            "existing_api_used": [
                "StatusBar",
                "registerStatusItem",
                "Widget.node.nextSibling",
            ],
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    node_version = _run((_NODE.as_posix(), "--version"), root, 15)
    if node_version.returncode != 0 or node_version.stdout.strip() != b"v22.22.0":
        raise RuntimeError("#17700 Node22 runtime drifted")
    runtime = _write(
        root,
        _RUNTIME,
        (profile.reference,),
        {
            "candidate": _CANDIDATE,
            "state": "ready",
            "complete": True,
            "runtime": "node",
            "runtime_path": _NODE.as_posix(),
            "runtime_version": "v22.22.0",
            "jest_version": "29.5.0",
            "typescript_version": "5.9.3",
            "dependency_snapshot": "candidate-local APFS clones from configured modern JupyterLab cache",
            "dependencies_per_variant": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    materialization = _write(
        root,
        _MATERIALIZATION,
        (capsule.reference, profile.reference, runtime.reference),
        {
            "candidate": _CANDIDATE,
            "state": "built",
            "complete": True,
            "fixed_tree_path": f"{_PAIR.as_posix()}/fixed",
            "buggy_tree_path": f"{_PAIR.as_posix()}/buggy",
            "only_different_tracked_paths": differences,
            "fixed_inventory_fingerprint": fixed_inventory.hexdigest(),
            "buggy_inventory_fingerprint": buggy_inventory.hexdigest(),
            "fixed_test_fingerprint": _TEST_HASH,
            "buggy_test_fingerprint": _TEST_HASH,
            "fixed_test_bytes_preserved": True,
            "node_modules_snapshot_per_variant": True,
            "candidate_execution_performed": False,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )
    return (
        source,
        patch,
        atomicity,
        manifest,
        capsule,
        profile,
        runtime,
        materialization,
    )


def _run_build(
    root: Path,
    variant: str,
    chain: tuple[StableArtifact, ...],
    auth: AuthorizationRecord,
) -> StableArtifact:
    tree = _directory(root / _PAIR / variant, root)
    package = _directory(tree / _PACKAGE, root)
    argv = (
        _NODE.as_posix(),
        str(tree / _TSC),
        "--build",
        "tsconfig.json",
        "tsconfig.test.json",
        "--force",
    )
    receipt = _run(argv, package, 300)
    output = (
        _file(tree / _BUILD_OUTPUT, root)
        if receipt.outcome == "completed" and receipt.returncode == 0
        else None
    )
    ref = ArtifactReference(
        f"candidate_build/jupyterlab/17700/{variant}/node22/current-v1",
        "candidate_build",
        _VERSION,
    )
    return _write(
        root,
        ref,
        tuple(item.reference for item in chain),
        {
            "candidate": _CANDIDATE,
            "variant": variant,
            "state": "passed" if output else "failed",
            "complete": output is not None,
            "process_outcome": receipt.outcome,
            "returncode": receipt.returncode,
            "argv": list(argv),
            "build_output": _BUILD_OUTPUT.as_posix(),
            "build_output_hash": _sha(output) if output else None,
            "stdout_base64": base64.b64encode(receipt.stdout).decode(),
            "stderr_base64": base64.b64encode(receipt.stderr).decode(),
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )


def _run_attempt(
    root: Path,
    phase: str,
    variant: str,
    repetition: int,
    chain: tuple[StableArtifact, ...],
    auth: AuthorizationRecord,
) -> StableArtifact:
    tree = _directory(root / _PAIR / variant, root)
    package = _directory(tree / _PACKAGE, root)
    selector = _F2P if phase == "f2p" else _P2P
    run_home = root / _RUNTIME_HOME / phase / variant / str(repetition)
    run_home.mkdir(parents=True, mode=0o700)
    argv = (
        _NODE.as_posix(),
        str(tree / _JEST),
        "--runTestsByPath",
        "test/statusbar.spec.ts",
        "-t",
        selector,
        "--runInBand",
        "--no-cache",
        "--cacheDirectory",
        str(run_home / "jest-cache"),
    )
    receipt = run_bounded_process(
        argv,
        cwd=package,
        env={
            **os.environ,
            "HOME": str(run_home),
            "LANG": "C",
            "LC_ALL": "C",
            "CI": "true",
        },
        timeout_seconds=300,
    )
    observed = (
        "pass"
        if receipt.outcome == "completed" and receipt.returncode == 0
        else "failure"
        if receipt.outcome == "completed"
        else receipt.outcome
    )
    expected = "failure" if phase == "f2p" and variant == "buggy" else "pass"
    ref = ArtifactReference(
        f"candidate_test_attempt/jupyterlab/17700/{phase}/{variant}/{repetition}/node22-current-v1",
        "candidate_test_attempt",
        _VERSION,
    )
    return _write(
        root,
        ref,
        tuple(item.reference for item in chain),
        {
            "candidate": _CANDIDATE,
            "fresh_epoch": "jupyterlab-17700-node22-current-v1",
            "phase": phase,
            "variant": variant,
            "repetition": repetition,
            "selector": selector,
            "expected_outcome": expected,
            "observed_outcome": observed,
            "matched_expected": observed == expected,
            "process_outcome": receipt.outcome,
            "returncode": receipt.returncode,
            "argv": list(argv),
            "stdout_base64": base64.b64encode(receipt.stdout).decode(),
            "stderr_base64": base64.b64encode(receipt.stderr).decode(),
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )


def _register_aggregate(
    root: Path,
    chain: tuple[StableArtifact, ...],
    attempts: tuple[StableArtifact, ...],
    auth: AuthorizationRecord,
) -> StableArtifact:
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault(
            (str(attempt.payload["phase"]), str(attempt.payload["variant"])), []
        ).append(attempt)
    cells = []
    for phase, variant in (
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    ):
        items = groups[(phase, variant)]
        cells.append(
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    item.payload["observed_outcome"] for item in items
                ],
                "matched_expected": [
                    item.payload["matched_expected"] for item in items
                ],
            }
        )
    qualified = len(attempts) == 12 and all(
        item.payload["matched_expected"] is True
        and item.payload["process_outcome"] == "completed"
        for item in attempts
    )
    return _write(
        root,
        _AGGREGATE,
        tuple(item.reference for item in chain + attempts),
        {
            "candidate": _CANDIDATE,
            "state": "qualified" if qualified else "failed",
            "outcome": "gate57_qualified" if qualified else "gate57_failed",
            "qualified": qualified,
            "complete": True,
            "attempt_count": len(attempts),
            "repetitions_per_cell": 3,
            "cells": cells,
            "no_timeout_or_unknown": qualified,
            "stable_repetitions": qualified,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )


def _register_transition(
    root: Path,
    chain: tuple[StableArtifact, ...],
    attempts: tuple[StableArtifact, ...],
    aggregate: StableArtifact,
    auth: AuthorizationRecord,
) -> StableArtifact:
    passed = aggregate.payload.get("qualified") is True
    return _write(
        root,
        _TRANSITION,
        tuple(item.reference for item in chain + attempts) + (aggregate.reference,),
        {
            "candidate": _CANDIDATE,
            "state": "passed" if passed else "terminal",
            "outcome": "qualified" if passed else "failed",
            "complete": True,
            "terminal": not passed,
            "resumable": False,
            "attempt_count": 12,
            "gate_status": {
                "source_authority": "passed",
                "single_bug_mapping": "passed",
                "strict_production_inverse": "passed",
                "test_api_compatibility": "passed",
                "fixed_f2p_3x": "passed" if passed else "failed",
                "buggy_f2p_3x_failure": "passed" if passed else "failed",
                "fixed_buggy_p2p_3x": "passed" if passed else "failed",
            },
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "gate8_13_performed": False,
        },
        auth,
    )


def _assert_fresh(root: Path) -> None:
    for ref in (_AGGREGATE, _TRANSITION, _ADMISSION, _TERMINAL):
        if (root / "artifacts" / ref.artifact_id / ref.semantic_version).exists():
            raise RuntimeError(
                f"#17700 fresh namespace already exists: {ref.artifact_id}"
            )
    attempt_root = root / "artifacts/candidate_test_attempt/jupyterlab/17700"
    if attempt_root.exists():
        raise RuntimeError("#17700 attempt namespace already exists")


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("inactive preannotation authorization")


def _write(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    auth: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = load_stable_artifact(
            root,
            artifact_id=ref.artifact_id,
            artifact_kind=ref.artifact_kind,
            semantic_version=ref.semantic_version,
        )
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"artifact conflict: {ref.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=auth,
    )
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _run(argv: tuple[str, ...], cwd: Path, timeout: int) -> NativeProcessReceipt:
    return run_bounded_process(
        argv,
        cwd=cwd,
        env={**os.environ, "LANG": "C", "LC_ALL": "C", "CI": "true"},
        timeout_seconds=timeout,
    )


def _directory(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError(f"unsafe directory: {path}")
    return resolved


def _file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError(f"unsafe file: {path}")
    return resolved


def _tree_bytes(path: Path, mode: str, root: Path) -> bytes:
    return (
        os.readlink(path).encode()
        if mode == "120000"
        else _file(path, root).read_bytes()
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(cwd), *args),
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:500])
    return result.stdout.decode().strip()


def _json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    value = hashlib.sha256()
    value.update(path.read_bytes())
    return value.hexdigest()


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = ["CONFIRMATION", "execute_jupyterlab_17700_gate57"]
