"""Live, public-settings C1 runtime for OpenRA.

The existing OpenRA gameplay adapter is deliberately one-shot: a test-gated
probe executes a complete action list, captures a frame, and exits.  That is a
good semantic checkpoint runner, but it cannot be handed to a pixel policy
without letting the probe perform the policy's transition for it.

This module therefore uses a different, public OpenRA surface for C1: the
normal Settings UI and its isolated ``settings.yaml``.  Agent-S changes the
setting through the real foreground UI; the grader only reads the public
settings file.  The state arm writes the same setting transition directly.
No gameplay probe action is allowed to run during an Agent-S phase.
"""

from __future__ import annotations

import os
import json
import hashlib
import copy
import signal
import subprocess
import time
import uuid
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .c1_background_window_guard import MacOSBackgroundWindowGuard
from .native_input import QuartzNativeInput
from .openra_runtime import OpenRARuntimeAdapter
from .policy_frame import copy_fixed_policy_frame


_SECTIONS = frozenset({"Player", "Game", "Sound", "Server", "Debug"})
_SAFE_OPENRA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ALLOWED_SETTING_KEYS = frozenset(
    {
        ("Player", "Name"),
        ("Game", "ViewportEdgeScroll"),
        ("Game", "TargetLines"),
        ("Game", "StatusBars"),
        ("Game", "IntroductionPromptVersion"),
        ("Debug", "SystemInformationVersionPrompt"),
        ("Debug", "SendSystemInformation"),
    }
)


def _pid_is_live(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


OPENRA_DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "Player": {"Name": "Newbie"},
    "Game": {
        "ViewportEdgeScroll": True,
        "TargetLines": "Manual",
        "StatusBars": "Standard",
        "IntroductionPromptVersion": 0,
    },
    "Debug": {
        "SystemInformationVersionPrompt": 0,
        "SendSystemInformation": True,
    },
}


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (str, int, float)) and not isinstance(value, complex):
        text = str(value).strip()
        if "\n" in text or "\r" in text or "\t" in text:
            raise ValueError("OpenRA setting values must be single-line scalars")
        return text
    raise ValueError(f"Unsupported OpenRA setting scalar: {value!r}")


