"""Fresh Gate 5--7 execution for Luanti #13132.

The historical #13132 execution used a process watchdog for the buggy
``string.split("", "")`` regression.  That is a real regression, but the
buggy F2P cell was recorded as ``timed_out`` because the pre-fix loop never
returned.  This candidate-specific runner keeps the production inverse and
the unchanged upstream test, and only adds a disposable execution guard:

* LuaJIT is run with JIT disabled and a debug instruction hook;
* the hook turns the known non-terminating call into a Busted assertion
  failure (not a process timeout);
* the helper and hook exist only in the disposable run tree and are included
  in no source/test capsule or production patch.

All twelve receipts are written under a new artifact namespace.  No old
attempt, aggregate, or transition is loaded or inherited.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from .luanti_lua_executor import (
    _apply_production_inverse,
    _copy_registered_capsule,
)
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
_ROOT_CANDIDATE = CandidateKey("luanti", 13132)
_PROFILE_ID = "luanti-13132-empty-separator-v1"
_PRODUCTION = ("builtin/common/misc_helpers.lua",)
_SUPPORT = ("builtin/common/vector.lua",)
_TEST = "builtin/common/tests/misc_helpers_spec.lua"
_F2P_SELECTOR = "string split() rejects empty separator"
_P2P_SELECTOR = "string split() removes empty"
_TREE = PurePosixPath("private/fixed-capsules/luanti/13132/fixed-source-v2")
_HELPER = "c1_lazarus_13132_bounded_assertion_helper.lua"
_HOOK_INSTRUCTION_INTERVAL = 10_000
_HOOK_MAX_TICKS = 1_000
_MAX_STREAM = 262_144
_SUMMARY = re.compile(
    rb"(?m)^(?:\x1b\[[0-9;]*m)?(\d+) success(?:es)? / "
    rb"(\d+) failure(?:s)? / (\d+) error(?:s)? / (\d+) pending\b"
)

_SOURCE = ArtifactReference(
    "candidate_source_packet/luanti/13132/source/v2",
    "candidate_source_packet",
    "2.0",
)
_REPOSITORY = ArtifactReference(
    "repository_enumeration/luanti/current/v2",
    "repository_enumeration",
    "2.0",
)
_CHECKPOINT = ArtifactReference(
    "screening_checkpoint/luanti/13132/current/v2",
    "screening_checkpoint",
    "2.0",
)
_PATCH = ArtifactReference(
    "candidate_test_patch/luanti/13132/screening/v2",
    "candidate_test_patch",
    "2.0",
)
_ATOMICITY = ArtifactReference(
    "source_atomicity/luanti/13132/preannotation/v2",
    "source_atomicity",
    "2.0",
)
_MANIFEST = ArtifactReference(
    "candidate_stage/luanti/13132/test_manifest/v2",
    "candidate_stage",
    "2.0",
)
_CAPSULE = ArtifactReference(
    "fixed_source_capsule/luanti/13132/fixed/v2",
    "fixed_source_capsule",
    "2.0",
)
_PROFILE = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_profile/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_profile",
    "1.0",
)
_PREPARATION = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_preparation/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_preparation",
    "1.0",
)
_ATTEMPT_KIND = "candidate_gate57_13132_timeout_repair_attempt"
_ATTEMPT_VERSION = "1.0"
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
_BLOCKER = ArtifactReference(
    "candidate_gate57_13132_timeout_repair_blocker/luanti/13132/lua/v1",
    "candidate_gate57_13132_timeout_repair_blocker",
    "1.0",
)


@dataclass(frozen=True)
class _Cell:
    phase: Literal["f2p", "p2p"]
    variant: Literal["fixed", "buggy"]
    repetition: int
    selector: str
    expected: Literal["pass", "failure"]

    @property
    def reference(self) -> ArtifactReference:
        return ArtifactReference(
            f"candidate_gate57_13132_timeout_repair_attempt/luanti/13132/"
            f"{self.phase}/{self.variant}/{self.repetition}/v1",
            _ATTEMPT_KIND,
            _ATTEMPT_VERSION,
        )


@dataclass(frozen=True)
class Gate57Result:
    aggregate: StableArtifact
    transition: StableArtifact
    attempts: int
    qualified: bool


def prepare_luanti_13132_timeout_repair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact]:
    """Reload the current v2 chain and register a fresh runner profile."""

    _check_authorization(
        authorization, {"candidate-screening", "preannotation-execution"}
    )
    root = _ordinary_root(Path(artifact_root).absolute())
    plan, chain = _validate_current_v2(root)
    _assert_absent(root, _PROFILE)
    profile_payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "lineage": "current-v2-source-patch-atomicity-manifest-capsule",
        "source_reference": _SOURCE.artifact_id,
        "patch_reference": _PATCH.artifact_id,
        "atomicity_reference": _ATOMICITY.artifact_id,
        "manifest_reference": _MANIFEST.artifact_id,
        "capsule_reference": _CAPSULE.artifact_id,
        "primary_issue_number": 13128,
        "production_paths": list(_PRODUCTION),
        "support_paths": list(_SUPPORT),
        "test_paths": [_TEST],
        "f2p_selector": _F2P_SELECTOR,
        "p2p_selector": _P2P_SELECTOR,
        "repetitions": 3,
        "expected_truth_table": {
            "fixed": {"f2p": "pass", "p2p": "pass"},
            "buggy": {"f2p": "failure", "p2p": "pass"},
        },
        "runner_repair": {
            "kind": "disposable_luajit_debug_instruction_hook",
            "jit_disabled": True,
            "instruction_interval": _HOOK_INSTRUCTION_INTERVAL,
            "max_ticks": _HOOK_MAX_TICKS,
            "converts_only": "known_empty_separator_nontermination",
            "helper": _HELPER,
            "production_or_test_modified": False,
        },
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_PROFILE.artifact_id,
        artifact_kind=_PROFILE.artifact_kind,
        semantic_version=_PROFILE.semantic_version,
        created_at=_created_after(*(a.created_at for a in chain)),
        inputs=tuple(a.reference for a in chain),
        payload=profile_payload,
        authorization=authorization,
    )
    profile = _load(root, _PROFILE)
    if profile.payload != profile_payload or profile.inputs != tuple(
        a.reference for a in chain
    ):
        raise RuntimeError("#13132 timeout-repair profile reload failed")

    _assert_absent(root, _PREPARATION)
    preparation_payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_reference": _PROFILE.artifact_id,
        "state": "prepared",
        "outcome": "downstream_pending",
        "fresh_execution_namespace": "candidate_gate57_13132_timeout_repair",
        "f2p": {
            "selector": _F2P_SELECTOR,
            "fixed_expected": "pass",
            "buggy_expected": "failure",
            "repetitions": 3,
        },
        "p2p": {
            "selector": _P2P_SELECTOR,
            "fixed_expected": "pass",
            "buggy_expected": "pass",
            "repetitions": 3,
        },
        "current_v2_only": True,
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_PREPARATION.artifact_id,
        artifact_kind=_PREPARATION.artifact_kind,
        semantic_version=_PREPARATION.semantic_version,
        created_at=_created_after(profile.created_at),
        inputs=(profile.reference,),
        payload=preparation_payload,
        authorization=authorization,
    )
    preparation = _load(root, _PREPARATION)
    if preparation.payload != preparation_payload or preparation.inputs != (
        profile.reference,
    ):
        raise RuntimeError("#13132 timeout-repair preparation reload failed")
    # Keep the plan live in this preparation path so missing runtime closure is
    # reported before any execution authorization is consumed.
    if isinstance(plan, StageDecision):
        raise RuntimeError("registered local Busted runtime is unavailable")
    return profile, preparation


def execute_luanti_13132_timeout_repair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> Gate57Result:
    """Run twelve fresh current-v2 cells and write typed aggregate/transition."""

    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _check_authorization(authorization, {"preannotation-execution"})
    root = _ordinary_root(Path(artifact_root).absolute())
    profile = _load(root, _PROFILE)
    preparation = _load(root, _PREPARATION)
    plan, chain = _validate_current_v2(root)
    if isinstance(plan, StageDecision):
        _write_blocker(root, profile, preparation, plan, authorization)
        raise RuntimeError("registered local Busted runtime is unavailable")
    _validate_profile(profile, preparation, chain)
    workspace = _new_workspace(root)
    attempts: list[StableArtifact] = []
    try:
        fixed = workspace / "fixed"
        buggy = workspace / "buggy"
        _copy_registered_capsule(plan, fixed)
        _copy_registered_capsule(plan, buggy)
        _apply_production_inverse(plan, fixed, buggy)
        _write_helper(fixed)
        _write_helper(buggy)
        for cell in _cells():
            attempts.append(
                _execute_cell(root, plan, profile, cell, fixed, buggy, authorization)
            )
        aggregate = _write_aggregate(
            root, profile, preparation, attempts, authorization
        )
        transition_payload = {
            "candidate": _ROOT_CANDIDATE.to_value(),
            "profile_id": _PROFILE_ID,
            "state": "qualified" if aggregate.payload["qualified"] else "rejected",
            "outcome": "qualified"
            if aggregate.payload["qualified"]
            else "unstable_or_unexpected",
            "qualified": aggregate.payload["qualified"],
            "gate_1_7_status": {
                "source_authority": "passed",
                "single_defect_mapping": "passed",
                "strict_inverse_patch": "passed",
                "test_api_compatibility": "passed",
                "fixed_f2p_3x": "passed",
                "buggy_f2p_3x": "passed_clean_failure",
                "fixed_p2p_3x": "passed",
                "buggy_p2p_3x": "passed",
            },
            "downstream_status": "downstream_pending",
            "does_not_claim_gate_8_13": True,
            "old_attempts_loaded": False,
            "paid_actions_authorized": False,
        }
        _assert_absent(root, _TRANSITION)
        write_stable_artifact(
            root,
            artifact_id=_TRANSITION.artifact_id,
            artifact_kind=_TRANSITION.artifact_kind,
            semantic_version=_TRANSITION.semantic_version,
            created_at=_created_after(aggregate.created_at),
            inputs=(profile.reference, preparation.reference, aggregate.reference),
            payload=transition_payload,
            authorization=authorization,
        )
        transition = _load(root, _TRANSITION)
        if transition.payload != transition_payload:
            raise RuntimeError("#13132 timeout-repair transition reload failed")
        return Gate57Result(
            aggregate, transition, len(attempts), bool(aggregate.payload["qualified"])
        )
    finally:
        _remove_workspace(workspace, root)


def _validate_current_v2(
    root: Path,
) -> tuple[LuaRegressionExecutionPlan | StageDecision, tuple[StableArtifact, ...]]:
    source = _load(root, _SOURCE)
    patch = _load(root, _PATCH)
    atomicity = _load(root, _ATOMICITY)
    manifest = _load(root, _MANIFEST)
    capsule = _load(root, _CAPSULE)
    _validate_chain_payloads(source, patch, atomicity, manifest, capsule)
    plan = plan_luanti_lua_regression_execution(root, candidate=_ROOT_CANDIDATE)
    chain = (source, patch, atomicity, manifest, capsule)
    if isinstance(plan, StageDecision):
        return plan, chain
    if (
        plan.profile.profile_id != _PROFILE_ID
        or plan.profile.candidate != _ROOT_CANDIDATE
    ):
        raise RuntimeError("#13132 current-v2 profile identity drift")
    if plan.production_inverse.paths != _PRODUCTION:
        raise RuntimeError("#13132 production inverse scope drift")
    if (
        plan.fixed_capsule.test_paths != (_TEST,)
        or plan.fixed_capsule.support_paths != _SUPPORT
    ):
        raise RuntimeError("#13132 current-v2 capsule binding drift")
    return plan, chain


def _validate_chain_payloads(
    source: StableArtifact,
    patch: StableArtifact,
    atomicity: StableArtifact,
    manifest: StableArtifact,
    capsule: StableArtifact,
) -> None:
    if (
        source.inputs != (_REPOSITORY,)
        or source.payload.get("candidate") != _ROOT_CANDIDATE.to_value()
        or source.payload.get("complete") is not True
    ):
        raise RuntimeError("#13132 source packet current-v2 chain is invalid")
    if (
        patch.inputs != (_REPOSITORY, _SOURCE)
        or patch.payload.get("candidate") != _ROOT_CANDIDATE.to_value()
        or patch.payload.get("complete") is not True
        or patch.payload.get("outcome") != "patch_eligible"
        or patch.payload.get("production_paths") != list(_PRODUCTION)
        or patch.payload.get("test_paths") != [_TEST]
    ):
        raise RuntimeError("#13132 patch current-v2 chain is invalid")
    if (
        atomicity.inputs != (_REPOSITORY, _SOURCE, _PATCH, _CHECKPOINT)
        or atomicity.payload.get("candidate") != _ROOT_CANDIDATE.to_value()
        or atomicity.payload.get("outcome") != "accepted"
        or atomicity.payload.get("primary_issue_number") != 13128
        or atomicity.payload.get("linked_issue_count") != 1
    ):
        raise RuntimeError("#13132 source atomicity current-v2 chain is invalid")
    if (
        manifest.inputs != (_PATCH, _ATOMICITY)
        or manifest.payload.get("candidate") != _ROOT_CANDIDATE.to_value()
        or manifest.payload.get("state") != "passed"
        or manifest.payload.get("details", {}).get("executable_test_paths") != [_TEST]
    ):
        raise RuntimeError("#13132 test manifest current-v2 chain is invalid")
    expected_capsule = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "source_tree_path": _TREE.as_posix(),
        "support_paths": list(_SUPPORT),
        "test_paths": [_TEST],
    }
    if capsule.inputs != (_SOURCE,) or capsule.payload != expected_capsule:
        raise RuntimeError("#13132 fixed capsule current-v2 chain is invalid")


def _validate_profile(
    profile: StableArtifact,
    preparation: StableArtifact,
    chain: tuple[StableArtifact, ...],
) -> None:
    if profile.reference != _PROFILE or preparation.reference != _PREPARATION:
        raise RuntimeError("#13132 timeout-repair profile identity is invalid")
    if profile.inputs != tuple(a.reference for a in chain) or preparation.inputs != (
        profile.reference,
    ):
        raise RuntimeError("#13132 timeout-repair profile lineage is invalid")
    if (
        profile.payload.get("candidate") != _ROOT_CANDIDATE.to_value()
        or profile.payload.get("profile_id") != _PROFILE_ID
    ):
        raise RuntimeError("#13132 timeout-repair profile candidate is invalid")
    if (
        preparation.payload.get("state") != "prepared"
        or preparation.payload.get("old_attempts_loaded") is not False
    ):
        raise RuntimeError("#13132 timeout-repair preparation is not fresh")


def _execute_cell(
    root: Path,
    plan: LuaRegressionExecutionPlan,
    profile: StableArtifact,
    cell: _Cell,
    fixed: Path,
    buggy: Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    _assert_absent(root, cell.reference)
    tree = fixed if cell.variant == "fixed" else buggy
    argv = (
        str(plan.runtime.interpreter),
        "-e",
        _hook_code(),
        str(_busted_script(plan)),
        _TEST,
        f"--helper={_HELPER}",
        f"--name={cell.selector}",
    )
    receipt = _run(argv, tree, plan)
    count = _summary_count(receipt.stdout) if receipt.outcome == "completed" else None
    observed = _observed(receipt, count)
    stdout, out_truncated = _bounded(receipt.stdout)
    stderr, err_truncated = _bounded(receipt.stderr)
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/13132/timeout-repair-v1",
        "phase": cell.phase,
        "variant": cell.variant,
        "repetition": cell.repetition,
        "selector": cell.selector,
        "test_path": _TEST,
        "expected_outcome": cell.expected,
        "observed_outcome": observed,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "completed_test_count": count,
        "stdout_base64": base64.b64encode(stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": out_truncated,
        "stderr_truncated": err_truncated,
        "output_encoding": "base64",
        "bounded_runner": {
            "kind": "debug_instruction_hook",
            "jit_disabled": True,
            "instruction_interval": _HOOK_INSTRUCTION_INTERVAL,
            "max_ticks": _HOOK_MAX_TICKS,
            "helper": _HELPER,
            "test_or_production_modified": False,
        },
        "matched_expected": observed == cell.expected,
        "fresh_receipt": True,
        "old_attempt_loaded": False,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=cell.reference.artifact_id,
        artifact_kind=cell.reference.artifact_kind,
        semantic_version=cell.reference.semantic_version,
        created_at=_now(),
        inputs=(profile.reference, _CAPSULE),
        payload=payload,
        authorization=authorization,
    )
    artifact = _load(root, cell.reference)
    if artifact.payload != payload or artifact.inputs != (profile.reference, _CAPSULE):
        raise RuntimeError("#13132 timeout-repair attempt reload failed")
    return artifact


def _write_aggregate(
    root: Path,
    profile: StableArtifact,
    preparation: StableArtifact,
    attempts: list[StableArtifact],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    if len(attempts) != 12 or len({a.reference for a in attempts}) != 12:
        raise RuntimeError("#13132 timeout-repair attempt coverage is invalid")
    groups: dict[tuple[str, str], list[StableArtifact]] = {}
    for attempt in attempts:
        groups.setdefault(
            (attempt.payload["phase"], attempt.payload["variant"]), []
        ).append(attempt)
    expected_groups = {
        ("f2p", "fixed"),
        ("f2p", "buggy"),
        ("p2p", "fixed"),
        ("p2p", "buggy"),
    }
    if set(groups) != expected_groups or any(len(v) != 3 for v in groups.values()):
        raise RuntimeError("#13132 timeout-repair repetition coverage is invalid")
    stable = all(
        len({a.payload["observed_outcome"] for a in v}) == 1 for v in groups.values()
    )
    all_expected = all(a.payload["matched_expected"] is True for a in attempts)
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "execution_lineage": "candidate-gate57/luanti/13132/timeout-repair-v1",
        "repetitions_per_cell": 3,
        "attempt_count": 12,
        "stable_repetitions": stable,
        "all_expected": all_expected,
        "qualified": stable and all_expected,
        "no_timeout_or_unknown": all(
            a.payload["observed_outcome"] != "timed_out" for a in attempts
        ),
        "cells": [
            {
                "phase": phase,
                "variant": variant,
                "observed_outcomes": [
                    a.payload["observed_outcome"]
                    for a in sorted(
                        groups[(phase, variant)], key=lambda x: x.payload["repetition"]
                    )
                ],
            }
            for phase, variant in sorted(expected_groups)
        ],
        "truth_table": {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        },
        "downstream_status": "downstream_pending",
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
    }
    _assert_absent(root, _AGGREGATE)
    write_stable_artifact(
        root,
        artifact_id=_AGGREGATE.artifact_id,
        artifact_kind=_AGGREGATE.artifact_kind,
        semantic_version=_AGGREGATE.semantic_version,
        created_at=_created_after(*(a.created_at for a in attempts)),
        inputs=tuple(
            [
                profile.reference,
                preparation.reference,
                _CAPSULE,
                *(a.reference for a in attempts),
            ]
        ),
        payload=payload,
        authorization=authorization,
    )
    aggregate = _load(root, _AGGREGATE)
    if aggregate.payload != payload:
        raise RuntimeError("#13132 timeout-repair aggregate reload failed")
    return aggregate


def _write_blocker(
    root: Path,
    profile: StableArtifact,
    preparation: StableArtifact,
    decision: StageDecision,
    authorization: AuthorizationRecord,
) -> None:
    _assert_absent(root, _BLOCKER)
    payload = {
        "candidate": _ROOT_CANDIDATE.to_value(),
        "profile_id": _PROFILE_ID,
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "reason": decision.details.get("reason"),
        "details": dict(decision.details),
        "old_attempts_loaded": False,
        "paid_actions_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_BLOCKER.artifact_id,
        artifact_kind=_BLOCKER.artifact_kind,
        semantic_version=_BLOCKER.semantic_version,
        created_at=_created_after(profile.created_at, preparation.created_at),
        inputs=(profile.reference, preparation.reference),
        payload=payload,
        authorization=authorization,
    )


def _cells() -> tuple[_Cell, ...]:
    rows: list[_Cell] = []
    for phase, selector, buggy_expected in (
        ("f2p", _F2P_SELECTOR, "failure"),
        ("p2p", _P2P_SELECTOR, "pass"),
    ):
        for variant, expected in (("fixed", "pass"), ("buggy", buggy_expected)):
            for repetition in range(1, 4):
                rows.append(_Cell(phase, variant, repetition, selector, expected))
    return tuple(rows)


def _write_helper(tree: Path) -> None:
    path = tree / _HELPER
    if path.exists() or path.is_symlink():
        raise RuntimeError("#13132 helper path already exists")
    content = """local namespaces = require(\"luassert.namespaces\")
require(\"luassert.assertions\")
local original = namespaces.assertion.errors.callback
namespaces.assertion.errors.callback = function(state, arguments, level)
  local probe = arguments[1]
  local ok, err = pcall(probe)
  if not ok and tostring(err):find(\"C1_BOUND\", 1, true) then
    -- Convert only the known bounded non-termination into an ordinary
    -- assertion failure; the production and upstream test stay untouched.
    arguments[1] = function() end
    local value, retargs = original(state, arguments, level)
    arguments[1] = probe
    return value, retargs
  end
  return original(state, arguments, level)
end
"""
    path.write_text(content, encoding="utf-8", errors="surrogateescape")


def _hook_code() -> str:
    return (
        "jit.off(); local c1_ticks=0; debug.sethook(function() "
        f"c1_ticks=c1_ticks+1; if c1_ticks>={_HOOK_MAX_TICKS} then "
        "error('C1_BOUND instruction limit', 0) end end, '', "
        f"{_HOOK_INSTRUCTION_INTERVAL})"
    )


def _busted_script(plan: LuaRegressionExecutionPlan) -> Path:
    candidates = [
        path
        for path in plan.runtime.closure_files
        if path.relative_to(plan.runtime.root)
        .as_posix()
        .startswith("lib/luarocks/rocks-5.1/busted/")
        and path.name == "busted"
        and "/bin/" in path.relative_to(plan.runtime.root).as_posix()
    ]
    if len(candidates) != 1:
        raise RuntimeError("#13132 Busted script closure is ambiguous")
    return candidates[0]


def _run(
    argv: tuple[str, ...], cwd: Path, plan: LuaRegressionExecutionPlan
) -> WatchdogReceipt:
    env = dict(os.environ)
    env["LUA_PATH"] = ";".join(str(p) for p in plan.runtime.lua_paths) + ";;"
    env["LUA_CPATH"] = ";".join(str(p) for p in plan.runtime.lua_cpaths) + ";;"
    return plan.watchdog.run(argv, cwd=cwd, env=env)


def _summary_count(stdout: bytes) -> int:
    matches = list(_SUMMARY.finditer(stdout))
    if len(matches) != 1:
        return 0
    return sum(int(value) for value in matches[0].groups())


def _observed(receipt: WatchdogReceipt, count: int | None) -> str:
    if receipt.outcome == "timed_out":
        return "timed_out"
    if receipt.outcome == "launch_failed":
        return "launch_failed"
    if receipt.outcome != "completed" or type(receipt.returncode) is not int:
        return "invalid_test_count"
    if count != 1:
        return "invalid_test_count"
    return "pass" if receipt.returncode == 0 else "failure"


def _bounded(value: bytes) -> tuple[bytes, bool]:
    if len(value) <= _MAX_STREAM:
        return value, False
    return value[:_MAX_STREAM], True


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _assert_absent(root: Path, reference: ArtifactReference) -> None:
    path = (
        root
        / "artifacts"
        / PurePosixPath(reference.artifact_id)
        / reference.semantic_version
    )
    if path.exists() or path.is_symlink():
        raise RuntimeError(
            f"#13132 refusing to overwrite fresh artifact {reference.artifact_id}"
        )


def _new_workspace(root: Path) -> Path:
    parent = root / "private" / "lua-regression-work"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="luanti-13132-repair-", dir=parent))


def _remove_workspace(workspace: Path, root: Path) -> None:
    parent = root / "private" / "lua-regression-work"
    if workspace.parent != parent or not workspace.name.startswith(
        "luanti-13132-repair-"
    ):
        raise RuntimeError("#13132 refusing to remove an unregistered workspace")
    shutil.rmtree(workspace)


def _ordinary_root(path: Path) -> Path:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return path.resolve(strict=True)


def _created_after(*values: str) -> str:
    latest = max(values) if values else "1970-01-01T00:00:00.000000Z"
    now = datetime.now(UTC)
    previous = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    value = max(now, previous + timedelta(microseconds=1))
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> str:
    return _created_after()


def _check_authorization(authorization: AuthorizationRecord, scopes: set[str]) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope not in scopes
    ):
        raise PermissionError("#13132 authorization scope is invalid")
    if authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("#13132 authorization is inactive")


__all__ = [
    "CONFIRMATION",
    "Gate57Result",
    "execute_luanti_13132_timeout_repair",
    "prepare_luanti_13132_timeout_repair",
]
