"""Native warm-session support for OpenRA C3 fuzzing.

OpenRA is not a browser-state benchmark: a candidate must be injected into a
real loaded map while preserving the fixed ``mod`` and ``map`` selected by the
oracle.  This module owns the small file protocol used by the instrumented
engine and the deliberately narrow fuzz-state projection exposed to the LLM.

The protocol is intentionally request/report/ready rather than a collection of
launch arguments.  A single process owns one fixed map, handles a monotonically
increasing sequence of requests, and writes ``ready`` only after it has restored
the clean map baseline for the next request.
"""
from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import run_context
from .openra_runtime import OpenRARuntimeAdapter


C3_SESSION_VERSION = "c3-session-v1"

# These are data identifiers, never paths or command-line fragments.  Keeping
# them intentionally small prevents an LLM candidate from escaping the fixed
# map/mod process through a JSON field that the native probe later interprets.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CELL = re.compile(r"^-?\d+,-?\d+$")
_MAX_PLAYERS = 32
_MAX_ACTIONS = 8
_MAX_TICKS = 900
_MAX_CASH = 1_000_000
_MAX_PLACEMENT_RADIUS = 64


class OpenRAFuzzStateError(ValueError):
    """A candidate is outside the public C3 fuzz-state schema."""


class OpenRAWarmSessionError(RuntimeError):
    """The native warm session could not be bootstrapped."""


def _safe_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or ".." in value:
        raise OpenRAFuzzStateError(
            f"{field_name} must be a safe identifier (1-64 alphanumeric/._- characters)"
        )
    return value


def _bounded_int(value: Any, field_name: str, *, minimum: int = 0, maximum: int = _MAX_TICKS) -> int:
    # ``bool`` is an ``int`` subclass; accepting it here would make an invalid
    # fuzzer shape look like a normal zero/one tick value.
    if type(value) is not int or not minimum <= value <= maximum:
        raise OpenRAFuzzStateError(f"{field_name} must be an integer in [{minimum}, {maximum}]")
    return value


def _bool(value: Any, field_name: str) -> bool:
    if type(value) is not bool:
        raise OpenRAFuzzStateError(f"{field_name} must be a boolean")
    return value


