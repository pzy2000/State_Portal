"""Strict candidate-specific fresh Gate 5--7 epoch for Luanti #9572.

This adapter intentionally does not call the shared candidate plan: the
shared Lua registry has no #9572 entry and an unrelated candidate plan would
weaken provenance.  It binds the current #9572 Gate 1--4 chain, copies the
same-merge support closure into a fresh capsule, and invokes only the
registered offline Busted runtime by absolute path.
"""

from __future__ import annotations

import base64
import hashlib
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

from .luanti_9572_gate57 import _production_patch, _sections
from .luanti_fixed_source_legacy_migration_reader import read_legacy_merged_revision
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_9572_FRESH_GATE57"
_CANDIDATE = {"repository_key": "luanti", "change_number": 9572}
_SOURCE = ArtifactReference("candidate_source_packet/luanti/9572/source/v2", "candidate_source_packet", "2.0")
_PATCH = ArtifactReference("candidate_test_patch/luanti/9572/screening/v2", "candidate_test_patch", "2.0")
_ATOMICITY = ArtifactReference("source_atomicity/luanti/9572/preannotation/v2", "source_atomicity", "2.0")
_MANIFEST = ArtifactReference("candidate_stage/luanti/9572/test_manifest/v2", "candidate_stage", "2.0")
_CHECKPOINT = ArtifactReference("screening_checkpoint/luanti/9572/current/v2", "screening_checkpoint", "2.0")
_BASE_PROFILE = ArtifactReference("candidate_gate57_9572_closure_profile/luanti/9572/lua/v1", "candidate_gate57_9572_closure_profile", "1.0")
_BASE_CAPSULE = ArtifactReference("candidate_gate57_9572_closure_capsule/luanti/9572/lua/v1", "candidate_gate57_9572_closure_capsule", "1.0")
_PROFILE = ArtifactReference("candidate_gate57_9572_fresh_profile/luanti/9572/lua/v2", "candidate_gate57_9572_fresh_profile", "1.0")
_PREPARATION = ArtifactReference("candidate_gate57_9572_fresh_preparation/luanti/9572/lua/v2", "candidate_gate57_9572_fresh_preparation", "1.0")
_CAPSULE = ArtifactReference("candidate_gate57_9572_fresh_capsule/luanti/9572/lua/v2", "candidate_gate57_9572_fresh_capsule", "1.0")
_ATTEMPT_KIND = "candidate_gate57_9572_fresh_attempt"
_ATTEMPT_VERSION = "1.0"
_AGGREGATE = ArtifactReference("candidate_gate57_9572_fresh_aggregate/luanti/9572/lua/v2", "candidate_gate57_9572_fresh_aggregate", "1.0")
_TRANSITION = ArtifactReference("candidate_gate57_9572_fresh_transition/luanti/9572/lua/v2", "candidate_gate57_9572_fresh_transition", "1.0")
_BLOCKER = ArtifactReference("candidate_gate57_9572_fresh_blocker/luanti/9572/lua/v2", "candidate_gate57_9572_fresh_blocker", "1.0")
_TREE = PurePosixPath("private/fixed-capsules/luanti/9572/fresh-source-v2")
_SOURCE_TREE = PurePosixPath("private/fixed-capsules/luanti/9572/closure-source-v1")
_PRODUCTION = "builtin/common/vector.lua"
_SUPPORT = "builtin/common/misc_helpers.lua"
_TEST = "builtin/common/tests/vector_spec.lua"
_LOADER = "c1_lazarus_9572_registered_support_loader.lua"
_LOADER_BYTES = b'_G.core = _G.core or {}\ndofile("builtin/common/misc_helpers.lua")\n'
_SUMMARY = re.compile(rb"(?m)^(?:\x1b\[[0-9;]*m)?(\d+) success(?:es)? / (\d+) failure(?:s)? / (\d+) error(?:s)? / (\d+) pending\b")


@dataclass(frozen=True)
class FreshResult:
    aggregate: StableArtifact
    transition: StableArtifact
    attempts: int


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
            f"candidate_gate57_9572_fresh_attempt/luanti/9572/lua/{self.phase}/{self.variant}/{self.repetition}/v2",
            _ATTEMPT_KIND,
            _ATTEMPT_VERSION,
        )


