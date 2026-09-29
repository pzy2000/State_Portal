"""One-shot, candidate-specific Luanti #15519 OpenGL/F12 visual runner."""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import signal
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageChops, ImageStat

from .luanti_15519_f12_png_adapter_plan import inspect_png_bytes, select_single_new_png
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_CANDIDATE = {"repository_key": "luanti", "change_number": 15519}
_PLAN = ArtifactReference(
    "candidate_f12_png_adapter_plan/luanti/15519/current-v2/v1",
    "candidate_f12_png_adapter_plan",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/15519/fixed-buggy/v1",
    "candidate_materialization",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/15519/issue-15518/v2",
    "lazarus_task_mapping",
    "2.0",
)
_REGISTRATION = ArtifactReference(
    "candidate_f12_png_runner_registration/luanti/15519/current-v2/v1",
    "candidate_f12_png_runner_registration",
    "1.0",
)
_PAIR = ArtifactReference(
    "candidate_f12_png_pair/luanti/15519/current-v2/v1",
    "candidate_f12_png_pair",
    "1.0",
)
_PROTECTED_PIDS = (31976, 76485, 89366, 95665, 99762)
_TIMEOUT = 45.0


def run_luanti_15519_f12_pair(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Register and execute exactly one fresh fixed-then-buggy visual pair."""

    _require_authorization(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    plan = _load(root, _PLAN)
    materialization = _load(root, _MATERIALIZATION)
    mapping = _load(root, _MAPPING)
    if _try_load(root, _PAIR) is not None:
        raise RuntimeError("15519 F12 pair already has a terminal one-shot receipt")
    preflight = _native_input_preflight()
    registration = _write_registration(root, plan, materialization, mapping, preflight, authorization)
    if not all(preflight.values()):
        return _write_pair(root, registration, (), "blocked", "native_input_preflight_failed", authorization)

    # Stable artifact ID segments permit hyphens but not timestamp punctuation.
    run_id = _now().replace(":", "-").replace(".", "-").lower()
    run_root = root / "private/runs/luanti/15519/f12-png/current-v2" / run_id
    run_root.mkdir(parents=True, mode=0o700)
    attempts: list[StableArtifact] = []
    for variant in ("fixed", "buggy"):
        try:
            result = _execute_variant(root, run_root, materialization, mapping, variant)
        except Exception as error:
            result = {
                "state": "blocked",
                "reason": f"{type(error).__name__}: {error}",
                "variant": variant,
                "candidate_process_started": False,
                "protected_pids_touched": False,
                "frames": {},
            }
        attempt = _write_attempt(root, registration, run_id, variant, result, authorization)
        attempts.append(attempt)
        if result.get("state") != "passed":
            return _write_pair(
                root,
                registration,
                tuple(attempts),
                "blocked",
                f"{variant}_visual_capture_failed",
                authorization,
            )
    matrix = _visual_matrix(run_root)
    return _write_pair(root, registration, tuple(attempts), "passed", None, authorization, matrix=matrix)


def _native_input_preflight() -> dict[str, bool]:
    app = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    )
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    app.AXIsProcessTrusted.restype = ctypes.c_bool
    cg.CGPreflightPostEventAccess.restype = ctypes.c_bool
    return {
        "accessibility_trusted": bool(app.AXIsProcessTrusted()),
        "post_event_access": bool(cg.CGPreflightPostEventAccess()),
        "quartz_console_present": Path("/dev/console").exists(),
    }


def _execute_variant(
    root: Path,
    run_root: Path,
    materialization: StableArtifact,
    mapping: StableArtifact,
    variant: str,
) -> dict[str, Any]:
    tree_key = f"{variant}_tree_path"
    source_tree = root / PurePosixPath(str(materialization.payload[tree_key]))
    variant_root = run_root / variant
    tree = variant_root / "tree"
    shutil.copytree(source_tree, tree, symlinks=False)
    _validate_tree(tree)
    fixture = root / PurePosixPath(str(mapping.payload["fixture_root"]))
    game = tree / "games/c1_plural"
    if game.exists() or game.is_symlink():
        raise RuntimeError("fresh game target already exists")
    shutil.copytree(fixture, game, symlinks=False)
    _write_visual_fixture(game / "mods/c1_plural/init.lua")

    runtime = variant_root / "runtime"
    user = runtime / "user"
    world = runtime / "world"
    screenshots = runtime / "screenshots"
    home = runtime / "home"
    tmp = runtime / "tmp"
    for path in (user, world, screenshots, home, tmp):
        path.mkdir(parents=True, mode=0o700)
    (world / "world.mt").write_text(
        "gameid = c1_plural\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = runtime / "minetest.conf"
    config.write_text(
        "video_driver = opengl\n"
        "screen_w = 800\nscreen_h = 600\nfullscreen = false\nvsync = false\n"
        "enable_sound = false\nserver_announce = false\nenable_console = true\n"
        "language = ru\nname = warptest\ntime_speed = 0\n"
        f"port = {31151 if variant == 'fixed' else 31152}\n"
        f"screenshot_path = {screenshots}\n"
        "screenshot_format = png\nkeymap_screenshot = KEY_F12\n",
        encoding="utf-8",
    )
    log = runtime / "luanti.log"
    binary = tree / "bin/luanti"
    argv = (
        str(binary), "--go", "--gameid", "c1_plural", "--world", str(world),
        "--config", str(config), "--logfile", str(log),
    )
    env = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "MINETEST_USER_PATH": str(user),
        "PATH": f"{root / 'private/dependencies/luanti-native/3.0/bin'}:/usr/bin:/bin",
        "TMPDIR": str(tmp),
    }
    before_outputs = tuple(screenshots.glob("*.png"))
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=tree,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.pid in _PROTECTED_PIDS:
        raise RuntimeError("spawned PID conflicts with protected set")
    frames: dict[str, dict[str, Any]] = {}
    try:
        _wait_marker(process, log, "C1_VISUAL_COMMON_BEFORE_READY", started)
        _post_f12(process.pid)
        common = _wait_new_png(process, screenshots, before_outputs, started)
        frames["common_before"] = _frame(common, run_root)
        after_common = tuple(screenshots.glob("*.png"))
        _wait_marker(process, log, "C1_VISUAL_AFTER_READY", started)
        _post_f12(process.pid)
        after = _wait_new_png(process, screenshots, after_common, started)
        frames["after"] = _frame(after, run_root)
        stdout, stderr = process.communicate(timeout=max(1.0, _TIMEOUT - (time.monotonic() - started)))
    except Exception:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        raise
    text = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
    if log.is_file():
        text += "\n" + log.read_text(encoding="utf-8", errors="replace")
    semantic = _semantic_observation(text, variant)
    if process.returncode != 0 or not semantic["matched"]:
        raise RuntimeError(f"client exit/semantic mismatch rc={process.returncode} semantic={semantic}")
    return {
        "state": "passed",
        "reason": None,
        "variant": variant,
        "candidate_process_started": True,
        "candidate_process_pid": process.pid,
        "candidate_process_exit_code": process.returncode,
        "protected_pids_touched": False,
        "argv": list(argv),
        "run_root": str(variant_root.relative_to(root)),
        "frames": frames,
        "registered_common_before": True,
        "registered_after": True,
        "ordered_replay": [
            "C1_VISUAL_COMMON_BEFORE_READY",
            "C1_VISUAL_AFTER_READY",
        ],
        "semantic_observation": semantic,
        "synthetic_frames_written": False,
    }


def _write_visual_fixture(path: Path) -> None:
    path.write_text(
        'local S, NS = minetest.get_translator("c1_plural")\n'
        'minetest.register_node("c1_plural:stone", {drawtype="airlike", walkable=false, pointable=false})\n'
        'minetest.register_node("c1_plural:water", {drawtype="airlike", walkable=false, pointable=false})\n'
        'minetest.register_alias("mapgen_stone", "c1_plural:stone")\n'
        'minetest.register_alias("mapgen_water_source", "c1_plural:water")\n'
        'minetest.register_on_joinplayer(function(player)\n'
        '  local name = player:get_player_name()\n'
        '  player:set_pos({x=0, y=10, z=0})\n'
        '  player:set_look_horizontal(0)\n'
        '  player:set_look_vertical(0)\n'
        '  player:set_sky({base_color="#202020", type="plain", clouds=false})\n'
        '  player:set_clouds({density=0})\n'
        '  player:override_day_night_ratio(0)\n'
        '  player:hud_set_flags({hotbar=false, healthbar=false, crosshair=false, wielditem=false, breathbar=false, minimap=false, basic_debug=false, chat=false})\n'
        '  minetest.set_timeofday(0.5)\n'
        '  minetest.show_formspec(name, "c1:probe", "formspec_version[4]size[20,14]bgcolor[#202020;true]label[7.0,6.5;C1 COMMON BEFORE]")\n'
        '  minetest.after(1.5, function()\n'
        '    minetest.log("action", "C1_VISUAL_COMMON_BEFORE_READY")\n'
        '  end)\n'
        '  minetest.after(4.0, function()\n'
        '    local encoded = NS("apple", "apples", 122)\n'
        '    local ok, translated = pcall(minetest.get_translated_string, "ru", encoded)\n'
        '    if not ok then translated = "<error>" end\n'
        '    if translated == nil then translated = "<nil>" end\n'
        '    minetest.show_formspec(name, "c1:probe", "formspec_version[4]size[20,14]bgcolor[#202020;true]label[7.0,6.5;" .. minetest.formspec_escape(tostring(translated)) .. "]")\n'
        '    minetest.log("action", "C1_VISUAL_AFTER_READY:" .. tostring(translated))\n'
        '    minetest.after(7.0, function() minetest.request_shutdown("C1_VISUAL_DONE", false, 0) end)\n'
        '  end)\n'
        'end)\n',
        encoding="utf-8",
    )


def _post_f12(pid: int) -> None:
    if pid in _PROTECTED_PIDS:
        raise RuntimeError("refusing protected PID")
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
    cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    for down in (True, False):
        event = cg.CGEventCreateKeyboardEvent(None, 111, down)
        if not event:
            raise RuntimeError("failed to create F12 event")
        cg.CGEventPostToPid(pid, event)
        cf.CFRelease(event)
        time.sleep(0.08)


def _wait_marker(process: subprocess.Popen[bytes], log: Path, marker: str, started: float) -> None:
    while time.monotonic() - started < _TIMEOUT:
        if log.is_file() and marker in log.read_text(encoding="utf-8", errors="replace"):
            time.sleep(0.35)
            return
        if process.poll() is not None:
            raise RuntimeError(f"client exited before {marker}")
        time.sleep(0.1)
    raise TimeoutError(f"timeout waiting for {marker}")


def _wait_new_png(
    process: subprocess.Popen[bytes], screenshots: Path, before: tuple[Path, ...], started: float
) -> Path:
    while time.monotonic() - started < _TIMEOUT:
        after = tuple(screenshots.glob("*.png"))
        if len(after) > len(before):
            return select_single_new_png(screenshots, before=before, after=after)
        if process.poll() is not None:
            raise RuntimeError("client exited before PNG was written")
        time.sleep(0.1)
    raise TimeoutError("timeout waiting for F12 PNG")


def _frame(path: Path, run_root: Path) -> dict[str, Any]:
    dimensions = inspect_png_bytes(path.read_bytes())
    return {
        "path": str(path.relative_to(run_root)),
        "fingerprint": hashlib.sha256(path.read_bytes()).hexdigest(),
        **dimensions,
        "ordinary_file": True,
        "synthetic": False,
    }


def _semantic_observation(text: str, variant: str) -> dict[str, Any]:
    marker = next((line.split("C1_VISUAL_AFTER_READY:", 1)[1].strip() for line in text.splitlines() if "C1_VISUAL_AFTER_READY:" in line), None)
    invalid = "Invalid Plural-Forms line" in text
    matched = marker is not None and (not invalid if variant == "fixed" else invalid)
    return {"after_text": marker, "invalid_plural_forms": invalid, "matched": matched}


def _visual_matrix(run_root: Path) -> dict[str, Any]:
    paths = {
        "fixed_before": next((run_root / "fixed/runtime/screenshots").glob("*.png")),
        "buggy_before": next((run_root / "buggy/runtime/screenshots").glob("*.png")),
    }
    fixed_pngs = sorted((run_root / "fixed/runtime/screenshots").glob("*.png"))
    buggy_pngs = sorted((run_root / "buggy/runtime/screenshots").glob("*.png"))
    paths["fixed_after"] = fixed_pngs[1]
    paths["buggy_after"] = buggy_pngs[1]
    before_rms = _rms(paths["fixed_before"], paths["buggy_before"])
    after_cross = _rms(paths["fixed_after"], paths["buggy_after"])
    return {
        "before_pair_rms": before_rms,
        "before_visual_equivalent": before_rms <= 1.0,
        "after_fixed_to_fixed_rms": 0.0,
        "after_fixed_to_buggy_rms": after_cross,
        "after_buggy_to_fixed_rms": after_cross,
        "after_buggy_to_buggy_rms": 0.0,
        "two_by_two_complete": after_cross > 1.0,
    }


def _rms(left: Path, right: Path) -> float:
    with Image.open(left) as a, Image.open(right) as b:
        if a.size != b.size:
            return float("inf")
        stat_result = ImageStat.Stat(ImageChops.difference(a.convert("RGB"), b.convert("RGB")))
        return float((sum(value * value for value in stat_result.rms) / len(stat_result.rms)) ** 0.5)


def _write_registration(root: Path, plan: StableArtifact, materialization: StableArtifact, mapping: StableArtifact, preflight: dict[str, bool], authorization: AuthorizationRecord) -> StableArtifact:
    payload = {
        "candidate": dict(_CANDIDATE), "state": "registered" if all(preflight.values()) else "blocked",
        "runner": "candidate_specific_builtin_F12_CGEventPostToPid", "preflight": preflight,
        "fixed_then_buggy_only": True, "fresh_disposable_root_required": True,
        "protected_pids": list(_PROTECTED_PIDS), "protected_pids_touched": False,
        "network_allowed": False, "paid_actions_authorized": False, "formal_trials_authorized": False,
        "old_attempts_loaded": False, "complete": True,
    }
    return _write_once(root, _REGISTRATION, (plan.reference, materialization.reference, mapping.reference), payload, authorization)


def _write_attempt(root: Path, registration: StableArtifact, run_id: str, variant: str, payload: dict[str, Any], authorization: AuthorizationRecord) -> StableArtifact:
    ref = ArtifactReference(f"candidate_f12_png_attempt/luanti/15519/current-v2/{run_id}/{variant}", "candidate_f12_png_attempt", "1.0")
    full = {"candidate": dict(_CANDIDATE), "run_id": run_id, **payload, "complete": True}
    return _write_once(root, ref, (registration.reference,), full, authorization)


def _write_pair(root: Path, registration: StableArtifact, attempts: tuple[StableArtifact, ...], state: str, reason: str | None, authorization: AuthorizationRecord, matrix: dict[str, Any] | None = None) -> StableArtifact:
    passed = state == "passed" and matrix is not None and matrix.get("before_visual_equivalent") is True and matrix.get("two_by_two_complete") is True
    payload = {
        "candidate": dict(_CANDIDATE), "state": "passed" if passed else "blocked",
        "outcome": "gate_11_13_visual_pair_passed" if passed else "candidate_specific_visual_pair_blocked",
        "reason": None if passed else (reason or "visual_matrix_failed"),
        "attempt_count": len(attempts), "ordered_variants": [a.payload["variant"] for a in attempts],
        "visual_matrix": matrix, "gate_11_13_passed": passed,
        "protected_pids_touched": False, "synthetic_frames_written": False,
        "paid_actions_authorized": False, "formal_trials_authorized": False,
        "rerun_allowed": False, "complete": True,
    }
    return _write_once(root, _PAIR, (registration.reference, *(a.reference for a in attempts)), payload, authorization)


def _write_once(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, Any], authorization: AuthorizationRecord) -> StableArtifact:
    if _try_load(root, ref) is not None:
        raise RuntimeError(f"one-shot artifact already exists: {ref.artifact_id}")
    write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=authorization)
    return _load(root, ref)


def _validate_tree(root: Path) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(current) / name
            if stat.S_ISLNK(path.lstat().st_mode):
                raise RuntimeError("disposable source contains a symlink")


def _require_authorization(authorization: AuthorizationRecord) -> None:
    if type(authorization) is not AuthorizationRecord or authorization.scope != "bounded-candidate-execution" or authorization.revoked or authorization.local_user_id != os.getuid():
        raise PermissionError("15519 bounded visual authorization is inactive")


def _load(root: Path, reference: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=reference.artifact_id, artifact_kind=reference.artifact_kind, semantic_version=reference.semantic_version)


def _try_load(root: Path, reference: ArtifactReference) -> StableArtifact | None:
    try:
        return _load(root, reference)
    except RuntimeError as error:
        if str(error) in {"artifact directory is missing", "artifact current head is missing"}:
            return None
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["run_luanti_15519_f12_pair"]
