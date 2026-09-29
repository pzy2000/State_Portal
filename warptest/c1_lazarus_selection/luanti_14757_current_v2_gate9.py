"""One fresh current-v2 Gate 9/10 attempt for Luanti #14757.

This adapter is intentionally narrower than the historical #14757 bounded
launch runner.  It binds the current Gate 1--7 aggregate/transition, the v4
task mapping, and the immutable repackage-v2 receipt.  It never loads the old
bounded-launch attempts, old construction receipts, or protected PIDs.  The
fixed application is run first; a UE/timeout/missing semantic marker fails
closed and writes a non-terminal blocker without starting the buggy side.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = {"repository_key": "luanti", "change_number": 14757}
EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_14757_CURRENT_V2_GATE9"
_V1 = "1.0"
_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
_WATCHDOG_SECONDS = 60
_MAX_OUTPUT = 262_144
_PROTECTED_PIDS = (20946, 21627, 30865, 70361, 60839)
_RUN_PARENT = PurePosixPath("private/bounded-runs/luanti/14757")
_REPACKAGE_NAMESPACE = "repackage-v2"
_ATTEMPT_NAMESPACE: str | None = None

_GATE_AGGREGATE = ArtifactReference(
    "lua_regression_aggregate/luanti/14757/fixed-buggy/v3",
    "lua_regression_aggregate",
    _V3,
)
_GATE_TRANSITION = ArtifactReference(
    "luanti_lua_regression_transition/luanti/14757/test-repetitions/v2",
    "luanti_lua_regression_transition",
    _V2,
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    _V4,
)
_REPACKAGE = ArtifactReference(
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v2/v1",
    "candidate_downstream_repackage_receipt",
    _V1,
)
_PREFLIGHT = ArtifactReference(
    "candidate_gate9_current_v2_preflight/luanti/14757/repackage-v2/v1",
    "candidate_gate9_current_v2_preflight",
    _V1,
)
_ATTEMPT_KIND = "candidate_gate9_current_v2_attempt"
_OBSERVATION = ArtifactReference(
    "candidate_gate10_current_v2_observation/luanti/14757/repackage-v2/v1",
    "candidate_gate10_current_v2_observation",
    _V1,
)
_REPLAY = ArtifactReference(
    "candidate_gate9_current_v2_replay/luanti/14757/repackage-v2/v1",
    "candidate_gate9_current_v2_replay",
    _V1,
)
_ORACLE = ArtifactReference(
    "candidate_gate10_current_v2_oracle/luanti/14757/repackage-v2/v1",
    "candidate_gate10_current_v2_oracle",
    _V1,
)
_BLOCKER = ArtifactReference(
    "candidate_gate9_current_v2_blocker/luanti/14757/repackage-v2/v1",
    "candidate_gate9_current_v2_blocker",
    _V1,
)
_EVIDENCE_LOSS = ArtifactReference(
    "candidate_gate9_current_v2_evidence_loss/luanti/14757/repackage-v2/v1",
    "candidate_gate9_current_v2_evidence_loss",
    _V1,
)

_START_RE = re.compile(r"C1_LAZARUS_14757_START marker=(\S+) result_empty=(true|false)")
_ACTION_RE = re.compile(
    r"C1_LAZARUS_14757_ACTION command=(\S+) count=([0-9]+) "
    r"result=(\S+) ok=(true|false)"
)


@dataclass(frozen=True)
class CurrentV2Binding:
    gate_aggregate: StableArtifact
    gate_transition: StableArtifact
    mapping: StableArtifact
    repackage: StableArtifact
    fixture: Path
    fixed_app: Path
    buggy_app: Path


@dataclass(frozen=True)
class StartSpec:
    variant: Literal["fixed", "buggy"]
    app_root: Path
    binary: Path
    fixture: Path
    run_root: Path
    world: Path
    config: Path
    log: Path
    watchdog_seconds: int


@dataclass(frozen=True)
class StartObservation:
    process_outcome: Literal["completed", "launch_failed", "timed_out"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    start_marker: str | None
    start_result_empty: bool | None
    action_command: str | None
    action_count: int
    semantic_result: str | None
    action_ok: bool | None


@dataclass(frozen=True)
class Gate9Result:
    preflight: StableArtifact
    attempts: tuple[StableArtifact, ...]
    observation: StableArtifact | None
    replay: StableArtifact | None
    oracle: StableArtifact | None
    blocker: StableArtifact | None


def execute_luanti_14757_current_v2_gate9(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    watchdog_seconds: int = _WATCHDOG_SECONDS,
) -> Gate9Result:
    """Run at most one fixed/buggy current-v2 Start/replay pair."""

    _require_authorization(authorization)
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(watchdog_seconds) is not int or not 5 <= watchdog_seconds <= 600:
        raise ValueError("bounded launch watchdog is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    binding = load_current_v2_binding(root)
    preflight = write_current_v2_preflight(
        root,
        binding=binding,
        run_root=Path(run_root).absolute(),
        watchdog_seconds=watchdog_seconds,
        authorization=authorization,
    )
    run = _reserve_run_root(root, Path(run_root).absolute())
    attempts: list[StableArtifact] = []
    try:
        for variant in ("fixed", "buggy"):
            spec = _prepare_variant(root, run, binding, variant, watchdog_seconds)
            observation = _run_start(spec)
            raw_path = _persist_raw_observation(root, spec, observation)
            attempt = _write_attempt(
                root,
                binding=binding,
                spec=spec,
                observation=observation,
                raw_path=raw_path,
                authorization=authorization,
            )
            attempts.append(attempt)
            _remove_variant_tree(spec.run_root, run)
            if not _semantic_match(variant, observation):
                blocker = _write_blocker(
                    root,
                    binding=binding,
                    preflight=preflight,
                    attempts=tuple(attempts),
                    failed_variant=variant,
                    observation=observation,
                    authorization=authorization,
                )
                return Gate9Result(
                    preflight, tuple(attempts), None, None, None, blocker
                )
        observation_artifact = _write_observation(
            root,
            binding=binding,
            preflight=preflight,
            attempts=tuple(attempts),
            authorization=authorization,
        )
        replay = _write_replay(
            root,
            binding=binding,
            preflight=preflight,
            attempts=tuple(attempts),
            observation=observation_artifact,
            authorization=authorization,
        )
        oracle = _write_oracle(
            root,
            binding=binding,
            replay=replay,
            observation=observation_artifact,
            attempts=tuple(attempts),
            authorization=authorization,
        )
        return Gate9Result(
            preflight, tuple(attempts), observation_artifact, replay, oracle, None
        )
    finally:
        for variant in ("fixed", "buggy"):
            tree = run / variant
            if tree.exists() or tree.is_symlink():
                _remove_variant_tree(tree, run)


def load_current_v2_binding(artifact_root: str | Path) -> CurrentV2Binding:
    """Strictly load current Gate 1--7, mapping, repackage, and fixture."""

    root = _ordinary_root(Path(artifact_root).absolute())
    aggregate = _load(root, _GATE_AGGREGATE)
    transition = _load(root, _GATE_TRANSITION)
    mapping = _load(root, _MAPPING)
    repackage = _load(root, _REPACKAGE)
    _validate_gate(aggregate, transition)
    _validate_mapping(mapping)
    fixture_rel = mapping.payload["fixture_root"]
    fixture = _ordinary_directory(root / PurePosixPath(fixture_rel), root)
    for relative in ("mods/c1_lazarus/init.lua", "mods/c1_lazarus/mod.conf"):
        _ordinary_file(fixture / PurePosixPath(relative), fixture)
    _validate_fixture_contract(fixture)
    payload = repackage.payload
    if (
        payload.get("candidate") != CANDIDATE
        or payload.get("outcome") != "static_packaging_qualified"
        or payload.get("state") != "downstream_pending"
        or payload.get("fresh_namespace")
        != f"private/candidate_downstream/luanti/14757/{_REPACKAGE_NAMESPACE}"
        or payload.get("fresh_copy_status") != "succeeded"
        or payload.get("source_pair_status") != "complete_launch_unverified"
        or payload.get("downstream_start_admission", {}).get("start_executed")
        is not False
        or payload.get("downstream_start_admission", {}).get("gate_9_claimed")
        is not False
        or payload.get("original_pair_modified") is not False
        or payload.get("old_attempts_loaded") is not False
        or tuple(payload.get("do_not_touch_pids", ())) != _PROTECTED_PIDS
    ):
        raise RuntimeError("#14757 repackage-v2 receipt is not current-v2 eligible")
    paths = {
        "fixed": root / PurePosixPath(payload["binding_application_fixed_path"])
        if "binding_application_fixed_path" in payload
        else root
        / f"private/candidate_downstream/luanti/14757/{_REPACKAGE_NAMESPACE}/fixed-minetest.app",
        "buggy": root / PurePosixPath(payload["binding_application_buggy_path"])
        if "binding_application_buggy_path" in payload
        else root
        / f"private/candidate_downstream/luanti/14757/{_REPACKAGE_NAMESPACE}/buggy-minetest.app",
    }
    for app in paths.values():
        _ordinary_directory(app, root)
        binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
        if not os.access(binary, os.X_OK):
            raise RuntimeError("#14757 current-v2 repackage binary is not executable")
        if not (app / "Contents/Resources/client/shaders/Irrlicht").is_symlink():
            raise RuntimeError("#14757 current-v2 repackage resource repair is missing")
    if paths["fixed"] == paths["buggy"]:
        raise RuntimeError("#14757 current-v2 fixed/buggy apps are not distinct")
    return CurrentV2Binding(
        aggregate,
        transition,
        mapping,
        repackage,
        fixture,
        paths["fixed"],
        paths["buggy"],
    )


def execute_luanti_14757_current_v2_gate9_namespace(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    namespace: Literal["repackage-v2", "repackage-v3"],
    watchdog_seconds: int = _WATCHDOG_SECONDS,
) -> Gate9Result:
    """Run one isolated Gate 9/10 epoch against a registered repackage.

    The original v2 runner remains immutable by default.  This narrow
    namespace adapter is used only for the separately typed repackage-v3
    receipt; all attempt, observation, replay, oracle, and blocker heads are
    written below that namespace so no v2 attempt can be overwritten.
    """

    if namespace == "repackage-v2":
        return execute_luanti_14757_current_v2_gate9(
            artifact_root,
            run_root=run_root,
            authorization=authorization,
            confirmation=confirmation,
            watchdog_seconds=watchdog_seconds,
        )
    if namespace != "repackage-v3":
        raise ValueError("unsupported current-v2 repackage namespace")

    global \
        _REPACKAGE_NAMESPACE, \
        _ATTEMPT_NAMESPACE, \
        _REPACKAGE, \
        _PREFLIGHT, \
        _OBSERVATION, \
        _REPLAY, \
        _ORACLE, \
        _BLOCKER
    saved = (
        _REPACKAGE_NAMESPACE,
        _ATTEMPT_NAMESPACE,
        _REPACKAGE,
        _PREFLIGHT,
        _OBSERVATION,
        _REPLAY,
        _ORACLE,
        _BLOCKER,
    )
    prefix = f"candidate_downstream_repackage_receipt/luanti/14757/{namespace}/v1"
    _REPACKAGE_NAMESPACE = namespace
    _ATTEMPT_NAMESPACE = namespace
    _REPACKAGE = ArtifactReference(
        prefix, "candidate_downstream_repackage_receipt", _V1
    )
    _PREFLIGHT = ArtifactReference(
        f"candidate_gate9_current_v2_preflight/luanti/14757/{namespace}/v1",
        "candidate_gate9_current_v2_preflight",
        _V1,
    )
    _OBSERVATION = ArtifactReference(
        f"candidate_gate10_current_v2_observation/luanti/14757/{namespace}/v1",
        "candidate_gate10_current_v2_observation",
        _V1,
    )
    _REPLAY = ArtifactReference(
        f"candidate_gate9_current_v2_replay/luanti/14757/{namespace}/v1",
        "candidate_gate9_current_v2_replay",
        _V1,
    )
    _ORACLE = ArtifactReference(
        f"candidate_gate10_current_v2_oracle/luanti/14757/{namespace}/v1",
        "candidate_gate10_current_v2_oracle",
        _V1,
    )
    _BLOCKER = ArtifactReference(
        f"candidate_gate9_current_v2_blocker/luanti/14757/{namespace}/v1",
        "candidate_gate9_current_v2_blocker",
        _V1,
    )
    try:
        return execute_luanti_14757_current_v2_gate9(
            artifact_root,
            run_root=run_root,
            authorization=authorization,
            confirmation=confirmation,
            watchdog_seconds=watchdog_seconds,
        )
    finally:
        (
            _REPACKAGE_NAMESPACE,
            _ATTEMPT_NAMESPACE,
            _REPACKAGE,
            _PREFLIGHT,
            _OBSERVATION,
            _REPLAY,
            _ORACLE,
            _BLOCKER,
        ) = saved


def write_current_v2_preflight(
    artifact_root: str | Path,
    *,
    binding: CurrentV2Binding,
    run_root: str | Path,
    watchdog_seconds: int,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    root = _ordinary_root(Path(artifact_root).absolute())
    run = Path(run_root).absolute()
    allowed = root / _RUN_PARENT
    try:
        run.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError(
            "current-v2 run root is outside registered parent"
        ) from error
    if run.exists() or run.is_symlink():
        raise RuntimeError("current-v2 run root already exists")
    payload = {
        "candidate": CANDIDATE,
        "state": "ready_for_single_bounded_start_replay",
        "terminal": False,
        "resumable": True,
        "current_v2_only": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "watchdog_seconds": watchdog_seconds,
        "run_root": run.relative_to(root).as_posix(),
        "cleanup": {
            "owned_process_group_only": True,
            "sigterm_then_sigkill": True,
            "run_root_cleanup_after_each_variant": True,
        },
        "protected_pids": list(_PROTECTED_PIDS),
        "fixture_root": binding.mapping.payload["fixture_root"],
        "fresh_repackage_namespace": binding.repackage.payload["fresh_namespace"],
        "gate9_status": "pending",
        "gate10_status": "pending",
        "gate11_to_13_status": "pending",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
    }
    inputs = (
        binding.gate_aggregate.reference,
        binding.gate_transition.reference,
        binding.mapping.reference,
        binding.repackage.reference,
    )
    existing = _try_load(root, _PREFLIGHT)
    if existing is None:
        _write(root, _PREFLIGHT, inputs, payload, authorization)
        existing = _load(root, _PREFLIGHT)
    if existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#14757 current-v2 preflight drifted")
    return existing


def write_current_v2_evidence_loss_note(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Record the first-run raw-retention defect without rewriting evidence.

    The initial runner removed the variant scratch tree together with its raw
    observation.  This note is deliberately separate from the immutable
    attempt and blocker heads: it records the missing raw file and prevents
    anyone from treating a reconstructed payload as a real observation.
    """

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    attempt = _load(
        root,
        ArtifactReference(
            f"{_ATTEMPT_KIND}/luanti/14757/fixed/1/v1",
            _ATTEMPT_KIND,
            _V1,
        ),
    )
    blocker = _load(root, _BLOCKER)
    raw_relative = attempt.payload.get("raw_observation_path")
    raw_exists = (
        isinstance(raw_relative, str) and (root / PurePosixPath(raw_relative)).is_file()
    )
    payload = {
        "candidate": CANDIDATE,
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "terminal": False,
        "resumable": True,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "attempt_reference": attempt.reference.artifact_id,
        "blocker_reference": blocker.reference.artifact_id,
        "raw_observation_path": raw_relative,
        "raw_observation_retained": raw_exists,
        "raw_observation_loss": {
            "observed": raw_exists is False,
            "cause": "variant_cleanup_removed_run_subtree_after_typed_attempt_write",
            "scope": "first_current_v2_fixed_attempt_only",
            "no_reconstruction_performed": True,
        },
        "fixed_attempt_summary": {
            "process_outcome": attempt.payload.get("process_outcome"),
            "returncode": attempt.payload.get("returncode"),
            "start_marker": attempt.payload.get("start_observation", {}).get("marker"),
            "action_marker": attempt.payload.get("action_observation", {}).get(
                "command"
            ),
            "semantic_match": attempt.payload.get("semantic_match"),
        },
        "next_action": "use_retention_fixed_runner_for_any_future_authorized_attempt; do_not_reinterpret_this_attempt",
        "protected_pids": list(_PROTECTED_PIDS),
        "complete": True,
    }
    inputs = (attempt.reference, blocker.reference)
    _write(root, _EVIDENCE_LOSS, inputs, payload, authorization)
    artifact = _load(root, _EVIDENCE_LOSS)
    if artifact.inputs != inputs or dict(artifact.payload) != payload:
        raise RuntimeError("#14757 evidence-loss note typed reload failed")
    return artifact


