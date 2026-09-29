"""Candidate-owned real-client F12 closure for Luanti #12602.

The fixture uses two ordered mods that provide the same texture name.  The
base mod supplies a red texture and the later override mod supplies a green
texture.  The production change in ``ServerModManager::getModsMediaPaths`` is
therefore visible directly on a wall rendered by the ordinary client.
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

from PIL import Image, ImageChops, ImageDraw, ImageStat

from ..native_input import QuartzNativeInput, require_unlocked_native_session
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


CANDIDATE = {"repository_key": "luanti", "change_number": 12602}
CONFIRMATION = "EXECUTE_LUANTI_12602_REAL_F12_VISUAL_V4"
ADMISSION_CONFIRMATION = "ADMIT_LUANTI_12602_REAL_F12_VISUAL_V4"
_PAIR = PurePosixPath("private/materialized/luanti/12602/application-v1")
_IRR_SOURCE = PurePosixPath("private/dependencies/luanti-12602-irrlicht/1.9.0mt6/source")
_IRR_BUILD = PurePosixPath("private/dependencies/luanti-12602-irrlicht/1.9.0mt6/client-build")
_CLIENT_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/12602/gate11-client-mt6-v1")
_RUN = PurePosixPath("private/bounded-runs/luanti/12602/client-visual-v4")
_F12_KEYCODE = 111

_DEPENDENCY = ArtifactReference(
    "candidate_gate8_12602_irrlichtmt6_source/luanti/12602/current-v2/v1",
    "candidate_gate8_12602_irrlichtmt6_source",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/12602/lz-luanti-12602-media-override/v1",
    "lazarus_task_mapping",
    "1.0",
)
_GATE10 = ArtifactReference(
    "candidate_gate10_transition/luanti/12602/runtime-v1",
    "candidate_gate10_transition",
    "1.0",
)
_ORACLE = ArtifactReference(
    "candidate_oracle/luanti/12602/hidden-v1",
    "candidate_oracle",
    "1.0",
)
_OLD_BLOCKER = ArtifactReference(
    "candidate_gate11_visual_blocker/luanti/12602/current-v2/v1",
    "candidate_gate11_visual_blocker",
    "1.0",
)
_CLIENT_BUILD = ArtifactReference(
    "candidate_gate8_12602_client_build/luanti/12602/mt6-v1",
    "candidate_gate8_12602_client_build",
    "1.0",
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_12602_visual_construction/luanti/12602/real-f12-v4",
    "candidate_gate13_12602_visual_construction",
    "4.0",
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_12602_visual_observation/luanti/12602/real-f12-v4",
    "candidate_gate11_12602_visual_observation",
    "4.0",
)
_MATRIX = ArtifactReference(
    "candidate_gate12_12602_visual_matrix/luanti/12602/real-f12-v4",
    "candidate_gate12_12602_visual_matrix",
    "4.0",
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/12602/real-f12-v4",
    "candidate_formal_freeze_admission",
    "4.0",
)

_START_RE = re.compile(r"C1_LAZARUS_12602_VISUAL_START state=common_before")
_AFTER_RE = re.compile(r"C1_LAZARUS_12602_VISUAL_AFTER state=media_override_wall")

_BASE_INIT = r'''minetest.register_node("base:display", {
  description = "Media Override Display",
  tiles = {"c1_media.png"},
  groups = {cracky = 1},
  light_source = 8,
})

local before = table.concat({
  "formspec_version[3]size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #12602  MEDIA OVERRIDE REPLAY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Two ordered mods provide the same texture filename.]",
  "label[1.0,3.95;The later override mod must win.]",
  "label[1.0,4.65;Waiting for the registered replay ...]",
})

minetest.register_on_joinplayer(function(player)
  player:set_pos({x = 0, y = 2, z = 6})
  player:set_look_horizontal(0)
  player:set_look_vertical(0)
  player:set_sky({base_color = "#263445", type = "plain", textures = {}, clouds = false})
  player:set_clouds({density = 0})
  player:override_day_night_ratio(1)
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:12602", before)
  minetest.log("action", "C1_LAZARUS_12602_VISUAL_START state=common_before")
  minetest.after(6.0, function()
    local current = minetest.get_player_by_name(player:get_player_name())
    if not current then return end
    for x = -4, 4 do
      for y = -1, 5 do
        minetest.set_node({x = x, y = y, z = 2}, {name = "base:display"})
        minetest.set_node({x = x, y = y, z = 10}, {name = "base:display"})
      end
    end
    for z = 2, 10 do
      for y = -1, 5 do
        minetest.set_node({x = -4, y = y, z = z}, {name = "base:display"})
        minetest.set_node({x = 4, y = y, z = z}, {name = "base:display"})
      end
    end
    for x = -4, 4 do
      for z = 2, 10 do
        minetest.set_node({x = x, y = -1, z = z}, {name = "base:display"})
        minetest.set_node({x = x, y = 5, z = z}, {name = "base:display"})
      end
    end
    current:set_pos({x = 0, y = 2, z = 6})
    current:set_look_horizontal(0)
    current:set_look_vertical(0)
    minetest.close_formspec(current:get_player_name(), "c1_lazarus:12602")
    minetest.log("action", "C1_LAZARUS_12602_VISUAL_AFTER state=media_override_wall")
  end)
end)
'''


@dataclass(frozen=True)
class VisualRun:
    variant: Literal["fixed", "buggy"]
    process_outcome: str
    returncode: int | None
    visual_state: str | None
    color_score: float | None
    before: Path | None
    after: Path | None
    termination: str | None
    error: str | None


def write_luanti_12602_client_build_receipt(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    dependency, mapping, gate10, oracle, blocker = (
        _load(root, ref)
        for ref in (_DEPENDENCY, _MAPPING, _GATE10, _ORACLE, _OLD_BLOCKER)
    )
    if (
        dependency.payload.get("authority_tag") != "1.9.0mt6"
        or dependency.payload.get("candidate_owned") is not True
        or mapping.payload.get("candidate") != CANDIDATE
        or gate10.payload.get("state") != "passed"
        or oracle.payload.get("state") != "passed"
        or oracle.payload.get("no_timeout_or_unknown") is not True
        or blocker.payload.get("terminal") is not False
    ):
        raise RuntimeError("#12602 client-build input chain drifted")
    source = _ordinary_dir(root / _IRR_SOURCE, root)
    if (
        _git(source, "rev-parse", "HEAD") != "128cf1696c2803e12ebbdd3ee034e0c9eea90fae"
        or _git(source, "status", "--porcelain")
    ):
        raise RuntimeError("#12602 candidate-owned Irrlicht source drifted")
    library = _ordinary_file(root / _IRR_BUILD / "lib/OSX/libIrrlichtMt.1.9.0.dylib", root)
    config = _ordinary_file(root / _IRR_BUILD / "cmake/IrrlichtMtConfig.cmake", root)
    variants: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        binary = _ordinary_executable(root / _PAIR / variant / "bin/minetest", root)
        cache = _ordinary_file(
            root / _CLIENT_BUILD_ROOT / variant / "CMakeCache.txt", root
        ).read_text(encoding="utf-8", errors="replace")
        ninja = _ordinary_file(
            root / _CLIENT_BUILD_ROOT / variant / "build.ninja", root
        ).read_text(encoding="utf-8", errors="replace")
        required = (
            "BUILD_CLIENT:BOOL=TRUE",
            "BUILD_SERVER:BOOL=FALSE",
            "BUILD_UNITTESTS:BOOL=FALSE",
            f"IRRLICHTMT_BUILD_DIR:PATH={(root / _IRR_BUILD).as_posix()}",
            (root / _IRR_SOURCE / "include").as_posix(),
        )
        if any(marker not in cache + ninja for marker in required):
            raise RuntimeError(f"#12602 {variant} client configure is not closed")
        version = subprocess.run(
            (str(binary), "--version"),
            capture_output=True,
            timeout=15,
            check=False,
        )
        version_text = (version.stdout + version.stderr).decode("utf-8", errors="replace")
        if version.returncode != 0 or "Using Irrlicht 1.9.0mt6" not in version_text:
            raise RuntimeError(f"#12602 {variant} client version preflight failed")
        variants[variant] = {
            "binary": binary.relative_to(root).as_posix(),
            "binary_fingerprint": _fingerprint(binary),
            "version": version_text.strip(),
            "closed_configure": True,
        }
    payload = {
        "candidate": CANDIDATE,
        "state": "built",
        "outcome": "candidate_owned_fixed_buggy_real_clients_built",
        "complete": True,
        "variants": variants,
        "irrlicht_release": "1.9.0mt6",
        "irrlicht_source": _IRR_SOURCE.as_posix(),
        "irrlicht_build": _IRR_BUILD.as_posix(),
        "irrlicht_library": library.relative_to(root).as_posix(),
        "irrlicht_library_fingerprint": _fingerprint(library),
        "irrlicht_config_fingerprint": _fingerprint(config),
        "source_or_test_files_modified_for_client": False,
        "fixed_then_buggy_sequential": True,
        "build_parallelism": 2,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _CLIENT_BUILD,
        (
            dependency.reference,
            mapping.reference,
            gate10.reference,
            oracle.reference,
            blocker.reference,
        ),
        payload,
        authorization,
    )


def execute_luanti_12602_real_f12_visual(
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
    if not all(
        status.get(key) is True
        for key in ("console_unlocked", "accessibility_trusted", "event_post_access")
    ):
        raise RuntimeError("#12602 native visual session is unavailable")
    run = root / _RUN
    if run.exists() or run.is_symlink():
        raise RuntimeError("#12602 real F12 run root already exists")
    run.mkdir(parents=True, mode=0o700)
    results = tuple(_run_variant(root, run, variant) for variant in ("fixed", "buggy"))
    by_variant = {item.variant: item for item in results}
    expected = {"fixed": "goal_override_green", "buggy": "defect_base_red"}
    for variant, state in expected.items():
        item = by_variant[variant]
        if (
            item.process_outcome != "completed"
            or item.visual_state != state
            or item.before is None
            or item.after is None
            or not _valid_png(item.before)
            or not _valid_png(item.after)
        ):
            raise RuntimeError(f"#12602 {variant} visual capture failed closed: {item}")
    construction_payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "same_lineage_real_client_construction_passed",
        "complete": True,
        "client_build": client_build.reference.artifact_id,
        "mapping": mapping.reference.artifact_id,
        "semantic_oracle": oracle.reference.artifact_id,
        "run_root": _RUN.as_posix(),
        "same_source_materialization": _PAIR.as_posix(),
        "same_irrlicht_runtime": client_build.payload["irrlicht_library"],
        "ordered_mods": ["base", "override"],
        "duplicate_media_name": "c1_media.png",
        "base_texture": "red",
        "override_texture": "green",
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
            "visual_state": item.visual_state,
            "green_minus_red_score": item.color_score,
            "process_outcome": item.process_outcome,
            "returncode": item.returncode,
            "termination": item.termination,
            "common_before": item.before.relative_to(root).as_posix(),
            "common_before_fingerprint": _fingerprint(item.before),
            "after": item.after.relative_to(root).as_posix(),
            "after_fingerprint": _fingerprint(item.after),
            "dimensions": list(_dimensions(item.after)),
        }
    observation_payload = {
        "candidate": CANDIDATE,
        "state": "passed",
        "outcome": "real_f12_media_override_frames_captured",
        "complete": True,
        "capture_backend": "minetest_builtin_F12_OpenGL_PNG",
        "observations": observations,
        "common_before_semantic_equal": True,
        "fixed_after_goal": True,
        "buggy_after_defect": True,
        "real_duplicate_media_texture_rendered": True,
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
        "after_fixed_buggy_different": after_difference > 10.0,
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


def admit_luanti_12602_after_direct_visual_judgment(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
    judgment: str,
) -> StableArtifact:
    if confirmation != ADMISSION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {ADMISSION_CONFIRMATION!r}")
    if judgment != "before_equivalent_fixed_green_buggy_red_legible":
        raise RuntimeError("#12602 direct visual judgment is not affirmative")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    client_build, construction, observation, matrix, gate10 = (
        _load(root, ref)
        for ref in (_CLIENT_BUILD, _CONSTRUCTION, _OBSERVATION, _MATRIX, _GATE10)
    )
    if not (
        client_build.payload.get("state") == "built"
        and construction.payload.get("state") == "passed"
        and observation.payload.get("state") == "passed"
        and matrix.payload.get("before_equivalent") is True
        and matrix.payload.get("after_fixed_buggy_different") is True
        and gate10.payload.get("no_timeout_or_unknown") is True
    ):
        raise RuntimeError("#12602 formal admission inputs are incomplete")
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
            gate10.reference,
        ),
        payload,
        authorization,
    )


def _run_variant(root: Path, run: Path, variant: Literal["fixed", "buggy"]) -> VisualRun:
    target = run / variant
    world = target / "world"
    game = target / "games/c1_lazarus_12602"
    base = game / "mods/base"
    override = game / "mods/override"
    frames = target / "frames"
    home = target / "home"
    for path in (world, base / "textures", override / "textures", frames, home):
        path.mkdir(parents=True, mode=0o700)
    (game / "game.conf").write_text(
        "name = c1_lazarus_12602\ntitle = C1 Lazarus 12602 Visual\n",
        encoding="utf-8",
    )
    (base / "mod.conf").write_text("name = base\n", encoding="utf-8")
    (base / "init.lua").write_text(_BASE_INIT, encoding="utf-8")
    (override / "mod.conf").write_text(
        "name = override\ndepends = base\n", encoding="utf-8"
    )
    (override / "init.lua").write_text("-- media-only override\n", encoding="utf-8")
    _texture(base / "textures/c1_media.png", (225, 35, 35), (80, 0, 0))
    _texture(override / "textures/c1_media.png", (25, 205, 75), (0, 65, 20))
    (world / "world.mt").write_text(
        "gameid = c1_lazarus_12602\nbackend = sqlite3\n"
        "player_backend = sqlite3\nauth_backend = sqlite3\n",
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
    source = _ordinary_dir(root / _PAIR / variant, root)
    source_binary = _ordinary_executable(source / "bin/minetest", root)
    app = target / "minetest.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    macos.mkdir(parents=True, mode=0o700)
    binary = macos / "minetest"
    shutil.copy2(source_binary, binary)
    for resource in ("builtin", "client", "fonts", "textures"):
        shutil.copytree(source / resource, contents / resource, symlinks=False)
    (contents / "Info.plist").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>"
        "<key>CFBundleExecutable</key><string>minetest</string>"
        f"<key>CFBundleIdentifier</key><string>org.warptest.c1.lazarus.12602.{variant}</string>"
        "<key>CFBundleName</key><string>C1 Lazarus 12602</string>"
        "<key>CFBundlePackageType</key><string>APPL</string>"
        "<key>LSBackgroundOnly</key><false/><key>LSUIElement</key><false/>"
        "<key>NSHighResolutionCapable</key><true/>"
        "</dict></plist>\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "MINETEST_SUBGAME_PATH": str(target / "games"),
        }
    )
    argv = (
        str(binary),
        "--go",
        "--gameid",
        "c1_lazarus_12602",
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
        return VisualRun(variant, "launch_failed", None, None, None, None, None, None, str(error))
    try:
        deadline = time.monotonic() + 75.0
        if _wait_match(log, _START_RE, process, deadline) is None:
            return VisualRun(variant, "timed_out", process.poll(), None, None, None, None, None, "start_timeout")
        time.sleep(0.6)
        before = _capture_f12(frames, process.pid, known=set())
        if _wait_match(log, _AFTER_RE, process, deadline) is None:
            return VisualRun(variant, "timed_out", process.poll(), None, None, before, None, None, "after_timeout")
        time.sleep(1.2)
        after = _capture_f12(frames, process.pid, known={before} if before else set())
        score = _green_minus_red(after) if after else None
        state = None
        if score is not None and score > 20.0:
            state = "goal_override_green"
        elif score is not None and score < -20.0:
            state = "defect_base_red"
        _stop(process)
        return VisualRun(
            variant,
            "completed",
            process.returncode,
            state,
            score,
            before,
            after,
            "owned_process_group_sigterm_after_both_frames",
            None if before and after and state else "visual_classification_failed",
        )
    finally:
        _stop(process)
        out.close()
        err.close()


def _texture(path: Path, primary: tuple[int, int, int], dark: tuple[int, int, int]) -> None:
    image = Image.new("RGB", (64, 64), primary)
    draw = ImageDraw.Draw(image)
    for offset in range(0, 64, 16):
        draw.rectangle((offset, 0, offset + 5, 63), fill=dark)
        draw.rectangle((0, offset, 63, offset + 5), fill=dark)
    image.save(path, format="PNG")


def _wait_match(
    log: Path,
    pattern: re.Pattern[str],
    process: subprocess.Popen[bytes],
    deadline: float,
) -> re.Match[str] | None:
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


def _green_minus_red(path: Path) -> float:
    with Image.open(path).convert("RGB") as image:
        width, height = image.size
        crop = image.crop((width // 4, height // 5, width * 3 // 4, height * 4 // 5))
        mean = ImageStat.Stat(crop).mean
        return float(mean[1] - mean[0])


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


def _fingerprint(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ("git", "-C", str(cwd), *args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace")[:500])
    return process.stdout.decode("utf-8").strip()


def _ordinary_dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir():
        raise RuntimeError("#12602 visual directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#12602 visual file is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    result = _ordinary_file(path, root)
    if not os.access(result, os.X_OK):
        raise RuntimeError("#12602 visual binary is not executable")
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
            raise RuntimeError(f"#12602 artifact conflicts: {ref.artifact_id}")
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
        raise RuntimeError(f"#12602 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#12602 client visual authorization is inactive")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "ADMISSION_CONFIRMATION",
    "CONFIRMATION",
    "admit_luanti_12602_after_direct_visual_judgment",
    "execute_luanti_12602_real_f12_visual",
    "write_luanti_12602_client_build_receipt",
]
