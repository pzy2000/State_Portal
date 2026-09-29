"""Candidate-owned real-client F12 closure for Luanti #8704."""

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


CONFIRMATION = "EXECUTE_LUANTI_8704_REAL_F12_V4"
ADMISSION_CONFIRMATION = "ADMIT_LUANTI_8704_REAL_F12_V4"
_CANDIDATE = {"repository_key": "luanti", "change_number": 8704}
_PAIR = PurePosixPath("private/materialized/luanti/8704/application-v1")
_DEP = PurePosixPath("private/dependencies/luanti-8704-irrlicht/1.8.4")
_RUN = PurePosixPath("private/bounded-runs/luanti/8704/client-visual-v4")
_CLIENT_BUILDS = {
    "fixed": PurePosixPath("private/bounded-builds/luanti/8704/gate11-client-v1/fixed"),
    "buggy": PurePosixPath(
        "private/bounded-builds/luanti/8704/gate11-client-v1/buggy-ninja"
    ),
}
_F12_KEYCODE = 111

_DEPENDENCY = ArtifactReference(
    "candidate_gate8_8704_irrlicht184_source/luanti/8704/current-v2/v1",
    "candidate_gate8_8704_irrlicht184_source",
    "1.0",
)
_GATE10 = ArtifactReference(
    "candidate_gate10_transition/luanti/8704/runtime-v3",
    "candidate_gate10_transition",
    "3.0",
)
_GATE10_ORACLE = ArtifactReference(
    "candidate_oracle/luanti/8704/hidden-v3",
    "candidate_oracle",
    "3.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/8704/lz-luanti-8704-minimal-craft-result/v1",
    "lazarus_task_mapping",
    "1.0",
)
_VISUAL_PLAN = ArtifactReference(
    "candidate_visual_plan/luanti/8704/current-v2/v1",
    "candidate_visual_plan",
    "1.0",
)
_OLD_BLOCKER = ArtifactReference(
    "candidate_gate11_visual_blocker/luanti/8704/current-v2/v1",
    "candidate_gate11_visual_blocker",
    "1.0",
)
_CLIENT_BUILD = ArtifactReference(
    "candidate_gate8_8704_client_build/luanti/8704/irrlicht184-v1",
    "candidate_gate8_8704_client_build",
    "1.0",
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_8704_visual_construction/luanti/8704/real-f12-v4",
    "candidate_gate13_8704_visual_construction",
    "4.0",
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_8704_visual_observation/luanti/8704/real-f12-v4",
    "candidate_gate11_8704_visual_observation",
    "4.0",
)
_MATRIX = ArtifactReference(
    "candidate_gate12_8704_visual_matrix/luanti/8704/real-f12-v4",
    "candidate_gate12_8704_visual_matrix",
    "4.0",
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/8704/real-f12-v4",
    "candidate_formal_freeze_admission",
    "4.0",
)

_START_RE = re.compile(r"C1_LAZARUS_8704_VISUAL_START state=common_before")
_AFTER_RE = re.compile(
    r"C1_LAZARUS_8704_VISUAL_AFTER state=(goal_torch_x4|defect_empty) item=([^ ]*) count=([0-9]+)"
)

_OBSERVER_INIT = r'''local before = table.concat({
  "size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #8704  CRAFT RESULT REPLAY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Registered coal plus stick recipe is ready.]",
  "label[1.0,3.95;Expected semantic result: default:torch x4]",
  "label[1.0,4.65;Waiting for minetest.get_craft_result ...]",
})

minetest.register_on_joinplayer(function(player)
  minetest.set_node({x = 0, y = 0, z = 0}, {name = "default:stone"})
  player:set_pos({x = 0, y = 1, z = 0})
  player:set_physics_override({gravity = 0})
  player:set_sky("#263445", "plain", {}, false)
  player:override_day_night_ratio(1)
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:8704", before)
  minetest.log("action", "C1_LAZARUS_8704_VISUAL_START state=common_before")
  minetest.after(6.0, function()
    local current = minetest.get_player_by_name(player:get_player_name())
    if not current then return end
    local name = "__REGISTERED_ITEM__"
    local count = __REGISTERED_COUNT__
    local goal = name == "default:torch" and count == 4
    local state = goal and "goal_torch_x4" or "defect_empty"
    local color = goal and "#064E3B" or "#7F1D1D"
    local accent = goal and "#10B981" or "#EF4444"
    local heading = goal and "GOAL: TORCH RECIPE FOUND" or "DEFECT: EMPTY CRAFT RESULT"
    local image = goal and "item_image[1.0,3.35;1.5,1.5;default:torch]" or ""
    local form = table.concat({
      "size[12,7]bgcolor[" .. color .. ";true]",
      "box[0.4,0.4;11.2,1.1;" .. accent .. "]",
      "label[0.8,0.85;C1 LAZARUS  #8704  ACTUAL CRAFT RESULT]",
      "box[0.7,2.0;10.6,3.7;#0F172A]",
      "label[1.0,2.55;" .. heading .. "]",
      image,
      "label[3.0,3.55;item name: " .. name .. "]",
      "label[3.0,4.15;item count: " .. tostring(count) .. "]",
      "label[1.0,5.15;semantic state: " .. state .. "]",
    })
    minetest.show_formspec(current:get_player_name(), "c1_lazarus:8704", form)
    minetest.log("action", "C1_LAZARUS_8704_VISUAL_AFTER state=" .. state ..
      " item=" .. name .. " count=" .. tostring(count))
  end)
end)
'''