def _validate_gate(aggregate: StableArtifact, transition: StableArtifact) -> None:
    cells = aggregate.payload.get("cells")
    expected_cells = [
        {"observed_outcomes": ["failure"] * 3, "phase": "f2p", "variant": "buggy"},
        {"observed_outcomes": ["pass"] * 3, "phase": "f2p", "variant": "fixed"},
        {"observed_outcomes": ["pass"] * 3, "phase": "p2p", "variant": "buggy"},
        {"observed_outcomes": ["pass"] * 3, "phase": "p2p", "variant": "fixed"},
    ]
    if (
        aggregate.payload.get("candidate") != CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("all_expected") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("stable_repetitions") is not True
        or cells != expected_cells
    ):
        raise RuntimeError("#14757 current Gate 1--7 aggregate is not qualified")
    if (
        transition.payload.get("candidate") != CANDIDATE
        or transition.payload.get("qualified") is not True
        or transition.payload.get("truth_table")
        != {"buggy": {"f2p": False, "p2p": True}, "fixed": {"f2p": True, "p2p": True}}
    ):
        raise RuntimeError("#14757 current Gate 1--7 transition is not qualified")


def _validate_mapping(mapping: StableArtifact) -> None:
    p = mapping.payload
    replay = p.get("ordered_replay")
    if (
        p.get("candidate") != CANDIDATE
        or p.get("complete") is not True
        or p.get("candidate_execution_performed") is not False
        or not isinstance(p.get("registered_start"), dict)
        or not isinstance(replay, list)
        or replay
        != [
            {"type": "restore_registered_start"},
            {
                "command": "/c1_lazarus_round",
                "type": "luanti_worldmod_math_round_boundary",
            },
        ]
        or not isinstance(p.get("semantic_oracle"), dict)
    ):
        raise RuntimeError("#14757 current mapping is not registered")


