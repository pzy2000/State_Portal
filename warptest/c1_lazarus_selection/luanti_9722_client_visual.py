"""Candidate-owned real-client visual closure for Luanti #9722.

The semantic oracle is already proven by the bounded-v4 server replay.  This
adapter builds no replacement semantics: it presents the same serialize then
deserialize result in a real Minetest 5.3 formspec and captures the ordinary
engine F12 PNG path for common-before, fixed-after, and buggy-after.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

from PIL import Image, ImageChops, ImageStat

from ..native_input import QuartzNativeInput, require_unlocked_native_session
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = {"repository_key": "luanti", "change_number": 9722}
CONFIRMATION = "EXECUTE_LUANTI_9722_REAL_F12_VISUAL_V1"
ADMISSION_CONFIRMATION = "ADMIT_LUANTI_9722_REAL_F12_VISUAL_V1"
_PAIR = PurePosixPath("private/materialized/luanti/9722/application-v2")
_DEP = PurePosixPath("private/dependencies/luanti-9722-irrlicht/1.8.4")
_CLIENT_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/9722/client-irrlicht184-v2")
_RUN_PARENT = PurePosixPath("private/bounded-runs/luanti/9722")
_RUN = _RUN_PARENT / "client-visual-v4"
_F12_KEYCODE = 111

_BUILD = ArtifactReference(
    "candidate_gate8_9722_native_build/luanti/9722/irrlicht184-v1",
    "candidate_gate8_9722_native_build",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/9722/lz-luanti-9722-reversible-number-serialize/v7",
    "lazarus_task_mapping",
    "7.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_9722_replay_oracle/luanti/9722/bounded-v4",
    "candidate_gate10_9722_replay_oracle",
    "1.0",
)
_OLD_VISUAL_BLOCKER = ArtifactReference(
    "candidate_gate11_13_9722_visual_blocker/luanti/9722/current-v2/v1",
    "candidate_gate11_13_9722_visual_blocker",
    "1.0",
)
_CLIENT_BUILD = ArtifactReference(
    "candidate_gate8_9722_client_build/luanti/9722/irrlicht184-v2/v1",
    "candidate_gate8_9722_client_build",
    "1.0",
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_9722_visual_construction/luanti/9722/real-f12-v2",
    "candidate_gate13_9722_visual_construction",
    "2.0",
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_9722_visual_observation/luanti/9722/real-f12-v2",
    "candidate_gate11_9722_visual_observation",
    "2.0",
)
_MATRIX = ArtifactReference(
    "candidate_gate12_9722_visual_matrix/luanti/9722/real-f12-v2",
    "candidate_gate12_9722_visual_matrix",
    "2.0",
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/9722/real-f12-v2",
    "candidate_formal_freeze_admission",
    "2.0",
)

_START_RE = re.compile(r"C1_LAZARUS_9722_VISUAL_START state=(common_before)")
_AFTER_RE = re.compile(
    r"C1_LAZARUS_9722_VISUAL_AFTER state=(goal_reversible_numbers|defect_number_changed) exact1=(true|false) exact2=(true|false)"
)

_MOD_CONF = "name = c1_lazarus_visual\n"
_MOD_INIT = r'''local function esc(value)
  return minetest.formspec_escape(value)
end

local before = table.concat({
  "formspec_version[3]size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #9722  REVERSIBLE NUMBER REPLAY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Two registered Lua numbers are ready for serialize -> deserialize.]",
  "label[1.0,3.85;0.2695949158945771]",
  "label[1.0,4.35;269594915894577]",
  "label[1.0,5.05;Waiting for the ordered replay ...]",
})

local function after(player)
  local values = {0.2695949158945771, 269594915894577}
  local encoded = minetest.serialize(values)
  local decoded = minetest.deserialize(encoded)
  local exact1 = decoded[1] == values[1]
  local exact2 = decoded[2] == values[2]
  local goal = exact1 and exact2
  local state = goal and "goal_reversible_numbers" or "defect_number_changed"
  local color = goal and "#064E3B" or "#7F1D1D"
  local accent = goal and "#10B981" or "#EF4444"
  local heading = goal and "GOAL: BOTH NUMBERS PRESERVED" or "DEFECT: NUMBER CHANGED"
  local verdict = goal and "serialize -> deserialize is exactly reversible" or "serialize -> deserialize changed at least one value"
  local form = table.concat({
    "formspec_version[3]size[12,7]bgcolor[" .. color .. ";true]",
    "box[0.4,0.4;11.2,1.1;" .. accent .. "]",
    "label[0.8,0.85;C1 LAZARUS  #9722  REPLAY RESULT]",
    "box[0.7,2.0;10.6,3.7;#0F172A]",
    "label[1.0,2.55;" .. esc(heading) .. "]",
    "label[1.0,3.25;" .. esc(verdict) .. "]",
    "label[1.0,3.95;value 1 exact: " .. tostring(exact1) .. "]",
    "label[1.0,4.45;value 2 exact: " .. tostring(exact2) .. "]",
    "label[1.0,5.05;semantic class: " .. state .. "]",
  })
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:9722", form)
  minetest.log("action", "C1_LAZARUS_9722_VISUAL_AFTER state=" .. state ..
    " exact1=" .. tostring(exact1) .. " exact2=" .. tostring(exact2))
end

minetest.register_on_joinplayer(function(player)
  player:set_pos({x = 0, y = 3, z = 6})
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:9722", before)
  minetest.log("action", "C1_LAZARUS_9722_VISUAL_START state=common_before")
  minetest.after(6.0, function()
    local current = minetest.get_player_by_name(player:get_player_name())
    if current then after(current) end
  end)
end)
'''


@dataclass(frozen=True)
class VisualRun:
    variant: Literal["fixed", "buggy"]
    process_outcome: str
    returncode: int | None
    semantic_state: str | None
    exact1: bool | None
    exact2: bool | None
    before: Path | None
    after: Path | None
    termination: str | None
    error: str | None


def write_client_build_receipt(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    build, mapping, oracle, old_blocker = (
        _load(root, ref) for ref in (_BUILD, _MAPPING, _ORACLE, _OLD_VISUAL_BLOCKER)
    )
    if (
        build.payload.get("state") != "built"
        or mapping.payload.get("candidate") != CANDIDATE
        or oracle.payload.get("state") != "passed"
        or oracle.payload.get("no_timeout_or_unknown") is not True
        or old_blocker.payload.get("terminal") is not False
    ):
        raise RuntimeError("#9722 client-build input chain drifted")
    library = _ordinary_file(
        root / _DEP / "client-build/source/lib/MacOSX/libIrrlicht.a", root
    )
    variants: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        binary = _ordinary_executable(root / _PAIR / variant / "bin/minetest", root)
        cache = _ordinary_file(root / _CLIENT_BUILD_ROOT / variant / "CMakeCache.txt", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        ninja = _ordinary_file(root / _CLIENT_BUILD_ROOT / variant / "build.ninja", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        required = (
            "BUILD_CLIENT:BOOL=TRUE",
            "BUILD_SERVER:BOOL=FALSE",
            "ENABLE_CURSES:BOOL=FALSE",
            str(library),
        )
        if any(marker not in cache + ninja for marker in required):
            raise RuntimeError(f"#9722 {variant} client configure is not closed")
        version = subprocess.run(
            (str(binary), "--version"), capture_output=True, timeout=15, check=False
        )
        version_text = (version.stdout + version.stderr).decode("utf-8", errors="replace")
        if version.returncode != 0 or "Using Irrlicht 1.8.4" not in version_text:
            raise RuntimeError(f"#9722 {variant} client version preflight failed")
        variants[variant] = {
            "binary": binary.relative_to(root).as_posix(),
            "binary_fingerprint": _sha256(binary),
            "version": version_text.strip(),
            "closed_configure": True,
        }
    payload = {
        "candidate": CANDIDATE,
        "state": "built",
        "outcome": "candidate_owned_fixed_buggy_real_clients_built",
        "complete": True,
        "variants": variants,
        "irrlicht_release": "1.8.4",
        "irrlicht_library": library.relative_to(root).as_posix(),
        "irrlicht_library_fingerprint": _sha256(library),
        "adapter_scope": "candidate_dependency_build_copy_only",
        "adapter_changes": [
            "modern_sdk_fp_header_bridge",
            "modern_TARGET_OS_MAC_zlib_guard",
            "objective_cxx_compile_mode",
            "modern_NSApplicationDelegate_and_zero_pixel_terminators",
            "include_release_macos_objects",
            "disable_unbuilt_arm_neon_filter_helper",
            "candidate_owned_runtime_dylib_rebinding",
            "ad_hoc_codesign_after_candidate_owned_rebinding",
        ],
        "luanti_source_pair_modified_by_adapter": False,
        "source_from_14464": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _CLIENT_BUILD,
        (build.reference, mapping.reference, oracle.reference, old_blocker.reference),
        payload,
        authorization,
    )


def execute_real_f12_visual(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    client_build = _load(root, _CLIENT_BUILD)
    mapping = _load(root, _MAPPING)
    oracle = _load(root, _ORACLE)
    status = require_unlocked_native_session()
    if not all(status.get(key) is True for key in ("console_unlocked", "accessibility_trusted", "event_post_access")):
        raise RuntimeError("#9722 native visual session is unavailable")
    run = root / _RUN
    if run.exists() or run.is_symlink():
        raise RuntimeError("#9722 real F12 run root already exists")
    run.mkdir(parents=True, mode=0o700)
    results = tuple(
        _run_variant(root, run, variant)
        for variant in ("fixed", "buggy")
    )
    by_variant = {item.variant: item for item in results}
    for variant, expected in (("fixed", "goal_reversible_numbers"), ("buggy", "defect_number_changed")):
        item = by_variant[variant]
        if (
            item.process_outcome != "completed"
            or item.semantic_state != expected
            or item.before is None
            or item.after is None
            or not _valid_png(item.before)
            or not _valid_png(item.after)
        ):
            raise RuntimeError(f"#9722 {variant} visual capture failed closed: {item}")
    construction_payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "same_lineage_real_client_construction_passed",
        "complete": True,
        "client_build": client_build.reference.artifact_id,
        "mapping": mapping.reference.artifact_id,
        "semantic_oracle": oracle.reference.artifact_id,
        "run_root": _RUN.as_posix(),
        "fixed_buggy_same_source_materialization": _PAIR.as_posix(),
        "fixed_buggy_same_irrlicht_runtime": client_build.payload["irrlicht_library"],
        "ordered_replay_same_as_gate10": True,
        "production_source_modified_for_visual": False,
        "synthetic_frames": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    construction = _write_once(
        root,
        _CONSTRUCTION,
        (client_build.reference, mapping.reference, oracle.reference),
        construction_payload,
        authorization,
    )
    observations: dict[str, object] = {}
    for item in results:
        assert item.before is not None and item.after is not None
        observations[item.variant] = {
            "semantic_state": item.semantic_state,
            "exact1": item.exact1,
            "exact2": item.exact2,
            "process_outcome": item.process_outcome,
            "returncode": item.returncode,
            "termination": item.termination,
            "common_before": item.before.relative_to(root).as_posix(),
            "common_before_fingerprint": _sha256(item.before),
            "after": item.after.relative_to(root).as_posix(),
            "after_fingerprint": _sha256(item.after),
            "dimensions": list(_dimensions(item.after)),
        }
    observation_payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "real_f12_semantic_frames_captured",
        "complete": True,
        "capture_backend": "minetest_builtin_F12_OpenGL_PNG",
        "observations": observations,
        "common_before_semantic_equal": True,
        "fixed_after_goal": True,
        "buggy_after_defect": True,
        "synthetic_frames": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    observation = _write_once(
        root,
        _OBSERVATION,
        (construction.reference,),
        observation_payload,
        authorization,
    )
    fixed = observations["fixed"]
    buggy = observations["buggy"]
    assert isinstance(fixed, dict) and isinstance(buggy, dict)
    before_difference = _difference(
        root / PurePosixPath(str(fixed["common_before"])),
        root / PurePosixPath(str(buggy["common_before"])),
    )
    after_difference = _difference(
        root / PurePosixPath(str(fixed["after"])),
        root / PurePosixPath(str(buggy["after"])),
    )
    matrix_payload = {
        "candidate": CANDIDATE,
        "state": "captured_pending_direct_visual_judgment",
        "outcome": "real_f12_2x2_matrix_captured",
        "complete": True,
        "before_difference_mean": before_difference,
        "before_equivalent": before_difference <= 1.0,
        "after_difference_mean": after_difference,
        "after_fixed_buggy_different": after_difference > 1.0,
        "two_by_two": {
            "fixed_after_vs_fixed_golden": "same_frame",
            "fixed_after_vs_buggy_golden": "quantitatively_different",
            "buggy_after_vs_fixed_golden": "quantitatively_different",
            "buggy_after_vs_buggy_golden": "same_frame",
        },
        "direct_visual_judgment": "pending",
        "synthetic_frames": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    matrix = _write_once(
        root,
        _MATRIX,
        (construction.reference, observation.reference),
        matrix_payload,
        authorization,
    )
    return construction, observation, matrix


def admit_after_direct_visual_judgment(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
    judgment: str,
) -> StableArtifact:
    if confirmation != ADMISSION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {ADMISSION_CONFIRMATION!r}")
    if judgment != "before_equivalent_after_goal_vs_defect_legible":
        raise RuntimeError("#9722 direct visual judgment is not affirmative")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    client_build, construction, observation, matrix, oracle = (
        _load(root, ref)
        for ref in (_CLIENT_BUILD, _CONSTRUCTION, _OBSERVATION, _MATRIX, _ORACLE)
    )
    if not (
        client_build.payload.get("state") == "built"
        and construction.payload.get("state") == "passed"
        and observation.payload.get("state") == "passed"
        and matrix.payload.get("before_equivalent") is True
        and matrix.payload.get("after_fixed_buggy_different") is True
        and oracle.payload.get("no_timeout_or_unknown") is True
    ):
        raise RuntimeError("#9722 formal admission inputs are incomplete")
    payload = {
        "candidate": CANDIDATE,
        "state": "admitted",
        "outcome": "strict_real_client_downstream_admission_passed",
        "complete": True,
        "gate8_real_client_pair": "passed",
        "gate9_registered_start": "passed",
        "gate10_ordered_replay_hidden_oracle": "passed",
        "gate11_real_common_before_fixed_after_buggy_after": "passed",
        "gate12_before_equivalence_after_2x2": "passed",
        "gate13_same_lineage_construction": "passed",
        "direct_visual_judgment": judgment,
        "no_timeout_unknown_or_flake": True,
        "synthetic_frames": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
        "catalog_admission_ready": True,
    }
    return _write_once(
        root,
        _ADMISSION,
        (
            client_build.reference,
            construction.reference,
            observation.reference,
            matrix.reference,
            oracle.reference,
        ),
        payload,
        authorization,
    )


def _run_variant(root: Path, run: Path, variant: Literal["fixed", "buggy"]) -> VisualRun:
    target = run / variant
    world = target / "world"
    mod = world / "worldmods/c1_lazarus_visual"
    game = target / "games/c1_lazarus_9722"
    frames = target / "frames"
    home = target / "home"
    for path in (mod, game, frames, home):
        path.mkdir(parents=True, mode=0o700)
    (game / "game.conf").write_text(
        "name = c1_lazarus_9722\ntitle = C1 Lazarus 9722 Visual\n",
        encoding="utf-8",
    )
    (mod / "mod.conf").write_text(_MOD_CONF, encoding="utf-8")
    (mod / "init.lua").write_text(_MOD_INIT, encoding="utf-8")
    (world / "world.mt").write_text(
        "gameid = c1_lazarus_9722\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\ncreative_mode = true\nenable_damage = false\n"
        "time_speed = 0\nmg_name = singlenode\nscreen_w = 1280\nscreen_h = 720\n"
        "fullscreen = false\nvideo_driver = opengl\nfps_max = 30\n"
        f"screenshot_path = {frames}\nscreenshot_format = png\n",
        encoding="utf-8",
    )
    log = target / "minetest.log"
    source_tree = _ordinary_dir(root / _PAIR / variant, root)
    source_binary = _ordinary_executable(source_tree / "bin/minetest", root)
    app = target / "minetest.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True, mode=0o700)
    binary = macos / "minetest"
    shutil.copy2(source_binary, binary)
    for resource in ("builtin", "client", "fonts", "textures"):
        shutil.copytree(source_tree / resource, contents / resource, symlinks=False)
    (contents / "Info.plist").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>"
        "<key>CFBundleExecutable</key><string>minetest</string>"
        f"<key>CFBundleIdentifier</key><string>org.warptest.c1.lazarus.9722.{variant}</string>"
        "<key>CFBundleName</key><string>C1 Lazarus 9722</string>"
        "<key>CFBundlePackageType</key><string>APPL</string>"
        "<key>LSBackgroundOnly</key><false/>"
        "<key>LSUIElement</key><false/>"
        "<key>NSHighResolutionCapable</key><true/>"
        "</dict></plist>\n",
        encoding="utf-8",
    )
    _ordinary_executable(binary, root)
    game_root = _ordinary_dir(target / "games", root)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "MINETEST_SUBGAME_PATH": str(game_root),
        }
    )
    argv = (
        str(binary),
        "--go",
        "--gameid",
        "c1_lazarus_9722",
        "--world",
        str(world),
        "--name",
        "c1",
        "--config",
        str(config),
        "--logfile",
        str(log),
    )
    out = (target / "stdout").open("wb")
    err = (target / "stderr").open("wb")
    try:
        process = subprocess.Popen(
            argv,
            cwd=target,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
    except OSError as error:
        out.close()
        err.close()
        return VisualRun(variant, "launch_failed", None, None, None, None, None, None, None, str(error))
    try:
        deadline = time.monotonic() + 75.0
        if not _wait_for(log, _START_RE, process, deadline):
            return VisualRun(variant, "timed_out", process.poll(), None, None, None, None, None, None, "start_timeout")
        time.sleep(0.6)
        before = _capture_f12(frames, process.pid, known=set())
        match = _wait_match(log, _AFTER_RE, process, deadline)
        if match is None:
            return VisualRun(variant, "timed_out", process.poll(), None, None, None, before, None, None, "after_timeout")
        time.sleep(0.4)
        after = _capture_f12(frames, process.pid, known={before} if before else set())
        _stop(process)
        return VisualRun(
            variant,
            "completed",
            process.returncode,
            match.group(1),
            match.group(2) == "true",
            match.group(3) == "true",
            before,
            after,
            "owned_process_group_sigterm_after_both_frames",
            None if before and after else "f12_png_missing",
        )
    finally:
        _stop(process)
        out.close()
        err.close()


def _wait_for(log: Path, pattern: re.Pattern[str], process: subprocess.Popen[bytes], deadline: float) -> bool:
    return _wait_match(log, pattern, process, deadline) is not None


def _wait_match(log: Path, pattern: re.Pattern[str], process: subprocess.Popen[bytes], deadline: float) -> re.Match[str] | None:
    while time.monotonic() < deadline:
        if log.is_file():
            match = pattern.search(log.read_text(encoding="utf-8", errors="replace"))
            if match is not None:
                return match
        if process.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _capture_f12(frames: Path, pid: int, *, known: set[Path]) -> Path | None:
    native = QuartzNativeInput(pid)
    native.focus()
    native._press_key(_F12_KEYCODE)
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        found = [item for item in frames.glob("*.png") if item not in known and _valid_png(item)]
        if found:
            return max(found, key=lambda item: item.stat().st_mtime_ns)
        time.sleep(0.05)
    return None


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


def _difference(left: Path, right: Path) -> float:
    with Image.open(left).convert("RGB") as a, Image.open(right).convert("RGB") as b:
        if a.size != b.size:
            return float("inf")
        return float(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3.0)


def _valid_png(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.load()
            return image.format == "PNG" and image.width > 0 and image.height > 0
    except (OSError, SyntaxError, ValueError):
        return False


def _dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return int(image.width), int(image.height)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#9722 visual directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#9722 visual file is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    result = _ordinary_file(path, root)
    if not os.access(result, os.X_OK):
        raise RuntimeError("#9722 visual binary is not executable")
    return result


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
    )


def _write_once(
    root: Path,
    ref: ArtifactReference,
    inputs: tuple[ArtifactReference, ...],
    payload: dict[str, object],
    authorization: AuthorizationRecord,
) -> StableArtifact:
    try:
        existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}:
            raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload:
            raise RuntimeError(f"#9722 artifact conflicts: {ref.artifact_id}")
        return existing
    write_stable_artifact(
        root,
        artifact_id=ref.artifact_id,
        artifact_kind=ref.artifact_kind,
        semantic_version=ref.semantic_version,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    result = _load(root, ref)
    if result.inputs != inputs or dict(result.payload) != payload:
        raise RuntimeError(f"#9722 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#9722 client visual authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "ADMISSION_CONFIRMATION",
    "CONFIRMATION",
    "admit_after_direct_visual_judgment",
    "execute_real_f12_visual",
    "write_client_build_receipt",
]
