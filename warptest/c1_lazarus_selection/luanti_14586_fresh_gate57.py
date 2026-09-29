"""Current-v2 Gate 3/4 profile and no-inherit fresh Gate 5--7 epoch for #14586.

The repository already contains an older #14586 server-unit aggregate.  This
adapter never loads that aggregate, its attempts, or its build receipts.  It
binds the current source/patch/atomicity/manifest chain, records a
candidate-specific profile/preparation/capsule receipt, creates fresh fixed
and production-only-inverse trees, builds both variants with the retained
native toolchain, and executes exactly twelve new F2P/P2P cells.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .luanti_14586_executor import (
    _configure_argv,
    _environment,
    _tool,
    run_bounded_process,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_14586_FRESH_GATE57_V2"
_CANDIDATE = {"repository_key": "luanti", "change_number": 14586}
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/14586/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/14586/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/14586/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/14586/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/14586/current/v2", "screening_checkpoint", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_profile/luanti/14586/preflight/v2", "candidate_profile", "2.0"
)
_PREPARATION = ArtifactReference(
    "candidate_preparation/luanti/14586/preannotation/v2",
    "candidate_preparation",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/14586/fixed/v2", "fixed_source_capsule", "2.0"
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/14586/preannotation/v2",
    "candidate_downstream_plan",
    "2.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_gate57_14586_fresh_materialization/luanti/14586/native/v2",
    "candidate_gate57_14586_fresh_materialization",
    "2.0",
)
_BUILD_KIND = "candidate_gate57_14586_fresh_build"
_ATTEMPT_KIND = "candidate_gate57_14586_fresh_attempt"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_14586_fresh_aggregate/luanti/14586/native/v2",
    "candidate_gate57_14586_fresh_aggregate",
    "2.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_14586_fresh_transition/luanti/14586/native/v2",
    "candidate_gate57_14586_fresh_transition",
    "2.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_14586_fresh_blocker/luanti/14586/native/v2",
    "candidate_gate57_14586_fresh_blocker",
    "2.0",
)

_CAPSULE_TREE = Path("private/fixed-capsules/luanti/14586/fixed-source")
_RUN_ROOT = Path("private/fresh-gate57-runs/luanti/14586/current-v2")
_BUILD_ROOT = Path("private/fresh-gate57-builds/luanti/14586/current-v2")
_TOOLCHAIN = Path("private/dependencies/luanti-native/3.0")
_PRODUCTION_PATH = "src/inventory.cpp"
_TEST_PATH = "src/unittest/test_moveaction.cpp"
_MAX_OUTPUT = 262_144


@dataclass(frozen=True)
class FreshResult:
    aggregate: StableArtifact
    transition: StableArtifact
    attempt_count: int


def prepare_luanti_14586_current_v2(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    """Mechanically verify Gate 2--4 and register profile/preparation/capsule."""

    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    sections = _parse_patch(root, chain[1])
    _validate_gate34(chain, sections)
    capsule_path = root / _CAPSULE_TREE
    if not capsule_path.is_dir():
        raise RuntimeError("#14586 candidate fixed source capsule is missing")
    _require_file(capsule_path / _PRODUCTION_PATH)
    _require_file(capsule_path / _TEST_PATH)
    capsule_fingerprint, capsule_files = _tree_fingerprint(capsule_path)
    if not (root / _TOOLCHAIN / "runtime.json").is_file():
        raise RuntimeError("#14586 registered native toolchain runtime is missing")

    chain_refs = tuple(item.reference for item in chain)
    capsule_payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14586-partial-move-v2",
        "source_tree_path": _CAPSULE_TREE.as_posix(),
        "production_paths": [_PRODUCTION_PATH],
        "test_paths": [_TEST_PATH],
        "tree_fingerprint": capsule_fingerprint,
        "file_count": capsule_files,
        "source_capsule_origin": "candidate_specific_merged_source_materialization",
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    capsule = _write_or_reload(
        root, _CAPSULE, (chain[0].reference,), capsule_payload, authorization
    )
    profile_payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-14586-partial-move-v2",
        "current_v2_chain": [item.reference.artifact_id for item in chain],
        "fixed_source_capsule": capsule.reference.artifact_id,
        "strict_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
        },
        "production_paths": [_PRODUCTION_PATH],
        "test_paths": [_TEST_PATH],
        "fixed_source_tree_path": _CAPSULE_TREE.as_posix(),
        "binary_relative_path": "bin/minetestserver",
        "build_mode": {
            "BUILD_CLIENT": False,
            "BUILD_SERVER": True,
            "BUILD_UNITTESTS": True,
        },
        "f2p": {
            "selector": "TestMoveAction",
            "fixed_expected": "pass",
            "buggy_expected": "failure",
            "repetitions": 3,
        },
        "p2p": {
            "selector": "TestSettings",
            "fixed_expected": "pass",
            "buggy_expected": "pass",
            "repetitions": 3,
        },
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    # The current-v2 profile may have been registered by the freeze setup
    # before this fresh executor was added.  Reuse it only when its exact
    # candidate/chain and strict Gate 3--4 status agree; never overwrite a
    # receipt with a new payload or silently load an old attempt.
    profile = _load_if_present(root, _PROFILE)
    if profile is None:
        profile = _write_or_reload(
            root,
            _PROFILE,
            chain_refs,
            profile_payload,
            authorization,
        )
    elif (
        profile.inputs not in (chain_refs, chain_refs[:-1])
        or profile.payload.get("candidate") != _CANDIDATE
        or profile.payload.get("strict_gate_status", {}).get("source_authority")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("single_defect_mapping")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("strict_inverse_patch")
        != "passed"
        or profile.payload.get("strict_gate_status", {}).get("test_api_compatibility")
        != "passed"
    ):
        raise RuntimeError("#14586 existing profile receipt is incompatible")
    preparation_payload = {
        "candidate": _CANDIDATE,
        "profile_reference": profile.reference.artifact_id,
        "capsule_reference": capsule.reference.artifact_id,
        "toolchain_root": _TOOLCHAIN.as_posix(),
        "state": "prepared",
        "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
        "required_attempt_count": 12,
        "inherit_prior_attempts": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    preparation = _load_if_present(root, _PREPARATION)
    if preparation is None:
        preparation = _write_or_reload(
            root, _PREPARATION, (profile.reference,), preparation_payload, authorization
        )
    elif (
        preparation.inputs != (profile.reference,)
        or preparation.payload.get("candidate") != _CANDIDATE
        or preparation.payload.get("state") != "prepared"
        or preparation.payload.get("candidate_execution_performed") is not False
    ):
        raise RuntimeError("#14586 existing preparation receipt is incompatible")
    plan_payload = {
        "candidate": _CANDIDATE,
        "profile_reference": profile.reference.artifact_id,
        "preparation_reference": preparation.reference.artifact_id,
        "state": "planned",
        "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
        "cells": _cell_contract(),
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    plan = _write_or_reload(
        root,
        _PLAN,
        (profile.reference, preparation.reference),
        plan_payload,
        authorization,
    )
    return profile, preparation, capsule, plan


def execute_luanti_14586_fresh(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> FreshResult:
    """Run one new fixed/buggy build and exactly twelve new cells."""

    _require_auth(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    chain = _load_chain(root)
    sections = _parse_patch(root, chain[1])
    _validate_gate34(chain, sections)
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    plan = _load(root, _PLAN)
    if (
        profile.payload.get("strict_gate_status", {}).get("test_api_compatibility")
        != "passed"
    ):
        raise RuntimeError("#14586 Gate 4 profile is not passed")
    if (
        preparation.payload.get("old_attempts_loaded", False) is True
        or plan.payload.get("old_attempts_loaded", False) is True
    ):
        raise RuntimeError("#14586 fresh preparation is contaminated by old attempts")
    _assert_fresh_namespace(root)
    fixed = root / _RUN_ROOT / "fixed"
    buggy = root / _RUN_ROOT / "buggy"
    try:
        shutil.copytree(root / _CAPSULE_TREE, fixed, symlinks=True)
        shutil.copytree(root / _CAPSULE_TREE, buggy, symlinks=True)
        _apply_production_inverse(root, buggy, sections)
        _verify_inverse(fixed, buggy)
        materialization_payload = {
            "candidate": _CANDIDATE,
            "state": "materialized",
            "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
            "fixed_tree_path": fixed.relative_to(root).as_posix(),
            "buggy_tree_path": buggy.relative_to(root).as_posix(),
            "production_inverse_paths": [_PRODUCTION_PATH],
            "preserved_test_paths": [_TEST_PATH],
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
        }
        materialization = _write_or_reload(
            root,
            _MATERIALIZATION,
            (
                profile.reference,
                preparation.reference,
                capsule.reference,
                plan.reference,
            ),
            materialization_payload,
            authorization,
        )
        builds = {
            variant: _build_variant(
                root,
                tree,
                variant,
                (
                    profile.reference,
                    preparation.reference,
                    capsule.reference,
                    plan.reference,
                    materialization.reference,
                ),
                authorization,
            )
            for variant, tree in (("fixed", fixed), ("buggy", buggy))
        }
        attempts: list[StableArtifact] = []
        for phase, selector, buggy_expected in (
            ("f2p", "TestMoveAction", "failure"),
            ("p2p", "TestSettings", "pass"),
        ):
            for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected)):
                for repetition in (1, 2, 3):
                    attempts.append(
                        _run_cell(
                            root,
                            tree=fixed if variant == "fixed" else buggy,
                            build=builds[variant],
                            phase=phase,
                            variant=variant,
                            repetition=repetition,
                            selector=selector,
                            expected=expected,
                            inputs=(
                                profile.reference,
                                preparation.reference,
                                capsule.reference,
                                plan.reference,
                                materialization.reference,
                                builds[variant].reference,
                            ),
                            authorization=authorization,
                        )
                    )
    except BaseException as error:
        payload = {
            "candidate": _CANDIDATE,
            "state": "blocked",
            "outcome": "infrastructure_blocked",
            "terminal": False,
            "resumable": True,
            "reason": "fresh_candidate_specific_native_execution_failed",
            "error_type": type(error).__name__,
            "error": str(error),
            "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
            "attempt_count": 0,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
        }
        blocker = _write_or_reload(
            root,
            _BLOCKER,
            (
                profile.reference,
                preparation.reference,
                capsule.reference,
                plan.reference,
            ),
            payload,
            authorization,
        )
        return FreshResult(blocker, blocker, 0)
    aggregate = _aggregate(
        root,
        profile,
        preparation,
        capsule,
        plan,
        materialization,
        builds,
        attempts,
        authorization,
    )
    transition_payload = {
        "candidate": _CANDIDATE,
        "state": "passed" if aggregate.payload["qualified"] else "terminal",
        "outcome": aggregate.payload["outcome"],
        "terminal": not bool(aggregate.payload["qualified"]),
        "resumable": bool(aggregate.payload["qualified"]),
        "aggregate_reference": aggregate.reference.artifact_id,
        "attempt_count": len(attempts),
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    transition = _write_or_reload(
        root,
        _TRANSITION,
        (
            profile.reference,
            preparation.reference,
            capsule.reference,
            plan.reference,
            aggregate.reference,
        ),
        transition_payload,
        authorization,
    )
    return FreshResult(aggregate, transition, len(attempts))


def _load_chain(root: Path) -> tuple[StableArtifact, ...]:
    refs = (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT)
    chain = tuple(_load(root, ref) for ref in refs)
    if any(item.payload.get("candidate") != _CANDIDATE for item in chain):
        raise RuntimeError("#14586 current-v2 chain candidate binding drifted")
    if (
        chain[0].payload.get("complete") is not True
        or chain[1].payload.get("complete") is not True
        or chain[1].payload.get("outcome") != "patch_eligible"
        or chain[2].payload.get("complete") is not True
        or chain[2].payload.get("outcome") != "accepted"
        or chain[2].payload.get("primary_issue_number") != 14572
        or chain[3].payload.get("complete") is not True
        or chain[3].payload.get("state") != "passed"
        or chain[3].payload.get("details", {}).get("test_count") != 1
        or chain[4].payload.get("state") != "complete"
    ):
        raise RuntimeError("#14586 current-v2 Gate 1--2 chain is not passed")
    return chain


def _parse_patch(root: Path, patch: StableArtifact) -> dict[str, bytes]:
    raw = root / PurePosixPath(patch.payload["raw_patch_path"])
    text = raw.read_bytes()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    import shlex

    for line in text.decode("utf-8", errors="surrogateescape").splitlines(
        keepends=True
    ):
        if line.startswith("diff --git "):
            parts = shlex.split(line.rstrip("\r\n"))
            if len(parts) != 4 or parts[2][2:] != parts[3][2:]:
                raise RuntimeError("#14586 raw patch header is invalid")
            current = parts[2][2:]
            sections[current] = [line]
        elif current is not None:
            sections[current].append(line)
    if set(sections) != {_PRODUCTION_PATH, _TEST_PATH}:
        raise RuntimeError("#14586 raw patch inventory is not exact")
    return {
        path: "".join(lines).encode("utf-8", "surrogateescape")
        for path, lines in sections.items()
    }


def _validate_gate34(
    chain: tuple[StableArtifact, ...], sections: dict[str, bytes]
) -> None:
    patch = chain[1]
    if patch.payload.get("production_paths") != [_PRODUCTION_PATH] or patch.payload.get(
        "test_paths"
    ) != [_TEST_PATH]:
        raise RuntimeError("#14586 declared Gate 3 path inventory drifted")
    test = sections[_TEST_PATH].decode("utf-8", errors="surrogateescape")
    production = sections[_PRODUCTION_PATH].decode("utf-8", errors="surrogateescape")
    markers = (
        "testMoveSomewherePartial",
        'apply_action("MoveSomewhere 111 player:p1 main 1 player:p2 main",',
        "default:stone 110",
    )
    if any(marker not in test for marker in markers):
        raise RuntimeError("#14586 preserved test markers are missing")
    if (
        "changeItem(i, leftover)" not in production
        or "source.add(leftover.count)" not in production
    ):
        raise RuntimeError("#14586 expected production fix hunk is missing")


def _apply_production_inverse(
    root: Path, tree: Path, sections: dict[str, bytes]
) -> None:
    git = _tool(root, "git")
    result = subprocess.run(
        (str(git), "apply", "-R", "--whitespace=nowarn", "-"),
        cwd=tree,
        input=sections[_PRODUCTION_PATH],
        env={
            "PATH": str(git.parent),
            "GIT_DIR": "/dev/null",
            "LANG": "C",
            "LC_ALL": "C",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("#14586 production-only inverse failed")


def _verify_inverse(fixed: Path, buggy: Path) -> None:
    a, b = fixed / _TEST_PATH, buggy / _TEST_PATH
    if a.read_bytes() != b.read_bytes():
        raise RuntimeError("#14586 preserved test differs between fixed/buggy trees")
    if (fixed / _PRODUCTION_PATH).read_bytes() == (
        buggy / _PRODUCTION_PATH
    ).read_bytes():
        raise RuntimeError("#14586 production inverse made no change")


def _build_variant(
    root: Path,
    tree: Path,
    variant: str,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    build_dir = root / _BUILD_ROOT / variant
    build_dir.mkdir(parents=True, exist_ok=True)
    configure_argv = _configure_argv(root, tree, build_dir)
    configure = run_bounded_process(
        configure_argv, cwd=tree, env=_environment(root), timeout_seconds=600
    )
    payload = _process_payload(variant, "configure", configure_argv, configure)
    configure_ref = ArtifactReference(
        f"{_BUILD_KIND}/luanti/14586/{variant}/configure/v2", _BUILD_KIND, "2.0"
    )
    configure_artifact = _write_or_reload(
        root, configure_ref, inputs, payload, authorization
    )
    if configure.outcome != "completed" or configure.returncode != 0:
        raise RuntimeError(f"#14586 {variant} configure failed")
    build_argv = (
        str(_tool(root, "cmake")),
        "--build",
        str(build_dir),
        "--target",
        "minetestserver",
        "--parallel",
        "1",
    )
    built = run_bounded_process(
        build_argv, cwd=tree, env=_environment(root), timeout_seconds=1800
    )
    binary = tree / "bin/minetestserver"
    build_payload = _process_payload(variant, "build", build_argv, built)
    build_payload.update(
        {
            "binary_path": binary.relative_to(root).as_posix()
            if binary.exists()
            else None
        }
    )
    build_ref = ArtifactReference(
        f"{_BUILD_KIND}/luanti/14586/{variant}/build/v2", _BUILD_KIND, "2.0"
    )
    build_artifact = _write_or_reload(
        root, build_ref, (configure_artifact.reference,), build_payload, authorization
    )
    if built.outcome != "completed" or built.returncode != 0 or not binary.is_file():
        raise RuntimeError(f"#14586 {variant} build failed")
    return build_artifact


def _run_cell(
    root: Path,
    *,
    tree: Path,
    build: StableArtifact,
    phase: str,
    variant: str,
    repetition: int,
    selector: str,
    expected: str,
    inputs: tuple[ArtifactReference, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    binary = tree / "bin/minetestserver"
    argv = (str(binary), "--run-unittests", "--test-module", selector)
    receipt = run_bounded_process(
        argv, cwd=tree, env=_environment(root), timeout_seconds=300
    )
    observed = (
        "timed_out"
        if receipt.outcome == "timed_out"
        else "unknown"
        if receipt.outcome != "completed"
        else "pass"
        if receipt.returncode == 0
        else "failure"
    )
    ref = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14586/{phase}/{variant}/{repetition}/v2",
        _ATTEMPT_KIND,
        "2.0",
    )
    stdout, stderr = _bounded(receipt.stdout), _bounded(receipt.stderr)
    payload = {
        "candidate": _CANDIDATE,
        "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
        "phase": phase,
        "variant": variant,
        "repetition": repetition,
        "selector": selector,
        "expected_outcome": expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "argv": list(argv),
        "stdout_base64": base64.b64encode(stdout[0]).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr[0]).decode("ascii"),
        "stdout_truncated": stdout[1],
        "stderr_truncated": stderr[1],
        "matched_expected": observed == expected,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
    }
    return _write_or_reload(root, ref, inputs, payload, authorization)


def _aggregate(
    root: Path,
    profile: StableArtifact,
    preparation: StableArtifact,
    capsule: StableArtifact,
    plan: StableArtifact,
    materialization: StableArtifact,
    builds: dict[str, StableArtifact],
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    groups = {
        (phase, variant): [
            a
            for a in attempts
            if a.payload.get("phase") == phase and a.payload.get("variant") == variant
        ]
        for phase in ("f2p", "p2p")
        for variant in ("fixed", "buggy")
    }
    complete = len(attempts) == 12 and all(len(items) == 3 for items in groups.values())
    stable = complete and all(
        len({a.payload.get("observed_outcome") for a in items}) == 1
        for items in groups.values()
    )
    expected = complete and all(
        a.payload.get("matched_expected") is True for a in attempts
    )
    qualified = complete and stable and expected
    outcome = (
        "qualified"
        if qualified
        else "candidate_semantic_mismatch"
        if complete
        else "incomplete_matrix"
    )
    payload = {
        "candidate": _CANDIDATE,
        "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
        "state": "qualified" if qualified else "terminal",
        "outcome": outcome,
        "terminal": not qualified,
        "resumable": qualified,
        "qualified": qualified,
        "expected_attempt_count": 12,
        "attempt_count": len(attempts),
        "stable_repetitions": stable,
        "all_expected": expected,
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [a.payload.get("observed_outcome") for a in items],
                "matched_expected": [a.payload.get("matched_expected") for a in items],
            }
            for (phase, variant), items in groups.items()
        ],
        "candidate_execution_performed": bool(attempts),
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
    }
    refs = (
        profile.reference,
        preparation.reference,
        capsule.reference,
        plan.reference,
        materialization.reference,
    )
    refs += tuple(builds[name].reference for name in ("fixed", "buggy"))
    refs += tuple(a.reference for a in attempts)
    return _write_or_reload(root, _AGGREGATE, refs, payload, authorization)


def _process_payload(
    variant: str, step: str, argv: tuple[str, ...], receipt: Any
) -> dict[str, object]:
    stdout, stderr = _bounded(receipt.stdout), _bounded(receipt.stderr)
    return {
        "candidate": _CANDIDATE,
        "fresh_epoch": "candidate_gate57_14586_fresh_native_v2",
        "variant": variant,
        "step": step,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "argv": list(argv),
        "stdout_base64": base64.b64encode(stdout[0]).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr[0]).decode("ascii"),
        "stdout_truncated": stdout[1],
        "stderr_truncated": stderr[1],
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }


def _cell_contract() -> list[dict[str, object]]:
    result = []
    for phase, selector, buggy_expected in (
        ("f2p", "TestMoveAction", "failure"),
        ("p2p", "TestSettings", "pass"),
    ):
        for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected)):
            for repetition in (1, 2, 3):
                result.append(
                    {
                        "phase": phase,
                        "variant": variant,
                        "repetition": repetition,
                        "selector": selector,
                        "expected": expected,
                    }
                )
    return result


def _tree_fingerprint(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            count += 1
    return digest.hexdigest(), count


def _assert_fresh_namespace(root: Path) -> None:
    for path in (
        root / _RUN_ROOT,
        root / _BUILD_ROOT,
        root / "artifacts" / Path(_MATERIALIZATION.artifact_id),
        root / "artifacts" / Path(_AGGREGATE.artifact_id),
        root / "artifacts" / Path(_TRANSITION.artifact_id),
        root / "artifacts" / Path(_BLOCKER.artifact_id),
    ):
        if path.exists():
            raise RuntimeError(f"#14586 fresh namespace is not empty: {path}")


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#14586 fresh Gate 5--7 authorization is inactive")


def _require_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"#14586 required file is missing or unsafe: {path}")


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return (value, False) if len(value) <= _MAX_OUTPUT else (value[:_MAX_OUTPUT], True)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _load_if_present(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _write_or_reload(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, reference)
    except RuntimeError as error:
        if str(error) not in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(
                f"#14586 existing receipt drifted: {reference.artifact_id}"
            )
        return existing
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
    return _load(root, reference)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


__all__ = [
    "CONFIRMATION",
    "FreshResult",
    "execute_luanti_14586_fresh",
    "prepare_luanti_14586_current_v2",
]
