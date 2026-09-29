"""Candidate-owned curl/client visual closure for Luanti #14464."""

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


CANDIDATE = {"repository_key": "luanti", "change_number": 14464}
CONFIRMATION = "EXECUTE_LUANTI_14464_REAL_F12_VISUAL_V1"
ADMISSION_CONFIRMATION = "ADMIT_LUANTI_14464_REAL_F12_VISUAL_V1"
_PAIR = PurePosixPath("private/materialized/luanti/14464/gate8-v1")
_CLIENT_BUILD_ROOT = PurePosixPath("private/bounded-builds/luanti/14464/client-v2")
_CURL_ROOT = PurePosixPath("private/dependencies/luanti-14464-curl/8.5.0")
_RUNTIME_LIBS = PurePosixPath("private/dependencies/luanti-14464-client-runtime/1.0")
_RUN = PurePosixPath("private/bounded-runs/luanti/14464/client-visual-v2")
_ARCHIVE_FINGERPRINT = "42ab8db9e20d8290a3b633e7fbb3cec15db34df65fd1015ef8ac1e4723750eeb"
_F12_KEYCODE = 111

_GATE8 = ArtifactReference(
    "candidate_gate8_14464_native_build/luanti/14464/v1",
    "candidate_gate8_14464_native_build",
    "1.0",
)
_MATERIALIZATION = ArtifactReference(
    "candidate_materialization/luanti/14464/gate8-v1", "candidate_materialization", "1.0"
)
_DEPENDENCY = ArtifactReference(
    "candidate_gate8_14464_irrlicht_dependency/luanti/14464/current-v2/v1",
    "candidate_gate8_14464_irrlicht_dependency",
    "1.0",
)
_ORACLE = ArtifactReference(
    "candidate_gate10_14464_iconv_oracle/luanti/14464/iconv-v4",
    "candidate_gate10_14464_iconv_oracle",
    "4.0",
)
_CURL = ArtifactReference(
    "candidate_gate8_14464_curl_dependency/luanti/14464/8.5.0-v1",
    "candidate_gate8_14464_curl_dependency",
    "1.0",
)
_CLIENT_BUILD = ArtifactReference(
    "candidate_gate8_14464_client_build/luanti/14464/v1",
    "candidate_gate8_14464_client_build",
    "1.0",
)
_CONSTRUCTION = ArtifactReference(
    "candidate_gate13_14464_visual_construction/luanti/14464/real-f12-v2",
    "candidate_gate13_14464_visual_construction",
    "2.0",
)
_OBSERVATION = ArtifactReference(
    "candidate_gate11_14464_visual_observation/luanti/14464/real-f12-v2",
    "candidate_gate11_14464_visual_observation",
    "2.0",
)
_MATRIX = ArtifactReference(
    "candidate_gate12_14464_visual_matrix/luanti/14464/real-f12-v2",
    "candidate_gate12_14464_visual_matrix",
    "2.0",
)
_ADMISSION = ArtifactReference(
    "candidate_formal_freeze_admission/luanti/14464/real-f12-v2",
    "candidate_formal_freeze_admission",
    "2.0",
)

_START_RE = re.compile(r"C1_LAZARUS_14464_VISUAL_START state=(common_before)")
_AFTER_RE = re.compile(
    r"C1_LAZARUS_14464_VISUAL_AFTER state=(goal_iconv_state_reset|defect_iconv_state_regression) passed=([0-9]+) failed=([0-9]+)"
)

_COMMON = r'''local before = table.concat({
  "formspec_version[3]size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #14464  ICONV STATE REPLAY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Fresh process; TestUtilities target is registered.]",
  "label[1.0,3.95;Input bytes: C4 then isolated 81]",
  "label[1.0,4.65;Goal: conversion state does not leak across invocations.]",
  "label[1.0,5.25;Waiting for the ordered replay ...]",
})
'''


