"""Candidate-specific Luanti #9572 profile, capsule, and Gate 5--7 runner.

The source/patch chain is reloaded independently of the existing #9722
execution.  Only the two vector tests selected below are executed; no replay,
oracle, construction, annotation, or formal-trial work is performed here.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .luanti_lua_regression import (
    LuaRegressionExecutionPlan,
    WatchdogReceipt,
    plan_luanti_lua_regression_execution,
)
from .models import CandidateKey
from .pipeline import StageDecision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_LUA_REGRESSION"
_CANDIDATE = CandidateKey("luanti", 9572)
_PROFILE_ID = "luanti-9572-rotation-v1"
_PRODUCTION = ("builtin/common/vector.lua",)
_TEST = "builtin/common/tests/vector_spec.lua"
_F2P = "vector rotate()"
_P2P = "vector add()"
_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/9572/source/v2", "candidate_source_packet", "2.0"
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/9572/screening/v2", "candidate_test_patch", "2.0"
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/9572/preannotation/v2", "source_atomicity", "2.0"
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/9572/test_manifest/v2", "candidate_stage", "2.0"
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/9572/current/v2", "screening_checkpoint", "2.0"
)
_PROFILE = ArtifactReference(
    "candidate_profile/luanti/9572/preflight/v2", "candidate_profile", "2.0"
)
_PREPARATION = ArtifactReference(
    "candidate_preparation/luanti/9572/preannotation/v2", "candidate_preparation", "2.0"
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/9572/fixed/v2", "fixed_source_capsule", "2.0"
)
_ATTEMPT_KIND = "candidate_gate57_9572_attempt"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE = ArtifactReference(
    "candidate_gate57_9572_aggregate/luanti/9572/lua/v1",
    "candidate_gate57_9572_aggregate",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate57_9572_transition/luanti/9572/lua/v1",
    "candidate_gate57_9572_transition",
    "1.0",
)
_BLOCKER = ArtifactReference(
    "candidate_gate57_9572_blocker/luanti/9572/lua/v1",
    "candidate_gate57_9572_blocker",
    "1.0",
)
_TERMINAL = ArtifactReference(
    "candidate_gate57_9572_terminal/luanti/9572/lua/v1",
    "candidate_gate57_9572_terminal",
    "1.0",
)
_MAX_STREAM = 262_144
_SUMMARY = re.compile(
    rb"(?m)^(?:\x1b\[[0-9;]*m)?(\d+) success(?:es)? / "
    rb"(\d+) failure(?:s)? / (\d+) error(?:s)? / (\d+) pending\b"
)


@dataclass(frozen=True)
class Gate57Result:
    aggregate: StableArtifact
    followup: StableArtifact
    attempts: int
    qualified: bool


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int
    selector: str
    expected: str

    @property
    def reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate57_9572_attempt/luanti/9572/lua/"
            f"{self.phase}/{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            _ATTEMPT_VERSION,
        )


def prepare_luanti_9572_profile(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Reload gates 1--4 and persist profile, preparation, and fixed capsule."""

    if authorization.scope not in {"candidate-screening", "preannotation-execution"}:
        raise PermissionError("#9572 profile authorization scope is invalid")
    _check_user_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    chain, source_manifest, patch_path = _validate_chain(root)
    profile_payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "authority": "github:luanti-org/luanti",
        "primary_issue_number": 9798,
        "linked_issue_numbers": [9798, 10650],
        "source_issue_policy": "one authoritative issue; linked cross-reference retained",
        "pull_title": source_manifest["pull"].get("title"),
        "production_paths": list(_PRODUCTION),
        "test_paths": [_TEST],
        "test_markers": [
            'describe("rotate()"',
            'it("rotates"',
            'it("is counterclockwise"',
            'it("add()"',
        ],
        "f2p_selector": _F2P,
        "p2p_selector": _P2P,
        "strict_gate_status": {
            "source_authority": "passed",
            "single_defect_mapping": "passed",
            "strict_inverse_patch": "passed",
            "test_api_compatibility": "passed",
            "fixed_f2p_3x": "downstream_pending",
            "buggy_f2p_3x": "downstream_pending",
            "fixed_buggy_p2p_3x": "downstream_pending",
            "version_materialization": "downstream_pending",
            "replay_oracle": "downstream_pending",
            "construction_golden": "downstream_pending",
        },
        "downstream_status": "downstream_pending",
        "paid_actions_authorized": False,
        "candidate_execution_performed": False,
    }
    created = _created_after(*(a.created_at for a in (*chain,)))
    _assert_stable_absent(root, _PROFILE)
    write_stable_artifact(
        root,
        artifact_id=_PROFILE.artifact_id,
        artifact_kind=_PROFILE.artifact_kind,
        semantic_version=_PROFILE.semantic_version,
        created_at=created,
        inputs=(_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT),
        payload=profile_payload,
        authorization=authorization,
    )
    profile = _load(root, _PROFILE)
    if profile.payload != profile_payload:
        raise RuntimeError("#9572 profile typed reload failed")

    prep_payload = {
        "candidate": _CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "state": "prepared",
        "outcome": "downstream_pending",
        "profile_reference": _PROFILE.artifact_id,
        "production_inverse_paths": list(_PRODUCTION),
        "preserved_test_paths": [_TEST],
        "f2p": {"selector": _F2P, "repetitions": 3, "fixed_expected": "pass", "buggy_expected": "failure"},
        "p2p": {"selector": _P2P, "repetitions": 3, "fixed_expected": "pass", "buggy_expected": "pass"},
        "strict_gate_status": profile_payload["strict_gate_status"],
        "downstream_status": "downstream_pending",
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    _assert_stable_absent(root, _PREPARATION)
    write_stable_artifact(
        root,
        artifact_id=_PREPARATION.artifact_id,
        artifact_kind=_PREPARATION.artifact_kind,
        semantic_version=_PREPARATION.semantic_version,
        created_at=_created_after(profile.created_at),
        inputs=(profile.reference,),
        payload=prep_payload,
        authorization=authorization,
    )
    preparation = _load(root, _PREPARATION)
    if preparation.payload != prep_payload:
        raise RuntimeError("#9572 preparation typed reload failed")
    capsule = _materialize_capsule(root, source_manifest, profile, authorization)
    # Keep the raw paths only in the local validation scope; no raw digest or
    # source-object identity is projected into the stable artifact.
    del patch_path
    return profile, preparation, capsule


def execute_luanti_9572_gate57(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> Gate57Result:
    """Execute fresh fixed/buggy F2P/P2P repetitions for #9572."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _check_user_authorization(authorization)
    if authorization.scope != "preannotation-execution":
        raise PermissionError("#9572 Gate 5--7 requires preannotation authorization")
    root = _ordinary_root(Path(artifact_root).absolute())
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    if profile.payload.get("candidate") != _CANDIDATE.to_value() or preparation.payload.get("state") != "prepared":
        raise RuntimeError("#9572 profile/preparation is not executable")
    if capsule.payload.get("candidate") != _CANDIDATE.to_value():
        raise RuntimeError("#9572 fixed capsule identity is invalid")
    runtime_plan = plan_luanti_lua_regression_execution(root, candidate=CandidateKey("luanti", 9722))
    if isinstance(runtime_plan, StageDecision):
        raise RuntimeError("registered local Busted runtime is unavailable")
    _validate_runtime_plan(runtime_plan)
    workspace = _new_workspace(root)
    attempts: list[StableArtifact] = []
    try:
        fixed, buggy = workspace / "fixed", workspace / "buggy"
        _copy_capsule(root, fixed)
        _copy_capsule(root, buggy)
        inverse = _production_patch(root)
        _apply_inverse(buggy, inverse)
        for cell in _cells():
            attempts.append(_execute_cell(root, runtime_plan, cell, fixed, buggy, authorization))
        aggregate = _write_aggregate(root, attempts, authorization)
        if aggregate.payload["qualified"] is True:
            followup = _write_followup(
                root,
                _TRANSITION,
                (profile.reference, preparation.reference, capsule.reference, aggregate.reference),
                {
                    "candidate": _CANDIDATE.to_value(),
                    "profile_id": _PROFILE_ID,
                    "execution_lineage": "candidate-gate57/luanti/9572/lua/v1",
                    "state": "passed",
                    "outcome": "qualified",
                    "terminal": False,
                    "resumable": True,
                    "candidate_execution_performed": True,
                    "paid_actions_authorized": False,
                    "attempt_count": 12,
                    "gate_status": {"source_authority": "passed", "single_defect_mapping": "passed", "strict_inverse_patch": "passed", "test_api_compatibility": "passed", "version_materialization": "passed", "fixed_buggy_f2p_p2p_3x": "passed"},
                    "deferred_until_after_gate5_7": ["runtime_world_replay", "semantic_observations", "golden_cross_judgment", "construction_receipt"],
                },
                authorization,
            )
        elif _infrastructure_only(attempts):
            followup = _write_followup(
                root,
                _BLOCKER,
                (profile.reference, preparation.reference, capsule.reference, aggregate.reference),
                {
                    "candidate": _CANDIDATE.to_value(),
                    "profile_id": _PROFILE_ID,
                    "state": "blocked",
                    "outcome": "infrastructure_blocked",
                    "terminal": False,
                    "resumable": True,
                    "candidate_execution_performed": True,
                    "paid_actions_authorized": False,
                    "reason": "one_or_more_gate57_cells_need_infrastructure_followup",
                    "aggregate_reference": aggregate.reference.artifact_id,
                },
                authorization,
            )
        else:
            followup = _write_followup(
                root,
                _TERMINAL,
                (profile.reference, preparation.reference, capsule.reference, aggregate.reference),
                {
                    "candidate": _CANDIDATE.to_value(),
                    "profile_id": _PROFILE_ID,
                    "state": "terminal",
                    "outcome": "candidate_semantic_mismatch",
                    "terminal": True,
                    "resumable": False,
                    "candidate_execution_performed": True,
                    "paid_actions_authorized": False,
                    "reason": "stable_gate57_expectations_not_met",
                    "aggregate_reference": aggregate.reference.artifact_id,
                },
                authorization,
            )
        return Gate57Result(aggregate, followup, len(attempts), aggregate.payload["qualified"] is True)
    finally:
        _remove_workspace(workspace, root)


def _validate_chain(root: Path) -> tuple[tuple[StableArtifact, ...], dict[str, object], Path]:
    source, patch, atomicity, manifest, checkpoint = tuple(_load(root, ref) for ref in (_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT))
    source_path = _safe_relative(source.payload.get("raw_evidence_path"))
    source_manifest = _read_json(root / source_path)
    if source.inputs != (ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"),) or source.payload.get("candidate") != _CANDIDATE.to_value() or source.payload.get("complete") is not True:
        raise RuntimeError("#9572 source packet is invalid")
    if source_manifest.get("candidate") != _CANDIDATE.to_value() or source_manifest.get("complete") is not True or source_manifest.get("provider") != "github" or source_manifest.get("authority") != {"api_base": "https://api.github.com", "provider": "github", "repository": "luanti-org/luanti"} or source_manifest.get("issue_numbers") != [9798, 10650]:
        raise RuntimeError("#9572 source authority is invalid")
    pull = source_manifest.get("pull")
    if type(pull) is not dict or pull.get("number") != 9572 or pull.get("merged") is not True:
        raise RuntimeError("#9572 merged pull source is invalid")
    patch_path = _safe_relative(patch.payload.get("raw_patch_path"))
    raw = (root / patch_path).read_bytes()
    sections = _sections(raw)
    section_paths = tuple(section[0] for section in sections)
    if patch.inputs != (ArtifactReference("repository_enumeration/luanti/current/v2", "repository_enumeration", "2.0"), _SOURCE) or patch.payload.get("candidate") != _CANDIDATE.to_value() or patch.payload.get("complete") is not True or patch.payload.get("outcome") != "patch_eligible" or tuple(patch.payload.get("production_paths", ())) != _PRODUCTION or tuple(patch.payload.get("test_paths", ())) != (_TEST,) or patch.payload.get("source_manifest_path") != source_path or not set((*_PRODUCTION, _TEST)).issubset(section_paths) or len(section_paths) != len(set(section_paths)):
        raise RuntimeError("#9572 patch inventory is invalid")
    test_text = next(section[1] for section in sections if section[0] == _TEST).decode("utf-8", "surrogateescape")
    if not all(marker in test_text for marker in ('describe("rotate()"', 'it("rotates"', 'it("is counterclockwise"', 'it("add()"')):
        raise RuntimeError("#9572 test API markers are invalid")
    if atomicity.payload.get("candidate") != _CANDIDATE.to_value() or atomicity.payload.get("complete") is not True or atomicity.payload.get("outcome") != "accepted" or atomicity.payload.get("authoritative_issue_count") != 1 or atomicity.payload.get("authoritative_issue_numbers") != [9798] or atomicity.payload.get("primary_issue_number") != 9798 or atomicity.payload.get("linked_issue_numbers") != [9798, 10650] or atomicity.payload.get("source_manifest_path") != source_path:
        raise RuntimeError("#9572 single-defect mapping is invalid")
    details = manifest.payload.get("details")
    if manifest.inputs != (_PATCH,) or manifest.payload.get("candidate") != _CANDIDATE.to_value() or manifest.payload.get("complete") is not True or manifest.payload.get("stage") != "test_manifest" or manifest.payload.get("state") != "passed" or type(details) is not dict or details.get("executable_test_paths") != [_TEST] or details.get("test_count") != 1:
        raise RuntimeError("#9572 test manifest is invalid")
    if checkpoint.payload.get("candidate") != _CANDIDATE.to_value() or checkpoint.payload.get("state") != "complete" or checkpoint.payload.get("stage_artifact_id") != _PATCH.artifact_id:
        raise RuntimeError("#9572 checkpoint is invalid")
    return (source, patch, atomicity, manifest, checkpoint), source_manifest, root / patch_path


def _materialize_capsule(root: Path, source_manifest: dict[str, object], profile: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    _assert_stable_absent(root, _CAPSULE)
    raw_manifest = profile.payload.get("source_manifest_path")
    if not isinstance(raw_manifest, str):
        raw_manifest = _load(root, _SOURCE).payload["raw_evidence_path"]
    revision = read_legacy_merged_revision(root, raw_manifest_path=raw_manifest, candidate=_CANDIDATE)
    mirror = root / "private/repository-enumeration-git/luanti/mirror.git"
    tree_relative = PurePosixPath("private/fixed-capsules/luanti/9572/fixed-source-v2")
    destination = root.joinpath(*tree_relative.parts)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("#9572 fixed capsule destination already exists")
    files = (*_PRODUCTION, _TEST)
    staging_parent = destination.parent
    staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".fixed-source-", dir=staging_parent))
    try:
        for relative in files:
            content = _git_show(mirror, revision, relative)
            target = staging.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    payload = {"candidate": _CANDIDATE.to_value(), "profile_id": _PROFILE_ID, "source_tree_path": tree_relative.as_posix(), "support_paths": [], "test_paths": [_TEST]}
    try:
        write_stable_artifact(root, artifact_id=_CAPSULE.artifact_id, artifact_kind=_CAPSULE.artifact_kind, semantic_version=_CAPSULE.semantic_version, created_at=_created_after(profile.created_at), inputs=(_SOURCE,), payload=payload, authorization=authorization)
        artifact = _load(root, _CAPSULE)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    if artifact.payload != payload:
        raise RuntimeError("#9572 fixed capsule typed reload failed")
    return artifact


def _git_show(mirror: Path, revision: str, relative: str) -> bytes:
    result = subprocess.run(("git", f"--git-dir={mirror}", "show", f"{revision}:{relative}"), capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("#9572 fixed capsule source file unavailable")
    return result.stdout


def _production_patch(root: Path) -> bytes:
    patch = _load(root, _PATCH)
    raw = (root / _safe_relative(patch.payload["raw_patch_path"])).read_bytes()
    for path, content in _sections(raw):
        if path == _PRODUCTION[0]:
            return content
    raise RuntimeError("#9572 production inverse patch is missing")


def _sections(raw: bytes) -> tuple[tuple[str, bytes], ...]:
    sections: list[tuple[str, bytes]] = []
    current: list[str] = []
    current_path: str | None = None
    for line in raw.decode("utf-8", "surrogateescape").splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append((current_path, "".join(current).encode("utf-8", "surrogateescape")))
            parts = line.rstrip("\r\n").split(" ")
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/") or parts[2][2:] != parts[3][2:]:
                raise RuntimeError("#9572 patch header is invalid")
            current_path = parts[2][2:]
            current = [line]
        elif current_path is not None:
            current.append(line)
    if current_path is not None:
        sections.append((current_path, "".join(current).encode("utf-8", "surrogateescape")))
    if len(sections) < 2:
        raise RuntimeError("#9572 patch section coverage is invalid")
    return tuple(sections)


def _cells() -> tuple[_Cell, ...]:
    return tuple(
        _Cell(phase, variant, repetition, selector, expected)
        for phase, selector, expected_buggy in (("f2p", _F2P, "failure"), ("p2p", _P2P, "pass"))
        for variant, expected in (("fixed", "pass"), ("buggy", expected_buggy))
        for repetition in range(1, 4)
    )


def _execute_cell(root: Path, runtime_plan: LuaRegressionExecutionPlan, cell: _Cell, fixed: Path, buggy: Path, authorization: AuthorizationRecord) -> StableArtifact:
    _assert_stable_absent(root, cell.reference)
    tree = fixed if cell.variant == "fixed" else buggy
    argv = (str(runtime_plan.runtime.runner), _TEST, f"--name={cell.selector}")
    receipt = runtime_plan.watchdog.run(argv, cwd=tree, env=_runtime_env(root, runtime_plan))
    stdout, out_trunc = _bounded(receipt.stdout)
    stderr, err_trunc = _bounded(receipt.stderr)
    count = _count(receipt)
    observed = _observed(receipt, count)
    payload = {"candidate": _CANDIDATE.to_value(), "profile_id": _PROFILE_ID, "execution_lineage": "candidate-gate57/luanti/9572/lua/v1", "phase": cell.phase, "variant": cell.variant, "repetition": cell.repetition, "selector": cell.selector, "test_path": _TEST, "expected_outcome": cell.expected, "observed_outcome": observed, "process_outcome": receipt.outcome, "returncode": receipt.returncode, "completed_test_count": count, "stdout_base64": base64.b64encode(stdout).decode("ascii"), "stderr_base64": base64.b64encode(stderr).decode("ascii"), "stdout_truncated": out_trunc, "stderr_truncated": err_trunc, "output_encoding": "base64", "matched_expected": observed == cell.expected, "fresh_receipt": True, "candidate_execution_performed": True, "paid_actions_authorized": False}
    write_stable_artifact(root, artifact_id=cell.reference.artifact_id, artifact_kind=cell.reference.artifact_kind, semantic_version=cell.reference.semantic_version, created_at=_now(), inputs=(_CAPSULE,), payload=payload, authorization=authorization)
    artifact = _load(root, cell.reference)
    if artifact.payload != payload:
        raise RuntimeError("#9572 attempt typed reload failed")
    return artifact


def _write_aggregate(root: Path, attempts: list[StableArtifact], authorization: AuthorizationRecord) -> StableArtifact:
    if len(attempts) != 12 or len({a.reference for a in attempts}) != 12:
        raise RuntimeError("#9572 attempt coverage is invalid")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault((attempt.payload["phase"], attempt.payload["variant"]), []).append(attempt)
    expected_groups = {("f2p", "fixed"), ("f2p", "buggy"), ("p2p", "fixed"), ("p2p", "buggy")}
    if set(groups) != expected_groups or any(len(v) != 3 for v in groups.values()):
        raise RuntimeError("#9572 repetition coverage is invalid")
    stable = all(len({a.payload["observed_outcome"] for a in v}) == 1 for v in groups.values())
    expected = all(a.payload["matched_expected"] is True for a in attempts)
    infra = _infrastructure_only(attempts)
    outcome = "qualified" if stable and expected else ("infrastructure_blocked" if infra else "candidate_semantic_mismatch")
    payload = {"candidate": _CANDIDATE.to_value(), "profile_id": _PROFILE_ID, "execution_lineage": "candidate-gate57/luanti/9572/lua/v1", "state": "qualified" if stable and expected else "blocked", "outcome": outcome, "terminal": outcome == "candidate_semantic_mismatch", "resumable": outcome != "candidate_semantic_mismatch", "candidate_execution_performed": True, "paid_actions_authorized": False, "repetitions_per_cell": 3, "attempt_count": 12, "stable_repetitions": stable, "all_expected": expected, "qualified": stable and expected, "cells": [{"phase": phase, "variant": variant, "observed_outcomes": [a.payload["observed_outcome"] for a in sorted(groups[(phase, variant)], key=lambda x: x.payload["repetition"])], "matched_expected": [a.payload["matched_expected"] for a in sorted(groups[(phase, variant)], key=lambda x: x.payload["repetition"])]} for phase, variant in sorted(expected_groups)], "deferred_until_after_gate5_7": ["runtime_world_replay", "semantic_observations", "golden_cross_judgment", "construction_receipt"], "resource_policy": {"network": False, "paid": False, "formal_trials": False}}
    write_stable_artifact(root, artifact_id=_AGGREGATE.artifact_id, artifact_kind=_AGGREGATE.artifact_kind, semantic_version=_AGGREGATE.semantic_version, created_at=_now(), inputs=tuple(a.reference for a in attempts), payload=payload, authorization=authorization)
    artifact = _load(root, _AGGREGATE)
    if artifact.payload != payload:
        raise RuntimeError("#9572 aggregate typed reload failed")
    return artifact


def _write_followup(root: Path, reference: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> StableArtifact:
    write_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, reference)


def _runtime_env(root: Path, plan: LuaRegressionExecutionPlan) -> dict[str, str]:
    home = root / "private/runtime-home/luanti-9572-gate57"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {"HOME": str(home), "LANG": "C", "LC_ALL": "C", "PATH": str(plan.runtime.root / "bin"), "LUA_PATH": ";".join(str(p) for p in plan.runtime.lua_paths), "LUA_CPATH": ";".join(str(p) for p in plan.runtime.lua_cpaths)}


def _apply_inverse(buggy: Path, patch: bytes) -> None:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"):
        env.pop(name, None)
    env["GIT_CEILING_DIRECTORIES"] = str(buggy.parent)
    result = subprocess.run(("git", "apply", "-R", "--whitespace=nowarn", "-"), cwd=buggy, input=patch, capture_output=True, check=False, timeout=10, env=env)
    if result.returncode != 0:
        raise RuntimeError("#9572 strict inverse patch failed")


def _copy_capsule(root: Path, destination: Path) -> None:
    source = root / "private/fixed-capsules/luanti/9572/fixed-source-v2"
    destination.mkdir(mode=0o700)
    for relative in (*_PRODUCTION, _TEST):
        target = destination / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (source / PurePosixPath(relative)).open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)


def _validate_runtime_plan(plan: LuaRegressionExecutionPlan) -> None:
    if not plan.runtime.runner.is_file() or not os.access(plan.runtime.runner, os.X_OK):
        raise RuntimeError("registered Busted runner is unavailable")


def _count(receipt: WatchdogReceipt) -> int | None:
    if receipt.outcome != "completed":
        return None
    matches = list(_SUMMARY.finditer(receipt.stdout))
    if len(matches) != 1:
        return 0
    return sum(int(value) for value in matches[0].groups())


def _observed(receipt: WatchdogReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or not isinstance(receipt.returncode, int):
        return "launch_failed"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _infrastructure_only(attempts: list[StableArtifact]) -> bool:
    return any(a.payload["observed_outcome"] in {"timed_out", "launch_failed", "invalid_test_count"} for a in attempts)


def _bounded(value: bytes) -> tuple[bytes, bool]:
    return (value, False) if len(value) <= _MAX_STREAM else (value[:_MAX_STREAM], True)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError("#9572 relative path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts) or path.as_posix() != value:
        raise RuntimeError("#9572 relative path is unsafe")
    return path.as_posix()


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError("#9572 source manifest is invalid")
    return value


def _check_user_authorization(auth: AuthorizationRecord) -> None:
    if auth.revoked or auth.local_user_id != os.getuid():
        raise PermissionError("#9572 authorization is inactive")


def _assert_stable_absent(root: Path, reference: ArtifactReference) -> None:
    path = root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version
    if path.exists():
        raise RuntimeError(f"#9572 fresh artifact already exists: {reference.artifact_id}")


def _new_workspace(root: Path) -> Path:
    parent = root / "private/candidate-gate57-work/luanti/9572"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def _remove_workspace(workspace: Path, root: Path) -> None:
    parent = root / "private/candidate-gate57-work/luanti/9572"
    if workspace.parent != parent or not workspace.name.startswith("run-"):
        raise RuntimeError("#9572 workspace identity is invalid")
    if workspace.exists():
        info = workspace.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("#9572 workspace became unsafe")
        shutil.rmtree(workspace)


def _ordinary_root(root: Path) -> Path:
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#9572 artifact root is unsafe")
    return root.resolve(strict=True)


def _created_after(*values: str) -> str:
    parsed = [datetime.fromisoformat(v.replace("Z", "+00:00")) for v in values]
    return (max(parsed) + timedelta(microseconds=1)).astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "Gate57Result", "execute_luanti_9572_gate57", "prepare_luanti_9572_profile"]