def _validate_fixture_contract(fixture: Path) -> None:
    mod = (fixture / "mods/c1_lazarus/init.lua").read_text(encoding="utf-8")
    if "C1_LAZARUS_14757_START" not in mod or "C1_LAZARUS_14757_ACTION" not in mod:
        raise RuntimeError("#14757 fixture lacks registered semantic markers")


def _prepare_variant(
    root: Path,
    run: Path,
    binding: CurrentV2Binding,
    variant: Literal["fixed", "buggy"],
    watchdog_seconds: int,
) -> StartSpec:
    app = binding.fixed_app if variant == "fixed" else binding.buggy_app
    target = run / variant
    target.mkdir(parents=True, mode=0o700)
    world = target / "world"
    mod = world / "worldmods/c1_lazarus"
    mod.parent.mkdir(parents=True, mode=0o700)
    shutil.copytree(binding.fixture / "mods/c1_lazarus", mod, symlinks=False)
    (world / "world.mt").write_text(
        "gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\ncreative_mode = true\nenable_damage = false\nscreen_w = 1280\nscreen_h = 720\nfullscreen = false\nwindow_maximized = false\ntime_speed = 0\n",
        encoding="utf-8",
    )
    log = target / "luanti.log"
    binary = app / "Contents/MacOS/minetest"
    return StartSpec(
        variant,
        app,
        binary,
        binding.fixture,
        target,
        world,
        config,
        log,
        watchdog_seconds,
    )


