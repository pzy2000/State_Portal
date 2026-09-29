"""Warm (persistent) state session for Luanti -- the first engine warm-restore.

The browser :class:`~warptest.state_store.StateSession` keeps a dev server + browser
alive so the loop pays startup once. Engine profiles spawn a fresh native process per
checkpoint, which `state_store` rejects. This module brings the same idea to Luanti:

* **skip-spawn**: launch one headless ``luantiserver`` with the WarpTest worldmod in
  ``warptest_persistent`` mode, then drive a stream of seq-tagged request/report files
  into the *same* live process instead of relaunching the engine each time.
* **skip-synthesis**: once a deep state has been synthesized into the live world, a
  fast-restore reuses it (``skip_synthesis``) and just re-validates + applies smoke,
  instead of re-running the (potentially expensive) Lua build. A validation miss falls
  back to a full re-synthesis (correctness guard), and the synthesized world directory
  is snapshotted to ``.warptest-cache`` so a fresh session can reload it.

This is the headless/semantic core (no rendered client needed). The before/after
screenshot path uses the engine C++ capture hook and is layered on top separately.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
import re
import socket
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .c1_task_deadline import (
    bounded_task_timeout_s,
    raise_if_task_timed_out,
    raise_task_timeout_if_expired,
)
from .c1_background_window_guard import MacOSBackgroundWindowGuard
from .luanti_runtime import (
    LUANTI_HIDDEN_WINDOW_SOURCE_RELATIVE_PATH,
    LUANTI_WORLDMOD_RELATIVE_DIR,
    LUANTI_WARPTEST_PLAYER,
    LuantiRuntimeAdapter,
    _build_utility_spec,
    _read_luanti_client_build_provenance,
    ensure_luanti_instrumentation,
    luanti_client_source_identity,
)
from .runtime import json_dumps


@dataclass
class LuantiRestoreOutcome:
    status: str
    elapsed_ms: int
    checks: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    source: str = "restore"  # "restore" (full synth) | "snapshot" (skip-synthesis)
    seq: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_NATURAL_TIME_PATTERN = re.compile(
    r"(?:time[_ -]?of[_ -]?day|timeofday|time[_ -]?speed|day[_ -]?night|"
    r"daylight|daytime|nighttime|sunrise|sunset|dawn|dusk|midnight|"
    r"\bnoon\b|\bnight\b|\bmoon\b|\bsun\b|昼夜|白昼|夜晚|日出|日落)",
    flags=re.IGNORECASE,
)


def luanti_daylight_mode_for_task(task: Any, oracle_spec: dict[str, Any]) -> str:
    """Choose fixed noon unless a task explicitly or semantically tests daylight.

    ``gametime`` is intentionally not a natural-time marker: Luanti's monotonic
    server game-time counter continues while the celestial time-of-day is frozen.
    A future time-sensitive task can opt out without relying on text detection via
    ``c1_daylight_mode: natural`` at the spec root.
    """

    explicit = oracle_spec.get("c1_daylight_mode")
    if explicit is not None:
        if explicit not in {"natural", "fixed_noon"}:
            raise ValueError(
                "Luanti c1_daylight_mode must be 'natural' or 'fixed_noon'"
            )
        return str(explicit)
    public_text = "\n".join(
        str(getattr(task, field_name, "") or "")
        for field_name in (
            "state_description",
            "preconditions",
            "reference_steps",
        )
    )
    spec_text = json.dumps(oracle_spec, ensure_ascii=False, sort_keys=True)
    return (
        "natural"
        if _NATURAL_TIME_PATTERN.search(public_text + "\n" + spec_text)
        else "fixed_noon"
    )


class LuantiWarmSession:
    """A persistent headless luantiserver serving seq-tagged WarpTest requests."""

    def __init__(
        self,
        repo: str | Path,
        report_dir: str | Path,
        *,
        cache_root: str | Path = ".warptest-cache",
        render: bool = False,
        allow_headless_player_skip: bool = False,
        map_backend: str = "sqlite3",
        experiment_seed: int | None = None,
        creative_mode: bool = False,
        daylight_mode: str = "natural",
        deterministic_render: bool = False,
        calibration_event_path: str | Path | None = None,
        listen_port: int | None = None,
        initial_world_snapshot: str | Path | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve()
        self.report_dir = Path(report_dir).expanduser().resolve()
        self.benchmark = "luanti"
        try:
            self.git_commit = subprocess.run(
                ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except Exception:
            self.git_commit = None
        self.render = render
        self.allow_headless_player_skip = allow_headless_player_skip
        self.experiment_seed = int(experiment_seed) if experiment_seed is not None else None
        self.creative_mode = bool(creative_mode)
        if daylight_mode not in {"natural", "fixed_noon"}:
            raise ValueError("daylight_mode must be 'natural' or 'fixed_noon'")
        self.daylight_mode = daylight_mode
        if type(deterministic_render) is not bool:
            raise TypeError("deterministic_render must be a boolean")
        self.deterministic_render = deterministic_render
        self.calibration_event_path = (
            Path(calibration_event_path).expanduser().resolve()
            if calibration_event_path is not None
            else None
        )
        self.initial_world_snapshot = (
            Path(initial_world_snapshot).expanduser().resolve()
            if initial_world_snapshot is not None
            else None
        )
        if self.initial_world_snapshot is not None and not self.initial_world_snapshot.is_dir():
            raise ValueError("initial_world_snapshot must be an existing directory")
        if listen_port is None:
            listen_port = self._allocate_loopback_port()
        if isinstance(listen_port, bool) or not isinstance(listen_port, int):
            raise TypeError("listen_port must be an integer")
        if not 1024 <= listen_port <= 65535:
            raise ValueError("listen_port must be between 1024 and 65535")
        self.listen_port = listen_port
        self.bootstrap_ms: int | None = None
        self.cache_dir = Path(cache_root).expanduser().resolve() / "state-loop-snapshots-luanti"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._adapter = LuantiRuntimeAdapter(self.repo, self.report_dir, headless=True, map_backend=map_backend)
        self._proc: subprocess.Popen[str] | None = None
        self._world: Path | None = None
        self._conf: Path | None = None
        self._request_path = self.report_dir / "warptest-request.json"
        self._report_path = self.report_dir / "warptest-report.json"
        self._shot_request_path = self.report_dir / "warptest-screenshot-request.txt"
        self._shot_done_path = self.report_dir / "warptest-screenshot-done.txt"
        self._background_request_path = self.report_dir / "warptest-background-request.json"
        self._background_response_path = self.report_dir / "warptest-background-response.json"
        self.session_id = uuid.uuid4().hex
        self._log = self.report_dir / "luanti-warm.log"
        self._seq = 0
        self._shot_seq = 0
        self._background_seq = 0
        self._background_window_guard = MacOSBackgroundWindowGuard(self.report_dir)
        self._hidden_at_create_provenance: dict[str, Any] = {}
        self.runtime_identity: str | None = None
        self._client_binary: Path | None = None
        self._client_build_provenance: dict[str, Any] = {}

    @staticmethod
    def _allocate_loopback_port() -> int:
        """Choose a session-local port without assuming Luanti's default is free."""

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "LuantiWarmSession":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start(self) -> "LuantiWarmSession":
        self.report_dir.mkdir(parents=True, exist_ok=True)
        if self.calibration_event_path is not None:
            self.calibration_event_path.parent.mkdir(parents=True, exist_ok=True)
            self.calibration_event_path.unlink(missing_ok=True)
        ensure_luanti_instrumentation(self.repo)
        self.runtime_identity = luanti_client_source_identity(self.repo)
        self._world = self._prepare_world("warm")
        conf = self._write_persistent_conf(self._world)
        self._conf = conf
        for path in (
            self._request_path,
            self._report_path,
            self._shot_request_path,
            self._shot_done_path,
            self._background_request_path,
            self._background_response_path,
        ):
            if path.exists():
                path.unlink()
        start = time.perf_counter()
        cmd = self._launch_command(conf, self._world)
        if self.render:
            self._background_window_guard.prepare()
            hidden_source = self.repo / LUANTI_HIDDEN_WINDOW_SOURCE_RELATIVE_PATH
            binary = self._client_binary
            build_provenance = (
                _read_luanti_client_build_provenance(self.repo, binary)
                if binary is not None
                else None
            )
            if binary is None or not hidden_source.is_file() or not binary.is_file():
                raise RuntimeError("Luanti hidden-at-create build provenance is unavailable")
            source_bytes = hidden_source.read_bytes()
            if b"WARPTEST_C1_BACKGROUND_WINDOW" not in source_bytes:
                raise RuntimeError("Luanti hidden-at-create source marker is missing")
            if build_provenance is None:
                raise RuntimeError(
                    "Luanti client does not match the current source identity"
                )
            if build_provenance.get("source_identity") != self.runtime_identity:
                raise RuntimeError("Luanti client source identity changed during launch")
            self._client_build_provenance = dict(build_provenance)
            self._hidden_at_create_provenance = {
                "policy": "sdl_window_hidden_at_create_v1",
                "requested": True,
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "source_identity": self.runtime_identity,
                "binary_path": str(binary),
                "binary_sha256": build_provenance["binary_sha256"],
                "binary_fresh": True,
                "build_provenance_valid": True,
            }
        log = self._log.open("w", encoding="utf-8")
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.repo),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._launch_environment(),
            start_new_session=True,
        )
        if self.render:
            self._background_window_guard.start(self._proc.pid)
        # Readiness: a no-op validate request round-trips once the persistent loop is up.
        boot_s = float(os.environ.get("WARPTEST_LUANTI_WARM_BOOT_S", "120" if self.render else "60"))
        self._await_ready(deadline_s=boot_s)
        if self.render:
            self._background_window_guard.await_hidden()
        self.bootstrap_ms = int((time.perf_counter() - start) * 1000)
        return self

    @property
    def hold_process(self) -> Any:
        """The client process to suspend while a Start judge request is in flight.

        Both C1 arms grade a rendered Luanti Start against a remote judge, so the
        simulation has to be frozen for the duration or the two arms compare
        different live states.
        """

        return self._proc

    def background_guard_metadata(self) -> dict[str, Any]:
        return {
            **self._background_window_guard.metadata(),
            "hidden_at_create": dict(self._hidden_at_create_provenance),
        }

    def _launch_command(self, conf: Path, world: Path) -> list[str]:
        import platform as _platform

        logfile = str(self.report_dir / "luanti.log")
        if not self.render:
            binary = self._ensure_server_binary()
            return [str(binary), "--config", str(conf), "--world", str(world), "--gameid", "devtest", "--logfile", logfile]
        # Rendered client: embeds a server (so the persistent worldmod loop runs) and
        # owns a framebuffer for the on-demand screenshot hook.
        binary = self._adapter._ensure_built(server=False)
        self._client_binary = binary.expanduser().resolve()
        base = [
            str(binary), "--go", "--gameid", "devtest", "--world", str(world),
            "--name", LUANTI_WARPTEST_PLAYER, "--config", str(conf), "--logfile", logfile,
        ]
        if _platform.system() != "Darwin" and shutil.which("xvfb-run") is not None:
            return ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24", *base]
        return base

    def _launch_environment(self) -> dict[str, str]:
        """Return the isolated process environment for a WarpTest session."""

        environment = dict(os.environ)
        if self.render and platform.system() == "Darwin":
            # SDL otherwise promotes a newly-created Cocoa window to the
            # foreground.  This hint is scoped to the WarpTest child process and
            # leaves normal Luanti launches unchanged.
            environment["SDL_MAC_BACKGROUND_APP"] = "1"
            environment["WARPTEST_C1_BACKGROUND_WINDOW"] = "1"
        return environment

    def screenshot(self, name: str = "luanti.png", *, timeout_s: float = 30.0) -> str | None:
        """Capture the current rendered frame via the persistent C++ on-demand hook.

        Returns the screenshot path, or None when not in render mode / capture failed.
        """

        if not self.render or self._proc is None or self._proc.poll() is not None:
            return None
        self._shot_seq += 1
        token = f"shot-{self._shot_seq}-{int(time.time() * 1000)}"
        out_path = self.report_dir / name
        if out_path.exists():
            out_path.unlink()
        if self._shot_done_path.exists():
            self._shot_done_path.unlink()
        tmp = self._shot_request_path.with_suffix(".txt.tmp")
        tmp.write_text(f"{token}\n{out_path}\n", encoding="utf-8")
        os.replace(tmp, self._shot_request_path)
        effective_timeout_s = bounded_task_timeout_s(timeout_s)
        deadline = time.monotonic() + float(effective_timeout_s)
        while time.monotonic() < deadline:
            if self._shot_done_path.exists():
                try:
                    done = self._shot_done_path.read_text(encoding="utf-8").strip()
                except Exception:
                    done = ""
                if done.startswith(token):
                    return str(out_path) if out_path.exists() else None
            time.sleep(0.1)
        raise_if_task_timed_out()
        return str(out_path) if out_path.exists() else None

    def background_request(
        self, operation: str, *, timeout_s: float = 30.0, **fields: Any
    ) -> dict[str, Any]:
        if not self.render or not self.is_alive():
            raise RuntimeError("Luanti background pixel request requires a live rendered session")
        if operation not in {"capture", "input_batch"}:
            raise ValueError(f"unsupported Luanti background operation: {operation!r}")
        self._background_seq += 1
        sequence = self._background_seq
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
        effective_timeout_s = bounded_task_timeout_s(timeout_s)
        deadline = time.monotonic() + float(effective_timeout_s)
        last_error = "response missing"
        while time.monotonic() < deadline:
            try:
                response = json.loads(
                    self._background_response_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                last_error = str(error)
                time.sleep(0.05)
                continue
            if not isinstance(response, dict):
                last_error = "response is not an object"
                continue
            if response.get("sequence") != sequence:
                last_error = "stale response sequence"
                continue
            if response.get("session_id") != self.session_id:
                raise RuntimeError("Luanti background response session nonce mismatch")
            if response.get("operation") != operation:
                raise RuntimeError("Luanti background response operation mismatch")
            return response
        error = TimeoutError(
            f"Luanti background {operation} timed out: {last_error}"
        )
        raise_task_timeout_if_expired(error)
        raise error

    def is_alive(self) -> bool:
        """True when the persistent engine process is still running."""

        return self._proc is not None and self._proc.poll() is None

    def seed_metadata(self) -> dict[str, Any]:
        """Return seed/config evidence read from the running warm session files.

        ``experiment_seed`` is only the requested value and is deliberately not used
        as the observed seed.  The attested value comes from the exact config passed to
        the live Luanti process; world metadata is included so a formal run can also
        verify the real game/backend/frame configuration.
        """

        conf_values = self._read_settings(self._conf)
        world_mt = self._world / "world.mt" if self._world is not None else None
        world_values = self._read_settings(world_mt)
        raw_seed = conf_values.get("fixed_map_seed")
        try:
            map_seed = int(raw_seed) if raw_seed is not None else None
        except (TypeError, ValueError):
            map_seed = None
        process_alive = self.is_alive()
        daylight_mode = getattr(self, "daylight_mode", "natural")
        client_binary = getattr(self, "_client_binary", None)
        stable_config = {
            "fixed_map_seed": map_seed,
            "mg_name": conf_values.get("mg_name"),
            "creative_mode": conf_values.get("creative_mode"),
            "time_speed": conf_values.get("time_speed"),
            "daylight_mode": daylight_mode,
            "gameid": world_values.get("gameid"),
            "backend": world_values.get("backend"),
            "player_backend": world_values.get("player_backend"),
            "auth_backend": world_values.get("auth_backend"),
            "render": bool(self.render),
            "frame": {"width": 1280, "height": 720} if self.render else None,
        }
        config_fingerprint = hashlib.sha256(
            json.dumps(stable_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return {
            "source": "luanti_warm_session",
            "observed": bool(
                process_alive
                and self._conf is not None
                and self._conf.exists()
                and self._world is not None
                and self._world.exists()
                and map_seed is not None
            ),
            "algorithm": "luanti_fixed_map_seed" if map_seed is not None else None,
            "seed": map_seed,
            "map_seed": map_seed,
            "process_alive_at_observation": process_alive,
            "render": bool(self.render),
            "frame": stable_config["frame"],
            "map_backend": world_values.get("backend"),
            "gameid": world_values.get("gameid"),
            "creative_mode": conf_values.get("creative_mode") == "true",
            "daylight_mode": daylight_mode,
            "daylight_locked": daylight_mode == "fixed_noon",
            "git_commit": self.git_commit,
            "runtime_identity": getattr(self, "runtime_identity", None),
            "client_binary_path": (
                str(client_binary) if client_binary is not None else None
            ),
            "client_build_provenance": dict(
                getattr(self, "_client_build_provenance", {})
            ),
            "world_path": str(self._world) if self._world is not None else None,
            "config_path": str(self._conf) if self._conf is not None else None,
            "config_fingerprint": config_fingerprint,
        }

    def semantic_snapshot(
        self,
        task_id: str,
        spec: dict[str, Any],
        *,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """Read a task-bound semantic snapshot from the live engine.

        The spec is used only to select public task-relevant positions, ranges,
        inventory items and storage keys.  Expected node names/counts/values are not
        included in the probe.  All fingerprinted values are supplied by the running
        worldmod after it reads the live world/player/session.
        """

        probe = self._semantic_probe(str(task_id), spec)
        request = {
            "seq": self._next_seq(),
            "spec_path": "",
            "screenshot_output_path": "",
            "skip_synthesis": True,
            "spec": _build_utility_spec(
                {
                    "target": {"kind": "world_loaded"},
                    "actions": [],
                    "assertions": [],
                    "semantic_probe": probe,
                }
            ),
        }
        report = self._roundtrip(request, timeout_s=timeout_s)
        snapshot = report.get("semantic_snapshot")
        valid = bool(
            report.get("status") == "success"
            and isinstance(snapshot, dict)
            and snapshot.get("source") == "luanti_live_semantic_probe"
            and snapshot.get("task_id") == str(task_id)
            and (
                isinstance(snapshot.get("nodes"), list)
                and bool(snapshot.get("nodes"))
                or isinstance(snapshot.get("ranges"), list)
                and bool(snapshot.get("ranges"))
            )
        )
        if not valid:
            return {
                "source": "luanti_live_semantic_probe",
                "observed": False,
                "task_id": str(task_id),
                "fingerprint": None,
                "snapshot": snapshot if isinstance(snapshot, dict) else None,
                "status": str(report.get("status") or "failure"),
            }
        canonical = json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return {
            "source": "luanti_live_semantic_probe",
            "observed": True,
            "task_id": str(task_id),
            "fingerprint": hashlib.sha256(canonical).hexdigest(),
            "snapshot": snapshot,
            "status": "success",
        }

    @staticmethod
    def _semantic_probe(task_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        positions: dict[tuple[int, int, int], dict[str, int]] = {}
        ranges: dict[tuple[int, int, int, int, int, int], dict[str, Any]] = {}
        fields: set[str] = set()
        inventory_items: set[str] = set()
        storage_keys: set[str] = set()
        meta: dict[tuple[int, int, int, str], set[str]] = {}
        include_player = False

        def normalized_pos(value: Any) -> dict[str, int] | None:
            if not isinstance(value, dict):
                return None
            coords: list[int] = []
            for name in ("x", "y", "z"):
                raw = value.get(name)
                if isinstance(raw, bool):
                    return None
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    return None
                if not math.isfinite(number):
                    return None
                coords.append(math.floor(number + 0.5))
            return {"x": coords[0], "y": coords[1], "z": coords[2]}

        def add_pos(value: Any) -> dict[str, int] | None:
            pos = normalized_pos(value)
            if pos is not None:
                positions[(pos["x"], pos["y"], pos["z"])] = pos
            return pos

        def add_range(start: Any, end: Any) -> None:
            p1, p2 = add_pos(start), add_pos(end)
            if p1 is None or p2 is None:
                return
            key = (p1["x"], p1["y"], p1["z"], p2["x"], p2["y"], p2["z"])
            ranges[key] = {"from": p1, "to": p2}

        target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
        for node in target.get("nodes") or []:
            if isinstance(node, dict):
                add_pos(node.get("pos"))
        for fill in target.get("fill") or []:
            if isinstance(fill, dict):
                add_range(fill.get("from"), fill.get("to"))
        for validation in [*(target.get("validations") or []), *(spec.get("validations") or [])]:
            if isinstance(validation, dict) and isinstance(validation.get("path"), str):
                fields.add(validation["path"])
        player = target.get("player") if isinstance(target.get("player"), dict) else {}
        include_player = bool(player)
        for item in player.get("inventory") or []:
            if isinstance(item, dict) and isinstance(item.get("item"), str):
                inventory_items.add(item["item"])
        for entry in target.get("mod_storage") or []:
            if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                storage_keys.add(entry["key"])
        for entry in target.get("meta_inventory") or []:
            if not isinstance(entry, dict):
                continue
            pos = add_pos(entry.get("pos"))
            if pos is None:
                continue
            list_name = str(entry.get("list") or "main")
            key = (pos["x"], pos["y"], pos["z"], list_name)
            items = meta.setdefault(key, set())
            for item in entry.get("items") or []:
                if isinstance(item, dict) and isinstance(item.get("item"), str):
                    items.add(item["item"])

        for entry in [*(spec.get("actions") or []), *(spec.get("assertions") or [])]:
            if not isinstance(entry, dict):
                continue
            pos = add_pos(entry.get("pos"))
            add_range(entry.get("from"), entry.get("to"))
            if isinstance(entry.get("item"), str):
                if entry.get("type") == "luanti_meta_inventory_contains" and pos is not None:
                    list_name = str(entry.get("list") or "main")
                    key = (pos["x"], pos["y"], pos["z"], list_name)
                    meta.setdefault(key, set()).add(entry["item"])
                else:
                    inventory_items.add(entry["item"])
                    include_player = True
            if isinstance(entry.get("key"), str):
                storage_keys.add(entry["key"])
            if str(entry.get("type") or "").startswith("luanti_player_"):
                include_player = True

        meta_queries = [
            {
                "pos": {"x": x, "y": y, "z": z},
                "list": list_name,
                "items": sorted(items),
            }
            for (x, y, z, list_name), items in sorted(meta.items())
        ]
        return {
            "version": 1,
            "task_id": str(task_id),
            "positions": [positions[key] for key in sorted(positions)],
            "ranges": [ranges[key] for key in sorted(ranges)],
            "fields": sorted(fields),
            "include_player": include_player,
            "inventory_items": sorted(inventory_items),
            "storage_keys": sorted(storage_keys),
            "meta_inventory": meta_queries,
        }

    @staticmethod
    def _read_settings(path: Path | None) -> dict[str, str]:
        if path is None or not path.exists():
            return {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return {}
        values: dict[str, str] = {}
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def restart(self) -> "LuantiWarmSession":
        """Relaunch a fresh headless server + world (recover from an engine crash).

        A malformed fuzzed state can hard-crash the engine (beyond what the worldmod
        can pcall-catch). Restarting lets the C3 fuzz loop keep testing the remaining
        candidates against a live engine instead of blaming them for the earlier crash.
        """

        self.close()
        return self.start()

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self._background_window_guard.close()

    # -- requests ----------------------------------------------------------

    def restore(
        self,
        spec: dict[str, Any],
        *,
        skip_synthesis: bool = False,
        actions: list[dict[str, Any]] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        timeout_s: float = 120.0,
    ) -> LuantiRestoreOutcome:
        """Send one request to the warm engine and return its checks.

        ``skip_synthesis`` reuses the live world (fast-restore); ``actions`` overrides
        the spec's smoke actions (pass ``[]`` for the before-state) and ``assertions``
        overrides the oracle checks (pass ``[]`` so a before-pass is graded by the
        target validations, not the post-smoke assertions).
        """

        spec_variant = copy.deepcopy(spec)
        if actions is not None:
            spec_variant["actions"] = actions
        if assertions is not None:
            spec_variant["assertions"] = assertions
        request = {
            "seq": self._next_seq(),
            "spec_path": "",
            "screenshot_output_path": "",
            "skip_synthesis": bool(skip_synthesis),
            "spec": _build_utility_spec(spec_variant),
        }
        start = time.perf_counter()
        report = self._roundtrip(request, timeout_s=timeout_s)
        elapsed = int((time.perf_counter() - start) * 1000)
        checks = report.get("checks", []) if isinstance(report.get("checks"), list) else []
        status = str(report.get("status", "failure"))
        return LuantiRestoreOutcome(
            status=status,
            elapsed_ms=elapsed,
            checks=checks,
            detail=str(report.get("detail", "")),
            source="snapshot" if skip_synthesis else "restore",
            seq=request["seq"],
        )

    def fast_restore(
        self,
        spec: dict[str, Any],
        *,
        actions: list[dict[str, Any]] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        timeout_s: float = 120.0,
    ) -> LuantiRestoreOutcome:
        """Reuse the live (already-synthesized) world; validate then optionally smoke.

        Returns ``status='validate_failed'`` when the live world no longer matches the
        target validations, so the caller can fall back to a full restore.
        """

        outcome = self.restore(
            spec, skip_synthesis=True, actions=actions, assertions=assertions, timeout_s=timeout_s
        )
        if outcome.status != "success":
            # Distinguish a validation miss (recoverable -> fallback) from engine error.
            validate_failed = any(
                str(check.get("name", "")).startswith("target.validate") and check.get("status") != "success"
                for check in outcome.checks
            )
            outcome.status = "validate_failed" if validate_failed else outcome.status
        return outcome

    # -- world-dir snapshot (cross-session skip-synthesis) -----------------

    def snapshot_world(self, fingerprint: str) -> str | None:
        if self._world is None or not self._world.exists():
            return None
        dest = self.cache_dir / self._runtime_cache_key(fingerprint)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(self._world, dest)
        return str(dest)

    def has_world_snapshot(self, fingerprint: str) -> bool:
        return (self.cache_dir / self._runtime_cache_key(fingerprint)).exists()

    def _runtime_cache_key(self, fingerprint: str) -> str:
        identity = self.runtime_identity or luanti_client_source_identity(self.repo)
        blob = f"{identity}\n{fingerprint}".encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:24]

    # -- internals ---------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _roundtrip(self, request: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            return {"status": "failure", "detail": "luanti warm session is not running", "checks": []}
        seq = request["seq"]
        tmp = self._request_path.with_suffix(".json.tmp")
        tmp.write_text(json_dumps(request), encoding="utf-8")
        os.replace(tmp, self._request_path)  # atomic publish
        effective_timeout_s = bounded_task_timeout_s(timeout_s)
        deadline = time.monotonic() + float(effective_timeout_s)
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                return {"status": "failure", "detail": "luanti warm session exited", "checks": []}
            report = self._read_report_if_seq(seq)
            if report is not None:
                return report
            time.sleep(0.1)
        raise_if_task_timed_out()
        return {"status": "failure", "detail": f"timed out waiting for report seq={seq}", "checks": []}

    def _read_report_if_seq(self, seq: int) -> dict[str, Any] | None:
        if not self._report_path.exists():
            return None
        try:
            data = json.loads(self._report_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(data, dict) and data.get("seq") == seq:
            return data
        return None

    def _await_ready(self, *, deadline_s: float) -> None:
        probe = {
            "seq": self._next_seq(),
            "spec_path": "",
            "screenshot_output_path": "",
            "skip_synthesis": True,
            "spec": _build_utility_spec({"target": {"kind": "world_loaded"}, "actions": [], "assertions": []}),
        }
        report = self._roundtrip(probe, timeout_s=deadline_s)
        if report.get("status") not in {"success", "failure"}:
            raise RuntimeError(f"luanti warm session failed readiness probe: {report.get('detail')}")

    def _ensure_server_binary(self) -> Path:
        return self._adapter._ensure_built(server=True)

    def _prepare_world(self, stem: str) -> Path:
        if self.initial_world_snapshot is None:
            world = self._adapter._prepare_world(stem)
        else:
            world = self._adapter.out_dir / f"{stem}-world"
            if world.exists():
                shutil.rmtree(world, ignore_errors=True)
            shutil.copytree(self.initial_world_snapshot, world)
            source_mod = self.repo / LUANTI_WORLDMOD_RELATIVE_DIR
            worldmod = world / "worldmods" / "warptest"
            if worldmod.exists():
                shutil.rmtree(worldmod, ignore_errors=True)
            if source_mod.is_dir():
                worldmod.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_mod, worldmod)
        world_mt = world / "world.mt"
        if world_mt.exists() and self.creative_mode:
            text = world_mt.read_text(encoding="utf-8")
            text = text.replace("creative_mode = false", "creative_mode = true")
            world_mt.write_text(text, encoding="utf-8")
        if self.render and self.daylight_mode == "fixed_noon":
            daylight_mod = world / "worldmods" / "warptest_c1_daylight"
            daylight_mod.mkdir(parents=True, exist_ok=True)
            (daylight_mod / "init.lua").write_text(
                "\n".join(
                    [
                        "-- C1 visual stability: freeze only tasks that do not test time-of-day.",
                        "core.set_timeofday(0.5)",
                        "core.register_on_joinplayer(function()",
                        "  core.after(0, function() core.set_timeofday(0.5) end)",
                        "end)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            if world_mt.exists():
                text = world_mt.read_text(encoding="utf-8")
                if "load_mod_warptest_c1_daylight" not in text:
                    text += "load_mod_warptest_c1_daylight = true\n"
                    world_mt.write_text(text, encoding="utf-8")
        if self.calibration_event_path is not None:
            calibration_mod = world / "worldmods" / "warptest_c1_calibration"
            calibration_mod.mkdir(parents=True, exist_ok=True)
            event_path = json.dumps(str(self.calibration_event_path), ensure_ascii=False)
            (calibration_mod / "init.lua").write_text(
                "\n".join(
                    [
                        f"local event_path = {event_path}",
                        'local formname = "warptest:c1_calibration"',
                        "local function write_event(value)",
                        '  local file = assert(io.open(event_path, "w"))',
                        '  file:write(value .. "\\n")',
                        "  file:close()",
                        "end",
                        'local formspec = "formspec_version[4]size[10,5]" ..',
                        '  "label[1.2,1.0;WarpTest native input calibration]" ..',
                        '  "button[2.0,2.2;6.0,1.2;c1_calibrate;WarpTest C1 calibration]"',
                        "core.register_on_joinplayer(function(player)",
                        "  core.after(1.0, function()",
                        "    if player and player:is_player() then core.show_formspec(player:get_player_name(), formname, formspec) end",
                        "  end)",
                        "end)",
                        "core.register_on_player_receive_fields(function(player, name, fields)",
                        "  if name ~= formname or not fields.c1_calibrate then return false end",
                        '  write_event("clicked")',
                        "  core.close_formspec(player:get_player_name(), formname)",
                        "  return true",
                        "end)",
                        'core.register_chatcommand("warptestcalibrate", {',
                        '  params = "alpha-beta-42 gamma-delta-73",',
                        "  func = function(_name, param)",
                        "    write_event(param)",
                        '    return true, "WarpTest input calibration recorded"',
                        "  end,",
                        "})",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            if world_mt.exists():
                text = world_mt.read_text(encoding="utf-8")
                if "load_mod_warptest_c1_calibration" not in text:
                    text += "load_mod_warptest_c1_calibration = true\n"
                    world_mt.write_text(text, encoding="utf-8")
        return world

    def _write_persistent_conf(self, world: Path) -> Path:
        conf_path = self.report_dir / "warm-minetest.conf"
        lines = [
            f"name = {LUANTI_WARPTEST_PLAYER}",
            "bind_address = 127.0.0.1",
            f"port = {self.listen_port}",
            f"remote_port = {self.listen_port}",
            "mg_name = flat",
            "mgflat_ground_level = 0",
            "enable_damage = true",
            f"creative_mode = {'true' if self.creative_mode else 'false'}",
            f"give_initial_stuff = {'true' if self.creative_mode else 'false'}",
            *(["time_speed = 0"] if self.daylight_mode == "fixed_noon" else []),
            "enable_sound = false",
            # Adversarial mutated states (unloaded/huge positions) make the engine
            # spam WARNING[Server]: Map::getNodeMetadata(): Block not found -- which
            # can balloon the debug log to hundreds of MB and exhaust disk. Log only
            # errors and cap+rotate the file so log size stays bounded.
            "debug_log_level = error",
            "debug_log_size_max = 5",
            "secure.enable_security = false",
            "secure.trusted_mods = warptest",
            "warptest_persistent = true",
            f"warptest_request_path = {self._request_path}",
            f"warptest_report_path = {self._report_path}",
        ]
        if self.allow_headless_player_skip:
            lines.append("warptest_allow_headless_player_skip = true")
        if self.experiment_seed is not None:
            lines.append(f"fixed_map_seed = {self.experiment_seed}")
        if self.render:
            lines += [
                "enable_sound = false",
                "screen_w = 1280",
                "screen_h = 720",
                "fullscreen = false",
                "window_maximized = false",
                "show_debug = false",
                "viewing_range = 80",
                f"screenshot_path = {self.report_dir}",
                "screenshot_format = png",
                f"warptest_screenshot_request_path = {self._shot_request_path}",
                f"warptest_screenshot_done_path = {self._shot_done_path}",
                f"warptest_background_request_path = {self._background_request_path}",
                f"warptest_background_response_path = {self._background_response_path}",
                f"warptest_background_session_id = {self.session_id}",
                f"warptest_background_root = {self.report_dir}",
            ]
            if self.deterministic_render:
                lines += [
                    "enable_clouds = false",
                    "enable_fog = false",
                    "enable_particles = false",
                    "enable_dynamic_shadows = false",
                    "enable_auto_exposure = false",
                    "show_hud = false",
                    "show_chat = false",
                    "view_bobbing_amount = 0",
                    "fall_bobbing_amount = 0",
                    "camera_smoothing = 0",
                    "cinematic = false",
                ]
        lines.append("")
        conf_path.write_text("\n".join(lines), encoding="utf-8")
        return conf_path