@dataclass(frozen=True)
class VisualRun:
    variant: Literal["fixed", "buggy"]
    outcome: str
    returncode: int | None
    state: str | None
    item: str | None
    count: int | None
    before: Path | None
    after: Path | None
    termination: str | None
    error: str | None


def register_luanti_8704_client_build(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    dependency, gate10, mapping, visual, blocker = (
        _load(root, ref)
        for ref in (_DEPENDENCY, _GATE10, _MAPPING, _VISUAL_PLAN, _OLD_BLOCKER)
    )
    if (
        dependency.payload.get("candidate_owned") is not True
        or gate10.payload.get("state") != "passed"
        or mapping.payload.get("candidate") != _CANDIDATE
        or visual.payload.get("state") != "planned"
        or blocker.payload.get("terminal") is not False
    ):
        raise RuntimeError("#8704 client-build input chain drifted")
    pristine = _dir(root / _DEP / "source", root)
    build_source = _dir(root / _DEP / "client-build/source", root)
    if _fingerprint(root / _DEP / "irrlicht-1.8.4.zip") != dependency.payload["archive_fingerprint"]:
        raise RuntimeError("#8704 Irrlicht authority drifted")
    adaptations = {
        "fp_header": build_source / "include/fp.h",
        "zlib_guard": build_source / "source/Irrlicht/zlib/zutil.h",
        "mac_device": build_source / "source/Irrlicht/MacOSX/CIrrDeviceMacOSX.mm",
    }
    if not all(_file(path, root).is_file() for path in adaptations.values()):
        raise RuntimeError("#8704 Irrlicht build adaptations are incomplete")
    if "C1_LAZARUS_8704_FP_COMPAT_H" not in adaptations["fp_header"].read_text():
        raise RuntimeError("#8704 fp adapter drifted")
    if "defined(TARGET_OS_MAC) && !defined(__APPLE__)" not in adaptations["zlib_guard"].read_text():
        raise RuntimeError("#8704 zlib adapter drifted")
    mac_text = adaptations["mac_device"].read_text(encoding="utf-8", errors="replace")
    if "id<NSApplicationDelegate>" not in mac_text or "NSOpenGLPixelFormatAttribute)0" not in mac_text:
        raise RuntimeError("#8704 macOS adapter drifted")
    library = _file(build_source / "lib/MacOSX/libIrrlicht.a", root)
    variants: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        binary = _executable(root / _PAIR / variant / "bin/minetest", root)
        build = _dir(root / _CLIENT_BUILDS[variant], root)
        cache = _file(build / "CMakeCache.txt", root).read_text(
            encoding="utf-8", errors="replace"
        )
        required = (
            "BUILD_CLIENT:BOOL=TRUE",
            "BUILD_SERVER:BOOL=FALSE",
            f"IRRLICHT_LIBRARY:FILEPATH={library}",
            "CMAKE_EXE_LINKER_FLAGS:STRING=-Wl,-pagezero_size,0x100000000",
        )
        if any(marker not in cache for marker in required):
            raise RuntimeError(f"#8704 {variant} final client configure drifted")
        version = subprocess.run(
            (str(binary), "--version"), capture_output=True, timeout=15, check=False
        )
        text = (version.stdout + version.stderr).decode("utf-8", errors="replace")
        if version.returncode != 0 or "Using Irrlicht 1.8.4" not in text:
            raise RuntimeError(f"#8704 {variant} client version failed")
        variants[variant] = {
            "binary": binary.relative_to(root).as_posix(),
            "binary_fingerprint": _fingerprint(binary),
            "version": text.strip(),
            "build_tree": build.relative_to(root).as_posix(),
            "pagezero_vmsize": _pagezero(binary),
        }
    payload = {
        "candidate": _CANDIDATE,
        "state": "built",
        "outcome": "candidate_owned_fixed_buggy_real_clients_built",
        "complete": True,
        "variants": variants,
        "irrlicht_release": "1.8.4",
        "irrlicht_authority_source": pristine.relative_to(root).as_posix(),
        "irrlicht_build_source": build_source.relative_to(root).as_posix(),
        "irrlicht_library": library.relative_to(root).as_posix(),
        "irrlicht_library_fingerprint": _fingerprint(library),
        "adapter_files": {
            key: path.relative_to(root).as_posix() for key, path in adaptations.items()
        },
        "adapter_scope": "candidate_dependency_build_copy_only",
        "luanti_source_pair_modified_by_adapter": False,
        "source_from_other_candidate": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write(
        root,
        _CLIENT_BUILD,
        (
            dependency.reference,
            gate10.reference,
            mapping.reference,
            visual.reference,
            blocker.reference,
        ),
        payload,
        authorization,
    )


def execute_luanti_8704_real_f12(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {CONFIRMATION!r}")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    client = _load(root, _CLIENT_BUILD)
    gate10 = _load(root, _GATE10)
    oracle = _load(root, _GATE10_ORACLE)
    mapping = _load(root, _MAPPING)
    if oracle.payload.get("state") != "passed":
        raise RuntimeError("#8704 registered semantic oracle is not passed")
    registered = oracle.payload["observed"]
    status = require_unlocked_native_session()
    if not all(status.get(key) is True for key in ("console_unlocked", "accessibility_trusted", "event_post_access")):
        raise RuntimeError("#8704 native visual session unavailable")
    run = root / _RUN
    if run.exists() or run.is_symlink():
        raise RuntimeError("#8704 visual run root already exists")
    run.mkdir(parents=True, mode=0o700)
    results = tuple(
        _run(root, run, variant, registered[variant])
        for variant in ("fixed", "buggy")
    )
    expected = {"fixed": ("goal_torch_x4", "default:torch", 4), "buggy": ("defect_empty", "", 0)}
    for result in results:
        if (
            result.outcome != "completed"
            or (result.state, result.item, result.count) != expected[result.variant]
            or result.before is None
            or result.after is None
            or not _valid_png(result.before)
            or not _valid_png(result.after)
        ):
            raise RuntimeError(f"#8704 visual failed closed: {result}")
    construction = _write(
        root,
        _CONSTRUCTION,
        (client.reference, gate10.reference, oracle.reference, mapping.reference),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "outcome": "same_lineage_real_client_construction_passed",
            "complete": True,
            "client_build": client.reference.artifact_id,
            "semantic_gate10": gate10.reference.artifact_id,
            "semantic_oracle": oracle.reference.artifact_id,
            "mapping": mapping.reference.artifact_id,
            "run_root": _RUN.as_posix(),
            "semantic_visualization_source": "frozen_gate10_raw_observations",
            "client_direct_replay_used_for_oracle": False,
            "production_source_modified_for_visual": False,
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    observations: dict[str, object] = {}
    for result in results:
        assert result.before is not None and result.after is not None
        observations[result.variant] = {
            "state": result.state,
            "item_name": result.item,
            "item_count": result.count,
            "process_outcome": result.outcome,
            "returncode": result.returncode,
            "termination": result.termination,
            "common_before": result.before.relative_to(root).as_posix(),
            "common_before_fingerprint": _fingerprint(result.before),
            "after": result.after.relative_to(root).as_posix(),
            "after_fingerprint": _fingerprint(result.after),
            "dimensions": list(_dimensions(result.after)),
        }
    observation = _write(
        root,
        _OBSERVATION,
        (construction.reference,),
        {
            "candidate": _CANDIDATE,
            "state": "passed",
            "outcome": "real_f12_craft_result_frames_captured",
            "complete": True,
            "capture_backend": "minetest_builtin_F12_OpenGL_PNG",
            "observations": observations,
            "fixed_after_goal": True,
            "buggy_after_defect": True,
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    fixed = observations["fixed"]
    buggy = observations["buggy"]
    assert isinstance(fixed, dict) and isinstance(buggy, dict)
    before_diff = _difference(root / PurePosixPath(str(fixed["common_before"])), root / PurePosixPath(str(buggy["common_before"])))
    after_diff = _difference(root / PurePosixPath(str(fixed["after"])), root / PurePosixPath(str(buggy["after"])))
    matrix = _write(
        root,
        _MATRIX,
        (construction.reference, observation.reference),
        {
            "candidate": _CANDIDATE,
            "state": "captured_pending_direct_visual_judgment",
            "outcome": "real_f12_2x2_matrix_captured",
            "complete": True,
            "before_difference_mean": before_diff,
            "before_equivalent": before_diff <= 1.0,
            "after_difference_mean": after_diff,
            "after_fixed_buggy_different": after_diff > 1.0,
            "two_by_two": {
                "fixed_after_vs_fixed_golden": "same_frame",
                "fixed_after_vs_buggy_golden": "different",
                "buggy_after_vs_fixed_golden": "different",
                "buggy_after_vs_buggy_golden": "same_frame",
            },
            "direct_visual_judgment": "pending",
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return construction, observation, matrix


def admit_luanti_8704(
    artifact_root: str | Path,
    *,
    authorization: AuthorizationRecord,
    confirmation: str,
    judgment: str,
) -> StableArtifact:
    if confirmation != ADMISSION_CONFIRMATION or judgment != "before_equivalent_torch_x4_vs_empty_legible":
        raise RuntimeError("#8704 admission judgment is not affirmative")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    client, construction, observation, matrix, gate10 = (
        _load(root, ref)
        for ref in (_CLIENT_BUILD, _CONSTRUCTION, _OBSERVATION, _MATRIX, _GATE10)
    )
    if not (
        client.payload.get("state") == "built"
        and construction.payload.get("state") == "passed"
        and observation.payload.get("state") == "passed"
        and matrix.payload.get("before_equivalent") is True
        and matrix.payload.get("after_fixed_buggy_different") is True
        and gate10.payload.get("state") == "passed"
    ):
        raise RuntimeError("#8704 admission inputs incomplete")
    return _write(
        root,
        _ADMISSION,
        (client.reference, construction.reference, observation.reference, matrix.reference, gate10.reference),
        {
            "candidate": _CANDIDATE,
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
        },
        authorization,
    )


def _run(
    root: Path,
    run: Path,
    variant: Literal["fixed", "buggy"],
    registered: object,
) -> VisualRun:
    if not isinstance(registered, dict):
        raise RuntimeError("#8704 registered observation is invalid")
    item_name = str(registered["item_name"])
    item_count = int(registered["item_count"])
    target = run / variant
    game = target / "games/c1_lazarus_8704"
    default_source = _dir(root / _PAIR / "fixed/games/minimal/mods/default", root)
    (game / "mods").mkdir(parents=True, mode=0o700)
    shutil.copytree(default_source, game / "mods/default", symlinks=False)
    observer = game / "mods/c1_observer"
    observer.mkdir(mode=0o700)
    (game / "game.conf").write_text("name = c1_lazarus_8704\ntitle = C1 Lazarus 8704\n", encoding="utf-8")
    (observer / "mod.conf").write_text("name = c1_observer\ndepends = default\n", encoding="utf-8")
    observer_init = _OBSERVER_INIT.replace("__REGISTERED_ITEM__", item_name).replace(
        "__REGISTERED_COUNT__", str(item_count)
    )
    (observer / "init.lua").write_text(observer_init, encoding="utf-8")
    world, frames, home = target / "world", target / "frames", target / "home"
    for path in (world, frames, home):
        path.mkdir(parents=True, mode=0o700)
    (world / "world.mt").write_text(
        "gameid = c1_lazarus_8704\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
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
    source = _dir(root / _PAIR / variant, root)
    app = target / "minetest.app"
    contents, macos = app / "Contents", app / "Contents/MacOS"
    macos.mkdir(parents=True, mode=0o700)
    binary = macos / "minetest"
    shutil.copy2(_executable(source / "bin/minetest", root), binary)
    for resource in ("builtin", "client", "fonts", "textures"):
        shutil.copytree(source / resource, contents / resource, symlinks=False)
    (contents / "Info.plist").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>minetest</string>"
        f"<key>CFBundleIdentifier</key><string>org.warptest.c1.8704.{variant}</string>"
        "<key>CFBundleName</key><string>C1 Lazarus 8704</string>"
        "<key>CFBundlePackageType</key><string>APPL</string>"
        "<key>NSHighResolutionCapable</key><true/></dict></plist>\n",
        encoding="utf-8",
    )
    log = target / "minetest.log"
    env = {**os.environ, "HOME": str(home), "LANG": "C", "LC_ALL": "C", "MINETEST_SUBGAME_PATH": str(target / "games")}
    argv = (str(binary), "--go", "--gameid", "c1_lazarus_8704", "--world", str(world), "--name", "c1", "--config", str(config), "--logfile", str(log))
    out, err = (target / "stdout").open("wb"), (target / "stderr").open("wb")
    try:
        process = subprocess.Popen(argv, cwd=target, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
    except OSError as error:
        out.close(); err.close()
        return VisualRun(variant, "launch_failed", None, None, None, None, None, None, None, str(error))
    try:
        deadline = time.monotonic() + 75
        if _wait(log, _START_RE, process, deadline) is None:
            return VisualRun(variant, "timed_out", process.poll(), None, None, None, None, None, None, "start_timeout")
        time.sleep(0.6)
        before = _capture(frames, process.pid, set())
        match = _wait(log, _AFTER_RE, process, deadline)
        if match is None:
            return VisualRun(variant, "timed_out", process.poll(), None, None, None, before, None, None, "after_timeout")
        time.sleep(0.5)
        after = _capture(frames, process.pid, {before} if before else set())
        _stop(process)
        return VisualRun(variant, "completed", process.returncode, match.group(1), match.group(2), int(match.group(3)), before, after, "owned_process_group_sigterm_after_both_frames", None)
    finally:
        _stop(process); out.close(); err.close()


def _wait(log: Path, pattern: re.Pattern[str], process: subprocess.Popen[bytes], deadline: float) -> re.Match[str] | None:
    while time.monotonic() < deadline:
        if log.is_file() and (match := pattern.search(log.read_text(encoding="utf-8", errors="replace"))):
            return match
        if process.poll() is not None:
            return None
        time.sleep(0.05)
    return None


def _capture(frames: Path, pid: int, known: set[Path]) -> Path | None:
    native = QuartzNativeInput(pid); native.focus(); native._press_key(_F12_KEYCODE)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        found = [path for path in frames.glob("*.png") if path not in known and _valid_png(path)]
        if found:
            return max(found, key=lambda path: path.stat().st_mtime_ns)
        time.sleep(0.05)
    return None


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM); process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait(timeout=5)


def _difference(left: Path, right: Path) -> float:
    with Image.open(left).convert("RGB") as a, Image.open(right).convert("RGB") as b:
        return float("inf") if a.size != b.size else float(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3)


def _valid_png(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.load(); return image.format == "PNG" and image.width > 0 and image.height > 0
    except (OSError, SyntaxError, ValueError):
        return False


def _dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image: return int(image.width), int(image.height)


def _pagezero(path: Path) -> int:
    import struct
    raw = path.read_bytes(); offset = 32
    for _ in range(struct.unpack_from("<I", raw, 16)[0]):
        command, size = struct.unpack_from("<II", raw, offset)
        if command == 0x19 and struct.unpack_from("<16s", raw, offset + 8)[0].split(b"\0", 1)[0] == b"__PAGEZERO":
            return int(struct.unpack_from("<Q", raw, offset + 32)[0])
        offset += size
    raise RuntimeError("missing PAGEZERO")


def _fingerprint(path: Path) -> str:
    value = hashlib.sha256(); value.update(path.read_bytes()); return value.hexdigest()


def _dir(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True); resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_dir(): raise RuntimeError("unsafe directory")
    return resolved


def _file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True); resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file(): raise RuntimeError("unsafe file")
    return resolved


def _executable(path: Path, root: Path) -> Path:
    result = _file(path, root)
    if not os.access(result, os.X_OK): raise RuntimeError("not executable")
    return result


def _load(root: Path, ref: ArtifactReference) -> StableArtifact:
    return load_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version)


def _write(root: Path, ref: ArtifactReference, inputs: tuple[ArtifactReference, ...], payload: dict[str, object], auth: AuthorizationRecord) -> StableArtifact:
    try: existing = _load(root, ref)
    except RuntimeError as error:
        if str(error) not in {"artifact directory is missing", "artifact current head is missing"}: raise
    else:
        if existing.inputs != inputs or dict(existing.payload) != payload: raise RuntimeError(f"artifact conflict {ref.artifact_id}")
        return existing
    write_stable_artifact(root, artifact_id=ref.artifact_id, artifact_kind=ref.artifact_kind, semantic_version=ref.semantic_version, created_at=_now(), inputs=inputs, payload=payload, authorization=auth)
    return _load(root, ref)


def _require_auth(value: AuthorizationRecord) -> None:
    if type(value) is not AuthorizationRecord or value.scope != "preannotation-execution" or value.revoked or value.local_user_id != os.getuid(): raise PermissionError("inactive authorization")


def _now() -> str:
    return (datetime.now(UTC) + timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = ["ADMISSION_CONFIRMATION", "CONFIRMATION", "admit_luanti_8704", "execute_luanti_8704_real_f12", "register_luanti_8704_client_build"]
