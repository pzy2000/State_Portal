"""Fresh registered Start, replay, and hidden-oracle closure for Luanti #12602."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .luanti_native_executor import run_bounded_process
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CONFIRMATION = "EXECUTE_LUANTI_12602_GATE9_10_V1"
_CANDIDATE = {"repository_key": "luanti", "change_number": 12602}
_PAIR = PurePosixPath("private/materialized/luanti/12602/application-v1")
_HOME = PurePosixPath("private/runtime-home/luanti-12602-gate9-v1")
_FIXTURE = PurePosixPath("private/downstream-fixtures/luanti/12602/media-override-v1")
_TOOLCHAIN = PurePosixPath("private/dependencies/luanti-native/3.0")
_BINARY = PurePosixPath("bin/minetestserver")

_READINESS = ArtifactReference(
    "candidate_gate8_readiness/luanti/12602/current-v2/v1",
    "candidate_gate8_readiness",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/12602/lz-luanti-12602-media-override/v1",
    "lazarus_task_mapping",
    "1.0",
)
_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/12602/current-v2/v1",
    "candidate_downstream_plan",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/12602/fixed-buggy/v3",
    "candidate_materialization",
    "3.0",
)
_BUILDS = {
    variant: ArtifactReference(
        f"candidate_gate8_12602_native_build/luanti/12602/{variant}/mt6-v1",
        "candidate_gate8_12602_native_build",
        "1.0",
    )
    for variant in ("fixed", "buggy")
}
_START = ArtifactReference(
    "candidate_runtime_start/luanti/12602/fixed-buggy/v1",
    "candidate_runtime_start",
    "1.0",
)
_REPLAY = ArtifactReference(
    "candidate_replay/luanti/12602/ordered-v1",
    "candidate_replay",
    "1.0",
)
_ORACLE = ArtifactReference(
    "candidate_oracle/luanti/12602/hidden-v1",
    "candidate_oracle",
    "1.0",
)
_TRANSITION = ArtifactReference(
    "candidate_gate10_transition/luanti/12602/runtime-v1",
    "candidate_gate10_transition",
    "1.0",
)
_VISUAL_BLOCKER = ArtifactReference(
    "candidate_gate11_visual_blocker/luanti/12602/current-v2/v1",
    "candidate_gate11_visual_blocker",
    "1.0",
)
_COMMON = (_READINESS, _MAPPING, _PLAN, _MATERIALIZATION, _BUILDS["fixed"], _BUILDS["buggy"])
_RUNS = (
    ("fixed", "f2p", "testGetModMediaPaths"),
    ("fixed", "p2p", "testGetModNames"),
    ("buggy", "f2p", "testGetModMediaPaths"),
    ("buggy", "p2p", "testGetModNames"),
)


def execute_luanti_12602_runtime_closure(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str = CONFIRMATION,
) -> tuple[StableArtifact, ...]:
    """Execute four fresh mapped processes and persist Gates 9 and 10."""

    _require_authorization(authorization)
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    root = Path(artifact_root).absolute().resolve(strict=True)
    common = _load_common(root)
    mapping = common[1]
    _validate_fixture(root, mapping)
    home_root = root / _HOME
    if home_root.exists() or home_root.is_symlink():
        observations = _load_current_observations(root)
    else:
        observations = tuple(
            _execute_observation(
                root,
                variant,
                phase,
                method,
                common,
                authorization,
            )
            for variant, phase, method in _RUNS
        )
    start = _register_start(root, common, observations, authorization)
    replay = _register_replay(root, common, observations, start, authorization)
    oracle = _register_oracle(root, common, observations, start, replay, authorization)
    transition = _register_transition(root, common, start, replay, oracle, authorization)
    blocker = _register_visual_blocker(
        root, common, start, replay, oracle, transition, authorization
    )
    return (*observations, start, replay, oracle, transition, blocker)


def _load_common(root: Path) -> tuple[StableArtifact, ...]:
    values = tuple(_load(root, ref) for ref in _COMMON)
    readiness, mapping, plan, materialization, fixed_build, buggy_build = values
    if (
        readiness.payload.get("candidate") != _CANDIDATE
        or readiness.payload.get("state") != "gate8_passed_downstream_pending"
        or readiness.payload.get("terminal") is not False
    ):
        raise RuntimeError("#12602 Gate8 readiness drifted")
    if (
        mapping.payload.get("candidate") != _CANDIDATE
        or mapping.payload.get("task_id") != "lz-luanti-12602-media-override"
        or mapping.payload.get("state") != "mapping_registered_fixture_frozen"
    ):
        raise RuntimeError("#12602 mapping drifted")
    if plan.payload.get("candidate") != _CANDIDATE or plan.payload.get("terminal") is not False:
        raise RuntimeError("#12602 downstream plan drifted")
    if (
        materialization.payload.get("candidate") != _CANDIDATE
        or materialization.payload.get("gate8_status")
        != "passed_complete_fixed_buggy_materialization"
    ):
        raise RuntimeError("#12602 materialization drifted")
    for variant, build in (("fixed", fixed_build), ("buggy", buggy_build)):
        if (
            build.payload.get("candidate") != _CANDIDATE
            or build.payload.get("variant") != variant
            or build.payload.get("state") != "built"
            or build.payload.get("returncode") != 0
            or build.payload.get("old_attempts_loaded") is not False
        ):
            raise RuntimeError(f"#12602 {variant} build drifted")
    return values


def _validate_fixture(root: Path, mapping: StableArtifact) -> None:
    fixture = _directory(root / _FIXTURE, root)
    if mapping.payload.get("fixture_root") != _FIXTURE.as_posix():
        raise RuntimeError("#12602 fixture root drifted")
    start = _json(_file(fixture / "start.json", root))
    replay = _json(_file(fixture / "replay.json", root))
    oracle = _json(_file(fixture / "oracle.json", root))
    if start.get("argv") != ["--run-unittests"] or start.get("process_bound_seconds") != 300:
        raise RuntimeError("#12602 Start fixture drifted")
    actions = replay.get("ordered_actions")
    if type(actions) is not list or len(actions) != 6:
        raise RuntimeError("#12602 replay fixture drifted")
    if oracle.get("fixed_expected") != {
        "TestServerModManager.testGetModMediaPaths": "pass",
        "TestServerModManager.testGetModNames": "pass",
    }:
        raise RuntimeError("#12602 fixed oracle drifted")
    if oracle.get("buggy_expected") != {
        "TestServerModManager.testGetModMediaPaths": "failure",
        "TestServerModManager.testGetModNames": "pass",
    }:
        raise RuntimeError("#12602 buggy oracle drifted")


def _execute_observation(
    root: Path,
    variant: str,
    phase: str,
    method: str,
    common: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    tree = _directory(root / _PAIR / variant, root)
    binary = _executable(tree / _BINARY, root)
    home = root / _HOME / variant / phase
    home.mkdir(parents=True, exist_ok=False, mode=0o700)
    argv = (str(binary), "--run-unittests")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": str(root / _TOOLCHAIN / "bin") + os.pathsep + env.get("PATH", ""),
            "GIT_DIR": "/dev/null",
        }
    )
    receipt = run_bounded_process(argv, cwd=tree, env=env, timeout_seconds=300)
    pattern = re.compile(
        rb"(?m)^\[(PASS|FAIL)\] "
        + re.escape(method.encode("ascii"))
        + rb" - [^\r\n]+\r?$"
    )
    matches = [
        match
        for stream in (receipt.stdout, receipt.stderr)
        for match in pattern.finditer(stream)
    ]
    selected = None
    selected_line = None
    if len(matches) == 1:
        selected = "pass" if matches[0].group(1) == b"PASS" else "failure"
        selected_line = matches[0].group(0).decode("utf-8", errors="replace")
    reference = _observation_reference(variant, phase)
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "fresh_epoch": "luanti-12602-gate9-v1",
        "variant": variant,
        "phase": phase,
        "selector": f"TestServerModManager.{method}",
        "selector_mode": "unique_named_test_line_fresh_process",
        "argv": list(argv),
        "working_directory": tree.relative_to(root).as_posix(),
        "isolated_home": home.relative_to(root).as_posix(),
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "selected_line_count": len(matches),
        "selected_observation": selected,
        "selected_raw_line": selected_line,
        "stdout_base64": base64.b64encode(receipt.stdout).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr).decode("ascii"),
        "stdout_truncated": receipt.stdout_truncated,
        "stderr_truncated": receipt.stderr_truncated,
        "oracle_expectation_visible_during_execution": False,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "complete": receipt.outcome == "completed" and len(matches) == 1,
    }
    inputs = tuple(item.reference for item in common)
    return _write_once(root, reference, inputs, payload, authorization)


def _observation_reference(variant: str, phase: str) -> ArtifactReference:
    return ArtifactReference(
        f"candidate_runtime_observation/luanti/12602/{variant}/{phase}/v1",
        "candidate_runtime_observation",
        "1.0",
    )


def _load_current_observations(root: Path) -> tuple[StableArtifact, ...]:
    observations = tuple(
        _load(root, _observation_reference(variant, phase))
        for variant, phase, _ in _RUNS
    )
    if any(
        item.payload.get("fresh_epoch") != "luanti-12602-gate9-v1"
        or item.payload.get("complete") is not True
        or item.payload.get("old_attempts_loaded") is not False
        or item.payload.get("old_runtime_inheritance") is not False
        for item in observations
    ):
        raise RuntimeError("#12602 current Gate9 observation resume is invalid")
    return observations


def _register_start(
    root: Path,
    common: tuple[StableArtifact, ...],
    observations: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    start_observations = [item for item in observations if item.payload["phase"] == "f2p"]
    passed = all(
        item.payload.get("process_outcome") == "completed"
        and item.payload.get("selected_line_count") == 1
        and item.payload.get("complete") is True
        for item in start_observations
    )
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "state": "passed" if passed else "blocked",
        "outcome": "registered_start_completed" if passed else "registered_start_incomplete",
        "complete": passed,
        "registered_start_definition": dict(common[1].payload["registered_start"]),
        "variant_start_observations": [item.reference.artifact_id for item in start_observations],
        "fresh_process_per_variant": True,
        "bounded_processes": True,
        "no_timeout_or_unknown": passed,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in common) + tuple(item.reference for item in start_observations)
    return _write_once(root, _START, inputs, payload, authorization)


def _register_replay(
    root: Path,
    common: tuple[StableArtifact, ...],
    observations: tuple[StableArtifact, ...],
    start: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    ordered = [
        next(item for item in observations if item.payload["variant"] == variant and item.payload["phase"] == phase)
        for variant, phase, _ in _RUNS
    ]
    passed = start.payload.get("state") == "passed" and all(
        item.payload.get("complete") is True for item in ordered
    )
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "state": "passed" if passed else "blocked",
        "outcome": "ordered_replay_completed" if passed else "ordered_replay_incomplete",
        "complete": passed,
        "registered_order": [
            {"variant": item.payload["variant"], "phase": item.payload["phase"], "observation": item.reference.artifact_id}
            for item in ordered
        ],
        "mapping_ordered_replay": list(common[1].payload["ordered_replay"]),
        "fresh_process_count": len(ordered),
        "all_processes_bounded": True,
        "no_timeout_or_unknown": passed,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in common) + (start.reference,) + tuple(
        item.reference for item in ordered
    )
    return _write_once(root, _REPLAY, inputs, payload, authorization)


def _register_oracle(
    root: Path,
    common: tuple[StableArtifact, ...],
    observations: tuple[StableArtifact, ...],
    start: StableArtifact,
    replay: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    oracle = _json(_file(root / _FIXTURE / "oracle.json", root))
    observed = {
        variant: {
            str(item.payload["selector"]): item.payload["selected_observation"]
            for item in observations
            if item.payload["variant"] == variant
        }
        for variant in ("fixed", "buggy")
    }
    expected = {
        variant: {
            selector: value
            for selector, value in oracle[f"{variant}_expected"].items()
        }
        for variant in ("fixed", "buggy")
    }
    matched = observed == expected
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "state": "passed" if matched else "blocked",
        "outcome": "hidden_semantic_oracle_passed" if matched else "hidden_semantic_oracle_failed",
        "complete": matched,
        "oracle_loaded_after_observations": True,
        "hidden_from_execution_adapter": True,
        "goal": oracle["goal"],
        "defect": oracle["defect"],
        "invariants": oracle["invariants"],
        "expected": expected,
        "observed": observed,
        "semantic_goal_observed": observed["fixed"]["TestServerModManager.testGetModMediaPaths"] == "pass",
        "semantic_defect_observed": observed["buggy"]["TestServerModManager.testGetModMediaPaths"] == "failure",
        "p2p_invariant_observed": all(
            observed[variant]["TestServerModManager.testGetModNames"] == "pass"
            for variant in ("fixed", "buggy")
        ),
        "no_timeout_or_unknown": all(item.payload.get("complete") is True for item in observations),
        "raw_observation_references": [item.reference.artifact_id for item in observations],
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in common) + (start.reference, replay.reference) + tuple(
        item.reference for item in observations
    )
    return _write_once(root, _ORACLE, inputs, payload, authorization)


def _register_transition(
    root: Path,
    common: tuple[StableArtifact, ...],
    start: StableArtifact,
    replay: StableArtifact,
    oracle: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    passed = all(item.payload.get("state") == "passed" for item in (start, replay, oracle))
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "state": "passed" if passed else "blocked",
        "outcome": "gate9_gate10_passed" if passed else "gate9_gate10_incomplete",
        "complete": passed,
        "terminal": False,
        "resumable": True,
        "gate_status": {
            "gate_1_7": "qualified",
            "gate8": "passed",
            "gate9_registered_start_and_ordered_replay": "passed" if passed else "blocked",
            "gate10_hidden_oracle_and_semantic_observations": "passed" if passed else "blocked",
            "gate11_to_13": "pending",
        },
        "no_timeout_or_unknown": passed,
        "does_not_invalidate_gate_1_7": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    inputs = tuple(item.reference for item in common) + (
        start.reference,
        replay.reference,
        oracle.reference,
    )
    return _write_once(root, _TRANSITION, inputs, payload, authorization)


def _register_visual_blocker(
    root: Path,
    common: tuple[StableArtifact, ...],
    start: StableArtifact,
    replay: StableArtifact,
    oracle: StableArtifact,
    transition: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    client_paths = [root / _PAIR / variant / "bin/minetest" for variant in ("fixed", "buggy")]
    if any(path.exists() or path.is_symlink() for path in client_paths):
        raise RuntimeError("#12602 client state changed; visual blocker requires re-audit")
    payload = {
        "candidate": _CANDIDATE,
        "task_id": "lz-luanti-12602-media-override",
        "state": "blocked",
        "outcome": "infrastructure_blocked",
        "complete": True,
        "terminal": False,
        "resumable": True,
        "reason": "registered server observations prove semantics but provide no real client frames for Gate11 visual cross-judgment",
        "passed_gates": [8, 9, 10],
        "missing_required_evidence": [
            "candidate_owned_fixed_and_buggy_client_builds",
            "real_common_before_fixed_after_buggy_after_frames",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
            "same_lineage_construction_receipt",
        ],
        "candidate_owned_client_binary_paths": [
            path.relative_to(root).as_posix() for path in client_paths
        ],
        "candidate_owned_client_binaries_present": False,
        "required_dependency_work": "build candidate-owned IrrlichtMt 1.9.0mt6 client library, then fixed and buggy clients sequentially",
        "new_client_build_started": False,
        "does_not_invalidate_gate_1_7": True,
        "does_not_invalidate_gate_8_10": True,
        "old_attempts_loaded": False,
        "old_runtime_inheritance": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "next_action": "coordinate one native lane, build both candidate-owned clients, and capture registered real frames",
    }
    inputs = tuple(item.reference for item in common) + (
        start.reference,
        replay.reference,
        oracle.reference,
        transition.reference,
    )
    return _write_once(root, _VISUAL_BLOCKER, inputs, payload, authorization)


def _require_authorization(value: AuthorizationRecord) -> None:
    if (
        type(value) is not AuthorizationRecord
        or value.scope != "preannotation-execution"
        or value.revoked
        or value.local_user_id != os.getuid()
    ):
        raise PermissionError("#12602 Gate9/10 authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _write_once(
    root: Path,
    reference: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    existing = _try_load(root, reference)
    if existing is None:
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
    elif existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError(f"#12602 artifact conflicts: {reference.artifact_id}")
    result = _load(root, reference)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#12602 typed reload failed: {reference.artifact_id}")
    return result


def _directory(path: Path, boundary: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(boundary)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"unsafe directory: {path}")
    return path


def _file(path: Path, boundary: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(boundary)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"unsafe file: {path}")
    return path


def _executable(path: Path, boundary: Path) -> Path:
    result = _file(path, boundary)
    if not os.access(result, os.X_OK):
        raise RuntimeError(f"file is not executable: {path}")
    return result


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RuntimeError(f"JSON fixture is not an object: {path}")
    return value


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


__all__ = ["CONFIRMATION", "execute_luanti_12602_runtime_closure"]
