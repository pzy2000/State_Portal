"""Bounded fixed/buggy application launch for admitted Luanti candidate 14757.

The writer records resumable downstream evidence only.  A timeout, launch
failure, incomplete Start, or semantic mismatch remains ``downstream_pending``
and never creates a selection terminal.
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
from typing import Callable, Literal, Protocol

from .luanti_lua_downstream_closure import (
    load_luanti_lua_downstream_preflight,
    prepare_luanti_lua_downstream_closure,
)
from .models import CandidateKey
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = CandidateKey("luanti", 14757)
EXECUTION_CONFIRMATION = "EXECUTE_LUANTI_14757_BOUNDED_LAUNCH"
_VERSION = "1.0"
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/14757/lz-luanti-14757-math-round/v4",
    "lazarus_task_mapping",
    "4.0",
)
_START = re.compile(r"C1_LAZARUS_14757_START marker=(\S+) result_empty=(true|false)")
_ACTION = re.compile(
    r"C1_LAZARUS_14757_ACTION command=(\S+) count=([0-9]+) "
    r"result=(\S+) ok=(true|false)"
)
_MAX_OUTPUT = 262_144


@dataclass(frozen=True)
class BoundedLaunchObservation:
    process_outcome: Literal["completed", "launch_failed", "timed_out"]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    start_marker: str | None
    start_result: str | None
    action_command: str | None
    action_count: int
    semantic_result: str | None

    def __post_init__(self) -> None:
        if (
            self.process_outcome not in {"completed", "launch_failed", "timed_out"}
            or self.returncode is not None
            and type(self.returncode) is not int
            or type(self.stdout) is not bytes
            or type(self.stderr) is not bytes
            or self.start_marker is not None
            and type(self.start_marker) is not str
            or self.start_result is not None
            and type(self.start_result) is not str
            or self.action_command is not None
            and type(self.action_command) is not str
            or type(self.action_count) is not int
            or self.action_count < 0
            or self.semantic_result is not None
            and type(self.semantic_result) is not str
        ):
            raise ValueError("bounded launch observation is invalid")


@dataclass(frozen=True)
class BoundedLaunchSpec:
    variant: Literal["fixed", "buggy"]
    application_root: Path
    application_binary: Path
    fixture_root: Path
    run_root: Path
    world: Path
    config: Path
    log: Path
    watchdog_seconds: int


class BoundedLaunchDriver(Protocol):
    def run(self, spec: BoundedLaunchSpec) -> BoundedLaunchObservation: ...


@dataclass(frozen=True)
class BoundedLaunchResult:
    attempts: tuple[StableArtifact, StableArtifact]
    aggregate: StableArtifact


PreflightLoader = Callable[[Path], StableArtifact]


def execute_luanti_14757_bounded_launch(
    artifact_root: str | Path,
    *,
    run_root: str | Path,
    authorization: AuthorizationRecord,
    confirmation: str,
    watchdog_seconds: int = 60,
    driver: BoundedLaunchDriver | None = None,
    preflight_loader: PreflightLoader | None = None,
) -> BoundedLaunchResult:
    """Launch fixed and buggy once each and persist typed pending evidence."""

    _require_authorization(authorization)
    if confirmation != EXECUTION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {EXECUTION_CONFIRMATION!r}")
    if type(watchdog_seconds) is not int or not 5 <= watchdog_seconds <= 600:
        raise ValueError("bounded launch watchdog is invalid")
    root = _ordinary_root(Path(artifact_root).absolute())
    run = _new_run_root(root, Path(run_root).absolute())
    if preflight_loader is None:
        prepare_luanti_lua_downstream_closure(
            root,
            candidate=CANDIDATE,
            authorization=authorization,
        )
        preflight = load_luanti_lua_downstream_preflight(root, candidate=CANDIDATE)
    else:
        preflight = preflight_loader(root)
    mapping, fixture, applications = _validate_boundary(root, preflight)
    launch_driver = driver or MacOS14757LaunchDriver()
    attempts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        spec = _materialize_run(
            root,
            run,
            variant=variant,
            application=applications[variant],
            fixture=fixture,
            watchdog_seconds=watchdog_seconds,
        )
        observation = launch_driver.run(spec)
        raw_path = _persist_raw_observation(root, spec, observation)
        attempts.append(
            _persist_attempt(
                root,
                spec=spec,
                observation=observation,
                raw_path=raw_path,
                inputs=(preflight.reference, mapping.reference),
                authorization=authorization,
            )
        )
    aggregate = _persist_aggregate(root, tuple(attempts), authorization)
    return BoundedLaunchResult((attempts[0], attempts[1]), aggregate)


class MacOS14757LaunchDriver:
    """Launch only the registered app process group and inject one chat action."""

    def run(self, spec: BoundedLaunchSpec) -> BoundedLaunchObservation:
        if os.uname().sysname != "Darwin":
            return _failed_observation(b"bounded GUI launch requires macOS")
        stdout_path = spec.run_root / "process.stdout"
        stderr_path = spec.run_root / "process.stderr"
        argv = (
            str(spec.application_binary),
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
        env["HOME"] = str(home)
        try:
            with (
                stdout_path.open("wb") as stdout_file,
                stderr_path.open("wb") as stderr_file,
            ):
                process = subprocess.Popen(
                    argv,
                    cwd=spec.application_root,
                    env=env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
        except OSError as error:
            return _failed_observation(str(error).encode("utf-8", errors="replace"))
        deadline = time.monotonic() + spec.watchdog_seconds
        action_sent = False
        start_seen_at: float | None = None
        timed_out = False
        driver_error: str | None = None
        try:
            while time.monotonic() < deadline:
                text = _read_log(spec.log)
                if _START.search(text) and start_seen_at is None:
                    start_seen_at = time.monotonic()
                if (
                    start_seen_at is not None
                    and time.monotonic() - start_seen_at >= 2.0
                    and not action_sent
                ):
                    from warptest.native_input import QuartzNativeInput

                    native = QuartzNativeInput(process.pid)
                    native.key("/")
                    native.type_text("c1_lazarus_round")
                    native.key("enter")
                    action_sent = True
                if _ACTION.search(text):
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            else:
                timed_out = True
        except Exception as error:
            driver_error = f"{type(error).__name__}: {error}"
        finally:
            _stop_owned_process_group(process)
        stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
        stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
        if driver_error is not None:
            stderr += ("\n" + driver_error).encode("utf-8", errors="replace")
        return _observation_from_log(
            _read_log(spec.log),
            process_outcome=(
                "launch_failed"
                if driver_error is not None
                else "timed_out"
                if timed_out
                else "completed"
            ),
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


def _observation_from_log(
    log: str,
    *,
    process_outcome: Literal["completed", "launch_failed", "timed_out"],
    returncode: int | None,
    stdout: bytes,
    stderr: bytes,
) -> BoundedLaunchObservation:
    start = _START.search(log)
    actions = list(_ACTION.finditer(log))
    action = actions[-1] if actions else None
    return BoundedLaunchObservation(
        process_outcome=process_outcome,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        start_marker=start.group(1) if start else None,
        start_result="" if start and start.group(2) == "true" else None,
        action_command=action.group(1) if action else None,
        action_count=int(action.group(2)) if action else 0,
        semantic_result=action.group(3) if action else None,
    )


def _failed_observation(stderr: bytes) -> BoundedLaunchObservation:
    return BoundedLaunchObservation(
        "launch_failed", None, b"", stderr, None, None, None, 0, None
    )


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
            # macOS GUI processes can remain in an uninterruptible kernel
            # wait after the watchdog.  The observation is still persisted as
            # bounded/pending by the caller; never turn that cleanup anomaly
            # into an uncaught executor error or a candidate conclusion.
            return


def _validate_boundary(
    root: Path, preflight: StableArtifact
) -> tuple[StableArtifact, Path, dict[str, Path]]:
    if (
        preflight.reference
        != ArtifactReference(
            "downstream_preflight/luanti/14757/runtime-visual/v4",
            "downstream_preflight",
            "4.0",
        )
        or preflight.payload.get("candidate") != CANDIDATE.to_value()
        or preflight.payload.get("application_pair_status")
        != "complete_launch_unverified"
        or preflight.payload.get("permanent_terminal_written") is not False
        or _MAPPING not in preflight.inputs
    ):
        raise RuntimeError("Luanti 14757 bounded launch preflight is invalid")
    mapping = load_stable_artifact(
        root,
        artifact_id=_MAPPING.artifact_id,
        artifact_kind=_MAPPING.artifact_kind,
        semantic_version=_MAPPING.semantic_version,
    )
    fixture_relative = mapping.payload.get("fixture_root")
    replay = mapping.payload.get("ordered_replay")
    if (
        mapping.payload.get("candidate") != CANDIDATE.to_value()
        or fixture_relative
        != "private/downstream-fixtures/luanti/14757/lz-luanti-14757-math-round/v2"
        or not isinstance(replay, list)
        or not replay
        or replay[-1]
        != {
            "type": "luanti_worldmod_math_round_boundary",
            "command": "/c1_lazarus_round",
        }
    ):
        raise RuntimeError("Luanti 14757 bounded launch mapping is invalid")
    fixture = _ordinary_directory(root / PurePosixPath(fixture_relative), root)
    for relative in ("mods/c1_lazarus/init.lua", "mods/c1_lazarus/mod.conf"):
        _ordinary_file(fixture / PurePosixPath(relative), fixture)
    registered = preflight.payload.get("registered_application_paths")
    if not isinstance(registered, list) or len(registered) != 2:
        raise RuntimeError("Luanti 14757 application pair is invalid")
    applications: dict[str, Path] = {}
    for variant, relative in zip(("fixed", "buggy"), registered, strict=True):
        expected = f"private/luanti-14757-formal-materialization/{variant}"
        if relative != expected:
            raise RuntimeError("Luanti 14757 application pair is invalid")
        application = _ordinary_directory(root / PurePosixPath(relative), root)
        binary = _ordinary_file(
            application / "build-app/out/minetest.app/Contents/MacOS/minetest",
            application,
        )
        if not os.access(binary, os.X_OK):
            raise RuntimeError("Luanti 14757 application binary is not executable")
        applications[variant] = application
    return mapping, fixture, applications


def _materialize_run(
    root: Path,
    run: Path,
    *,
    variant: Literal["fixed", "buggy"],
    application: Path,
    fixture: Path,
    watchdog_seconds: int,
) -> BoundedLaunchSpec:
    target = run / variant
    target.mkdir(mode=0o700)
    world = target / "world"
    mod = world / "worldmods/c1_lazarus"
    mod.parent.mkdir(parents=True, mode=0o700)
    shutil.copytree(fixture / "mods/c1_lazarus", mod)
    (world / "world.mt").write_text(
        "gameid = devtest\nbackend = sqlite3\nplayer_backend = sqlite3\n"
        "auth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\ncreative_mode = true\nenable_damage = false\n"
        "screen_w = 1280\nscreen_h = 720\nfullscreen = false\n"
        "window_maximized = false\ntime_speed = 0\n",
        encoding="utf-8",
    )
    binary = application / "build-app/out/minetest.app/Contents/MacOS/minetest"
    return BoundedLaunchSpec(
        variant,
        application,
        binary,
        fixture,
        target,
        world,
        config,
        target / "luanti.log",
        watchdog_seconds,
    )


def _persist_raw_observation(
    root: Path, spec: BoundedLaunchSpec, observation: BoundedLaunchObservation
) -> str:
    path = spec.run_root / "raw-observation.json"
    stdout = observation.stdout[:_MAX_OUTPUT]
    stderr = observation.stderr[:_MAX_OUTPUT]
    document = {
        **asdict(observation),
        "stdout": base64.b64encode(stdout).decode("ascii"),
        "stderr": base64.b64encode(stderr).decode("ascii"),
        "stdout_truncated": len(observation.stdout) > _MAX_OUTPUT,
        "stderr_truncated": len(observation.stderr) > _MAX_OUTPUT,
        "output_encoding": "base64",
    }
    _atomic_json(path, document)
    return path.relative_to(root).as_posix()


def _persist_attempt(
    root: Path,
    *,
    spec: BoundedLaunchSpec,
    observation: BoundedLaunchObservation,
    raw_path: str,
    inputs: tuple[ArtifactReference, ArtifactReference],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    semantic_match = _semantic_match(spec.variant, observation)
    reference = ArtifactReference(
        f"bounded_launch_attempt/luanti/14757/{spec.variant}/current/v1",
        "bounded_launch_attempt",
        _VERSION,
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "variant": spec.variant,
        "run_root": spec.run_root.relative_to(root).as_posix(),
        "fixture_version": "v2",
        "watchdog_seconds": spec.watchdog_seconds,
        "process_outcome": observation.process_outcome,
        "returncode": observation.returncode,
        "start_observation": {
            "marker": observation.start_marker,
            "result": observation.start_result,
        },
        "action_observation": {
            "command": observation.action_command,
            "count": observation.action_count,
        },
        "semantic_result": observation.semantic_result,
        "semantic_match": semantic_match,
        "raw_observation_path": raw_path,
        "candidate_execution_performed": True,
        "state": "downstream_pending",
        "permanent_terminal_written": False,
        "complete": True,
    }
    return _write(root, reference, inputs, payload, authorization)


def _semantic_match(variant: str, observation: BoundedLaunchObservation) -> bool:
    return (
        observation.process_outcome == "completed"
        and observation.start_marker == "basenodes:cobble"
        and observation.start_result == ""
        and observation.action_command == "/c1_lazarus_round"
        and observation.action_count == 1
        and (
            observation.semantic_result == "0,0"
            if variant == "fixed"
            else observation.semantic_result not in {None, "0,0"}
        )
    )


def _persist_aggregate(
    root: Path,
    attempts: tuple[StableArtifact, ...],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    semantic_pair = len(attempts) == 2 and all(
        item.payload.get("semantic_match") is True for item in attempts
    )
    reference = ArtifactReference(
        "bounded_launch_receipt/luanti/14757/fixed-buggy/current/v1",
        "bounded_launch_receipt",
        _VERSION,
    )
    payload = {
        "candidate": CANDIDATE.to_value(),
        "fixture_version": "v2",
        "attempt_count": len(attempts),
        "semantic_pair_observed": semantic_pair,
        "state": "downstream_pending",
        "next_action": (
            "capture_repeatable_visual_goldens"
            if semantic_pair
            else "retry_bounded_launch_or_resolve_infrastructure"
        ),
        "candidate_execution_performed": True,
        "permanent_terminal_written": False,
        "complete": True,
    }
    return _write(
        root,
        reference,
        tuple(item.reference for item in attempts),
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
    return load_stable_artifact(
        root,
        artifact_id=reference.artifact_id,
        artifact_kind=reference.artifact_kind,
        semantic_version=reference.semantic_version,
    )


def _new_run_root(root: Path, requested: Path) -> Path:
    allowed = root / "private/bounded-runs/luanti/14757"
    allowed.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        requested.relative_to(allowed)
    except ValueError as error:
        raise RuntimeError(
            "bounded launch run root is outside the registered root"
        ) from error
    if requested.exists() or requested.is_symlink():
        raise RuntimeError("bounded launch run root already exists")
    requested.mkdir(parents=False, mode=0o700)
    return requested.resolve(strict=True)


def _atomic_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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
    "EXECUTION_CONFIRMATION",
    "BoundedLaunchObservation",
    "BoundedLaunchResult",
    "BoundedLaunchSpec",
    "MacOS14757LaunchDriver",
    "execute_luanti_14757_bounded_launch",
]
