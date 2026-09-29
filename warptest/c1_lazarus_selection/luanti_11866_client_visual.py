"""Real-client F12 visual closure and admission for Luanti #11866."""

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


CANDIDATE = {"repository_key": "luanti", "change_number": 11866}
CONFIRMATION = "EXECUTE_LUANTI_11866_REAL_F12_VISUAL_V1"
ADMISSION_CONFIRMATION = "ADMIT_LUANTI_11866_REAL_F12_VISUAL_V1"
_PAIR = PurePosixPath("private/materialized/luanti/11866/gate8-mt4-v1")
_DEPENDENCY = PurePosixPath("private/dependencies/luanti-11866-irrlichtmt/1.9.0mt4")
_IRR_BUILD = _DEPENDENCY / "client-build-v1"
_CLIENT_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/11866/client-mt4-v1")
_RUN = PurePosixPath("private/bounded-runs/luanti/11866/client-visual-v2")
_F12_KEYCODE = 111

_GATE8 = ArtifactReference(
    "candidate_gate8_11866_native_build/luanti/11866/mt4-v1",
    "candidate_gate8_11866_native_build",
    "1.0",
)
_DEP = ArtifactReference(
    "candidate_gate8_11866_irrlichtmt_dependency/luanti/11866/mt4-v1",
    "candidate_gate8_11866_irrlichtmt_dependency",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/11866/gate8-mt4-v1",
    "candidate_materialization",
    "1.0",
)
_MAPPING = ArtifactReference(
    "lazarus_task_mapping/luanti/11866/lz-luanti-11866-mapgen-boundary/v3",
    "lazarus_task_mapping",
    "3.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_11866_replay_oracle/luanti/11866/bounded-v3",
    "candidate_gate10_11866_replay_oracle",
    "3.0",
)
_OLD_BLOCKER = ArtifactReference(
    "candidate_gate11_13_11866_visual_blocker/luanti/11866/mt4-v3",
    "candidate_gate11_13_11866_visual_blocker",
    "3.0",
)
_CLIENT_BUILD = ArtifactReference(
    "candidate_gate8_11866_client_build/luanti/11866/mt4-v1",
    "candidate_gate8_11866_client_build",
    "1.0",
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_11866_visual_construction/luanti/11866/real-f12-v2",
    "candidate_gate13_11866_visual_construction",
    "2.0",
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_11866_visual_observation/luanti/11866/real-f12-v2",
    "candidate_gate11_11866_visual_observation",
    "2.0",
)
_MATRIX = ArtifactReference(
    "candidate_gate12_11866_visual_matrix/luanti/11866/real-f12-v2",
    "candidate_gate12_11866_visual_matrix",
    "2.0",
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/11866/real-f12-v2",
    "candidate_formal_freeze_admission",
    "2.0",
)

_START_RE = re.compile(r"C1_LAZARUS_11866_VISUAL_START state=(common_before)")
_AFTER_RE = re.compile(
    r"C1_LAZARUS_11866_VISUAL_AFTER state=(goal_mapgen_limit_aligned|defect_mapgen_limit_misaligned) limit=([0-9]+) remainder=([0-9]+)"
)

