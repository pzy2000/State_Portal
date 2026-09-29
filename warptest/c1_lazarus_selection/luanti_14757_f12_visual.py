"""One fresh F12/OpenGL visual pair for Luanti #14757.

The v3 client has the ordinary engine screenshot path but no WarpTest
framebuffer hook.  This candidate-specific adapter therefore uses only the
registered engine F12 PNG path.  A delayed server-side fixture action leaves a
real common-before window; it then records fixed-after and buggy-after frames.
It never reuses GUI attempts, synthetic frames, or protected processes.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from PIL import Image, ImageChops, ImageStat

from ..native_input import QuartzNativeInput, NativeInputUnavailableError, require_unlocked_native_session
from .luanti_14757_server_side_replay import SERVER_TRIGGER_MARKER
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = {"repository_key": "luanti", "change_number": 14757}
EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_14757_F12_VISUAL_PAIR_V1"
_V1 = "1.0"
_V2 = "2.0"
_V3 = "3.0"
_V4 = "4.0"
_WATCHDOG_SECONDS = 60
_ACTION_DELAY_SECONDS = 5.0
_F12_KEYCODE = 111
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
_GATE9 = ArtifactReference(
    "candidate_gate9_server_side_replay/luanti/14757/repackage-v3/v1",
    "candidate_gate9_server_side_replay",
    _V1,
)
_GATE10_OBSERVATION = ArtifactReference(
    "candidate_gate10_server_side_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_observation",
    _V1,
)
_GATE10_ORACLE = ArtifactReference(
    "candidate_gate10_server_side_oracle/luanti/14757/repackage-v3/v1",
    "candidate_gate10_server_side_oracle",
    _V1,
)
_STATIC_PLAN = ArtifactReference(
    "candidate_gate11_visual_capture_plan/luanti/14757/repackage-v3/v1",
    "candidate_gate11_visual_capture_plan",
    _V1,
)
_PLAN = ArtifactReference(
    "candidate_gate11_f12_visual_plan/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_visual_plan",
    _V1,
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_f12_construction/luanti/14757/repackage-v3/v1",
    "candidate_gate13_f12_construction",
    _V1,
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_f12_visual_observation/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_visual_observation",
    _V1,
)
_MATRIX = ArtifactReference(
    "candidate_gate12_f12_visual_matrix/luanti/14757/repackage-v3/v1",
    "candidate_gate12_f12_visual_matrix",
    _V1,
)
_BLOCKER = ArtifactReference(
    "candidate_gate11_f12_visual_blocker/luanti/14757/repackage-v3/v1",
    "candidate_gate11_f12_visual_blocker",
    _V1,
)

_START_RE = re.compile(r"C1_LAZARUS_14757_START marker=(\S+) result_empty=(true|false)")
_TRIGGER_RE = re.compile(r"C1_LAZARUS_14757_SERVER_TRIGGER trigger=(\S+)")
_ACTION_RE = re.compile(
    r"C1_LAZARUS_14757_ACTION command=(\S+) count=([0-9]+) result=(\S+) ok=(true|false)"
)


@dataclass(frozen=True)
class VisualMarkers:
    ordered: bool
    start_marker: str | None
    start_result_empty: bool | None
    server_trigger_observed: bool
    action_command: str | None
    action_count: int
    semantic_result: str | None
    action_ok: bool | None


@dataclass(frozen=True)
class VisualObservation:
    process_outcome: Literal["completed", "launch_failed", "timed_out"]
    returncode: int | None
    start: VisualMarkers
    before_frame: Path | None
    after_frame: Path | None
    frame_dimensions: tuple[int, int] | None
    failure_reason: str | None
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class VisualSpec:
    variant: Literal["fixed", "buggy"]
    run_root: Path
    app_root: Path
    binary: Path
    world: Path
    config: Path
    log: Path
    frames: Path
    watchdog_seconds: int


@dataclass(frozen=True)
class VisualPairResult:
    plan: StableArtifact
    construction: StableArtifact | None
    attempts: tuple[StableArtifact, ...]
    observation: StableArtifact | None
    matrix: StableArtifact | None
    blocker: StableArtifact | None


def inject_delayed_server_trigger(
    fixture_text: str,
    *,
    delay_seconds: float,
    ready_global: str | None = None,
) -> str:
    if type(fixture_text) is not str:
        raise TypeError("#14757 visual fixture must be text")
    if not (0.1 <= delay_seconds <= 60.0):
        raise ValueError("#14757 visual action delay is invalid")
    registration = 'minetest.register_chatcommand("c1_lazarus_round"'
    if registration not in fixture_text or "C1_LAZARUS_14757_ACTION" not in fixture_text:
        raise ValueError("#14757 visual fixture lacks registered action")
    if ready_global is not None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ready_global):
            raise ValueError("#14757 visual readiness global is invalid")
        readiness = f'''
    local function c1_trigger_after_scene_ready()
      local ready = _G.{ready_global}
      if type(ready) == "function" and not ready() then
        minetest.after(0.2, c1_trigger_after_scene_ready)
        return
      end
      local command = minetest.registered_chatcommands["c1_lazarus_round"]
      if command == nil or command.func == nil then
        minetest.log("error", "C1_LAZARUS_14757_SERVER_TRIGGER missing_command=true")
        return
      end
      minetest.log("action", "{SERVER_TRIGGER_MARKER}")
      command.func("c1_lazarus_visual")
    end
'''
    else:
        readiness = f'''
    local function c1_trigger_after_scene_ready()
      local command = minetest.registered_chatcommands["c1_lazarus_round"]
      if command == nil or command.func == nil then
        minetest.log("error", "C1_LAZARUS_14757_SERVER_TRIGGER missing_command=true")
        return
      end
      minetest.log("action", "{SERVER_TRIGGER_MARKER}")
      command.func("c1_lazarus_visual")
    end
'''
    trigger = f'''

-- Candidate-specific visual delivery.  The registered action body is unchanged.
minetest.register_on_mods_loaded(function()
{readiness.rstrip()}
  minetest.after({delay_seconds:.1f}, c1_trigger_after_scene_ready)
end)
'''
    return fixture_text.rstrip() + trigger


def build_client_command(*, binary: Path, world: Path, config: Path, log: Path) -> tuple[str, ...]:
    return (
        str(binary),
        "--go",
        "--gameid",
        "devtest",
        "--world",
        str(world),
        "--name",
        "warptest",
        "--config",
        str(config),
        "--logfile",
        str(log),
    )


def parse_visual_markers(text: str) -> VisualMarkers:
    matches: list[tuple[int, str, re.Match[str]]] = []
    for kind, regex in (("start", _START_RE), ("trigger", _TRIGGER_RE), ("action", _ACTION_RE)):
        matches.extend((match.start(), kind, match) for match in regex.finditer(text))
    matches.sort(key=lambda item: item[0])
    start = next((item for item in matches if item[1] == "start"), None)
    trigger = next((item for item in matches if item[1] == "trigger"), None)
    action = next((item for item in matches if item[1] == "action"), None)
    ordered = bool(start and trigger and action and start[0] < trigger[0] < action[0])
    if not ordered:
        return VisualMarkers(False, None, None, False, None, 0, None, None)
    assert start is not None and trigger is not None and action is not None
    return VisualMarkers(
        True,
        start[2].group(1),
        start[2].group(2) == "true",
        trigger[2].group(1) == "server_after_start",
        action[2].group(1),
        int(action[2].group(2)),
        action[2].group(3),
        action[2].group(4) == "true",
    )


def frame_is_non_null(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with Image.open(path) as image:
            image.load()
            return image.format == "PNG" and image.width > 0 and image.height > 0
    # The engine creates the PNG path before the writer has flushed every
    # chunk.  Treat a transient Pillow parse error as "not ready yet" so the
    # bounded capture loop can poll the same file again.
    except (OSError, SyntaxError, ValueError):
        return False


def _difference_mean(left: Path, right: Path) -> float:
    with Image.open(left).convert("RGB") as a, Image.open(right).convert("RGB") as b:
        if a.size != b.size:
            return float("inf")
        return float(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3.0)


def visual_cross_matrix(*, before_fixed: Path, before_buggy: Path, after_fixed: Path, after_buggy: Path) -> dict[str, object]:
    before_difference = _difference_mean(before_fixed, before_buggy)
    after_difference = _difference_mean(after_fixed, after_buggy)
    return {
        "backend": "luanti_builtin_f12_engine_png_v1",
        "before_difference_mean": before_difference,
        "after_difference_mean": after_difference,
        "before_equivalent": before_difference <= 1.0,
        "after_fixed_buggy_different": after_difference > 1.0,
        "complete": True,
        "synthetic_frames": False,
    }


def write_f12_visual_plan(artifact_root: str | Path, *, authorization: AuthorizationRecord) -> StableArtifact:
    _require_authorization(authorization, scope="infrastructure-audit")
    root = _ordinary_root(Path(artifact_root).absolute())
    refs = (_GATE_AGGREGATE, _GATE_TRANSITION, _MAPPING, _REPACKAGE, _GATE9, _GATE10_OBSERVATION, _GATE10_ORACLE, _STATIC_PLAN)
    loaded = {ref.artifact_id: _load(root, ref) for ref in refs}
    _validate_receipts(loaded)
    preflight = require_unlocked_native_session()
    payload = {
        "candidate": CANDIDATE,
        "state": "ready_for_single_bounded_visual_pair",
        "outcome": "downstream_pending",
        "terminal": False,
        "resumable": True,
        "complete": True,
        "candidate_execution_performed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "capture_backend": "luanti_builtin_f12_engine_png_v1",
        "capture_contract": {
            "engine_createScreenShot": True,
            "f12_keycode": _F12_KEYCODE,
            "non_null_png_required": True,
            "synthetic_frames": False,
            "warptest_hook_required": False,
        },
        "action_delivery": {
            "mode": "server_side_after_start_timer",
            "delay_seconds": _ACTION_DELAY_SECONDS,
            "mapping_action_unchanged": True,
            "native_chat_input": False,
        },
        "native_input_preflight": {
            "console_unlocked": preflight.get("console_unlocked") is True,
            "accessibility_trusted": preflight.get("accessibility_trusted") is True,
            "event_post_access": preflight.get("event_post_access") is True,
        },
        "run_contract": {
            "new_root_parent": str(_RUN_PARENT),
            "copy_repackage_v3_before_launch": True,
            "owned_process_group_only": True,
            "launch_at_most_one_fixed_and_one_buggy": True,
            "retain_raw_observations": True,
        },
        "minimum_missing_evidence": [
            "fresh_common_before_fixed_after_buggy_after_pngs",
            "before_visual_equivalence",
            "after_2x2_cross_matrix",
        ],
    }
    existing = _try_load(root, _PLAN)
    if existing is None:
        _write(root, _PLAN, refs, payload, authorization)
        existing = _load(root, _PLAN)
    if existing.inputs != refs or dict(existing.payload) != payload:
        raise RuntimeError("#14757 F12 visual plan drifted")
    return existing


def execute_f12_visual_pair(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    watchdog_seconds: int = _WATCHDOG_SECONDS,
) -> VisualPairResult:
    _require_authorization(authorization, scope="preannotation-execution")
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(watchdog_seconds) is not int or not 5 <= watchdog_seconds <= 600:
        raise ValueError("#14757 visual watchdog is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    plan = _load(root, _PLAN)
    refs = (_GATE_AGGREGATE, _GATE_TRANSITION, _MAPPING, _REPACKAGE, _GATE9, _GATE10_OBSERVATION, _GATE10_ORACLE)
    loaded = {ref.artifact_id: _load(root, ref) for ref in refs}
    _validate_receipts(loaded)
    native_status = require_unlocked_native_session()
    if not all(native_status.get(key) is True for key in ("console_unlocked", "accessibility_trusted", "event_post_access")):
        raise RuntimeError("#14757 visual native preflight did not pass")
    run = _reserve_run_root(root, Path(run_root).absolute())
    attempts: list[StableArtifact] = []
    specs: list[VisualSpec] = []
    construction: StableArtifact | None = None
    try:
        for variant in ("fixed", "buggy"):
            spec = _prepare_variant(root, run, loaded[_MAPPING.artifact_id], loaded[_REPACKAGE.artifact_id], variant, watchdog_seconds)
            specs.append(spec)
            observation = _run_visual(spec)
            attempt = _write_attempt(root, plan=plan, spec=spec, observation=observation, authorization=authorization)
            attempts.append(attempt)
            if not _visual_semantic_pass(variant, observation):
                blocker = _write_blocker(root, plan=plan, attempts=tuple(attempts), observation=observation, failed_variant=variant, authorization=authorization)
                return VisualPairResult(plan, construction, tuple(attempts), None, None, blocker)
        construction = _write_construction(root, plan=plan, specs=tuple(specs), authorization=authorization)
        observation_artifact = _write_observation(root, plan=plan, construction=construction, attempts=tuple(attempts), authorization=authorization)
        matrix = _write_matrix(root, plan=plan, construction=construction, attempts=tuple(attempts), observation=observation_artifact, authorization=authorization)
        return VisualPairResult(plan, construction, tuple(attempts), observation_artifact, matrix, None)
    except NativeInputUnavailableError as error:
        blocker = _write_blocker(root, plan=plan, attempts=tuple(attempts), observation=None, failed_variant=specs[-1].variant if specs else "fixed", authorization=authorization, reason=f"native_input_unavailable:{error}")
        return VisualPairResult(plan, construction, tuple(attempts), None, None, blocker)


def _validate_receipts(loaded: dict[str, StableArtifact]) -> None:
    for artifact in loaded.values():
        if artifact.payload.get("candidate") != CANDIDATE:
            raise RuntimeError("#14757 F12 receipt candidate mismatch")
    if loaded[_GATE_AGGREGATE.artifact_id].payload.get("qualified") is not True or loaded[_GATE_TRANSITION.artifact_id].payload.get("qualified") is not True:
        raise RuntimeError("#14757 Gate1-7 is not qualified")
    if loaded[_GATE9.artifact_id].payload.get("gate9_status") != "complete" or loaded[_GATE10_ORACLE.artifact_id].payload.get("gate10_status") != "complete":
        raise RuntimeError("#14757 Gate9/10 receipts are incomplete")
    for ref in (_GATE9, _GATE10_OBSERVATION, _GATE10_ORACLE):
        p = loaded[ref.artifact_id].payload
        if p.get("old_attempts_loaded") is not False or p.get("old_construction_loaded") is not False:
            raise RuntimeError("#14757 F12 visual run would inherit old evidence")
    rp = loaded[_REPACKAGE.artifact_id].payload
    if rp.get("state") != "downstream_pending" or rp.get("outcome") != "static_packaging_qualified" or rp.get("candidate_execution_performed") is not False or rp.get("original_pair_modified") is not False:
        raise RuntimeError("#14757 repackage-v3 is not untouched")


def _prepare_variant(root: Path, run: Path, mapping: StableArtifact, repackage: StableArtifact, variant: Literal["fixed", "buggy"], watchdog_seconds: int) -> VisualSpec:
    source_app = root / PurePosixPath(repackage.payload[f"binding_application_{variant}_path"])
    _ordinary_directory(source_app, root)
    target = run / variant
    target.mkdir(parents=True, mode=0o700)
    app = target / f"{variant}-minetest.app"
    shutil.copytree(source_app, app, symlinks=True)
    binary = _ordinary_file(app / "Contents/MacOS/minetest", app)
    if not os.access(binary, os.X_OK):
        raise RuntimeError("#14757 copied F12 app is not executable")
    world = target / "world"
    mod = world / "worldmods/c1_lazarus"
    mod.mkdir(parents=True, mode=0o700)
    fixture = root / PurePosixPath(mapping.payload["fixture_root"])
    source_mod = fixture / "mods/c1_lazarus"
    (mod / "mod.conf").write_text((source_mod / "mod.conf").read_text(encoding="utf-8"), encoding="utf-8")
    (mod / "init.lua").write_text(inject_delayed_server_trigger((source_mod / "init.lua").read_text(encoding="utf-8"), delay_seconds=_ACTION_DELAY_SECONDS), encoding="utf-8")
    (world / "world.mt").write_text("gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n", encoding="utf-8")
    frames = target / "frames"
    frames.mkdir(mode=0o700)
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\ncreative_mode = true\nenable_damage = false\ntime_speed = 0\n"
        f"screen_w = 1280\nscreen_h = 720\nfullscreen = false\nscreenshot_path = {frames}\nscreenshot_format = png\n",
        encoding="utf-8",
    )
    log = target / "luanti.log"
    return VisualSpec(variant, target, app, binary, world, config, log, frames, watchdog_seconds)


def _run_visual(spec: VisualSpec) -> VisualObservation:
    stdout_path = spec.run_root / "process.stdout"
    stderr_path = spec.run_root / "process.stderr"
    argv = build_client_command(binary=spec.binary, world=spec.world, config=spec.config, log=spec.log)
    env = dict(os.environ)
    home = spec.run_root / "home"
    home.mkdir(mode=0o700)
    env.update({"HOME": str(home), "LANG": "C", "LC_ALL": "C"})
    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(argv, cwd=spec.app_root, env=env, stdout=out, stderr=err, stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as error:
        return VisualObservation("launch_failed", None, VisualMarkers(False, None, None, False, None, 0, None, None), None, None, None, str(error))
    if process.pid in _PROTECTED_PIDS:
        _stop_owned_process_group(process)
        raise RuntimeError("#14757 F12 runner unexpectedly reused a protected PID")
    deadline = time.monotonic() + spec.watchdog_seconds
    while time.monotonic() < deadline:
        markers = parse_visual_markers(_read_log(spec.log))
        if markers.start_marker is not None:
            break
        if process.poll() is not None:
            return _finish_failed(process, spec, "client_exited_before_start")
        time.sleep(0.05)
    else:
        _stop_owned_process_group(process)
        return VisualObservation("timed_out", process.returncode, parse_visual_markers(_read_log(spec.log)), None, None, None, "start_timeout")
    # The fixture's delayed callback is the only action delivery.  Capture
    # Start before that callback can fire, then wait for its semantic marker.
    time.sleep(0.25)
    before = _capture_f12(spec, process.pid, known=set())
    if before is None:
        _stop_owned_process_group(process)
        return _finish_failed(process, spec, "before_f12_png_missing")
    while time.monotonic() < deadline:
        markers = parse_visual_markers(_read_log(spec.log))
        if markers.action_command is not None:
            break
        if process.poll() is not None:
            return _finish_failed(process, spec, "client_exited_before_action", before=before)
        time.sleep(0.05)
    else:
        _stop_owned_process_group(process)
        return VisualObservation("timed_out", process.returncode, parse_visual_markers(_read_log(spec.log)), before, None, _frame_dimensions(before), "action_timeout")
    after = _capture_f12(spec, process.pid, known={before})
    if after is None:
        _stop_owned_process_group(process)
        return _finish_failed(process, spec, "after_f12_png_missing", before=before)
    time.sleep(0.2)
    _stop_owned_process_group(process)
    markers = parse_visual_markers(_read_log(spec.log))
    return VisualObservation("completed", process.returncode, markers, before, after, _frame_dimensions(after), None, stdout_path.read_bytes() if stdout_path.is_file() else b"", stderr_path.read_bytes() if stderr_path.is_file() else b"")


def _capture_f12(spec: VisualSpec, pid: int, *, known: set[Path]) -> Path | None:
    native = QuartzNativeInput(pid)
    native.focus()
    native._press_key(_F12_KEYCODE)  # candidate-specific engine screenshot key
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        candidates = [path for path in spec.frames.glob("*.png") if path not in known and frame_is_non_null(path)]
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime_ns)
        time.sleep(0.05)
    return None


def _visual_semantic_pass(variant: str, observation: VisualObservation) -> bool:
    return (
        observation.process_outcome == "completed"
        and observation.start.ordered
        and observation.start.start_marker == "basenodes:cobble"
        and observation.start.start_result_empty is True
        and observation.start.server_trigger_observed
        and observation.start.action_command == "/c1_lazarus_round"
        and observation.start.action_count == 1
        and observation.before_frame is not None
        and observation.after_frame is not None
        and frame_is_non_null(observation.before_frame)
        and frame_is_non_null(observation.after_frame)
        and observation.start.action_ok is (variant == "fixed")
    )


def _write_attempt(root: Path, *, plan: StableArtifact, spec: VisualSpec, observation: VisualObservation, authorization: AuthorizationRecord) -> StableArtifact:
    ref = ArtifactReference(f"candidate_gate11_f12_attempt/luanti/14757/repackage-v3/{spec.variant}/1/v1", "candidate_gate11_f12_attempt", _V1)
    payload = {
        "candidate": CANDIDATE,
        "variant": spec.variant,
        "state": "downstream_pending",
        "process_outcome": observation.process_outcome,
        "returncode": observation.returncode,
        "markers": asdict(observation.start),
        "before_frame": str(observation.before_frame.relative_to(root)) if observation.before_frame else None,
        "after_frame": str(observation.after_frame.relative_to(root)) if observation.after_frame else None,
        "frame_dimensions": list(observation.frame_dimensions) if observation.frame_dimensions else None,
        "synthetic_frames": False,
        "visual_semantic_match": _visual_semantic_pass(spec.variant, observation),
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, ref, (plan.reference,), payload, authorization)


def _write_construction(root: Path, *, plan: StableArtifact, specs: tuple[VisualSpec, ...], authorization: AuthorizationRecord) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "qualified",
        "source_repackage": _REPACKAGE.artifact_id,
        "copied_fixed_app": str(specs[0].app_root.relative_to(root)),
        "copied_buggy_app": str(specs[1].app_root.relative_to(root)),
        "same_lineage_source_pair": True,
        "production_test_bytes_changed": False,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "candidate_execution_performed": True,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, _CONSTRUCTION, (plan.reference, _REPACKAGE), payload, authorization)


def _write_observation(root: Path, *, plan: StableArtifact, construction: StableArtifact, attempts: tuple[StableArtifact, ...], authorization: AuthorizationRecord) -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "gate11_status": "semantic_frames_captured",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "synthetic_frames": False,
        "capture_backend": "luanti_builtin_f12_engine_png_v1",
        "fixed": attempts[0].payload["markers"],
        "buggy": attempts[1].payload["markers"],
        "fixed_before": attempts[0].payload["before_frame"],
        "fixed_after": attempts[0].payload["after_frame"],
        "buggy_before": attempts[1].payload["before_frame"],
        "buggy_after": attempts[1].payload["after_frame"],
        "construction_reference": construction.reference.artifact_id,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, _OBSERVATION, (plan.reference, construction.reference, attempts[0].reference, attempts[1].reference), payload, authorization)


def _write_matrix(root: Path, *, plan: StableArtifact, construction: StableArtifact, attempts: tuple[StableArtifact, ...], observation: StableArtifact, authorization: AuthorizationRecord) -> StableArtifact:
    paths = [root / PurePosixPath(attempts[index].payload[key]) for index, key in ((0, "before_frame"), (1, "before_frame"), (0, "after_frame"), (1, "after_frame"))]
    matrix = visual_cross_matrix(before_fixed=paths[0], before_buggy=paths[1], after_fixed=paths[2], after_buggy=paths[3])
    payload = {
        "candidate": CANDIDATE,
        "state": "downstream_pending",
        "gate12_status": "quantitative_cross_matrix_captured",
        "candidate_execution_performed": True,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "matrix": matrix,
        "human_or_model_visual_judgment": "pending",
        "observation_reference": observation.reference.artifact_id,
        "construction_reference": construction.reference.artifact_id,
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, _MATRIX, (plan.reference, construction.reference, observation.reference), payload, authorization)


def _write_blocker(root: Path, *, plan: StableArtifact, attempts: tuple[StableArtifact, ...], observation: VisualObservation | None, failed_variant: str, authorization: AuthorizationRecord, reason: str = "visual_start_or_capture_failed_closed") -> StableArtifact:
    payload = {
        "candidate": CANDIDATE,
        "state": "downstream_pending",
        "outcome": "infrastructure_blocked",
        "reason": reason,
        "failed_variant": failed_variant,
        "candidate_execution_performed": bool(attempts),
        "attempts_written": [item.reference.artifact_id for item in attempts],
        "failed_process_outcome": observation.process_outcome if observation else None,
        "old_attempts_loaded": False,
        "old_construction_loaded": False,
        "protected_pids": list(_PROTECTED_PIDS),
        "terminal": False,
        "resumable": True,
        "complete": True,
    }
    return _write(root, _BLOCKER, (plan.reference, *(item.reference for item in attempts)), payload, authorization)


def _finish_failed(process: subprocess.Popen[bytes], spec: VisualSpec, reason: str, *, before: Path | None = None) -> VisualObservation:
    _stop_owned_process_group(process)
    return VisualObservation("launch_failed", process.returncode, parse_visual_markers(_read_log(spec.log)), before, None, _frame_dimensions(before) if before else None, reason, b"", b"")


def _frame_dimensions(path: Path | None) -> tuple[int, int] | None:
    if path is None or not frame_is_non_null(path):
        return None
    with Image.open(path) as image:
        return int(image.width), int(image.height)


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version)


def _try_load(root: Path, ref: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, ref)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _write(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], authorization: AuthorizationRecord) -> StableArtifact:
    write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, ref)


def _reserve_run_root(root: Path, run: Path) -> Path:
    allowed = root / _RUN_PARENT
    allowed.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        run.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError("#14757 F12 run root escapes registered parent") from error
    if run.exists() or run.is_symlink():
        raise RuntimeError("#14757 F12 run root already exists")
    run.mkdir(mode=0o700)
    return run.resolve(strict=True)


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
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _ordinary_root(path: Path) -> Path:
    return _ordinary_directory(path, path)


def _ordinary_directory(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("#14757 F12 directory is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("#14757 F12 directory is missing or unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError("#14757 F12 file is missing or unsafe") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("#14757 F12 file is missing or unsafe")
    return resolved


def _require_authorization(authorization: AuthorizationRecord, *, scope: str) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope != scope or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError(f"#14757 F12 authorization requires {scope}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CANDIDATE",
    "EXECUTION_CONFIRMATION",
    "SERVER_TRIGGER_MARKER",
    "VisualMarkers",
    "VisualObservation",
    "build_client_command",
    "execute_f12_visual_pair",
    "frame_is_non_null",
    "inject_delayed_server_trigger",
    "parse_visual_markers",
    "visual_cross_matrix",
    "write_f12_visual_plan",
]
