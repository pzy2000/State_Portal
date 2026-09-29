"""Bounded local Start/replay/oracle closure for Luanti candidate 15519.

This adapter consumes the persistent fixed/buggy client pair and the existing
server semantic closure.  It runs each local game once under a hard watchdog,
persists raw observations before typed receipts, and keeps visual/golden
requirements pending because the registered null video driver cannot capture
frames.  It never writes a selection terminal or performs a paid operation.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .luanti_15519_version_materialization import (
    Luanti15519ReplayPlanningBinding,
    load_luanti_15519_replay_planning_binding,
)
from .luanti_native_executor import (
    NativeProcessReceipt,
    ProcessRunner,
    run_bounded_process,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


RUNTIME_CONFIRMATION = "EXECUTE_LUANTI_15519_RUNTIME_CLOSURE"
_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_VERSION = "1.0"
_STAGE_VERSION = "2.0"
_WATCHDOG_SECONDS = 60
_MAX_OUTPUT = 262_144
_GAME_ID = "c1_plural"
_BASE_RUNTIME = PurePosixPath("private/luanti-15519-runtime/v1")
_ATTEMPT_KIND = "luanti_runtime_attempt"
_AGGREGATE = ArtifactReference(
    "luanti_runtime_closure/luanti/15519/fixed-buggy/v1",
    "luanti_runtime_closure",
    _VERSION,
)
_REPLAY_STAGE = ArtifactReference(
    "candidate_stage/luanti/15519/replay/v2", "candidate_stage", _STAGE_VERSION
)
_CONSTRUCTION_STAGE = ArtifactReference(
    "candidate_stage/luanti/15519/construction_gate/v2",
    "candidate_stage",
    _STAGE_VERSION,
)
_PENDING_PLAN = ArtifactReference(
    "candidate_downstream_plan/luanti/15519/runtime-visual/v2",
    "candidate_downstream_plan",
    _STAGE_VERSION,
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_SERVER = ArtifactReference(
    "native_server_closure/luanti/15519/fixed-buggy/v1",
    "native_server_closure",
    "1.0",
)
_TASK_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    "2.0",
)
_MISSING_VISUAL = (
    "common_before_fixed_after_and_buggy_after_goldens",
    "pairwise_before_visual_equivalence",
    "two_by_two_after_visual_cross_matrix",
)
_MISSING_CONSTRUCTION = ("same_lineage_construction_receipt",)
_GATE_STATUS = (
    {
        "gate": 8,
        "name": "version_materialization",
        "state": "ready_persistent_pair",
        "evidence": "candidate_materialization/luanti/15519/fixed-buggy/v1",
    },
    {
        "gate": 9,
        "name": "ordered_start_replay",
        "state": "pending_no_client_observation",
        "evidence": None,
    },
    {
        "gate": 10,
        "name": "hidden_semantic_oracle",
        "state": "pending_no_client_fixed_buggy_pair",
        "evidence": "native_server_closure/luanti/15519/fixed-buggy/v1",
    },
    {
        "gate": 11,
        "name": "goldens_visual_cross_matrix",
        "state": "blocked_null_video_has_no_frames",
        "evidence": None,
    },
    {
        "gate": 12,
        "name": "construction_lineage",
        "state": "pending_same_lineage_receipt",
        "evidence": None,
    },
    {
        "gate": 13,
        "name": "preannotation_eligibility_freeze",
        "state": "blocked_until_gates_9_to_12",
        "evidence": None,
    },
)


@dataclass(frozen=True)
class RuntimeClosureResult:
    aggregate: StableArtifact
    replay_stage: StableArtifact
    construction_stage: StableArtifact
    state: Literal["pending", "replay_passed"]
    executed_attempts: int
    resumed_attempts: int


PlanningLoader = Callable[[str | Path], Luanti15519ReplayPlanningBinding]


def execute_luanti_15519_runtime_closure(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    planning_loader: PlanningLoader = load_luanti_15519_replay_planning_binding,
    process_runner: ProcessRunner = run_bounded_process,
) -> RuntimeClosureResult:
    """Run fixed then buggy Start/replay with a strict 60-second watchdog."""

    if confirmation != RUNTIME_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {RUNTIME_CONFIRMATION!r}")
    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = planning_loader(root)
    run = _reserve_run_root(root, Path(run_root).absolute())
    attempts: list[StableArtifact] = []
    executed = 0
    resumed = 0
    try:
        for variant in ("fixed", "buggy"):
            reference = _attempt_reference(variant)
            previous = _try_load(root, reference)
            if previous is not None:
                _validate_attempt(previous, binding, variant, root)
                attempts.append(previous)
                resumed += 1
                continue
            spec = _prepare_variant(root, run, binding, variant)
            receipt = process_runner(
                spec.argv,
                cwd=spec.tree,
                env=spec.environment,
                timeout_seconds=_WATCHDOG_SECONDS,
            )
            log_text = _read_text(spec.log)
            raw_path = _persist_raw_observation(root, spec, receipt, log_text)
            attempt = _write_attempt(
                root,
                binding=binding,
                spec=spec,
                receipt=receipt,
                log_text=log_text,
                raw_path=raw_path,
                authorization=authorization,
            )
            attempts.append(attempt)
            executed += 1
            _remove_variant_tree(spec.tree, run)
            if not _semantic_match(variant, attempt.payload):
                aggregate, replay, construction = _write_downstream(
                    root,
                    binding=binding,
                    attempts=tuple(attempts),
                    state="pending",
                    reason=f"{variant}_semantic_start_incomplete",
                    authorization=authorization,
                )
                return RuntimeClosureResult(
                    aggregate, replay, construction, "pending", executed, resumed
                )
        aggregate, replay, construction = _write_downstream(
            root,
            binding=binding,
            attempts=tuple(attempts),
            state="replay_passed",
            reason=None,
            authorization=authorization,
        )
        return RuntimeClosureResult(
            aggregate, replay, construction, "replay_passed", executed, resumed
        )
    finally:
        for variant in ("fixed", "buggy"):
            tree = run / variant
            if tree.exists() or tree.is_symlink():
                _remove_variant_tree(tree, run)


def write_luanti_15519_runtime_pending_plan(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    planning_loader: PlanningLoader = load_luanti_15519_replay_planning_binding,
) -> StableArtifact:
    """Persist only a downstream plan; never start a candidate process."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = planning_loader(root)
    payload = {
        "candidate": _CANDIDATE,
        "state": "downstream_pending",
        "pair_state": "complete_persistent_pair",
        "replay_planning_ready": True,
        "client_launch_performed": False,
        "candidate_execution_performed": False,
        "visual_capture_supported": False,
        "golden_disposition": "required_no_not_applicable",
        "gate_statuses": [dict(item) for item in _GATE_STATUS],
        "remaining_gates": [
            "ordered_reference_replay_with_actions_and_waits",
            "raw_fixed_and_buggy_start_observations",
            "semantic_goal_defect_and_invariant_observations",
            "common_before_fixed_after_and_buggy_after_goldens",
            "pairwise_before_visual_equivalence",
            "two_by_two_after_visual_cross_matrix",
            "same_lineage_construction_receipt",
            "preannotation_eligibility_freeze",
        ],
        "minimum_next_action": "bounded_non_gui_client_start_or_registered_runtime_adapter",
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    inputs = (
        binding.materialization.reference,
        binding.server_semantics.reference,
        binding.task_mapping.reference,
    )
    existing = _try_load(root, _PENDING_PLAN)
    if existing is not None:
        if existing.inputs != inputs or existing.payload != payload:
            raise RuntimeError("Luanti 15519 runtime pending plan conflicts")
        return existing
    return _write(
        root,
        _PENDING_PLAN,
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )


@dataclass(frozen=True)
class _RunSpec:
    variant: Literal["fixed", "buggy"]
    tree: Path
    world: Path
    config: Path
    log: Path
    argv: tuple[str, ...]
    environment: dict[str, str]


def _prepare_variant(
    root: Path,
    run: Path,
    binding: Luanti15519ReplayPlanningBinding,
    variant: Literal["fixed", "buggy"],
) -> _RunSpec:
    source = binding.fixed_tree if variant == "fixed" else binding.buggy_tree
    tree = run / variant
    shutil.copytree(source, tree, symlinks=False)
    _validate_no_symlink_tree(tree)
    fixture = root / PurePosixPath(str(binding.task_mapping.payload["fixture_root"]))
    target_game = tree / "games" / _GAME_ID
    if target_game.exists() or target_game.is_symlink():
        raise RuntimeError("runtime game fixture conflicts with source tree")
    shutil.copytree(fixture, target_game, symlinks=False)
    _validate_no_symlink_tree(target_game)
    runtime = run / "runtime" / variant
    world = runtime / "world"
    world.mkdir(parents=True, mode=0o700)
    (world / "world.mt").write_text(
        "gameid = c1_plural\nbackend = sqlite3\n"
        "player_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = runtime / "minetest.conf"
    config.write_text(
        "video_driver = null\n"
        "enable_sound = false\n"
        "server_announce = false\n"
        "enable_console = true\n"
        "language = ru\n"
        "name = warptest\n"
        "time_speed = 0\n",
        encoding="utf-8",
    )
    log = runtime / "luanti.log"
    home = runtime / "home"
    home.mkdir(mode=0o700)
    toolchain_bin = root / "private/dependencies/luanti-native/3.0/bin"
    environment = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(toolchain_bin),
        "TMPDIR": str(runtime / "tmp"),
    }
    (runtime / "tmp").mkdir(mode=0o700)
    binary = tree / "bin/luanti"
    _ordinary_file(binary, tree)
    argv = (
        str(binary),
        "--go",
        "--gameid",
        _GAME_ID,
        "--world",
        str(world),
        "--config",
        str(config),
        "--logfile",
        str(log),
    )
    return _RunSpec(variant, tree, world, config, log, argv, environment)


def _semantic_match(variant: str, payload: object) -> bool:
    if type(payload) is not dict:
        return False
    return (
        payload.get("process_outcome") in {"completed", "timed_out"}
        and payload.get("startup_marker_observed") is True
        and payload.get("fixture_loaded") is True
        and payload.get("world_started") is True
        and payload.get("invalid_plural_forms_observed") is (variant == "buggy")
        and payload.get("semantic_match") is True
    )


def _classify(variant: str, text: str) -> dict[str, object]:
    fixture_loaded = "C1 plural fixture loaded" in text
    world_started = (
        "Server created for gameid" in text
        or "Server for gameid=" in text
        or "Game started" in text
        or "Starting server" in text
    )
    invalid = "Invalid Plural-Forms" in text
    translated_russian = "яблоко" in text
    expected_semantics = translated_russian if variant == "fixed" else invalid
    return {
        "startup_marker_observed": fixture_loaded,
        "fixture_loaded": fixture_loaded,
        "world_started": world_started,
        "invalid_plural_forms_observed": invalid,
        "translated_russian_observed": translated_russian,
        "expected_invalid_plural_forms": variant == "buggy",
        "semantic_match": fixture_loaded
        and world_started
        and invalid is (variant == "buggy")
        and expected_semantics,
        "semantic_classification": (
            "fixed_goal_without_plural_parser_error"
            if fixture_loaded
            and world_started
            and not invalid
            and translated_russian
            and variant == "fixed"
            else "buggy_defect_plural_parser_error"
            if fixture_loaded and world_started and invalid and variant == "buggy"
            else "semantic_observation_incomplete"
        ),
    }


def _write_attempt(
    root: Path,
    *,
    binding: Luanti15519ReplayPlanningBinding,
    spec: _RunSpec,
    receipt: NativeProcessReceipt,
    log_text: str,
    raw_path: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    combined = (
        receipt.stdout[:_MAX_OUTPUT]
        + b"\n"
        + receipt.stderr[:_MAX_OUTPUT]
        + b"\n"
        + log_text.encode("utf-8", errors="replace")[:_MAX_OUTPUT]
    )
    payload: dict[str, object] = {
        "candidate": _CANDIDATE,
        "variant": spec.variant,
        "argv": list(spec.argv),
        "watchdog_seconds": _WATCHDOG_SECONDS,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout[:_MAX_OUTPUT]).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr[:_MAX_OUTPUT]).decode("ascii"),
        "log_text": log_text[:_MAX_OUTPUT],
        "raw_observation_path": raw_path,
        "output_encoding": "base64",
        "stdout_truncated": receipt.stdout_truncated
        or len(receipt.stdout) > _MAX_OUTPUT,
        "stderr_truncated": receipt.stderr_truncated
        or len(receipt.stderr) > _MAX_OUTPUT,
        "complete": True,
    }
    payload.update(_classify(spec.variant, combined.decode("utf-8", errors="replace")))
    reference = _attempt_reference(spec.variant)
    return _write(
        root,
        reference,
        inputs=(
            binding.materialization.reference,
            binding.server_semantics.reference,
            binding.task_mapping.reference,
        ),
        payload=payload,
        authorization=authorization,
    )