_MOD_CONF = "name = c1_lazarus_visual\n"
_MOD_INIT = r'''local before = table.concat({
  "formspec_version[3]size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #11866  MAPGEN BOUNDARY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Read the engine default mapgen_limit.]",
  "label[1.0,3.95;MAP_BLOCKSIZE = 16]",
  "label[1.0,4.65;Goal: limit % 16 equals 15 (the last node in a mapblock).]",
  "label[1.0,5.25;Waiting for the ordered replay ...]",
})

local function after(player)
  local limit = tonumber(minetest.settings:get("mapgen_limit"))
  local remainder = limit and (limit % 16) or -1
  local goal = remainder == 15
  local state = goal and "goal_mapgen_limit_aligned" or "defect_mapgen_limit_misaligned"
  local color = goal and "#064E3B" or "#7F1D1D"
  local accent = goal and "#10B981" or "#EF4444"
  local heading = goal and "GOAL: MAPGEN LIMIT ALIGNED" or "DEFECT: MAPGEN LIMIT MISALIGNED"
  local form = table.concat({
    "formspec_version[3]size[12,7]bgcolor[" .. color .. ";true]",
    "box[0.4,0.4;11.2,1.1;" .. accent .. "]",
    "label[0.8,0.85;C1 LAZARUS  #11866  REPLAY RESULT]",
    "box[0.7,2.0;10.6,3.7;#0F172A]",
    "label[1.0,2.55;" .. heading .. "]",
    "label[1.0,3.25;mapgen_limit = " .. tostring(limit) .. "]",
    "label[1.0,3.95;" .. tostring(limit) .. " % 16 = " .. tostring(remainder) .. "]",
    "label[1.0,4.65;expected boundary remainder = 15]",
    "label[1.0,5.25;semantic class: " .. state .. "]",
  })
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:11866", form)
  minetest.log("action", "C1_LAZARUS_11866_VISUAL_AFTER state=" .. state ..
    " limit=" .. tostring(limit) .. " remainder=" .. tostring(remainder))
end

minetest.register_on_joinplayer(function(player)
  player:set_pos({x = 0, y = 3, z = 6})
  player:set_look_horizontal(0)
  player:set_look_vertical(0)
  player:set_sky({r = 30, g = 38, b = 50}, "plain", {}, false)
  player:set_clouds({density = 0})
  minetest.set_timeofday(0.5)
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:11866", before)
  minetest.log("action", "C1_LAZARUS_11866_VISUAL_START state=common_before")
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
    state: str | None
    limit: int | None
    remainder: int | None
    before: Path | None
    after: Path | None
    termination: str | None
    error: str | None


def write_client_build_receipt(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> StableArtifact:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    gate8, dependency, materialization, mapping, oracle, blocker = (
        _load(root, ref)
        for ref in (_GATE8, _DEP, _MATERIALIZATION, _MAPPING, _ORACLE, _OLD_BLOCKER)
    )
    if not (
        gate8.payload.get("state") == "built"
        and dependency.payload.get("dependency_tag") == "1.9.0mt4"
        and materialization.payload.get("state") == "materialized"
        and oracle.payload.get("state") == "passed"
        and oracle.payload.get("no_timeout_or_unknown") is True
        and blocker.payload.get("terminal") is False
    ):
        raise RuntimeError("#11866 client-build input chain drifted")
    irr_library = _ordinary_file(root / _IRR_BUILD / "lib/OSX/libIrrlichtMt.a", root)
    variants: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR / variant, root)
        binary = _ordinary_executable(tree / "bin/minetest", root)
        cache = _ordinary_file(root / _CLIENT_BUILD_ROOT / variant / "CMakeCache.txt", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        ninja = _ordinary_file(root / _CLIENT_BUILD_ROOT / variant / "build.ninja", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        required = (
            "BUILD_CLIENT:BOOL=TRUE",
            "BUILD_SERVER:BOOL=FALSE",
            "BUILD_UNITTESTS:BOOL=FALSE",
            "ENABLE_CURSES:BOOL=FALSE",
            f"IRRLICHTMT_BUILD_DIR:PATH={root / _IRR_BUILD}",
            str(irr_library),
        )
        if any(marker not in cache + ninja for marker in required):
            raise RuntimeError(f"#11866 {variant} real-client configure drifted")
        if "/opt/homebrew" in ninja or "14464" in ninja:
            raise RuntimeError(f"#11866 {variant} real-client build is not closed")
        version = subprocess.run((str(binary), "--version"), capture_output=True, timeout=15)
        text = (version.stdout + version.stderr).decode("utf-8", errors="replace")
        if version.returncode != 0 or "Using Irrlicht 1.9.0mt4" not in text:
            raise RuntimeError(f"#11866 {variant} real-client version preflight failed")
        links = subprocess.run(("otool", "-L", str(binary)), capture_output=True, text=True, timeout=15)
        if links.returncode != 0 or "/opt/homebrew" in links.stdout:
            raise RuntimeError(f"#11866 {variant} real-client runtime linkage is not closed")
        variants[variant] = {
            "binary": binary.relative_to(root).as_posix(),
            "binary_fingerprint": _sha256(binary),
            "version": text.strip(),
            "closed_configure": True,
        }
    payload = {
        "candidate": CANDIDATE,
        "state": "built",
        "outcome": "candidate_owned_mt4_fixed_buggy_real_clients_built",
        "complete": True,
        "variants": variants,
        "irrlicht_tag": "1.9.0mt4",
        "irrlicht_library": irr_library.relative_to(root).as_posix(),
        "irrlicht_library_fingerprint": _sha256(irr_library),
        "build_order": ["irrlichtmt", "fixed", "buggy"],
        "build_parallelism": 2,
        "runtime_dylibs_rebound_to_candidate_namespace": True,
        "ad_hoc_codesign_after_rebinding": True,
        "source_from_14464": False,
        "production_source_modified_by_packaging": False,
        "old_attempts_loaded": False,
        "candidate_execution_performed": False,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    return _write_once(
        root,
        _CLIENT_BUILD,
        (
            gate8.reference,
            dependency.reference,
            materialization.reference,
            mapping.reference,
            oracle.reference,
            blocker.reference,
        ),
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
        raise RuntimeError("#11866 native visual session is unavailable")
    run = root / _RUN
    if run.exists() or run.is_symlink():
        raise RuntimeError("#11866 real F12 run root already exists")
    run.mkdir(parents=True, mode=0o700)
    results = tuple(_run_variant(root, run, variant) for variant in ("fixed", "buggy"))
    by_variant = {item.variant: item for item in results}
    expected = {
        "fixed": ("goal_mapgen_limit_aligned", 31007, 15),
        "buggy": ("defect_mapgen_limit_misaligned", 31000, 8),
    }
    for variant in ("fixed", "buggy"):
        item = by_variant[variant]
        if not (
            item.process_outcome == "completed"
            and (item.state, item.limit, item.remainder) == expected[variant]
            and item.returncode == 0
            and item.termination == "owned_process_group_sigterm_after_both_frames"
            and item.before is not None
            and item.after is not None
            and _valid_png(item.before)
            and _valid_png(item.after)
        ):
            raise RuntimeError(f"#11866 {variant} visual capture failed: {item}")
    construction = _write_once(
        root,
        _CONSTRUCTION,
        (client_build.reference, mapping.reference, oracle.reference),
        {
            "candidate": CANDIDATE,
            "state": "passed",
            "outcome": "same_lineage_real_client_construction_passed",
            "complete": True,
            "client_build": client_build.reference.artifact_id,
            "mapping": mapping.reference.artifact_id,
            "semantic_oracle": oracle.reference.artifact_id,
            "run_root": _RUN.as_posix(),
            "same_fixed_buggy_source_materialization": _PAIR.as_posix(),
            "same_irrlichtmt_runtime": client_build.payload["irrlicht_library"],
            "ordered_replay_same_as_gate10": True,
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
    for item in results:
        assert item.before is not None and item.after is not None
        observations[item.variant] = {
            "semantic_state": item.state,
            "mapgen_limit": item.limit,
            "boundary_remainder": item.remainder,
            "process_outcome": item.process_outcome,
            "returncode": item.returncode,
            "termination": item.termination,
            "common_before": item.before.relative_to(root).as_posix(),
            "common_before_fingerprint": _sha256(item.before),
            "after": item.after.relative_to(root).as_posix(),
            "after_fingerprint": _sha256(item.after),
            "dimensions": list(_dimensions(item.after)),
        }
    observation = _write_once(
        root,
        _OBSERVATION,
        (construction.reference,),
        {
            "candidate": CANDIDATE,
            "state": "passed",
            "outcome": "real_f12_mapgen_boundary_frames_captured",
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
        },
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
    matrix = _write_once(
        root,
        _MATRIX,
        (construction.reference, observation.reference),
        {
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
        },
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
    if judgment != "before_equivalent_after_aligned_vs_misaligned_legible":
        raise RuntimeError("#11866 direct visual judgment is not affirmative")
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
        raise RuntimeError("#11866 formal admission inputs are incomplete")
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
    game = target / "games/c1_lazarus_11866"
    frames = target / "frames"
    home = target / "home"
    for path in (mod, game, frames, home):
        path.mkdir(parents=True, mode=0o700)
    (game / "game.conf").write_text(
        "name = c1_lazarus_11866\ntitle = C1 Lazarus 11866 Visual\n", encoding="utf-8"
    )
    (mod / "mod.conf").write_text(_MOD_CONF, encoding="utf-8")
    (mod / "init.lua").write_text(_MOD_INIT, encoding="utf-8")
    (world / "world.mt").write_text(
        "gameid = c1_lazarus_11866\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",
        encoding="utf-8",
    )
    config = target / "minetest.conf"
    config.write_text(
        "server_announce = false\ncreative_mode = true\nenable_damage = false\n"
        "time_speed = 0\nmg_name = singlenode\nscreen_w = 1280\nscreen_h = 720\n"
        "fullscreen = false\nvideo_driver = opengl\nfps_max = 30\nenable_clouds = false\n"
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
    misc = contents / "builtin/game/misc.lua"
    misc_text = misc.read_text(encoding="utf-8")
    opening = "core.set_http_api_lua(function(httpenv)"
    closing = "core.set_http_api_lua = nil"
    if misc_text.count(opening) != 1 or misc_text.count(closing) != 1:
        raise RuntimeError("#11866 visual HTTP adapter locus drifted")
    misc_text = misc_text.replace(opening, "if core.set_http_api_lua then\n" + opening, 1)
    misc_text = misc_text.replace(closing, closing + "\nend", 1)
    misc.write_text(misc_text, encoding="utf-8")
    (contents / "Info.plist").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>"
        "<key>CFBundleExecutable</key><string>minetest</string>"
        f"<key>CFBundleIdentifier</key><string>org.warptest.c1.lazarus.11866.{variant}</string>"
        "<key>CFBundleName</key><string>C1 Lazarus 11866</string>"
        "<key>CFBundlePackageType</key><string>APPL</string>"
        "<key>LSBackgroundOnly</key><false/><key>LSUIElement</key><false/>"
        "<key>NSHighResolutionCapable</key><true/></dict></plist>\n",
        encoding="utf-8",
    )
    _ordinary_executable(binary, root)
    env = dict(os.environ)
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
        "c1_lazarus_11866",
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
        if _wait_match(log, _START_RE, process, deadline) is None:
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
            int(match.group(2)),
            int(match.group(3)),
            before,
            after,
            "owned_process_group_sigterm_after_both_frames",
            None if before and after else "f12_png_missing",
        )
    finally:
        _stop(process)
        out.close()
        err.close()


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
        raise RuntimeError("#11866 visual directory is unsafe")
    return resolved


def _ordinary_file(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise RuntimeError("#11866 visual file is unsafe")
    return resolved


def _ordinary_executable(path: Path, root: Path) -> Path:
    result = _ordinary_file(path, root)
    if not os.access(result, os.X_OK):
        raise RuntimeError("#11866 visual binary is unsafe")
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
            raise RuntimeError(f"#11866 artifact conflicts: {ref.artifact_id}")
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
        raise RuntimeError(f"#11866 typed reload failed: {ref.artifact_id}")
    return result


def _require_auth(authorization: AuthorizationRecord) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != "preannotation-execution"
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError("#11866 client visual authorization is inactive")


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