def _cell(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _CELL.fullmatch(value):
        raise OpenRAFuzzStateError(f"{field_name} must be a coordinate like '28,76'")
    return value


def _reject_extra(mapping: dict[str, Any], allowed: set[str], field_name: str) -> None:
    extras = sorted(str(key) for key in mapping if key not in allowed)
    if extras:
        raise OpenRAFuzzStateError(f"{field_name} contains unsupported field(s): {', '.join(extras)}")


def _sanitize_action(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OpenRAFuzzStateError(f"actions[{index}] must be an object")
    action_type = raw.get("type")
    prefix = f"actions[{index}]"

    if action_type == "openra_wait_ticks":
        _reject_extra(raw, {"type", "ticks"}, prefix)
        if set(raw) != {"type", "ticks"}:
            raise OpenRAFuzzStateError(f"{prefix} wait action requires type and ticks")
        return {"type": action_type, "ticks": _bounded_int(raw["ticks"], f"{prefix}.ticks")}

    if action_type == "openra_deploy_actor":
        allowed = {"type", "player", "actor_id", "wait_until_actor", "timeout_ticks"}
        _reject_extra(raw, allowed, prefix)
        required = {"type", "player", "actor_id"}
        if not required.issubset(raw):
            raise OpenRAFuzzStateError(f"{prefix} deploy action requires type, player, and actor_id")
        result: dict[str, Any] = {
            "type": action_type,
            "player": _safe_id(raw["player"], f"{prefix}.player"),
            "actor_id": _safe_id(raw["actor_id"], f"{prefix}.actor_id"),
        }
        if "wait_until_actor" in raw:
            result["wait_until_actor"] = _safe_id(raw["wait_until_actor"], f"{prefix}.wait_until_actor")
        if "timeout_ticks" in raw:
            result["timeout_ticks"] = _bounded_int(raw["timeout_ticks"], f"{prefix}.timeout_ticks")
        return result

    if action_type == "openra_build_actor":
        allowed = {
            "type", "player", "actor", "queue", "timeout_ticks", "place_at", "place_near",
            "post_complete_grace_ticks",
        }
        _reject_extra(raw, allowed, prefix)
        required = {"type", "player", "actor"}
        if not required.issubset(raw):
            raise OpenRAFuzzStateError(f"{prefix} build action requires type, player, and actor")
        result = {
            "type": action_type,
            "player": _safe_id(raw["player"], f"{prefix}.player"),
            "actor": _safe_id(raw["actor"], f"{prefix}.actor"),
        }
        if "queue" in raw:
            result["queue"] = _safe_id(raw["queue"], f"{prefix}.queue")
        if "timeout_ticks" in raw:
            result["timeout_ticks"] = _bounded_int(raw["timeout_ticks"], f"{prefix}.timeout_ticks")
        if "post_complete_grace_ticks" in raw:
            result["post_complete_grace_ticks"] = _bounded_int(
                raw["post_complete_grace_ticks"], f"{prefix}.post_complete_grace_ticks"
            )
        if "place_at" in raw:
            result["place_at"] = _cell(raw["place_at"], f"{prefix}.place_at")
        if "place_near" in raw:
            near = raw["place_near"]
            if not isinstance(near, dict):
                raise OpenRAFuzzStateError(f"{prefix}.place_near must be an object")
            _reject_extra(near, {"anchor", "radius"}, f"{prefix}.place_near")
            if "anchor" not in near:
                raise OpenRAFuzzStateError(f"{prefix}.place_near.anchor is required")
            sanitized_near: dict[str, Any] = {
                "anchor": _cell(near["anchor"], f"{prefix}.place_near.anchor"),
            }
            if "radius" in near:
                sanitized_near["radius"] = _bounded_int(
                    near["radius"], f"{prefix}.place_near.radius", maximum=_MAX_PLACEMENT_RADIUS
                )
            result["place_near"] = sanitized_near
        return result

    raise OpenRAFuzzStateError(f"{prefix}.type is not an OpenRA gameplay action")


def sanitize_openra_fuzz_state(candidate: Any) -> dict[str, Any]:
    """Validate and canonicalize the only deep-state shape OpenRA C3 may inject.

    Invalid *shape* is deliberately rejected before it ever reaches the engine.  A
    valid request may still name an unknown actor/player or request impossible
    production; the engine receives those and reports a normal robustness result.
    """

    if not isinstance(candidate, dict):
        raise OpenRAFuzzStateError("fuzz_state must be an object")
    _reject_extra(candidate, {"players", "actions"}, "fuzz_state")
    if set(candidate) != {"players", "actions"}:
        raise OpenRAFuzzStateError("fuzz_state requires exactly players and actions")
    players = candidate["players"]
    actions = candidate["actions"]
    if not isinstance(players, list) or len(players) > _MAX_PLAYERS:
        raise OpenRAFuzzStateError(f"fuzz_state.players must be a list with at most {_MAX_PLAYERS} entries")
    if not isinstance(actions, list) or len(actions) > _MAX_ACTIONS:
        raise OpenRAFuzzStateError(f"fuzz_state.actions must be a list with at most {_MAX_ACTIONS} entries")

    sanitized_players: list[dict[str, Any]] = []
    seen_players: set[str] = set()
    for index, raw in enumerate(players):
        if not isinstance(raw, dict):
            raise OpenRAFuzzStateError(f"players[{index}] must be an object")
        _reject_extra(raw, {"id", "cash", "fast_build", "build_anywhere"}, f"players[{index}]")
        if set(raw) != {"id", "cash", "fast_build", "build_anywhere"}:
            raise OpenRAFuzzStateError(
                f"players[{index}] requires exactly id, cash, fast_build, and build_anywhere"
            )
        player_id = _safe_id(raw["id"], f"players[{index}].id")
        if player_id.lower() in seen_players:
            raise OpenRAFuzzStateError(f"players[{index}].id duplicates a previous player")
        seen_players.add(player_id.lower())
        sanitized_players.append(
            {
                "id": player_id,
                "cash": _bounded_int(raw["cash"], f"players[{index}].cash", maximum=_MAX_CASH),
                "fast_build": _bool(raw["fast_build"], f"players[{index}].fast_build"),
                "build_anywhere": _bool(raw["build_anywhere"], f"players[{index}].build_anywhere"),
            }
        )

    return {
        "players": sanitized_players,
        "actions": [_sanitize_action(action, index) for index, action in enumerate(actions)],
    }


def project_openra_fuzz_state(seed_spec: dict[str, Any]) -> dict[str, Any]:
    """Project a gameplay oracle into its immutable C3 fuzz-state seed.

    The map, mod, target metadata, assertions, and launch configuration never enter
    this object.  The LLM can therefore only perturb setup granted to named players
    and the bounded gameplay-action list.
    """

    checkpoint = seed_spec.get("checkpoint")
    strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else None
    if not isinstance(strategies, list) or "openra_gameplay_production_validate" not in strategies:
        raise OpenRAFuzzStateError("OpenRA C3 only supports openra_gameplay_production_validate specs")

    setup_root = seed_spec.get("setup")
    setup = setup_root.get("openra_gameplay_probe") if isinstance(setup_root, dict) else None
    if not isinstance(setup, dict):
        raise OpenRAFuzzStateError("OpenRA gameplay spec is missing setup.openra_gameplay_probe")
    cash = _bounded_int(setup.get("cash"), "setup.openra_gameplay_probe.cash", maximum=_MAX_CASH)
    fast_build = _bool(setup.get("fast_build"), "setup.openra_gameplay_probe.fast_build")
    build_anywhere = _bool(setup.get("build_anywhere"), "setup.openra_gameplay_probe.build_anywhere")

    raw_actions = seed_spec.get("actions")
    if not isinstance(raw_actions, list):
        raise OpenRAFuzzStateError("OpenRA gameplay spec actions must be a list")

    # The original probe enabled cheats for all combatant players.  Preserve that
    # useful baseline while including an action-only player id if an oracle omitted
    # it from target.players.
    player_ids: list[str] = []
    target = seed_spec.get("target")
    target_players = target.get("players") if isinstance(target, dict) else None
    if isinstance(target_players, list):
        for index, player in enumerate(target_players):
            if isinstance(player, dict) and "id" in player:
                player_id = _safe_id(player["id"], f"target.players[{index}].id")
                if player_id.lower() not in {item.lower() for item in player_ids}:
                    player_ids.append(player_id)
    for index, action in enumerate(raw_actions):
        if isinstance(action, dict) and "player" in action:
            player_id = _safe_id(action["player"], f"actions[{index}].player")
            if player_id.lower() not in {item.lower() for item in player_ids}:
                player_ids.append(player_id)

    state = {
        "players": [
            {"id": player_id, "cash": cash, "fast_build": fast_build, "build_anywhere": build_anywhere}
            for player_id in player_ids
        ],
        "actions": copy.deepcopy(raw_actions),
    }
    return sanitize_openra_fuzz_state(state)


@dataclass
class OpenRAWarmOutcome:
    """One candidate round-trip through the native C3 session."""

    sequence: int
    status: str  # robust | rejected | engine_error
    category: str | None = None
    detail: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None
    reset_ready: bool = False
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OpenRAWarmSession:
    """A fixed-map OpenRA process driven by C3 request/report/ready files."""

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        mod: str,
        map_id: str,
        bootstrap_timeout_s: float | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.mod = _safe_id(mod, "mod")
        self.map_id = _safe_id(map_id, "map")
        self.bootstrap_timeout_s = bootstrap_timeout_s
        self.benchmark = "OpenRA"
        self.git_commit = run_context.git_commit(self.repo)
        self.request_path = self.report_dir / "warptest-openra-c3-request.json"
        self.report_path = self.report_dir / "warptest-openra-c3-report.json"
        self.ready_path = self.report_dir / "warptest-openra-c3-ready.json"
        self.stdout_path = self.report_dir / "openra-warm.stdout.log"
        self.stderr_path = self.report_dir / "openra-warm.stderr.log"
        self.support_dir = self.report_dir / "openra-support"
        self.screenshot_dir = self.report_dir / "screenshots"
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self._sequence = 0
        self.bootstrap_ms: int | None = None
        self.launch_command: list[str] = []

    def __enter__(self) -> "OpenRAWarmSession":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "OpenRAWarmSession":
        if self.is_alive():
            return self
        self.close()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.request_path, self.report_path, self.ready_path):
            path.unlink(missing_ok=True)

        # Reuse the normal native-runtime build/content helpers so C3 starts the
        # same game binary and campaign content as the regular gameplay benchmark.
        adapter = OpenRARuntimeAdapter(self.repo, self.report_dir)
        adapter._ensure_utility_built()
        self.support_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        adapter._ensure_openra_content(self.mod, self.support_dir)

        self._sequence = 0
        self.launch_command = [
            "./launch-game.sh",
            f"Game.Mod={self.mod}",
            f"Engine.SupportDir={self.support_dir.resolve()}",
            f"Launch.Map={self.map_id}",
            f"Launch.WarptestFuzzRequestPath={self.request_path.resolve()}",
            f"Launch.WarptestFuzzReportPath={self.report_path.resolve()}",
            f"Launch.WarptestFuzzReadyPath={self.ready_path.resolve()}",
            f"Launch.WarptestFuzzScreenshotDir={self.screenshot_dir.resolve()}",
            "Game.FetchNews=false",
            "Game.EnableDiscordService=false",
            "Sound.Mute=true",
            "Server.AdvertiseOnline=false",
            "Server.AdvertiseOnLocalNetwork=false",
        ]
        env = {**os.environ, "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "Major")}
        self._stdout_handle = self.stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = self.stderr_path.open("w", encoding="utf-8")
        started = time.perf_counter()
        try:
            self._proc = subprocess.Popen(
                self.launch_command,
                cwd=self.repo,
                env=env,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                start_new_session=True,
            )
            timeout = self.bootstrap_timeout_s
            if timeout is None:
                timeout = float(os.environ.get("WARPTEST_OPENRA_WARM_BOOT_S", "120"))
            ready, problem = self._wait_for_ready(0, timeout_s=timeout)
            if ready is None:
                raise OpenRAWarmSessionError(f"OpenRA warm session did not become ready: {problem}")
            self.bootstrap_ms = int((time.perf_counter() - started) * 1000)
            return self
        except Exception:
            self.close()
            raise

    def restart(self) -> "OpenRAWarmSession":
        """Start a clean process after a candidate damaged liveness/reset state."""

        self.close()
        return self.start()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    handle.close()
                finally:
                    setattr(self, attr, None)

    def flush_logs(self) -> None:
        """Flush parent-owned log descriptors before evidence copies are taken."""

        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None:
                try:
                    handle.flush()
                except OSError:
                    pass

    def run(self, fuzz_state: dict[str, Any], *, timeout_s: float = 90.0) -> OpenRAWarmOutcome:
        """Inject one already-bounded candidate and wait for its baseline reset."""

        try:
            state = sanitize_openra_fuzz_state(fuzz_state)
        except OpenRAFuzzStateError as error:
            return OpenRAWarmOutcome(0, "rejected", "schema", str(error))
        if not self.is_alive():
            return OpenRAWarmOutcome(
                self._sequence, "engine_error", "process_exit", "OpenRA warm session is not running"
            )

        self._sequence += 1
        sequence = self._sequence
        request = {"version": C3_SESSION_VERSION, "sequence": sequence, "fuzz_state": state}
        started = time.perf_counter()
        try:
            self._atomic_json(self.request_path, request)
        except Exception as error:
            return OpenRAWarmOutcome(sequence, "rejected", "protocol", f"could not publish request: {error}")

        report, problem = self._wait_for_report(sequence, timeout_s=timeout_s)
        elapsed = int((time.perf_counter() - started) * 1000)
        if report is None:
            category = "process_exit" if problem == "process exited" else "timeout"
            return OpenRAWarmOutcome(sequence, "engine_error", category, problem or "no report", elapsed_ms=elapsed)

        outcome = self._outcome_from_report(sequence, report, elapsed_ms=elapsed)
        if outcome.status == "rejected" and outcome.category == "protocol":
            return outcome
        ready, ready_problem = self._wait_for_ready(sequence, timeout_s=timeout_s)
        if ready is None:
            category = "reset_process_exit" if ready_problem == "process exited" else "reset_timeout"
            return OpenRAWarmOutcome(
                sequence,
                "engine_error",
                category,
                f"candidate report was written but baseline reset did not become ready: {ready_problem}",
                checks=outcome.checks,
                errors=outcome.errors,
                summary=outcome.summary,
                report=outcome.report,
                screenshot_path=outcome.screenshot_path,
                reset_ready=False,
                elapsed_ms=elapsed,
            )
        outcome.reset_ready = True
        return outcome

    def _wait_for_report(self, sequence: int, *, timeout_s: float) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            report = self._read_json(self.report_path)
            if report is not None and report.get("sequence") == sequence:
                if report.get("version") != C3_SESSION_VERSION:
                    return {
                        "version": report.get("version"),
                        "sequence": sequence,
                        "status": "rejected",
                        "category": "protocol",
                        "detail": "native report uses an unexpected C3 session version",
                    }, None
                return report, None
            # A native exception can flush its report immediately before the
            # process exits. Read the matching report first so defect artifacts
            # retain that evidence rather than mislabelling it as report-less.
            if not self.is_alive():
                return None, "process exited"
            time.sleep(0.05)
        return None, f"timed out waiting for OpenRA report sequence={sequence}"

    def _wait_for_ready(self, sequence: int, *, timeout_s: float) -> tuple[dict[str, Any] | None, str | None]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if not self.is_alive():
                return None, "process exited"
            ready = self._read_json(self.ready_path)
            if ready is not None and ready.get("sequence") == sequence:
                if ready.get("version") != C3_SESSION_VERSION or ready.get("status") != "ready":
                    return None, "native ready record has an unexpected protocol shape"
                return ready, None
            time.sleep(0.05)
        return None, f"timed out waiting for OpenRA ready sequence={sequence}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        # ``allow_nan=False`` is intentional: non-finite values are a structural
        # rejection, not a malformed protocol payload that can poison the session.
        temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _outcome_from_report(sequence: int, report: dict[str, Any], *, elapsed_ms: int) -> OpenRAWarmOutcome:
        raw_status = str(report.get("status") or "").lower()
        category = str(report.get("category")) if report.get("category") is not None else None
        detail = str(report.get("detail") or "")[:1000]
        checks = [item for item in report.get("checks", []) if isinstance(item, dict)]
        errors = [str(item) for item in report.get("errors", [])] if isinstance(report.get("errors"), list) else []
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        screenshot = report.get("screenshot_path")
        screenshot_path = str(screenshot) if isinstance(screenshot, str) and screenshot else None

        if raw_status in {"robust", "success", "accepted", "ok"}:
            status = "robust"
        elif raw_status in {"rejected", "invalid", "schema"}:
            status = "rejected"
        elif raw_status in {"engine_error", "engine", "crash", "timeout"}:
            status = "engine_error"
        elif raw_status == "failure" and category in {
            "unknown_actor", "unknown_player", "precondition", "prerequisite", "queue_rejected",
            "action_rejected", "robust",
        }:
            # Backward-compatible native reports may still say ``failure`` for
            # gameplay-level rejections.  They are expected robustness behaviour,
            # not a C3 defect.
            status = "robust"
        else:
            # Do not turn a malformed or unknown harness response into a game bug.
            status = "rejected"
            category = category or "protocol"
            detail = detail or f"unexpected native OpenRA C3 report status: {raw_status or '<missing>'}"
        return OpenRAWarmOutcome(
            sequence=sequence,
            status=status,
            category=category,
            detail=detail,
            checks=checks,
            errors=errors,
            summary=summary,
            report=copy.deepcopy(report),
            screenshot_path=screenshot_path,
            elapsed_ms=elapsed_ms,
        )
