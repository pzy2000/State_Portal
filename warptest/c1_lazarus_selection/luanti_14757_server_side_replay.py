"""Headless, candidate-specific Gate 9/10 replay for Luanti #14757.

The current-v2 GUI runner is blocked because QuartzNativeInput is unavailable
on this host.  This adapter keeps the registered chatcommand and its
production ``math.round`` body unchanged, but delivers the one registered
action from a disposable server-side timer after the Start callback.  It is a
typed, fresh namespace and never loads the GUI attempts, historical roots, or
protected processes.

This is intentionally not a generic Luanti runner.  The plan and parser are
bound to the #14757 Start/action markers and to the current mapping's exact
ordered replay contract.
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
from dataclasses import asdict, dataclass
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
EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_14757_SERVER_SIDE_REPLAY_V1"
SERVER_TRIGGER_MARKER = (
    "C1_LAZARUS_14757_SERVER_TRIGGER trigger=server_after_start"
)
_V1 = "1.0"
_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
_WATCHDOG_SECONDS = 60
_MAX_OUTPUT = 262_144
_PROTECTED_PIDS = (20946, 21627, 30865, 70361, 60839)
_RUN_PARENT = PurePosixPath("private/bounded-runs/luanti/14757")

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
    "candidate_downstream_repackage_receipt/luanti/14757/repackage-v3/v1",
    "candidate_downstream_repackage_receipt",
    _V1,
)
_PLAN = ArtifactReference(
    "candidate_gate9_server_side_plan/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_plan",
    _V1,
)
_SEMANTIC_CORRECTION = ArtifactReference(
    "candidate_gate9_server_side_semantic_correction/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_semantic_correction",
    _V1,
)
_ATTEMPT_KIND = "candidate_gate9_server_side_attempt"
_OBSERVATION = ArtifactReference(
    "candidate_gate10_server_side_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_observation",
    _V1,
)
_REPLAY = ArtifactReference(
    "candidate_gate9_server_side_replay/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_replay",
    _V1,
)
_ORACLE = ArtifactReference(
    "candidate_gate10_server_side_oracle/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_oracle",
    _V1,
)
_BLOCKER = ArtifactReference(
    "candidate_gate9_server_side_blocker/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_blocker",
    _V1,
)

_START_RE = re.compile(
    r"C1_LAZARUS_14757_START marker=(\S+) result_empty=(true|false)"
)
_TRIGGER_RE = re.compile(
    r"C1_LAZARUS_14757_SERVER_TRIGGER trigger=(\S+)"
)
_ACTION_RE = re.compile(
    r"C1_LAZARUS_14757_ACTION command=(\S+) count=([0-9]+) "
    r"result=(\S+) ok=(true|false)"
)


@dataclass(frozen=True)
class ServerObservation:
    process_outcome: Literal["completed", "launch_failed", "timed_out"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    start_marker: str | None
    start_result_empty: bool | None
    server_trigger_observed: bool
    action_command: str | None
    action_count: int
    semantic_result: str | None
    action_ok: bool | None


@dataclass(frozen=True)
class ServerSpec:
    variant: Literal["fixed", "buggy"]
    app_root: Path
    binary: Path
    fixture: Path
    run_root: Path
    world: Path
    config: Path
    log: Path
    port: int
    watchdog_seconds: int


@dataclass(frozen=True)
class ServerReplayResult:
    plan: StableArtifact
    attempts: tuple[StableArtifact, ...]
    observation: StableArtifact | None
    replay: StableArtifact | None
    oracle: StableArtifact | None
    blocker: StableArtifact | None


def inject_server_side_trigger(fixture_text: str) -> str:
    """Append a deterministic server callback without changing the action body."""

    if type(fixture_text) is not str:
        raise TypeError("#14757 fixture text must be a string")
    registration = 'minetest.register_chatcommand("c1_lazarus_round"'
    if registration not in fixture_text:
        raise ValueError("#14757 fixture lacks the registered round command")
    if "C1_LAZARUS_14757_ACTION" not in fixture_text:
        raise ValueError("#14757 fixture lacks the typed action marker")
    trigger = f'''

-- C1 Lazarus candidate-specific server-side delivery.  The registered
-- command body above remains the sole production action implementation.
minetest.register_on_mods_loaded(function()
  minetest.after(0, function()
    local command = minetest.registered_chatcommands["c1_lazarus_round"]
    if command == nil or command.func == nil then
      minetest.log("error", "C1_LAZARUS_14757_SERVER_TRIGGER missing_command=true")
      return
    end
    minetest.log("action", "{SERVER_TRIGGER_MARKER}")
    command.func("c1_lazarus_server")
  end)
end)
'''
    return fixture_text.rstrip() + trigger


def build_server_command(
    *, binary: Path, world: Path, config: Path, log: Path, port: int
) -> tuple[str, ...]:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("server port is invalid")
    return (
        str(binary),
        "--server",
        "--gameid",
        "devtest",
        "--world",
        str(world),
        "--config",
        str(config),
        "--logfile",
        str(log),
        "--port",
        str(port),
    )


def parse_server_observation(text: str) -> ServerObservation:
    """Parse only a Start -> server-trigger -> action sequence."""

    if type(text) is not str:
        raise TypeError("server log must be text")
    matches: list[tuple[int, str, re.Match[str]]] = []
    for kind, pattern in (
        ("start", _START_RE),
        ("trigger", _TRIGGER_RE),
        ("action", _ACTION_RE),
    ):
        matches.extend((match.start(), kind, match) for match in pattern.finditer(text))
    matches.sort(key=lambda item: item[0])
    start = next((item for item in matches if item[1] == "start"), None)
    trigger = next((item for item in matches if item[1] == "trigger"), None)
    action = next((item for item in matches if item[1] == "action"), None)
    ordered = (
        start is not None
        and trigger is not None
        and action is not None
        and start[0] < trigger[0] < action[0]
    )
    if not ordered:
        return ServerObservation(
            "completed",
            None,
            b"",
            b"",
            None,
            None,
            False,
            None,
            0,
            None,
            None,
        )
    assert start is not None and trigger is not None and action is not None
    return ServerObservation(
        "completed",
        None,
        b"",
        b"",
        start[2].group(1),
        start[2].group(2) == "true",
        trigger[2].group(1) == "server_after_start",
        action[2].group(1),
        int(action[2].group(2)),
        action[2].group(3),
        action[2].group(4) == "true",
    )


def write_server_side_plan(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    """Write and reload the typed plan before any candidate process starts."""

    _require_authorization(authorization)
    root = _ordinary_root(Path(artifact_root).absolute())
    aggregate = _load(root, _GATE_AGGREGATE)
    transition = _load(root, _GATE_TRANSITION)
    mapping = _load(root, _MAPPING)
    repackage = _load(root, _REPACKAGE)
    _validate_inputs(aggregate, transition, mapping, repackage)
    payload = {
        "candidate": CANDIDATE,
        "state": "ready_for_single_bounded_start_replay",
        "terminal": False,
        "resumable": True,
        "current_v2_only": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "delivery": {
            "mode": "dedicated_server_after_start_timer",
            "transport": "registered_chatcommand_function",
            "native_input": False,
            "terminal_flag": False,
            "trigger_marker": SERVER_TRIGGER_MARKER,
            "production_action_unchanged": True,
            "ordered_replay_preserved": True,
        },
        "mapping_ordered_replay": mapping.payload["ordered_replay"],
        "fresh_repackage_namespace": repackage.payload["fresh_namespace"],
        "gate9_status": "pending",
        "gate10_status": "pending",
        "gate11_to_13_status": "pending",
        "resource_policy": {"network": False, "paid": False, "formal_trials": False},
        "protected_pids": list(_PROTECTED_PIDS),
    }
    inputs = (
        aggregate.reference,
        transition.reference,
        mapping.reference,
        repackage.reference,
    )
    existing = _try_load(root, _PLAN)
    if existing is None:
        _write(root, _PLAN, inputs, payload, authorization)
        existing = _load(root, _PLAN)
    if existing.inputs != inputs or dict(existing.payload) != payload:
        raise RuntimeError("#14757 server-side plan drifted")
    return existing


def execute_server_side_replay(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    watchdog_seconds: int = _WATCHDOG_SECONDS,
) -> ServerReplayResult:
    """Run one fresh fixed/buggy pair using the server-side trigger."""

    _require_authorization(authorization)
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(watchdog_seconds) is not int or not 5 <= watchdog_seconds <= 600:
        raise ValueError("bounded launch watchdog is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    plan = write_server_side_plan(root, authorization=authorization)
    binding = _load_binding(root)
    run = _reserve_run_root(root, Path(run_root).absolute())
    attempts: list[StableArtifact] = []
    try:
        for index, variant in enumerate(("fixed", "buggy"), start=1):
            spec = _prepare_variant(
                root,
                run,
                binding,
                variant,
                watchdog_seconds,
                port=43157 + index,
            )
            observation = _run_server(spec)
            raw_path = _persist_raw_observation(root, spec, observation)
            attempt = _write_attempt(
                root,
                binding=binding,
                plan=plan,
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
                    plan=plan,
                    attempts=tuple(attempts),
                    failed_variant=variant,
                    observation=observation,
                    authorization=authorization,
                )
                return ServerReplayResult(plan, tuple(attempts), None, None, None, blocker)
        observation_artifact = _write_observation(
            root, plan=plan, attempts=tuple(attempts), authorization=authorization
        )
        replay = _write_replay(
            root, plan=plan, attempts=tuple(attempts), observation=observation_artifact, authorization=authorization
        )
        oracle = _write_oracle(
            root, replay=replay, observation=observation_artifact, attempts=tuple(attempts), authorization=authorization
        )
        return ServerReplayResult(plan, tuple(attempts), observation_artifact, replay, oracle, None)
    finally:
        for variant in ("fixed", "buggy"):
            tree = run / variant
            if tree.exists() or tree.is_symlink():
                _remove_variant_tree(tree, run)


def resume_buggy_after_fixed_observation(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    watchdog_seconds: int = _WATCHDOG_SECONDS,
) -> ServerReplayResult:
    """Use the one already-executed fixed process, then run buggy once.

    The fixed process produced the mathematically correct result ``0,-0``;
    the first adapter revision incorrectly required the textual spelling
    ``0,0``.  This resume path records a typed correction from retained raw
    evidence and never launches fixed again.  It is therefore still one
    fresh fixed+buggy pair, not a second candidate run.
    """

    _require_authorization(authorization)
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(watchdog_seconds) is not int or not 5 <= watchdog_seconds <= 600:
        raise ValueError("bounded launch watchdog is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    plan = _load(root, _PLAN)
    binding = _load_binding(root)
    fixed_ref = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14757/repackage-v3/fixed/1/v1",
        _ATTEMPT_KIND,
        _V1,
    )
    fixed_previous = _load(root, fixed_ref)
    raw = _load_raw_observation(root, fixed_previous)
    if not _semantic_match("fixed", raw):
        blocker = _write_blocker(
            root,
            plan=plan,
            attempts=(fixed_previous,),
            failed_variant="fixed",
            observation=raw,
            authorization=authorization,
        )
        return ServerReplayResult(plan, (fixed_previous,), None, None, None, blocker)
    correction_payload = {
        "candidate": CANDIDATE,
        "state": "typed_semantic_normalization",
        "source_attempt": fixed_previous.reference.artifact_id,
        "source_revision": fixed_previous.revision_path.name,
        "raw_observation_path": fixed_previous.payload["raw_observation_path"],
        "raw_semantic_result": raw.semantic_result,
        "normalized_numeric_result": [0.0, -0.0],
        "previous_adapter_predicate": fixed_previous.payload.get("semantic_match"),
        "corrected_semantic_match": True,
        "reason": "Lua_tostring_preserves_negative_zero_but_mapping_oracle_is_numeric_zero",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    correction = _write(
        root,
        _SEMANTIC_CORRECTION,
        (plan.reference, fixed_previous.reference),
        correction_payload,
        authorization,
    )
    fixed = _write_corrected_fixed_attempt(
        root,
        fixed_previous=fixed_previous,
        correction=correction,
        authorization=authorization,
    )
    run = _reuse_reserved_run_root(root, Path(run_root).absolute())
    spec = _prepare_variant(
        root,
        run,
        binding,
        "buggy",
        watchdog_seconds,
        port=43159,
    )
    observation = _run_server(spec)
    raw_path = _persist_raw_observation(root, spec, observation)
    buggy = _write_attempt(
        root,
        binding=binding,
        plan=plan,
        spec=spec,
        observation=observation,
        raw_path=raw_path,
        authorization=authorization,
    )
    _remove_variant_tree(spec.run_root, run)
    attempts = (fixed, buggy)
    if not _semantic_match("buggy", observation):
        blocker = _write_blocker(
            root,
            plan=plan,
            attempts=attempts,
            failed_variant="buggy",
            observation=observation,
            authorization=authorization,
        )
        return ServerReplayResult(plan, attempts, None, None, None, blocker)
    observation_artifact = _write_observation(
        root,
        plan=plan,
        attempts=attempts,
        authorization=authorization,
        semantic_correction=correction,
    )
    replay = _write_replay(
        root,
        plan=plan,
        attempts=attempts,
        observation=observation_artifact,
        authorization=authorization,
        semantic_correction=correction,
    )
    oracle = _write_oracle(
        root,
        replay=replay,
        observation=observation_artifact,
        attempts=attempts,
        authorization=authorization,
        semantic_correction=correction,
    )
    return ServerReplayResult(plan, attempts, observation_artifact, replay, oracle, None)


def _load_raw_observation(root: Path, attempt: StableArtifact) -> ServerObservation:
    relative = attempt.payload.get("raw_observation_path")
    if type(relative) is not str:
        raise RuntimeError("#14757 fixed attempt has no retained raw observation")
    path = _ordinary_file(root / PurePosixPath(relative), root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        stdout = base64.b64decode(value.get("stdout", ""), validate=True)
        stderr = base64.b64decode(value.get("stderr", ""), validate=True)
        return ServerObservation(
            process_outcome=value["process_outcome"],
            returncode=value.get("returncode"),
            stdout=stdout,
            stderr=stderr,
            start_marker=value.get("start_marker"),
            start_result_empty=value.get("start_result_empty"),
            server_trigger_observed=value.get("server_trigger_observed") is True,
            action_command=value.get("action_command"),
            action_count=value.get("action_count", 0),
            semantic_result=value.get("semantic_result"),
            action_ok=value.get("action_ok"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("#14757 retained fixed observation is invalid") from error


def _write_corrected_fixed_attempt(
    root: Path,
    *,
    fixed_previous: StableArtifact,
    correction: StableArtifact,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    payload = dict(fixed_previous.payload)
    payload["semantic_match"] = True
    payload["semantic_normalization"] = {
        "correction_reference": correction.reference.artifact_id,
        "raw_result": payload.get("action_observation", {}).get("result"),
        "numeric_goal_satisfied": True,
        "adapter_textual_predicate_before_correction": False,
    }
    payload["complete"] = True
    return _write(
        root,
        fixed_previous.reference,
        (correction.reference,),
        payload,
        authorization,
    )


def _load_binding(root: Path) -> tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact]:
    aggregate = _load(root, _GATE_AGGREGATE)
    transition = _load(root, _GATE_TRANSITION)
    mapping = _load(root, _MAPPING)
    repackage = _load(root, _REPACKAGE)
    _validate_inputs(aggregate, transition, mapping, repackage)
    return aggregate, transition, mapping, repackage


def _validate_inputs(
    aggregate: StableArtifact,
    transition: StableArtifact,
    mapping: StableArtifact,
    repackage: StableArtifact,
) -> None:
    if (
        aggregate.payload.get("candidate") != CANDIDATE
        or aggregate.payload.get("qualified") is not True
        or aggregate.payload.get("attempt_count") != 12
        or aggregate.payload.get("stable_repetitions") is not True
    ):
        raise RuntimeError("#14757 Gate 1--7 aggregate is not qualified")
    if (
        transition.payload.get("candidate") != CANDIDATE
        or transition.payload.get("qualified") is not True
    ):
        raise RuntimeError("#14757 Gate 1--7 transition is not qualified")
    if (
        mapping.payload.get("candidate") != CANDIDATE
        or mapping.payload.get("complete") is not True
        or mapping.payload.get("candidate_execution_performed") is not False
        or mapping.payload.get("ordered_replay")
        != [
            {"type": "restore_registered_start"},
            {"command": "/c1_lazarus_round", "type": "luanti_worldmod_math_round_boundary"},
        ]
    ):
        raise RuntimeError("#14757 ordered replay mapping is not current")
    rp = repackage.payload
    if (
        rp.get("candidate") != CANDIDATE
        or rp.get("outcome") != "static_packaging_qualified"
        or rp.get("state") != "downstream_pending"
        or rp.get("fresh_namespace") != "private/candidate_downstream/luanti/14757/repackage-v3"
        or rp.get("fresh_copy_status") != "succeeded"
        or rp.get("candidate_execution_performed") is not False
        or rp.get("registered_replay_executed") is not False
        or rp.get("same_lineage_construction_receipt_complete") is not True
        or rp.get("old_attempts_loaded") is not False
        or tuple(rp.get("do_not_touch_pids", ())) != _PROTECTED_PIDS
    ):
        raise RuntimeError("#14757 repackage-v3 is not server-side eligible")


def _prepare_variant(
    root: Path,
    run: Path,
    binding: tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact],
    variant: Literal["fixed", "buggy"],
    watchdog_seconds: int,
    *,
    port: int,
) -> ServerSpec:
    _aggregate, _transition, mapping, repackage = binding
    app_key = "binding_application_fixed_path" if variant == "fixed" else "binding_application_buggy_path"
    app = root / PurePosixPath(repackage.payload[app_key])
    _ordinary_directory(app, root)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    if not os.access(binary, os.X_OK):
        raise RuntimeError("#14757 repackage-v3 binary is not executable")
    target = run / variant
    target.mkdir(parents=True, mode=0o700)
    world = target / "world"
    mod = world / "worldmods/c1_lazarus"
    mod.parent.mkdir(parents=True, mode=0o700)
    mod.mkdir(mode=0o700)
    fixture = root / PurePosixPath(mapping.payload["fixture_root"])
    source_mod = fixture / "mods/c1_lazarus"
    init = source_mod / "init.lua"
    transformed = inject_server_side_trigger(init.read_text(encoding="utf-8"))
    shutil.copy2(source_mod / "mod.conf", mod / "mod.conf")
    (mod / "init.lua").write_text(transformed, encoding="utf-8")
    (world / "world.mt").write_text(
        "gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\ncreative_mode = true\nenable_damage = false\ntime_speed = 0\n",
        encoding="utf-8",
    )
    log = target / "luanti.log"
    return ServerSpec(variant, app, binary, fixture, target, world, config, log, port, watchdog_seconds)


def _run_server(spec: ServerSpec) -> ServerObservation:
    stdout_path = spec.run_root / "process.stdout"
    stderr_path = spec.run_root / "process.stderr"
    argv = build_server_command(
        binary=spec.binary,
        world=spec.world,
        config=spec.config,
        log=spec.log,
        port=spec.port,
    )
    env = dict(os.environ)
    home = spec.run_root / "home"
    home.mkdir(mode=0o700)
    env.update({"HOME": str(home), "LANG": "C", "LC_ALL": "C"})
    started_at = time.monotonic()
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(
                argv,
                cwd=spec.app_root,
                env=env,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as error:
        return ServerObservation("launch_failed", None, b"", str(error).encode(), None, None, False, None, 0, None, None)
    if process.pid in _PROTECTED_PIDS:
        _stop_owned_process_group(process)
        raise RuntimeError("server-side runner unexpectedly reused a protected PID")
    timed_out = False
    while time.monotonic() - started_at < spec.watchdog_seconds:
        text = _read_log(spec.log)
        parsed = parse_server_observation(text)
        if parsed.action_command is not None or process.poll() is not None:
            break
        time.sleep(0.05)
    else:
        timed_out = True
    if not timed_out:
        # Let the log writer flush the action line before group shutdown.
        time.sleep(0.25)
    _stop_owned_process_group(process)
    stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
    stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
    text = _read_log(spec.log)
    parsed = parse_server_observation(text)
    return ServerObservation(
        "timed_out" if timed_out else parsed.process_outcome,
        process.returncode,
        stdout,
        stderr,
        parsed.start_marker,
        parsed.start_result_empty,
        parsed.server_trigger_observed,
        parsed.action_command,
        parsed.action_count,
        parsed.semantic_result,
        parsed.action_ok,
    )


def _semantic_match(variant: str, observation: ServerObservation) -> bool:
    values = _numeric_result(observation.semantic_result)
    return (
        observation.process_outcome == "completed"
        and observation.start_marker == "basenodes:cobble"
        and observation.start_result_empty is True
        and observation.server_trigger_observed is True
        and observation.action_command == "/c1_lazarus_round"
        and observation.action_count == 1
        and values is not None
        # Lua's tostring(-0) is "-0".  The registered oracle is numeric
        # (both boundary values round to zero), so textual "0,-0" is a valid
        # fixed observation and must not be mistaken for a semantic failure.
        and ((values[0] == 0 and values[1] == 0) if variant == "fixed" else (values[0] != 0 or values[1] != 0))
        and (observation.action_ok is True if variant == "fixed" else observation.action_ok is False)
    )


def _numeric_result(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        parsed = (float(parts[0]), float(parts[1]))
    except ValueError:
        return None
    if any(not (-float("inf") < item < float("inf")) for item in parsed):
        return None
    return parsed


def _write_attempt(
    root: Path,
    *,
    binding: tuple[StableArtifact, StableArtifact, StableArtifact, StableArtifact],
    plan: StableArtifact,
    spec: ServerSpec,
    observation: ServerObservation,
    raw_path: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    _aggregate, _transition, mapping, repackage = binding
    ref = ArtifactReference(
        f"{_ATTEMPT_KIND}/luanti/14757/repackage-v3/{spec.variant}/1/v1",
        _ATTEMPT_KIND,
        _V1,
    )
    payload = {
        "candidate": CANDIDATE,
        "variant": spec.variant,
        "run_root": spec.run_root.relative_to(root).as_posix(),
        "fresh_repackage_namespace": repackage.payload["fresh_namespace"],
        "delivery_mode": "dedicated_server_after_start_timer",
        "ordered_replay": mapping.payload["ordered_replay"],
        "watchdog_seconds": spec.watchdog_seconds,
        "port": spec.port,
        "process_outcome": observation.process_outcome,
        "returncode": observation.returncode,
        "start_observation": {"marker": observation.start_marker, "result_empty": observation.start_result_empty},
        "server_trigger_observed": observation.server_trigger_observed,
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
    return _write(root, ref, (plan.reference,), payload, authorization)


def _write_observation(
    root: Path,
    *,
    plan: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
    semantic_correction: StableArtifact | None = None,
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
        "delivery_mode": "dedicated_server_after_start_timer",
        "semantic_correction": semantic_correction.reference.artifact_id
        if semantic_correction is not None
        else None,
        "fixed": fixed.payload["action_observation"],
        "buggy": buggy.payload["action_observation"],
        "invariants": {"world_responsive": True, "marker_and_result_same_replay": True},
        "missing_visual_and_lineage": [
            "common_before_fixed_after_and_buggy_after_goldens",
            "before_visual_equivalence_and_after_2x2_cross_judgment",
        ],
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    inputs = (plan.reference, fixed.reference, buggy.reference)
    if semantic_correction is not None:
        inputs = (plan.reference, semantic_correction.reference, fixed.reference, buggy.reference)
    return _write(root, _OBSERVATION, inputs, payload, authorization)


def _write_replay(
    root: Path,
    *,
    plan: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    observation: StableArtifact,
    authorization: AuthorizationRecord,
    semantic_correction: StableArtifact | None = None,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "gate9_status": "complete",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "delivery_mode": "dedicated_server_after_start_timer",
        "semantic_correction": semantic_correction.reference.artifact_id
        if semantic_correction is not None
        else None,
        "ordered_replay": attempts[0].payload["ordered_replay"],
        "fixed_attempt": attempts[0].reference.artifact_id,
        "buggy_attempt": attempts[1].reference.artifact_id,
        "raw_start_observations": True,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    inputs = (plan.reference, attempts[0].reference, attempts[1].reference, observation.reference)
    if semantic_correction is not None:
        inputs = (plan.reference, semantic_correction.reference, attempts[0].reference, attempts[1].reference, observation.reference)
    return _write(root, _REPLAY, inputs, payload, authorization)


def _write_oracle(
    root: Path,
    *,
    replay: StableArtifact,
    observation: StableArtifact,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
    semantic_correction: StableArtifact | None = None,
) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "gate10_status": "complete",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "hidden_oracle": True,
        "real_semantic_observations": True,
        "semantic_correction": semantic_correction.reference.artifact_id
        if semantic_correction is not None
        else None,
        "fixed_observation": attempts[0].payload["action_observation"],
        "buggy_observation": attempts[1].payload["action_observation"],
        "invariants": {"world_responsive": True, "marker_and_result_same_replay": True},
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    inputs = (replay.reference, observation.reference)
    if semantic_correction is not None:
        inputs = (replay.reference, semantic_correction.reference, observation.reference)
    return _write(root, _ORACLE, inputs, payload, authorization)


def _write_blocker(root: Path, *, plan: StableArtifact, attempts: tuple[StableArtifact, ...], failed_variant: str, observation: ServerObservation, authorization: AuthorizationRecord) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": "server_side_start_or_action_failed_closed",
        "failed_variant": failed_variant,
        "failed_process_outcome": observation.process_outcome,
        "start_marker": observation.start_marker,
        "server_trigger_observed": observation.server_trigger_observed,
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
    return _write(root, _BLOCKER, (plan.reference, *(item.reference for item in attempts)), payload, authorization)


def _persist_raw_observation(root: Path, spec: ServerSpec, observation: ServerObservation) -> str:
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


def _write(root: Path, reference: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> StableArtifact:
    write_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, reference)


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _reserve_run_root(root: Path, run: Path) -> Path:
    allowed = root / _RUN_PARENT
    allowed.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        run.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError("server-side run root is outside registered parent") from error
    if run.exists() or run.is_symlink():
        raise RuntimeError("server-side run root already exists")
    run.mkdir(mode=0o700)
    return run.resolve(strict=True)


def _reuse_reserved_run_root(root: Path, run: Path) -> Path:
    """Reuse only the owned pair root after fixed cleanup, never an old root."""

    allowed = root / _RUN_PARENT
    try:
        resolved = run.resolve(strict=True)
        resolved.relative_to(allowed.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("server-side resume root is missing or outside registered parent") from error
    info = run.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("server-side resume root is unsafe")
    if (run / "fixed").exists() or (run / "fixed").is_symlink():
        raise RuntimeError("server-side resume root still contains fixed scratch")
    if (run / "buggy").exists() or (run / "buggy").is_symlink():
        raise RuntimeError("server-side resume root already contains buggy scratch")
    return resolved


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
    if type(authorization) is not AuthorizationRecord or authorization.scope != "preannotation-execution" or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("preannotation-execution authorization is required")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CANDIDATE",
    "EXECUTION_CONFIRMATION",
    "SERVER_TRIGGER_MARKER",
    "ServerObservation",
    "ServerReplayResult",
    "ServerSpec",
    "build_server_command",
    "execute_server_side_replay",
    "resume_buggy_after_fixed_observation",
    "inject_server_side_trigger",
    "parse_server_observation",
    "write_server_side_plan",
]