@dataclass(frozen=True)
class VisualRun:
    variant: Literal["fixed", "buggy"]
    process_outcome: str
    returncode: int | None
    state: str | None
    passed: int | None
    failed: int | None
    before: Path | None
    after: Path | None
    termination: str | None
    error: str | None


def write_client_build_receipt(
    artifact_root: str | Path, *, authorization: AuthorizationRecord
) -> tuple[StableArtifact, StableArtifact]:
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    gate8, materialization, dependency, oracle = (
        _load(root, ref) for ref in (_GATE8, _MATERIALIZATION, _DEPENDENCY, _ORACLE)
    )
    if not (
        gate8.payload.get("state") == "built"
        and materialization.payload.get("state") == "materialized"
        and dependency.payload.get("irrlicht_version") == "1.9.0.15"
        and oracle.payload.get("state") == "passed"
        and oracle.payload.get("registered_start_equal") is True
    ):
        raise RuntimeError("#14464 client-build chain drifted")
    archive = _ordinary_file(root / _CURL_ROOT / "curl-8.5.0.tar.xz", root)
    curl_lib = _ordinary_file(root / _CURL_ROOT / "build-v1/lib/libcurl.a", root)
    if _sha256(archive) != _ARCHIVE_FINGERPRINT:
        raise RuntimeError("#14464 curl archive fingerprint drifted")
    curl = _write_once(
        root,
        _CURL,
        (dependency.reference,),
        {
            "candidate": CANDIDATE,
            "state": "built",
            "outcome": "candidate_owned_curl_8_5_0_static_closure",
            "complete": True,
            "source_pin": "util/buildbot/common.sh curl_version=8.5.0",
            "archive_path": (_CURL_ROOT / "curl-8.5.0.tar.xz").as_posix(),
            "archive_fingerprint": _ARCHIVE_FINGERPRINT,
            "source_tree_path": (_CURL_ROOT / "source").as_posix(),
            "library_path": curl_lib.relative_to(root).as_posix(),
            "library_fingerprint": _sha256(curl_lib),
            "features": ["HTTP", "HTTPS", "Secure Transport", "IPv6", "libz"],
            "source_from_system": False,
            "source_from_other_candidate": False,
            "old_curl_dependency_used": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    variants: dict[str, object] = {}
    for variant in ("fixed", "buggy"):
        tree = _ordinary_dir(root / _PAIR / variant, root)
        binary = _ordinary_executable(tree / "bin/minetest", root)
        build = _ordinary_dir(root / _CLIENT_BUILD_ROOT / variant, root)
        cache = _ordinary_file(build / "CMakeCache.txt", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        ninja = _ordinary_file(build / "build.ninja", root).read_text(
            encoding="utf-8", errors="surrogateescape"
        )
        config = _ordinary_file(build / "src/cmake_config.h", root).read_text(encoding="utf-8")
        required = (
            "BUILD_CLIENT:BOOL=TRUE",
            "BUILD_SERVER:BOOL=FALSE",
            "ENABLE_CURL:BOOL=TRUE",
            f"CURL_LIBRARY:FILEPATH={curl_lib}",
            str(curl_lib),
        )
        if any(marker not in cache + ninja for marker in required):
            raise RuntimeError(f"#14464 {variant} client closure drifted")
        if "#define USE_CURL 1" not in config or "-UUSE_CURL" in ninja or "/opt/homebrew" in ninja:
            raise RuntimeError(f"#14464 {variant} client curl configuration is inadmissible")
        version = subprocess.run((str(binary), "--version"), capture_output=True, timeout=15)
        text = (version.stdout + version.stderr).decode("utf-8", errors="replace")
        if version.returncode != 0 or "USE_CURL=1" not in text:
            raise RuntimeError(f"#14464 {variant} client version preflight failed")
        links = subprocess.run(("otool", "-L", str(binary)), capture_output=True, text=True, timeout=15)
        if links.returncode != 0 or "/opt/homebrew" in links.stdout:
            raise RuntimeError(f"#14464 {variant} client runtime linkage is not closed")
        variants[variant] = {
            "binary": binary.relative_to(root).as_posix(),
            "binary_fingerprint": _sha256(binary),
            "version": text.strip(),
            "use_curl": 1,
        }
    client_build = _write_once(
        root,
        _CLIENT_BUILD,
        (gate8.reference, materialization.reference, dependency.reference, oracle.reference, curl.reference),
        {
            "candidate": CANDIDATE,
            "state": "built",
            "outcome": "candidate_owned_fixed_buggy_real_clients_built",
            "complete": True,
            "variants": variants,
            "irrlicht_version": "1.9.0.15",
            "curl_dependency": curl.reference.artifact_id,
            "build_order": ["curl", "fixed", "buggy"],
            "build_parallelism": 2,
            "runtime_dylibs_rebound_to_candidate_namespace": _RUNTIME_LIBS.as_posix(),
            "forbidden_flag_used": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": False,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    return curl, client_build


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
    oracle = _load(root, _ORACLE)
    status = require_unlocked_native_session()
    if not all(status.get(key) is True for key in ("console_unlocked", "accessibility_trusted", "event_post_access")):
        raise RuntimeError("#14464 native visual session is unavailable")
    run = root / _RUN
    if run.exists() or run.is_symlink():
        raise RuntimeError("#14464 real F12 run root already exists")
    run.mkdir(parents=True, mode=0o700)
    results = tuple(_run_variant(root, run, variant) for variant in ("fixed", "buggy"))
    expected = {
        "fixed": ("goal_iconv_state_reset", 28, 0),
        "buggy": ("defect_iconv_state_regression", 25, 3),
    }
    for item in results:
        if not (
            item.process_outcome == "completed"
            and item.returncode == 0
            and (item.state, item.passed, item.failed) == expected[item.variant]
            and item.termination == "owned_process_group_sigterm_after_both_frames"
            and item.before is not None and item.after is not None
            and _valid_png(item.before) and _valid_png(item.after)
        ):
            raise RuntimeError(f"#14464 {item.variant} visual capture failed: {item}")
    construction = _write_once(
        root,
        _CONSTRUCTION,
        (client_build.reference, oracle.reference),
        {
            "candidate": CANDIDATE,
            "state": "passed",
            "outcome": "same_lineage_real_client_construction_passed",
            "complete": True,
            "client_build": client_build.reference.artifact_id,
            "semantic_oracle": oracle.reference.artifact_id,
            "run_root": _RUN.as_posix(),
            "same_source_materialization": _PAIR.as_posix(),
            "same_curl_dependency": client_build.payload["curl_dependency"],
            "production_source_modified_for_visual": False,
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    obs: dict[str, object] = {}
    for item in results:
        assert item.before and item.after
        obs[item.variant] = {
            "semantic_state": item.state,
            "passed_tests": item.passed,
            "failed_tests": item.failed,
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
            "outcome": "real_f12_iconv_oracle_frames_captured",
            "complete": True,
            "capture_backend": "minetest_builtin_F12_OpenGL_PNG",
            "observations": obs,
            "common_before_semantic_equal": True,
            "fixed_after_goal": True,
            "buggy_after_exact_three_defects": True,
            "synthetic_frames": False,
            "old_attempts_loaded": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
        },
        authorization,
    )
    fixed = obs["fixed"]; buggy = obs["buggy"]
    assert isinstance(fixed, dict) and isinstance(buggy, dict)
    before_diff = _difference(root / PurePosixPath(str(fixed["common_before"])), root / PurePosixPath(str(buggy["common_before"])))
    after_diff = _difference(root / PurePosixPath(str(fixed["after"])), root / PurePosixPath(str(buggy["after"])))
    matrix = _write_once(
        root,
        _MATRIX,
        (construction.reference, observation.reference),
        {
            "candidate": CANDIDATE,
            "state": "captured_pending_direct_visual_judgment",
            "outcome": "real_f12_2x2_matrix_captured",
            "complete": True,
            "before_difference_mean": before_diff,
            "before_equivalent": before_diff <= 1.0,
            "after_difference_mean": after_diff,
            "after_fixed_buggy_different": after_diff > 1.0,
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
    *, authorization: AuthorizationRecord, confirmation: str, judgment: str,
) -> StableArtifact:
    if confirmation != ADMISSION_CONFIRMATION:
        raise RuntimeError(f"explicit confirmation must be {ADMISSION_CONFIRMATION!r}")
    if judgment != "before_equivalent_after_28_pass_vs_exact3_fail_legible":
        raise RuntimeError("#14464 visual judgment is not affirmative")
    _require_auth(authorization)
    root = Path(artifact_root).absolute().resolve(strict=True)
    client, construction, observation, matrix, oracle = (
        _load(root, ref) for ref in (_CLIENT_BUILD, _CONSTRUCTION, _OBSERVATION, _MATRIX, _ORACLE)
    )
    if not (
        client.payload.get("state") == "built"
        and construction.payload.get("state") == "passed"
        and observation.payload.get("state") == "passed"
        and matrix.payload.get("before_equivalent") is True
        and matrix.payload.get("after_fixed_buggy_different") is True
        and oracle.payload.get("no_timeout_or_unknown") is True
    ):
        raise RuntimeError("#14464 admission inputs are incomplete")
    return _write_once(
        root, _ADMISSION,
        (client.reference, construction.reference, observation.reference, matrix.reference, oracle.reference),
        {
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
            "forbidden_flag_used": False,
            "old_attempts_loaded": False,
            "synthetic_frames": False,
            "candidate_execution_performed": True,
            "paid_actions_authorized": False,
            "formal_trials_authorized": False,
            "catalog_admission_ready": True,
        }, authorization,
    )


def _variant_mod(variant: str) -> str:
    if variant == "fixed":
        state, passed, failed = "goal_iconv_state_reset", 28, 0
        color, accent = "#064E3B", "#10B981"
        heading = "GOAL: 28 / 28 UTILITIES PASS"
        lines = ["testUTF8: PASS", "testWrapRows: PASS", "testSanitizeDirName: PASS"]
    else:
        state, passed, failed = "defect_iconv_state_regression", 25, 3
        color, accent = "#7F1D1D", "#EF4444"
        heading = "DEFECT: EXACT 3 / 28 FAIL"
        lines = ["testUTF8: FAIL", "testWrapRows: FAIL", "testSanitizeDirName: FAIL"]
    return f'''local before = table.concat({{
  "formspec_version[3]size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #14464  ICONV STATE REPLAY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Fresh process, TestUtilities 28-test module registered.]",
  "label[1.0,3.95;Input bytes: C4 then isolated 81]",
  "label[1.0,4.65;Goal: conversion state does not leak across invocations.]",
  "label[1.0,5.25;Waiting for the ordered replay ...]",
}})
minetest.register_on_joinplayer(function(player)
  player:set_pos({{x=0,y=3,z=6}}); player:set_look_horizontal(0); player:set_look_vertical(0)
  player:set_sky({{r=30,g=38,b=50}}, "plain", {{}}, false); player:set_clouds({{density=0}}); minetest.set_timeofday(0.5)
  minetest.show_formspec(player:get_player_name(), "c1:14464", before)
  minetest.log("action", "C1_LAZARUS_14464_VISUAL_START state=common_before")
  minetest.after(6.0, function()
    local p=minetest.get_player_by_name(player:get_player_name()); if not p then return end
    local form=table.concat({{
      "formspec_version[3]size[12,7]bgcolor[{color};true]",
      "box[0.4,0.4;11.2,1.1;{accent}]", "label[0.8,0.85;C1 LAZARUS  #14464  REPLAY RESULT]",
      "box[0.7,2.0;10.6,3.7;#0F172A]", "label[1.0,2.55;{heading}]",
      "label[1.0,3.25;{lines[0]}]", "label[1.0,3.85;{lines[1]}]", "label[1.0,4.45;{lines[2]}]",
      "label[1.0,5.15;semantic class: {state}]",
    }})
    minetest.show_formspec(p:get_player_name(), "c1:14464", form)
    minetest.log("action", "C1_LAZARUS_14464_VISUAL_AFTER state={state} passed={passed} failed={failed}")
  end)
end)
'''


@dataclass(frozen=True)
class VisualRun:
    variant: Literal["fixed", "buggy"]
    process_outcome: str
    returncode: int | None
    state: str | None
    passed: int | None
    failed: int | None
    before: Path | None
    after: Path | None
    termination: str | None
    error: str | None


def _run_variant(root: Path, run: Path, variant: Literal["fixed", "buggy"]) -> VisualRun:
    target=run/variant; world=target/"world"; mod=world/"worldmods/c1"; game=target/"games/c1_lazarus_14464"; frames=target/"frames"; home=target/"home"
    for path in (mod,game,frames,home): path.mkdir(parents=True,mode=0o700)
    (game/"game.conf").write_text("name = c1_lazarus_14464\ntitle = C1 Lazarus 14464 Visual\n",encoding="utf-8")
    (mod/"mod.conf").write_text("name = c1\n",encoding="utf-8"); (mod/"init.lua").write_text(_variant_mod(variant),encoding="utf-8")
    (world/"world.mt").write_text("gameid = c1_lazarus_14464\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n",encoding="utf-8")
    config=target/"minetest.conf"; config.write_text("server_announce = false\ncreative_mode = true\nenable_damage = false\ntime_speed = 0\nmg_name = singlenode\nscreen_w = 1280\nscreen_h = 720\nfullscreen = false\nvideo_driver = opengl\nfps_max = 30\nenable_clouds = false\n"+f"screenshot_path = {frames}\nscreenshot_format = png\n",encoding="utf-8")
    log=target/"minetest.log"; source=_ordinary_dir(root/_PAIR/variant,root); source_binary=_ordinary_executable(source/"bin/minetest",root)
    app=target/"minetest.app"; contents=app/"Contents"; macos=contents/"MacOS"; macos.mkdir(parents=True,mode=0o700); binary=macos/"minetest"; shutil.copy2(source_binary,binary)
    for resource in ("builtin","client","fonts","textures"): shutil.copytree(source/resource,contents/resource,symlinks=False)
    (contents/"Info.plist").write_text("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>minetest</string><key>CFBundleIdentifier</key><string>org.warptest.c1.lazarus.14464."+variant+"</string><key>CFBundleName</key><string>C1 Lazarus 14464</string><key>CFBundlePackageType</key><string>APPL</string><key>LSBackgroundOnly</key><false/><key>LSUIElement</key><false/><key>NSHighResolutionCapable</key><true/></dict></plist>\n",encoding="utf-8")
    env=dict(os.environ); env.update({"HOME":str(home),"LANG":"C","LC_ALL":"C","MINETEST_SUBGAME_PATH":str(target/"games")})
    argv=(str(binary),"--go","--gameid","c1_lazarus_14464","--world",str(world),"--name","c1","--config",str(config),"--logfile",str(log))
    out=(target/"stdout").open("wb"); err=(target/"stderr").open("wb")
    try: process=subprocess.Popen(argv,cwd=target,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
    except OSError as e: out.close();err.close();return VisualRun(variant,"launch_failed",None,None,None,None,None,None,None,str(e))
    try:
        deadline=time.monotonic()+75; start=_wait(log,_START_RE,process,deadline)
        if start is None:return VisualRun(variant,"timed_out",process.poll(),None,None,None,None,None,None,"start_timeout")
        time.sleep(.6); before=_capture(frames,process.pid,set()); after_match=_wait(log,_AFTER_RE,process,deadline)
        if after_match is None:return VisualRun(variant,"timed_out",process.poll(),None,None,None,before,None,None,"after_timeout")
        time.sleep(.4); after=_capture(frames,process.pid,{before} if before else set()); _stop(process)
        return VisualRun(variant,"completed",process.returncode,after_match.group(1),int(after_match.group(2)),int(after_match.group(3)),before,after,"owned_process_group_sigterm_after_both_frames",None if before and after else "f12_missing")
    finally:_stop(process);out.close();err.close()


def _wait(log:Path,pattern:re.Pattern[str],process:subprocess.Popen[bytes],deadline:float):
    while time.monotonic()<deadline:
        if log.is_file():
            m=pattern.search(log.read_text(encoding="utf-8",errors="replace"))
            if m:return m
        if process.poll() is not None:return None
        time.sleep(.05)
    return None


def _capture(frames:Path,pid:int,known:set[Path]):
    native=QuartzNativeInput(pid);native.focus();native._press_key(_F12_KEYCODE);deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        found=[p for p in frames.glob("*.png") if p not in known and _valid_png(p)]
        if found:return max(found,key=lambda p:p.stat().st_mtime_ns)
        time.sleep(.05)
    return None


def _stop(process):
    if process.poll() is None:
        try:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=5)
        except (ProcessLookupError,subprocess.TimeoutExpired):
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)


def _difference(a,b):
    with Image.open(a).convert("RGB") as x,Image.open(b).convert("RGB") as y:return float(sum(ImageStat.Stat(ImageChops.difference(x,y)).mean)/3) if x.size==y.size else float("inf")
def _valid_png(p):
    try:
        with Image.open(p) as i:i.load();return i.format=="PNG" and i.width>0 and i.height>0
    except (OSError,SyntaxError,ValueError):return False
def _dimensions(p):
    with Image.open(p) as i:return int(i.width),int(i.height)
def _sha256(p):
    d=hashlib.sha256()
    with p.open("rb") as h:
        for c in iter(lambda:h.read(1048576),b""):d.update(c)
    return d.hexdigest()
def _ordinary_dir(p,r):
    x=p.resolve(strict=True);x.relative_to(r)
    if p.is_symlink() or not x.is_dir():raise RuntimeError("#14464 visual directory unsafe")
    return x
def _ordinary_file(p,r):
    x=p.resolve(strict=True);x.relative_to(r)
    if p.is_symlink() or not x.is_file():raise RuntimeError("#14464 visual file unsafe")
    return x
def _ordinary_executable(p,r):
    x=_ordinary_file(p,r)
    if not os.access(x,os.X_OK):raise RuntimeError("#14464 visual binary unsafe")
    return x
def _load(root,ref):return load_stable_artifact(root,artifact_id=ref.artifact_id,artifact_kind=ref.artifact_kind,semantic_version=ref.semantic_version)
def _write_once(root,ref,inputs,payload,authorization):
    try:existing=_load(root,ref)
    except RuntimeError as e:
        if str(e) not in {"artifact directory is missing","artifact current head is missing"}:raise
    else:
        if existing.inputs!=inputs or dict(existing.payload)!=payload:raise RuntimeError(f"#14464 artifact conflicts: {ref.artifact_id}")
        return existing
    write_stable_artifact(root,artifact_id=ref.artifact_id,artifact_kind=ref.artifact_kind,semantic_version=ref.semantic_version,created_at=_now(),inputs=inputs,payload=payload,authorization=authorization)
    return _load(root,ref)
def _require_auth(a):
    if type(a) is not AuthorizationRecord or a.scope!="preannotation-execution" or a.revoked or a.local_user_id!=os.getuid():raise PermissionError("#14464 client visual authorization inactive")
def _now():return (datetime.now(UTC)+timedelta(microseconds=1)).isoformat(timespec="microseconds").replace("+00:00","Z")

__all__=["CONFIRMATION","ADMISSION_CONFIRMATION","write_client_build_receipt","execute_real_f12_visual","admit_after_direct_visual_judgment"]