def _coerce_scalar(value: str) -> Any:
    text = value.strip()
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def read_openra_settings(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read the shallow public portions of OpenRA's MiniYaml settings file."""

    source = Path(path)
    if not source.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    section: str | None = None
    for raw in source.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw == raw.lstrip() and raw.rstrip().endswith(":"):
            candidate = raw.rstrip()[:-1].strip()
            section = candidate if candidate in _SECTIONS else None
            if section is not None:
                result.setdefault(section, {})
            continue
        if section is None or ":" not in raw:
            continue
        key, value = raw.strip().split(":", 1)
        result[section][key.strip()] = _coerce_scalar(value)
    return result


def read_effective_openra_settings(path: str | Path) -> dict[str, dict[str, Any]]:
    """Return persisted values overlaid on the engine's public defaults."""

    persisted = read_openra_settings(path)
    return {
        section: {**defaults, **persisted.get(section, {})}
        for section, defaults in OPENRA_DEFAULT_SETTINGS.items()
    } | {
        section: values
        for section, values in persisted.items()
        if section not in OPENRA_DEFAULT_SETTINGS
    }


def write_openra_settings(
    path: str | Path,
    settings: Mapping[str, Mapping[str, Any]],
) -> Path:
    """Write a bounded MiniYaml settings document into an isolated support dir."""

    destination = Path(path).expanduser().resolve()
    lines: list[str] = []
    for section in ("Player", "Game", "Sound", "Server", "Debug"):
        values = settings.get(section)
        if not isinstance(values, Mapping) or not values:
            continue
        lines.append(f"{section}:")
        for key, value in values.items():
            if (
                section in {"Player", "Game", "Debug"}
                and (
                    section,
                    str(key),
                )
                not in _ALLOWED_SETTING_KEYS
            ):
                # Infrastructure-only flags are allowlisted separately below.
                if (section, str(key)) not in {
                    ("Game", "FetchNews"),
                    ("Game", "EnableDiscordService"),
                }:
                    raise ValueError(f"Unsupported OpenRA C1 setting: {section}.{key}")
            lines.append(f"\t{key}: {_scalar_text(value)}")
        lines.append("")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return destination


def target_settings(spec_or_target: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    target = (
        spec_or_target.get("target") if "target" in spec_or_target else spec_or_target
    )
    if not isinstance(target, Mapping) or target.get("kind") != "openra_settings":
        raise ValueError("OpenRA C1 target.kind must be openra_settings")
    raw = target.get("settings")
    if not isinstance(raw, Mapping):
        raise ValueError("OpenRA C1 target.settings must be a mapping")
    selected: dict[str, dict[str, Any]] = {}
    for section, values in raw.items():
        if section not in _SECTIONS or not isinstance(values, Mapping):
            raise ValueError(f"Unsupported OpenRA C1 settings section: {section!r}")
        selected[str(section)] = {str(key): value for key, value in values.items()}
    return selected


def apply_setting_actions(
    settings: dict[str, dict[str, Any]],
    actions: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result = {section: dict(values) for section, values in settings.items()}
    for action in actions:
        if action.get("type") == "wait":
            continue
        if action.get("type") != "openra_set_setting":
            raise ValueError(f"Unsupported OpenRA C1 action: {action.get('type')!r}")
        section = str(action.get("section") or "")
        key = str(action.get("key") or "")
        if (section, key) not in _ALLOWED_SETTING_KEYS:
            raise ValueError(f"Unsupported OpenRA C1 setting action: {section}.{key}")
        result.setdefault(section, {})[key] = action.get("value")
    return result


@dataclass(frozen=True)
class OpenRASettingsGrade:
    ok: bool
    status: str
    detail: dict[str, Any]


class OpenRASettingsSession:
    """Persistent normal OpenRA shell with an isolated public settings file."""

    execution_backend = "background_pixel_v1"
    foreground_required = False
    coordinate_space = "frame_pixels_top_left_v1"
    capture_backend = "openra_renderer_save_screenshot_sync_v1"
    input_backend = "openra_default_input_handler_v1"

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        settings: Mapping[str, Mapping[str, Any]],
        mod: str = "ra",
        experiment_seed: int = 0,
        bootstrap_timeout_s: float = 120.0,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.support_dir = self.report_dir / "openra-support"
        self.settings_path = self.support_dir / "settings.yaml"
        self.mod = str(mod)
        self.experiment_seed = int(experiment_seed)
        self.bootstrap_timeout_s = float(bootstrap_timeout_s)
        self._requested_settings = {
            str(section): {str(key): value for key, value in values.items()}
            for section, values in settings.items()
        }
        self._proc: subprocess.Popen[str] | None = None
        self._stdout_handle: Any | None = None
        self._stderr_handle: Any | None = None
        self.native: QuartzNativeInput | None = None
        self.session_id = uuid.uuid4().hex
        self.last_policy_frame_id: int | None = None
        self.width = 1280
        self.height = 720
        self._background_sequence = 0
        self._capture_sequence = 0
        self._background_request_path = self.report_dir / "openra-background-request.json"
        self._background_response_path = self.report_dir / "openra-background-response.json"
        self._window_pid: int | None = None
        self.logs: list[str] = []
        self.app_url = None
        self._background_window_guard = MacOSBackgroundWindowGuard(self.report_dir)
        self._hidden_at_create_provenance: dict[str, Any] = {}

    def __enter__(self) -> "OpenRASettingsSession":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def hold_pid(self) -> int | None:
        if self._proc is None or self._proc.poll() is not None:
            return None
        return self._window_pid or self._proc.pid

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> "OpenRASettingsSession":
        if self.is_alive():
            return self
        self.close()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        adapter = OpenRARuntimeAdapter(self.repo, self.report_dir)
        adapter._ensure_utility_built()
        hidden_source = self.repo / "OpenRA.Platforms.Default" / "Sdl2PlatformWindow.cs"
        platform_dll = self.repo / "bin" / "OpenRA.Platforms.Default.dll"
        if not hidden_source.is_file() or not platform_dll.is_file():
            raise RuntimeError("OpenRA hidden-at-create build provenance is unavailable")
        source_bytes = hidden_source.read_bytes()
        if b'WARPTEST_C1_BACKGROUND_WINDOW' not in source_bytes:
            raise RuntimeError("OpenRA hidden-at-create source marker is missing")
        if platform_dll.stat().st_mtime < hidden_source.stat().st_mtime:
            raise RuntimeError("OpenRA hidden-at-create platform assembly is stale")
        self._hidden_at_create_provenance = {
            "policy": "sdl_window_hidden_at_create_v1",
            "requested": True,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "binary_sha256": hashlib.sha256(platform_dll.read_bytes()).hexdigest(),
            "binary_fresh": True,
        }
        self.support_dir.mkdir(parents=True, exist_ok=True)
        adapter._ensure_openra_content(self.mod, self.support_dir)
        baseline = {
            **self._requested_settings,
            "Game": {
                **self._requested_settings.get("Game", {}),
                "FetchNews": False,
                "EnableDiscordService": False,
                # Avoid the first-run dialog; C1 tasks exercise the real Settings UI.
                "IntroductionPromptVersion": int(
                    self._requested_settings.get("Game", {}).get(
                        "IntroductionPromptVersion", 999
                    )
                ),
            },
            "Sound": {"Mute": True},
            "Server": {
                "AdvertiseOnline": False,
                "AdvertiseOnLocalNetwork": False,
            },
            "Debug": {
                # These are public settings. Pre-seeding the already-seen version
                # avoids a first-run modal that would otherwise obscure every C1
                # screenshot without changing the task setting under study.
                "SystemInformationVersionPrompt": 6,
                "SendSystemInformation": False,
            },
        }
        write_openra_settings(self.settings_path, baseline)
        stdout_path = self.report_dir / "openra-c1.stdout.log"
        stderr_path = self.report_dir / "openra-c1.stderr.log"
        self.logs.extend([str(stdout_path), str(stderr_path), str(self.settings_path)])
        self._stdout_handle = stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = stderr_path.open("w", encoding="utf-8")
        command = [
            "./launch-game.sh",
            f"Game.Mod={self.mod}",
            f"Engine.SupportDir={self.support_dir.resolve()}",
            "Game.FetchNews=false",
            "Game.EnableDiscordService=false",
            "Sound.Mute=true",
            "Server.AdvertiseOnline=false",
            "Server.AdvertiseOnLocalNetwork=false",
            "Graphics.Mode=Windowed",
            "Graphics.WindowedSize=1280,720",
            f"Launch.WarptestBackgroundRequestPath={self._background_request_path.resolve()}",
            f"Launch.WarptestBackgroundResponsePath={self._background_response_path.resolve()}",
            f"Launch.WarptestBackgroundSessionId={self.session_id}",
            f"Launch.WarptestBackgroundRoot={self.report_dir.resolve()}",
        ]
        env = {
            **os.environ,
            "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "Major"),
            "SDL_MAC_BACKGROUND_APP": "1",
            "WARPTEST_C1_BACKGROUND_WINDOW": "1",
        }
        try:
            self._background_window_guard.prepare()
            self._proc = subprocess.Popen(
                command,
                cwd=str(self.repo),
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                env=env,
                start_new_session=True,
            )
            self._background_window_guard.start(self._proc.pid)
            self._window_pid = self._proc.pid
            self.capture_policy_frame(
                self.report_dir / "openra-background-bootstrap.png",
                timeout_s=self.bootstrap_timeout_s,
            )
            self._background_window_guard.await_hidden()
            return self
        except Exception:
            self.close()
            raise

    def screenshot(self, path: str | Path, full_page: bool = False) -> str:
        del full_page
        return self.capture_policy_frame(path)

    def policy_frame_dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    def background_guard_metadata(self) -> dict[str, Any]:
        return {
            **self._background_window_guard.metadata(),
            "hidden_at_create": dict(self._hidden_at_create_provenance),
        }

    def _background_request(
        self, operation: str, *, timeout_s: float | None = None, **fields: Any
    ) -> dict[str, Any]:
        if not self.is_alive():
            raise RuntimeError("OpenRA background runtime is not alive")
        self._background_sequence += 1
        sequence = self._background_sequence
        self._background_response_path.unlink(missing_ok=True)
        payload = {
            "schema_version": 1,
            "session_id": self.session_id,
            "sequence": sequence,
            "operation": operation,
            **fields,
        }
        temporary = self._background_request_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(temporary, self._background_request_path)
        deadline = time.monotonic() + float(timeout_s or self.bootstrap_timeout_s)
        last_error = "response missing"
        while time.monotonic() < deadline:
            if not self.is_alive():
                raise RuntimeError("OpenRA exited during a background request")
            try:
                response = json.loads(
                    self._background_response_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                last_error = str(error)
                time.sleep(0.05)
                continue
            if not isinstance(response, dict) or response.get("sequence") != sequence:
                last_error = "stale or malformed response"
                continue
            if response.get("session_id") != self.session_id:
                raise RuntimeError("OpenRA background response session nonce mismatch")
            if response.get("operation") not in {operation, "error"}:
                raise RuntimeError("OpenRA background response operation mismatch")
            return response
        raise TimeoutError(f"OpenRA background {operation} timed out: {last_error}")

    def capture_policy_frame(
        self, path: str | Path, *, timeout_s: float | None = None
    ) -> str:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._capture_sequence += 1
        internal = (
            self.report_dir
            / "background-frames"
            / f"frame-{self._capture_sequence:08d}.png"
        ).resolve()
        internal.parent.mkdir(parents=True, exist_ok=True)
        result = self._background_request(
            "capture", timeout_s=timeout_s, output_path=str(internal)
        )
        if result.get("status") != "success" or result.get("accepted") is not True:
            raise RuntimeError(f"OpenRA framebuffer capture rejected: {result}")
        if (result.get("width"), result.get("height")) != (self.width, self.height):
            raise RuntimeError("OpenRA policy frame dimensions drifted from 1280x720")
        frame_id = result.get("frame_id")
        if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
            raise RuntimeError("OpenRA framebuffer capture returned an invalid frame id")
        if not internal.is_file():
            raise RuntimeError("OpenRA framebuffer capture produced no file")
        self.last_policy_frame_capture = copy_fixed_policy_frame(
            internal,
            destination,
            width=self.width,
            height=self.height,
            backend=self.capture_backend,
        )
        self.last_policy_frame_id = frame_id
        return str(destination)

    def dispatch_input_batch(self, batch: Any) -> dict[str, Any]:
        if getattr(batch, "session_id", None) != self.session_id:
            raise RuntimeError("OpenRA input batch session mismatch")
        result = self._background_request(
            "input_batch",
            frame_id=getattr(batch, "frame_id", None),
            batch_id=getattr(batch, "batch_id", None),
            actions=[action.to_dict() for action in getattr(batch, "actions", ())],
        )
        if result.get("status") != "success" or result.get("accepted") is not True:
            raise RuntimeError(f"OpenRA input batch rejected: {result}")
        return result

    def grade_settings(
        self, expected: Mapping[str, Mapping[str, Any]]
    ) -> OpenRASettingsGrade:
        observed = read_effective_openra_settings(self.settings_path)
        checks: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        for section, values in expected.items():
            for key, value in values.items():
                actual = observed.get(str(section), {}).get(str(key))
                matches = type(actual) is type(value) and actual == value
                check = {
                    "section": str(section),
                    "key": str(key),
                    "expected": value,
                    "actual": actual,
                    "ok": matches,
                }
                checks.append(check)
                if not matches:
                    mismatches.append(
                        {
                            "section": str(section),
                            "key": str(key),
                            "expected": value,
                            "actual": actual,
                        }
                    )
        alive = self.is_alive()
        ok = alive and not mismatches
        return OpenRASettingsGrade(
            ok,
            "success" if ok else "settings_mismatch" if alive else "process_exit",
            {
                "process_alive": alive,
                "settings_path": str(self.settings_path),
                "checks": checks,
                "mismatches": mismatches,
            },
        )

    def grade_assertions(
        self, assertions: Iterable[Mapping[str, Any]]
    ) -> OpenRASettingsGrade:
        expected: dict[str, dict[str, Any]] = {}
        for assertion in assertions:
            kind = assertion.get("type")
            if kind == "no_openra_errors":
                continue
            if kind != "openra_setting_equals":
                return OpenRASettingsGrade(
                    False,
                    "unsupported_assertion",
                    {"assertion": dict(assertion)},
                )
            section = str(assertion.get("section") or "")
            key = str(assertion.get("key") or "")
            if (section, key) not in _ALLOWED_SETTING_KEYS:
                return OpenRASettingsGrade(
                    False,
                    "unsupported_assertion",
                    {"assertion": dict(assertion)},
                )
            expected.setdefault(section, {})[key] = assertion.get("expected")
        return self.grade_settings(expected)

    def seed_evidence(self, task_id: str) -> dict[str, Any]:
        current = read_effective_openra_settings(self.settings_path)
        process_id = self.hold_pid
        process_alive = self.is_alive() and _pid_is_live(process_id)
        return {
            "schema_version": 1,
            "observed": process_alive,
            "source": "openra_live_settings_process",
            "algorithm": "not_applicable_public_settings",
            "seed": self.experiment_seed,
            "process_id": process_id,
            "process_alive_at_observation": process_alive,
            "benchmark": "OpenRA",
            "task_id": str(task_id),
            "semantic_fingerprint": {
                section: {
                    key: values[key]
                    for key in sorted(values)
                    if (section, key) in _ALLOWED_SETTING_KEYS
                }
                for section, values in sorted(current.items())
                if isinstance(values, dict)
            },
        }

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        self.native = None
        self._window_pid = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
        self._background_window_guard.close()
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                try:
                    handle.close()
                finally:
                    setattr(self, attr, None)


class OpenRAGameplaySession(OpenRASettingsSession):
    """Persistent pixel-only OpenRA match with a read-only bounded grader."""

    semantic_source = "openra_live_gameplay_probe_v1"

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        spec: Mapping[str, Any],
        experiment_seed: int = 0,
        bootstrap_timeout_s: float = 120.0,
    ) -> None:
        super().__init__(
            repo,
            report_dir,
            settings=OPENRA_DEFAULT_SETTINGS,
            mod=str((spec.get("target") or {}).get("mod") or "ra"),
            experiment_seed=experiment_seed,
            bootstrap_timeout_s=bootstrap_timeout_s,
        )
        self.spec = copy.deepcopy(dict(spec))
        target = self.spec.get("target")
        if not isinstance(target, dict) or target.get("kind") != "openra_map":
            raise ValueError("OpenRA gameplay session requires target.kind=openra_map")
        self.map_id = str(target.get("map") or "")
        if not self.map_id or not _SAFE_OPENRA_ID.fullmatch(self.map_id):
            raise ValueError("OpenRA gameplay target.map is invalid")
        self.gameplay_request_path = self.report_dir / "openra-live-gameplay-request.json"
        self.gameplay_report_path = self.report_dir / "openra-live-gameplay-report.json"
        self._semantic_sequence = 0
        self._last_semantic_report: dict[str, Any] = {}

    def _write_gameplay_request(self, payload: Mapping[str, Any]) -> None:
        self.gameplay_request_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.gameplay_request_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.gameplay_request_path)

    def _wait_gameplay_report(
        self, sequence: int, *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        deadline = time.monotonic() + float(timeout_s or self.bootstrap_timeout_s)
        last_error = "report missing"
        while time.monotonic() < deadline:
            if not self.is_alive():
                raise RuntimeError("OpenRA exited during live gameplay grading")
            try:
                report = json.loads(
                    self.gameplay_report_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                last_error = str(error)
                time.sleep(0.05)
                continue
            if isinstance(report, dict) and report.get("sequence") == sequence:
                self._last_semantic_report = report
                return report
            last_error = "stale or malformed gameplay report"
            time.sleep(0.05)
        raise TimeoutError(
            f"OpenRA live gameplay grade sequence {sequence} timed out: {last_error}"
        )

    def start(self) -> "OpenRAGameplaySession":
        if self.is_alive():
            return self
        self.close()
        self.report_dir.mkdir(parents=True, exist_ok=True)
        adapter = OpenRARuntimeAdapter(self.repo, self.report_dir)
        adapter._ensure_utility_built()
        hidden_source = self.repo / "OpenRA.Platforms.Default" / "Sdl2PlatformWindow.cs"
        platform_dll = self.repo / "bin" / "OpenRA.Platforms.Default.dll"
        if not hidden_source.is_file() or not platform_dll.is_file():
            raise RuntimeError("OpenRA hidden-at-create build provenance is unavailable")
        source_bytes = hidden_source.read_bytes()
        if b"WARPTEST_C1_BACKGROUND_WINDOW" not in source_bytes:
            raise RuntimeError("OpenRA hidden-at-create source marker is missing")
        if platform_dll.stat().st_mtime < hidden_source.stat().st_mtime:
            raise RuntimeError("OpenRA hidden-at-create platform assembly is stale")
        self._hidden_at_create_provenance = {
            "policy": "sdl_window_hidden_at_create_v1",
            "requested": True,
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "binary_sha256": hashlib.sha256(platform_dll.read_bytes()).hexdigest(),
            "binary_fresh": True,
        }

        self.support_dir.mkdir(parents=True, exist_ok=True)
        adapter._ensure_openra_content(self.mod, self.support_dir)
        baseline = {
            **OPENRA_DEFAULT_SETTINGS,
            "Game": {
                **OPENRA_DEFAULT_SETTINGS["Game"],
                "FetchNews": False,
                "EnableDiscordService": False,
                "IntroductionPromptVersion": 999,
            },
            "Sound": {"Mute": True},
            "Server": {
                "AdvertiseOnline": False,
                "AdvertiseOnLocalNetwork": False,
            },
            "Debug": {
                "SystemInformationVersionPrompt": 6,
                "SendSystemInformation": False,
            },
        }
        write_openra_settings(self.settings_path, baseline)
        interactive_spec = copy.deepcopy(self.spec)
        setup = interactive_spec.setdefault("setup", {})
        gameplay_setup = setup.setdefault("openra_gameplay_probe", {})
        gameplay_setup["interactive"] = True
        interactive_spec["actions"] = []
        interactive_spec["assertions"] = []
        self.gameplay_report_path.unlink(missing_ok=True)
        self._write_gameplay_request({"spec": interactive_spec})

        stdout_path = self.report_dir / "openra-c1-gameplay.stdout.log"
        stderr_path = self.report_dir / "openra-c1-gameplay.stderr.log"
        self.logs.extend(
            [
                str(stdout_path),
                str(stderr_path),
                str(self.settings_path),
                str(self.gameplay_report_path),
            ]
        )
        self._stdout_handle = stdout_path.open("w", encoding="utf-8")
        self._stderr_handle = stderr_path.open("w", encoding="utf-8")
        command = [
            "./launch-game.sh",
            f"Game.Mod={self.mod}",
            f"Engine.SupportDir={self.support_dir.resolve()}",
            f"Launch.Map={self.map_id}",
            f"Launch.WarptestGameplayRequestPath={self.gameplay_request_path.resolve()}",
            f"Launch.WarptestGameplayReportPath={self.gameplay_report_path.resolve()}",
            "Game.FetchNews=false",
            "Game.EnableDiscordService=false",
            "Sound.Mute=true",
            "Server.AdvertiseOnline=false",
            "Server.AdvertiseOnLocalNetwork=false",
            "Graphics.Mode=Windowed",
            "Graphics.WindowedSize=1280,720",
            f"Launch.WarptestBackgroundRequestPath={self._background_request_path.resolve()}",
            f"Launch.WarptestBackgroundResponsePath={self._background_response_path.resolve()}",
            f"Launch.WarptestBackgroundSessionId={self.session_id}",
            f"Launch.WarptestBackgroundRoot={self.report_dir.resolve()}",
        ]
        env = {
            **os.environ,
            "DOTNET_ROLL_FORWARD": os.environ.get("DOTNET_ROLL_FORWARD", "Major"),
            "SDL_MAC_BACKGROUND_APP": "1",
            "WARPTEST_C1_BACKGROUND_WINDOW": "1",
        }
        try:
            self._background_window_guard.prepare()
            self._proc = subprocess.Popen(
                command,
                cwd=str(self.repo),
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
                env=env,
                start_new_session=True,
            )
            self._background_window_guard.start(self._proc.pid)
            self._window_pid = self._proc.pid
            self._wait_gameplay_report(0)
            self.capture_policy_frame(
                self.report_dir / "openra-gameplay-background-bootstrap.png",
                timeout_s=self.bootstrap_timeout_s,
            )
            self._background_window_guard.await_hidden()
            return self
        except Exception:
            self.close()
            raise

    def _grade(
        self,
        assertions: Iterable[Mapping[str, Any]],
        *,
        actions: Iterable[Mapping[str, Any]] = (),
    ) -> OpenRASettingsGrade:
        self._semantic_sequence += 1
        sequence = self._semantic_sequence
        self._write_gameplay_request(
            {
                "schema_version": 1,
                "sequence": sequence,
                "actions": [dict(action) for action in actions],
                "assertions": [dict(assertion) for assertion in assertions],
            }
        )
        report = self._wait_gameplay_report(sequence)
        checks = report.get("checks") if isinstance(report.get("checks"), list) else []
        errors = report.get("errors") if isinstance(report.get("errors"), list) else []
        alive = self.is_alive()
        ok = alive and report.get("status") == "success"
        return OpenRASettingsGrade(
            ok,
            "success" if ok else "gameplay_mismatch" if alive else "process_exit",
            {
                "process_alive": alive,
                "report_path": str(self.gameplay_report_path),
                "sequence": sequence,
                "checks": checks,
                "errors": errors,
                "world_tick": report.get("worldTick"),
                "actions_executed": report.get("actionsExecuted"),
                "actions_total": report.get("actionsTotal"),
            },
        )

    def grade_target(
        self, spec: Mapping[str, Any] | None = None
    ) -> OpenRASettingsGrade:
        target = (spec or self.spec).get("target")
        actors = target.get("actors") if isinstance(target, dict) else []
        assertions = [
            {"type": "openra_actor_exists", "actor_id": actor["id"]}
            for actor in actors
            if isinstance(actor, dict) and actor.get("id")
        ]
        return self._grade(assertions)

    def grade_assertions(
        self, assertions: Iterable[Mapping[str, Any]]
    ) -> OpenRASettingsGrade:
        return self._grade(assertions)

    def execute_actions_and_grade(
        self,
        actions: Iterable[Mapping[str, Any]],
        assertions: Iterable[Mapping[str, Any]],
    ) -> OpenRASettingsGrade:
        """Execute one bounded registered transition in the live match and grade it."""

        return self._grade(assertions, actions=actions)

    def seed_evidence(self, task_id: str) -> dict[str, Any]:
        process_id = self.hold_pid
        process_alive = self.is_alive() and _pid_is_live(process_id)
        checks = self._last_semantic_report.get("checks")
        return {
            "schema_version": 1,
            "observed": process_alive,
            "source": "openra_live_gameplay_process",
            "algorithm": "not_applicable_live_gameplay",
            "seed": self.experiment_seed,
            "process_id": process_id,
            "process_alive_at_observation": process_alive,
            "benchmark": "OpenRA",
            "task_id": str(task_id),
            "semantic_fingerprint": {
                "source": self.semantic_source,
                "checks": checks if isinstance(checks, list) else [],
            },
        }


def _descendant_pids(root_pid: int) -> list[int]:
    """Return a bounded process tree including the launch wrapper and engine."""

    found = [int(root_pid)]
    frontier = [int(root_pid)]
    while frontier and len(found) < 32:
        parent = frontier.pop(0)
        completed = subprocess.run(
            ["pgrep", "-P", str(parent)],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if completed.returncode not in {0, 1}:
            continue
        for raw in completed.stdout.split():
            try:
                child = int(raw)
            except ValueError:
                continue
            if child not in found:
                found.append(child)
                frontier.append(child)
    return found


__all__ = [
    "OpenRAGameplaySession",
    "OpenRASettingsGrade",
    "OpenRASettingsSession",
    "apply_setting_actions",
    "read_effective_openra_settings",
    "read_openra_settings",
    "target_settings",
    "write_openra_settings",
]