def _write_downstream(
    root: Path,
    *,
    binding: Luanti15519ReplayPlanningBinding,
    attempts: tuple[StableArtifact, ...],
    state: Literal["pending", "replay_passed"],
    reason: str | None,
    authorization: AuthorizationRecord,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    aggregate_inputs = (
        binding.materialization.reference,
        binding.server_semantics.reference,
        binding.task_mapping.reference,
        *(attempt.reference for attempt in attempts),
    )
    semantic_pair = len(attempts) == 2 and all(
        _semantic_match(item.payload.get("variant", ""), item.payload)
        for item in attempts
    )
    aggregate_payload = {
        "candidate": _CANDIDATE,
        "attempt_count": len(attempts),
        "fixed_buggy_semantic_pair": semantic_pair,
        "state": "replay_passed"
        if semantic_pair and state == "replay_passed"
        else "downstream_pending",
        "reason": reason,
        "client_execution_performed": bool(attempts),
        "visual_capture_supported": False,
        "golden_disposition": "required_no_not_applicable",
        "missing_required_evidence": list(_MISSING_VISUAL + _MISSING_CONSTRUCTION),
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    aggregate = _write(
        root,
        _AGGREGATE,
        inputs=aggregate_inputs,
        payload=aggregate_payload,
        authorization=authorization,
    )
    replay_inputs = (*aggregate_inputs, aggregate.reference)
    replay_payload = {
        "candidate": _CANDIDATE,
        "stage": "replay",
        "state": "passed" if semantic_pair else "downstream_pending",
        "outcome": None if semantic_pair else "semantic_start_incomplete",
        "ordered_replay": semantic_pair,
        "hidden_oracle": semantic_pair,
        "raw_start_observations": len(attempts) == 2,
        "visual_capture_supported": False,
        "missing_required_evidence": list(_MISSING_VISUAL + _MISSING_CONSTRUCTION),
        "complete": True,
    }
    replay = _write(
        root,
        _REPLAY_STAGE,
        inputs=replay_inputs,
        payload=replay_payload,
        authorization=authorization,
    )
    construction_inputs = (replay.reference, aggregate.reference)
    construction_payload = {
        "candidate": _CANDIDATE,
        "stage": "construction_gate",
        "state": "downstream_pending",
        "outcome": None,
        "replay_state": replay_payload["state"],
        "semantic_oracle_state": "passed" if semantic_pair else "pending",
        "golden_state": "pending_null_video_frame_capture",
        "construction_lineage_state": "pending",
        "missing_required_evidence": list(_MISSING_VISUAL + _MISSING_CONSTRUCTION),
        "permanent_terminal_written": False,
        "paid_actions_authorized": False,
        "complete": True,
    }
    construction = _write(
        root,
        _CONSTRUCTION_STAGE,
        inputs=construction_inputs,
        payload=construction_payload,
        authorization=authorization,
    )
    return aggregate, replay, construction


def load_luanti_15519_runtime_closure(
    artifact_root: str | Path,
) -> RuntimeClosureResult:
    """Strictly reload runtime, replay and construction receipts."""

    root = _ordinary_root(Path(artifact_root).absolute())
    aggregate = _load(root, _AGGREGATE)
    attempts = tuple(
        _load(root, _attempt_reference(variant)) for variant in ("fixed", "buggy")
    )
    binding = load_luanti_15519_replay_planning_binding(root)
    for attempt, variant in zip(attempts, ("fixed", "buggy"), strict=True):
        _validate_attempt(attempt, binding, variant, root)
    expected_inputs = (
        binding.materialization.reference,
        binding.server_semantics.reference,
        binding.task_mapping.reference,
        *(item.reference for item in attempts),
    )
    if (
        aggregate.inputs != expected_inputs
        or aggregate.payload.get("complete") is not True
    ):
        raise RuntimeError("Luanti 15519 runtime aggregate binding is invalid")
    replay = _load(root, _REPLAY_STAGE)
    if replay.inputs != (*expected_inputs, aggregate.reference):
        raise RuntimeError("Luanti 15519 replay stage binding is invalid")
    construction = _load(root, _CONSTRUCTION_STAGE)
    if construction.inputs != (replay.reference, aggregate.reference):
        raise RuntimeError("Luanti 15519 construction stage binding is invalid")
    state = "replay_passed" if replay.payload.get("state") == "passed" else "pending"
    return RuntimeClosureResult(aggregate, replay, construction, state, 0, 2)


def _validate_attempt(
    artifact: StableArtifact,
    binding: Luanti15519ReplayPlanningBinding,
    variant: str,
    root: Path,
) -> None:
    if (
        artifact.reference != _attempt_reference(variant)
        or artifact.inputs
        != (
            binding.materialization.reference,
            binding.server_semantics.reference,
            binding.task_mapping.reference,
        )
        or artifact.payload.get("candidate") != _CANDIDATE
        or artifact.payload.get("variant") != variant
        or artifact.payload.get("complete") is not True
        or type(artifact.payload.get("raw_observation_path")) is not str
    ):
        raise RuntimeError("Luanti 15519 runtime attempt binding is invalid")
    raw = root / PurePosixPath(str(artifact.payload["raw_observation_path"]))
    _ordinary_file(raw, root)


def _attempt_reference(variant: str) -> ArtifactReference:
    if variant not in {"fixed", "buggy"}:
        raise ValueError("Luanti 15519 runtime variant is invalid")
    return ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/15519/{variant}/v1", _ATTEMPT_KIND, _VERSION
    )


def _persist_raw_observation(
    root: Path,
    spec: _RunSpec,
    receipt: NativeProcessReceipt,
    log_text: str,
) -> str:
    parent = root / _BASE_RUNTIME / "observations"
    _make_directory(parent, root)
    path = parent / f"{spec.variant}-current.json"
    document = {
        "candidate": _CANDIDATE,
        "variant": spec.variant,
        "argv": list(spec.argv),
        "watchdog_seconds": _WATCHDOG_SECONDS,
        "process_outcome": receipt.outcome,
        "returncode": receipt.returncode,
        "stdout_base64": base64.b64encode(receipt.stdout[:_MAX_OUTPUT]).decode("ascii"),
        "stderr_base64": base64.b64encode(receipt.stderr[:_MAX_OUTPUT]).decode("ascii"),
        "log_text": log_text[:_MAX_OUTPUT],
        "output_encoding": "base64",
    }
    temporary = Path(tempfile.mkstemp(prefix=".raw-", dir=parent)[1])
    try:
        temporary.write_text(
            json.dumps(document, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path.relative_to(root).as_posix()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _write(
    root: Path,
    reference: ArtifactReference,
    *,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
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
    if artifact.inputs != inputs or artifact.payload != payload:
        raise RuntimeError("Luanti 15519 runtime receipt typed reload failed")
    return artifact


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
        if str(error) in {
            "artifact directory is missing",
            "artifact current head is missing",
        }:
            return None
        raise


def _reserve_run_root(root: Path, requested: Path) -> Path:
    allowed = root / _BASE_RUNTIME
    _make_directory(allowed, root)
    try:
        requested.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError(
            "Luanti 15519 runtime run root escapes registered root"
        ) from error
    if requested.parent != allowed or not requested.name:
        raise RuntimeError("Luanti 15519 runtime run root is invalid")
    if requested.exists() or requested.is_symlink():
        raise RuntimeError("Luanti 15519 runtime run root already exists")
    requested.mkdir(mode=0o700)
    return requested


def _remove_variant_tree(tree: Path, run: Path) -> None:
    if tree.parent != run or tree.name not in {"fixed", "buggy"}:
        raise RuntimeError("Luanti 15519 runtime cleanup path is unsafe")
    if tree.is_symlink() or not tree.is_dir():
        raise RuntimeError("Luanti 15519 runtime tree is unsafe")
    shutil.rmtree(tree)


def _make_directory(path: Path, root: Path) -> None:
    path.relative_to(root)
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("Luanti 15519 runtime directory is unsafe")
        else:
            current.mkdir(mode=0o700)


def _ordinary_root(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError("artifact root is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("artifact root is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required runtime file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required runtime file is missing or unsafe")
    return resolved


def _validate_no_symlink_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("runtime candidate tree is unsafe")
    for item in path.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise RuntimeError("runtime candidate tree contains an unsafe entry")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "RUNTIME_CONFIRMATION",
    "RuntimeClosureResult",
    "execute_luanti_15519_runtime_closure",
    "load_luanti_15519_runtime_closure",
    "write_luanti_15519_runtime_pending_plan",
]