def prepare_luanti_9572_fresh(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    """Reload current Gate 1--4 and materialize a fresh support-complete capsule."""

    _auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    checkpoint = _load(root, _CHECKPOINT)
    base = _load(root, _BASE_PROFILE)
    base_capsule = _load(root, _BASE_CAPSULE)
    if source.payload.get("candidate") != _CANDIDATE or patch.payload.get("candidate") != _CANDIDATE:
        raise RuntimeError("#9572 fresh source candidate mismatch")
    gates = base.payload.get("strict_gate_status")
    if type(gates) is not dict or any(gates.get(name) != "passed" for name in ("source_authority", "single_defect_mapping", "strict_inverse_patch", "test_api_compatibility")):
        raise RuntimeError("#9572 fresh Gate 1--4 chain is not accepted")
    if base.payload.get("candidate_execution_performed") is not False:
        raise RuntimeError("#9572 fresh profile would inherit execution")
    merge_sha, raw_manifest = _merge_sha(root, source)
    profile_payload = {
        "candidate": _CANDIDATE,
        "profile_id": "luanti-9572-rotation-fresh-v2",
        "candidate_lifecycle": "active",
        "tier": "on_path_candidate",
        "source_refs": [_SOURCE.artifact_id, _PATCH.artifact_id, _ATOMICITY.artifact_id, _MANIFEST.artifact_id, _CHECKPOINT.artifact_id],
        "support_closure_ref": base.reference.artifact_id,
        "authority": "github:luanti-org/luanti",
        "primary_issue_number": 9798,
        "merge_revision": merge_sha,
        "source_manifest_path": raw_manifest,
        "production_paths": [_PRODUCTION],
        "support_paths": [_SUPPORT],
        "test_paths": [_TEST],
        "f2p_selector": "vector rotate() rotates",
        "p2p_selector": "vector add()",
        "repetitions": 3,
        "registered_runtime": "private/dependencies/luanti-lua-busted/1.0/bin/busted",
        "registered_bootstrap": {"script": _LOADER, "loads": [_SUPPORT], "core_table_initialized": True, "before_test": True, "modifies_test": False},
        "strict_gate_status": {"source_authority": "passed", "single_defect_mapping": "passed", "strict_inverse_patch": "passed", "test_api_compatibility": "passed", "fixed_f2p_3x": "downstream_pending", "buggy_f2p_3x": "downstream_pending", "fixed_buggy_p2p_3x": "downstream_pending"},
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    _assert_absent(root, _PROFILE)
    write_stable_artifact(root, artifact_id=_PROFILE.artifact_id, artifact_kind=_PROFILE.artifact_kind, semantic_version=_PROFILE.semantic_version, created_at=_now(), inputs=(_SOURCE, _PATCH, _ATOMICITY, _MANIFEST, _CHECKPOINT, base.reference, base_capsule.reference), payload=profile_payload, authorization=authorization)
    profile = _load(root, _PROFILE)
    prep_payload = {"candidate": _CANDIDATE, "profile_id": profile_payload["profile_id"], "state": "prepared", "outcome": "downstream_pending", "profile_reference": profile.reference.artifact_id, "capsule_base_reference": base_capsule.reference.artifact_id, "production_paths": [_PRODUCTION], "support_paths": [_SUPPORT], "test_paths": [_TEST], "registered_bootstrap": profile_payload["registered_bootstrap"], "f2p": {"selector": "vector rotate() rotates", "fixed_expected": "pass", "buggy_expected": "failure", "repetitions": 3}, "p2p": {"selector": "vector add()", "fixed_expected": "pass", "buggy_expected": "pass", "repetitions": 3}, "candidate_execution_performed": False, "paid_actions_authorized": False}
    _assert_absent(root, _PREPARATION)
    write_stable_artifact(root, artifact_id=_PREPARATION.artifact_id, artifact_kind=_PREPARATION.artifact_kind, semantic_version=_PREPARATION.semantic_version, created_at=_now(), inputs=(profile.reference,), payload=prep_payload, authorization=authorization)
    preparation = _load(root, _PREPARATION)
    capsule = _copy_capsule(root, source, profile, authorization, merge_sha)
    del atomicity, manifest, checkpoint
    return profile, preparation, capsule


def execute_luanti_9572_fresh(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> FreshResult:
    """Execute exactly twelve fresh cells from the candidate-specific plan."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    capsule = _load(root, _CAPSULE)
    if preparation.payload.get("state") != "prepared" or profile.payload.get("candidate_execution_performed") is not False:
        raise RuntimeError("#9572 fresh preparation is not executable")
    runtime = _runtime(root)
    source_tree = root.joinpath(*PurePosixPath(capsule.payload["source_tree_path"]).parts)
    workspace_parent = root / "private/candidate-gate57-fresh-work/luanti/9572"
    workspace_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = Path(tempfile.mkdtemp(prefix="epoch-v2-", dir=workspace_parent))
    attempts: list[StableArtifact] = []
    try:
        fixed, buggy = workspace / "fixed", workspace / "buggy"
        _copy_tree(source_tree, fixed)
        _copy_tree(source_tree, buggy)
        _write_loader(fixed)
        _write_loader(buggy)
        patch_bytes = _production_patch(root)
        _inverse(buggy, patch_bytes)
        if (fixed / _SUPPORT).read_bytes() != (buggy / _SUPPORT).read_bytes() or (fixed / _TEST).read_bytes() != (buggy / _TEST).read_bytes():
            raise RuntimeError("#9572 fresh fixed/buggy support or test diverged")
        if (fixed / _PRODUCTION).read_bytes() == (buggy / _PRODUCTION).read_bytes():
            raise RuntimeError("#9572 fresh inverse did not change production")
        for cell in _cells():
            attempts.append(_run_cell(root, runtime, profile, capsule, cell, fixed, buggy, authorization))
        aggregate = _aggregate(root, profile, capsule, attempts, authorization)
        transition = _followup(root, profile, preparation, capsule, aggregate, authorization)
        return FreshResult(aggregate, transition, len(attempts))
    finally:
        _remove_workspace(workspace, workspace_parent)


def _copy_capsule(root: Path, source: StableArtifact, profile: StableArtifact, authorization: AuthorizationRecord, merge_sha: str) -> StableArtifact:
    _assert_absent(root, _CAPSULE)
    base = root.joinpath(*_SOURCE_TREE.parts)
    destination = root.joinpath(*_TREE.parts)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("#9572 fresh capsule destination already exists")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".fresh-source-", dir=parent))
    try:
        for rel in (_PRODUCTION, _SUPPORT, _TEST):
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (base / rel).open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
        os.replace(stage, destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    payload = {"candidate": _CANDIDATE, "profile_id": profile.payload["profile_id"], "source_tree_path": _TREE.as_posix(), "merge_revision": merge_sha, "production_paths": [_PRODUCTION], "support_paths": [_SUPPORT], "test_paths": [_TEST], "registered_bootstrap": {"script": _LOADER, "loads": [_SUPPORT], "core_table_initialized": True, "before_test": True, "modifies_test": False}}
    try:
        write_stable_artifact(root, artifact_id=_CAPSULE.artifact_id, artifact_kind=_CAPSULE.artifact_kind, semantic_version=_CAPSULE.semantic_version, created_at=_now(), inputs=(profile.reference, _BASE_CAPSULE), payload=payload, authorization=authorization)
        capsule = _load(root, _CAPSULE)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return capsule


def _runtime(root: Path) -> tuple[Path, tuple[Path, ...], tuple[Path, ...]]:
    base = root / "private/dependencies/luanti-lua-busted/1.0"
    manifest = json.loads((base / "runtime.json").read_text(encoding="utf-8"))
    if manifest.get("dependency_id") != "luanti-busted-runtime" or manifest.get("semantic_version") != "1.0" or manifest.get("lua_abi") != "5.1" or manifest.get("runner") != "bin/busted" or manifest.get("interpreter") != "bin/luajit":
        raise RuntimeError("#9572 fresh registered runtime manifest is invalid")
    runner = base / "bin/busted"
    interpreter = base / "bin/luajit"
    if not runner.is_file() or not interpreter.is_file() or not os.access(runner, os.X_OK) or not os.access(interpreter, os.X_OK):
        raise RuntimeError("#9572 fresh registered runtime is incomplete")
    closure = set(manifest.get("closure_files", []))
    actual = {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and p.name != "runtime.json"}
    if actual != closure:
        raise RuntimeError("#9572 fresh registered runtime closure changed")
    return runner, tuple(base / p for p in manifest["lua_paths"]), tuple(base / p for p in manifest["lua_cpaths"])


def _cells() -> tuple[_Cell, ...]:
    return tuple(_Cell(phase, variant, repetition, selector, expected) for phase, selector, buggy in (("f2p", "vector rotate() rotates", "failure"), ("p2p", "vector add()", "pass")) for variant, expected in (("fixed", "pass"), ("buggy", buggy)) for repetition in range(1, 4))


def _run_cell(root: Path, runtime: tuple[Path, tuple[Path, ...], tuple[Path, ...]], profile: StableArtifact, capsule: StableArtifact, cell: _Cell, fixed: Path, buggy: Path, authorization: AuthorizationRecord) -> StableArtifact:
    _assert_absent(root, cell.reference)
    runner, lua_paths, lua_cpaths = runtime
    tree = fixed if cell.variant == "fixed" else buggy
    home = root / "private/runtime-home/luanti-9572-fresh-v2"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {"HOME": str(home), "LANG": "C", "LC_ALL": "C", "PATH": str(runner.parent), "LUA_PATH": ";".join(str(p) for p in lua_paths), "LUA_CPATH": ";".join(str(p) for p in lua_cpaths)}
    argv = (str(runner), _TEST, f"--helper={_LOADER}", f"--name={cell.selector}")
    try:
        proc = subprocess.Popen(argv, cwd=tree, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=10)
            process_outcome = "completed"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            stdout, stderr = proc.communicate()
            process_outcome = "timed_out"
    except OSError as error:
        stdout, stderr, process_outcome = b"", str(error).encode("utf-8", "replace"), "launch_failed"
        proc = None
    count = _count(stdout) if process_outcome == "completed" else None
    observed = "timed_out" if process_outcome == "timed_out" else "launch_failed" if process_outcome == "launch_failed" else "invalid_test_count" if count != 1 else "pass" if proc.returncode == 0 else "failure"
    payload = {"candidate": _CANDIDATE, "profile_id": profile.payload["profile_id"], "execution_lineage": "candidate-gate57/luanti/9572/fresh-v2", "phase": cell.phase, "variant": cell.variant, "repetition": cell.repetition, "selector": cell.selector, "test_path": _TEST, "expected_outcome": cell.expected, "observed_outcome": observed, "process_outcome": process_outcome, "returncode": None if proc is None else proc.returncode, "completed_test_count": count, "stdout_base64": base64.b64encode(stdout[:262144]).decode("ascii"), "stderr_base64": base64.b64encode(stderr[:262144]).decode("ascii"), "stdout_truncated": len(stdout) > 262144, "stderr_truncated": len(stderr) > 262144, "output_encoding": "base64", "support_loader": {"kind": "registered_support_loader", "script": _LOADER, "loads": [_SUPPORT], "modifies_test": False, "core_table_initialized": True, "before_test": True, "script_fingerprint": hashlib.sha256(_LOADER_BYTES).hexdigest()}, "matched_expected": observed == cell.expected, "fresh_receipt": True, "candidate_execution_performed": True, "paid_actions_authorized": False}
    write_stable_artifact(root, artifact_id=cell.reference.artifact_id, artifact_kind=cell.reference.artifact_kind, semantic_version=cell.reference.semantic_version, created_at=_now(), inputs=(profile.reference, capsule.reference), payload=payload, authorization=authorization)
    return _load(root, cell.reference)


def _aggregate(root: Path, profile: StableArtifact, capsule: StableArtifact, attempts: list[StableArtifact], authorization: AuthorizationRecord) -> StableArtifact:
    if len(attempts) != 12 or len({a.reference for a in attempts}) != 12:
        raise RuntimeError("#9572 fresh attempt coverage is invalid")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for a in attempts:
        groups.setdefault((a.payload["phase"], a.payload["variant"]), []).append(a)
    stable = all(len({a.payload["observed_outcome"] for a in values}) == 1 for values in groups.values())
    expected = all(a.payload["matched_expected"] is True and a.payload["process_outcome"] == "completed" for a in attempts)
    outcome = "qualified" if stable and expected else "infrastructure_blocked" if any(a.payload["observed_outcome"] in {"timed_out", "launch_failed", "invalid_test_count"} for a in attempts) else "candidate_semantic_mismatch"
    payload = {"candidate": _CANDIDATE, "profile_id": profile.payload["profile_id"], "execution_lineage": "candidate-gate57/luanti/9572/fresh-v2", "state": "qualified" if outcome == "qualified" else "blocked", "outcome": outcome, "terminal": outcome == "candidate_semantic_mismatch", "resumable": outcome != "candidate_semantic_mismatch", "candidate_execution_performed": True, "paid_actions_authorized": False, "attempt_count": 12, "repetitions_per_cell": 3, "stable_repetitions": stable, "all_expected": expected, "qualified": stable and expected, "cells": [{"phase": phase, "variant": variant, "observed_outcomes": [a.payload["observed_outcome"] for a in sorted(groups[(phase, variant)], key=lambda x: x.payload["repetition"])], "matched_expected": [a.payload["matched_expected"] for a in sorted(groups[(phase, variant)], key=lambda x: x.payload["repetition"])]} for phase, variant in sorted(groups)], "registered_bootstrap": {"script": _LOADER, "loads": [_SUPPORT], "core_table_initialized": True, "before_test": True, "modifies_test": False, "script_fingerprint": hashlib.sha256(_LOADER_BYTES).hexdigest()}, "deferred_until_after_gate5_7": ["runtime_world_replay", "semantic_observations", "golden_cross_judgment", "construction_receipt"], "resource_policy": {"network": False, "paid": False, "formal_trials": False}}
    write_stable_artifact(root, artifact_id=_AGGREGATE.artifact_id, artifact_kind=_AGGREGATE.artifact_kind, semantic_version=_AGGREGATE.semantic_version, created_at=_now(), inputs=tuple(a.reference for a in attempts), payload=payload, authorization=authorization)
    return _load(root, _AGGREGATE)


def _followup(root: Path, profile: StableArtifact, preparation: StableArtifact, capsule: StableArtifact, aggregate: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    reference = _TRANSITION if aggregate.payload["qualified"] else _BLOCKER
    payload = {"candidate": _CANDIDATE, "profile_id": profile.payload["profile_id"], "aggregate_reference": aggregate.reference.artifact_id, "state": "passed" if aggregate.payload["qualified"] else "blocked", "outcome": aggregate.payload["outcome"], "terminal": False, "resumable": True, "candidate_execution_performed": True, "paid_actions_authorized": False, "gate_status": {"source_authority": "passed", "single_defect_mapping": "passed", "strict_inverse_patch": "passed", "test_api_compatibility": "passed", "fixed_buggy_f2p_p2p_3x": "passed" if aggregate.payload["qualified"] else "pending"}, "deferred_until_after_gate5_7": ["runtime_world_replay", "semantic_observations", "golden_cross_judgment", "construction_receipt"]}
    _assert_absent(root, reference)
    write_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version, created_at=_now(), inputs=(profile.reference, preparation.reference, capsule.reference, aggregate.reference), payload=payload, authorization=authorization)
    return _load(root, reference)


def _inverse(tree: Path, patch: bytes) -> None:
    env = {k: v for k, v in os.environ.items() if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX"}}
    env["GIT_CEILING_DIRECTORIES"] = str(tree.parent)
    result = subprocess.run(("git", "apply", "-R", "--whitespace=nowarn", "-"), cwd=tree, input=patch, capture_output=True, check=False, timeout=10, env=env)
    if result.returncode != 0:
        raise RuntimeError("#9572 fresh strict inverse failed")


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for rel in (_PRODUCTION, _SUPPORT, _TEST):
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source / rel, target)


def _write_loader(tree: Path) -> None:
    (tree / _LOADER).write_bytes(_LOADER_BYTES)


def _merge_sha(root: Path, source: StableArtifact) -> tuple[str, str]:
    raw = source.payload.get("raw_evidence_path")
    if not isinstance(raw, str):
        raise RuntimeError("#9572 fresh source manifest path is invalid")
    data = json.loads((root / PurePosixPath(raw)).read_text(encoding="utf-8"))
    pull = data.get("pull")
    if not isinstance(pull, dict) or not isinstance(pull.get("merge_commit_sha"), str):
        raise RuntimeError("#9572 fresh merge revision is missing")
    return pull["merge_commit_sha"], raw


def _count(stdout: bytes) -> int:
    matches = list(_SUMMARY.finditer(stdout))
    if len(matches) != 1:
        return 0
    return sum(int(value) for value in matches[0].groups())


def _remove_workspace(workspace: Path, parent: Path) -> None:
    if workspace.parent != parent or not workspace.name.startswith("epoch-v2-"):
        raise RuntimeError("#9572 fresh workspace identity is invalid")
    if workspace.exists():
        if workspace.is_symlink() or not workspace.is_dir():
            raise RuntimeError("#9572 fresh workspace became unsafe")
        shutil.rmtree(workspace)


def _auth(value: AuthorizationRecord) -> None:
    if value.scope != "preannotation-execution" or value.revoked or value.local_user_id != os.getuid():
        raise PermissionError("#9572 fresh authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _assert_absent(root: Path, reference: ArtifactReference) -> None:
    if (root / "artifacts" / Path(reference.artifact_id) / reference.semantic_version).exists():
        raise RuntimeError(f"#9572 fresh artifact already exists: {reference.artifact_id}")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "FreshResult", "execute_luanti_9572_fresh", "prepare_luanti_9572_fresh"]