def _run_start(spec: StartSpec) -> StartObservation:
    stdout_path = spec.run_root / "process.stdout"
    stderr_path = spec.run_root / "process.stderr"
    argv = (
        str(spec.binary),
        "--go",
        "--gameid",
        "devtest",
        "--world",
        str(spec.world),
        "--name",
        "warptest",
        "--config",
        str(spec.config),
        "--logfile",
        str(spec.log),
    )
    env = dict(os.environ)
    home = spec.run_root / "home"
    home.mkdir(mode=0o700)
    env.update({"HOME": str(home), "LANG": "C", "LC_ALL": "C"})
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(
                argv,
                cwd=spec.app_root,
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
    except OSError as error:
        return StartObservation(
            "launch_failed",
            None,
            b"",
            str(error).encode(),
            None,
            None,
            None,
            0,
            None,
            None,
        )
    deadline = time.monotonic() + spec.watchdog_seconds
    action_sent = False
    start_seen: float | None = None
    timed_out = False
    driver_error: str | None = None
    try:
        while time.monotonic() < deadline:
            text = _read_log(spec.log)
            if _START_RE.search(text) and start_seen is None:
                start_seen = time.monotonic()
            if (
                start_seen is not None
                and time.monotonic() - start_seen >= 2.0
                and not action_sent
            ):
                from warptest.native_input import QuartzNativeInput

                native = QuartzNativeInput(process.pid)
                native.key("/")
                native.type_text("c1_lazarus_round")
                native.key("enter")
                action_sent = True
            if _ACTION_RE.search(text) or process.poll() is not None:
                break
            time.sleep(0.05)
        else:
            timed_out = True
    except Exception as error:  # fail-closed typed observation below
        driver_error = f"{type(error).__name__}: {error}"
    finally:
        _stop_owned_process_group(process)
    stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
    stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
    if driver_error:
        stderr += ("\n" + driver_error).encode("utf-8", errors="replace")
    text = _read_log(spec.log)
    start = _START_RE.search(text)
    action = _ACTION_RE.search(text)
    return StartObservation(
        "launch_failed" if driver_error else "timed_out" if timed_out else "completed",
        process.returncode,
        stdout,
        stderr,
        start.group(1) if start else None,
        start.group(2) == "true" if start else None,
        action.group(1) if action else None,
        int(action.group(2)) if action else 0,
        action.group(3) if action else None,
        action.group(4) == "true" if action else None,
    )


def _semantic_match(variant: str, observation: StartObservation) -> bool:
    return (
        observation.process_outcome == "completed"
        and observation.start_marker == "basenodes:cobble"
        and observation.start_result_empty is True
        and observation.action_command == "/c1_lazarus_round"
        and observation.action_count == 1
        and observation.semantic_result is not None
        and (
            observation.semantic_result == "0,0"
            if variant == "fixed"
            else observation.semantic_result != "0,0"
        )
        and (
            observation.action_ok is True
            if variant == "fixed"
            else observation.action_ok is False
        )
    )


def _persist_raw_observation(
    root: Path, spec: StartSpec, observation: StartObservation
) -> str:
    # The per-variant directory is scratch and is removed after process-group
    # cleanup. Keep raw evidence in a sibling observations directory so the
    # typed attempt/blocker never points at a deleted file.
    path = spec.run_root.parent / "observations" / f"{spec.variant}.json"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    value = asdict(observation)
    value["stdout"] = base64.b64encode(observation.stdout[:_MAX_OUTPUT]).decode("ascii")
    value["stderr"] = base64.b64encode(observation.stderr[:_MAX_OUTPUT]).decode("ascii")
    value["stdout_truncated"] = len(observation.stdout) > _MAX_OUTPUT
    value["stderr_truncated"] = len(observation.stderr) > _MAX_OUTPUT
    value["output_encoding"] = "base64"
    _atomic_json(path, value)
    return path.relative_to(root).as_posix()


def _write_attempt(
    root: Path,
    *,
    binding: CurrentV2Binding,
    spec: StartSpec,
    observation: StartObservation,
    raw_path: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    attempt_namespace = (
        f"/{_ATTEMPT_NAMESPACE}" if _ATTEMPT_NAMESPACE is not None else ""
    )
    ref = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14757{attempt_namespace}/{spec.variant}/1/v1",
        _ATTEMPT_KIND,
        _V1,
    )
    payload = {
        "candidate": CANDIDATE,
        "variant": spec.variant,
        "run_root": spec.run_root.relative_to(root).as_posix(),
        "fresh_repackage_namespace": binding.repackage.payload["fresh_namespace"],
        "watchdog_seconds": spec.watchdog_seconds,
        "process_outcome": observation.process_outcome,
        "returncode": observation.returncode,
        "start_observation": {
            "marker": observation.start_marker,
            "result_empty": observation.start_result_empty,
        },
        "action_observation": {
            "command": observation.action_command,
            "count": observation.action_count,
            "result": observation.semantic_result,
            "ok": observation.action_ok,
        },
        "raw_observation_path": raw_path,
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "semantic_match": _semantic_match(spec.variant, observation),
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        ref,
        (
            binding.gate_aggregate.reference,
            binding.gate_transition.reference,
            binding.mapping.reference,
            binding.repackage.reference,
        ),
        payload,
        authorization,
    )


def _write_observation(
    root: Path,
    *,
    binding: CurrentV2Binding,
    preflight: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    fixed, buggy = attempts
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "hidden_oracle": True,
        "real_semantic_observations": True,
        "fixed": fixed.payload["action_observation"],
        "buggy": buggy.payload["action_observation"],
        "invariants": {"world_responsive": True, "marker_and_result_same_replay": True},
        "missing_visual_and_lineage": [
            "common_before_fixed_after_and_buggy_after_goldens",
            "before_visual_equivalence_and_after_2x2_cross_judgment",
            "same_lineage_construction_receipt",
        ],
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        _OBSERVATION,
        (preflight.reference, fixed.reference, buggy.reference),
        payload,
        authorization,
    )


def _write_replay(
    root: Path,
    *,
    binding: CurrentV2Binding,
    preflight: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    observation: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "gate9_status": "complete",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "registered_start": binding.mapping.payload["registered_start"],
        "ordered_replay": binding.mapping.payload["ordered_replay"],
        "fixed_attempt": attempts[0].reference.artifact_id,
        "buggy_attempt": attempts[1].reference.artifact_id,
        "raw_start_observations": True,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        _REPLAY,
        (
            preflight.reference,
            attempts[0].reference,
            attempts[1].reference,
            observation.reference,
        ),
        payload,
        authorization,
    )


def _write_oracle(
    root: Path,
    *,
    binding: CurrentV2Binding,
    replay: StableArtifact,
    observation: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "gate10_status": "complete",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "hidden_oracle": binding.mapping.payload["semantic_oracle"],
        "real_semantic_observations": True,
        "fixed_observation": attempts[0].payload["action_observation"],
        "buggy_observation": attempts[1].payload["action_observation"],
        "invariants": {"world_responsive": True, "marker_and_result_same_replay": True},
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root, _ORACLE, (replay.reference, observation.reference), payload, authorization
    )


def _write_blocker(
    root: Path,
    *,
    binding: CurrentV2Binding,
    preflight: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    failed_variant: str,
    observation: StartObservation,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": "fixed_current_v2_start_failed_closed"
        if failed_variant == "fixed"
        else "buggy_current_v2_start_failed_closed",
        "failed_variant": failed_variant,
        "failed_process_outcome": observation.process_outcome,
        "missing_semantic_marker": observation.start_marker is None
        or observation.action_command is None,
        "candidate_execution_performed": True,
        "attempts_written": [item.reference.artifact_id for item in attempts],
        "buggy_not_started_after_fixed_failure": failed_variant == "fixed",
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(
        root,
        _BLOCKER,
        (preflight.reference, *(item.reference for item in attempts)),
        payload,
        authorization,
    )


def _write(
    root: Path,
    reference: ArtifactReference,
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
    return _load(root, reference)


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


def _reserve_run_root(root: Path, run: Path) -> Path:
    allowed = root / _RUN_PARENT
    allowed.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        run.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError(
            "current-v2 run root is outside registered parent"
        ) from error
    if run.exists() or run.is_symlink():
        raise RuntimeError("current-v2 run root already exists")
    run.mkdir(mode=0o700)
    return run.resolve(strict=True)


def _remove_variant_tree(path: Path, parent: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("variant cleanup escaped run root") from error
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _stop_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return


def _persist_raw_observation(
    root: Path, spec: StartSpec, observation: StartObservation
) -> str:
    # The per-variant directory is scratch and is removed after process-group
    # cleanup. Keep raw evidence in a sibling observations directory so the
    # typed attempt/blocker never points at a deleted file.
    path = spec.run_root.parent / "observations" / f"{spec.variant}.json"
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    value = asdict(observation)
    value["stdout"] = base64.b64encode(observation.stdout[:_MAX_OUTPUT]).decode("ascii")
    value["stderr"] = base64.b64encode(observation.stderr[:_MAX_OUTPUT]).decode("ascii")
    value["stdout_truncated"] = len(observation.stdout) > _MAX_OUTPUT
    value["stderr_truncated"] = len(observation.stderr) > _MAX_OUTPUT
    value["output_encoding"] = "base64"
    _atomic_json(path, value)
    return path.relative_to(root).as_posix()


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _atomic_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _ordinary_root(path: Path) -> Path:
    return _ordinary_directory(path, path)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("required directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("required file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("required file is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("preannotation-execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CANDIDATE",
    "EXECUTION_CONFIRMATION",
    "Gate9Result",
    "load_current_v2_binding",
    "execute_luanti_14757_current_v2_gate9_namespace",
    "write_current_v2_preflight",
    "write_current_v2_evidence_loss_note",
    "execute_luanti_14757_current_v2_gate9",
]
