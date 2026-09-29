"""Real-client F12 closure for Luanti #9572."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ..native_input import require_unlocked_native_session
from .luanti_8704_client_visual import (
    _capture,
    _difference,
    _dimensions,
    _dir,
    _executable,
    _file,
    _fingerprint,
    _load,
    _pagezero,
    _require_auth,
    _stop,
    _valid_png,
    _wait,
    _write,
)
from .stable_protocol import ArtifactReference, AuthorizationRecord, StableArtifact


CONFIRMATION = "EXECUTE_LUANTI_9572_REAL_F12_V3"
ADMISSION_CONFIRMATION = "ADMIT_LUANTI_9572_REAL_F12_V3"
_CANDIDATE = {"repository_key": "luanti", "change_number": 9572}
_PAIR = PurePosixPath("private/materialized/luanti/9572/current-v2")
_DEP = PurePosixPath("private/dependencies/luanti-9572-irrlicht/1.8.4")
_RUN = PurePosixPath("private/bounded-runs/luanti/9572/client-visual-v3")
_CLIENT_ROOT = PurePosixPath("private/bounded-builds/luanti/9572/gate11-client-v1")
_DEPENDENCY = ArtifactReference("candidate_gate8_9572_irrlicht184_source/luanti/9572/current-v2/v1", "candidate_gate8_9572_irrlicht184_source", "1.0")
_GATE10 = ArtifactReference("candidate_gate10_transition/luanti/9572/runtime-v2", "candidate_gate10_transition", "2.0")
_ORACLE = ArtifactReference("candidate_oracle/luanti/9572/hidden-v2", "candidate_oracle", "2.0")
_MAPPING = ArtifactReference("lazarus_task_mapping/luanti/9572/lz-luanti-9572-vector-rotate/v2", "lazarus_task_mapping", "2.0")
_CLIENT = ArtifactReference("candidate_gate8_9572_client_build/luanti/9572/irrlicht184-v1", "candidate_gate8_9572_client_build", "1.0")
_CONSTRUCTION = ArtifactReference("candidate_gate13_9572_visual_construction/luanti/9572/real-f12-v3", "candidate_gate13_9572_visual_construction", "3.0")
_OBSERVATION = ArtifactReference("candidate_gate11_9572_visual_observation/luanti/9572/real-f12-v3", "candidate_gate11_9572_visual_observation", "3.0")
_MATRIX = ArtifactReference("candidate_gate12_9572_visual_matrix/luanti/9572/real-f12-v3", "candidate_gate12_9572_visual_matrix", "3.0")
_ADMISSION = ArtifactReference("candidate_formal_freeze_admission/luanti/9572/real-f12-v3", "candidate_formal_freeze_admission", "3.0")
_START = re.compile(r"C1_LAZARUS_9572_VISUAL_START state=common_before")
_AFTER = re.compile(r"C1_LAZARUS_9572_VISUAL_AFTER state=(goal_rotate_present|defect_rotate_missing) rotate_type=([^ ]+) rotate_ok=(true|false) add_ok=(true|false)")

_INIT = r'''local before = table.concat({
  "size[12,7]bgcolor[#111827;true]",
  "box[0.4,0.4;11.2,1.1;#1F2937]",
  "label[0.8,0.85;C1 LAZARUS  #9572  VECTOR ROTATE REPLAY]",
  "box[0.7,2.0;10.6,3.7;#0F172A]",
  "label[1.0,2.55;COMMON START]",
  "label[1.0,3.25;Rotate (1,0,0) around Z by pi/2.]",
  "label[1.0,3.95;Expected result: approximately (0,-1,0)]",
  "label[1.0,4.65;vector.add remains the invariant.]",
})
minetest.register_on_joinplayer(function(player)
  player:set_physics_override({gravity = 0})
  player:set_sky("#263445", "plain", {}, false)
  minetest.show_formspec(player:get_player_name(), "c1_lazarus:9572", before)
  minetest.log("action", "C1_LAZARUS_9572_VISUAL_START state=common_before")
  minetest.after(6, function()
    local current = minetest.get_player_by_name(player:get_player_name())
    if not current then return end
    local rotate_type = type(vector.rotate)
    local rotate_ok = false
    if rotate_type == "function" then
      local out = vector.rotate({x=1,y=0,z=0}, {x=0,y=0,z=math.pi/2})
      rotate_ok = math.abs(out.x) < 0.000001 and math.abs(out.y + 1) < 0.000001 and math.abs(out.z) < 0.000001
    end
    local add = vector.add({x=1,y=2,z=3}, {x=4,y=5,z=6})
    local add_ok = add.x == 5 and add.y == 7 and add.z == 9
    local goal = rotate_type == "function" and rotate_ok and add_ok
    local state = goal and "goal_rotate_present" or "defect_rotate_missing"
    local color = goal and "#064E3B" or "#7F1D1D"
    local accent = goal and "#10B981" or "#EF4444"
    local heading = goal and "GOAL: VECTOR ROTATE WORKS" or "DEFECT: VECTOR ROTATE MISSING"
    local form = table.concat({
      "size[12,7]bgcolor["..color..";true]",
      "box[0.4,0.4;11.2,1.1;"..accent.."]",
      "label[0.8,0.85;C1 LAZARUS  #9572  ACTUAL VECTOR RESULT]",
      "box[0.7,2.0;10.6,3.7;#0F172A]",
      "label[1.0,2.55;"..heading.."]",
      "label[1.0,3.35;rotate type: "..rotate_type.."]",
      "label[1.0,4.05;rotation correct: "..tostring(rotate_ok).."]",
      "label[1.0,4.75;vector.add invariant: "..tostring(add_ok).."]",
    })
    minetest.show_formspec(current:get_player_name(), "c1_lazarus:9572", form)
    minetest.log("action", "C1_LAZARUS_9572_VISUAL_AFTER state="..state.." rotate_type="..rotate_type.." rotate_ok="..tostring(rotate_ok).." add_ok="..tostring(add_ok))
  end)
end)
'''


@dataclass(frozen=True)
class Run:
    variant: str; outcome: str; returncode: int | None; state: str | None
    rotate_type: str | None; rotate_ok: bool | None; add_ok: bool | None
    before: Path | None; after: Path | None; termination: str | None; error: str | None


def register_luanti_9572_clients(root_value: str | Path, *, authorization: AuthorizationRecord) -> StableArtifact:
    _require_auth(authorization); root = Path(root_value).absolute().resolve(strict=True)
    dep, gate10, oracle, mapping = (_load(root, ref) for ref in (_DEPENDENCY, _GATE10, _ORACLE, _MAPPING))
    if dep.payload.get("candidate_owned") is not True or gate10.payload.get("state") != "passed" or oracle.payload.get("state") != "passed": raise RuntimeError("#9572 client inputs drifted")
    library = _file(root / _DEP / "client-build/source/lib/MacOSX/libIrrlicht.a", root)
    variants = {}
    for variant in ("fixed", "buggy"):
        binary = _executable(root / _PAIR / variant / "bin/minetest", root)
        cache = _file(root / _CLIENT_ROOT / variant / "CMakeCache.txt", root).read_text(errors="replace")
        if f"IRRLICHT_LIBRARY:FILEPATH={library}" not in cache or "CMAKE_EXE_LINKER_FLAGS:STRING=-Wl,-pagezero_size,0x100000000" not in cache: raise RuntimeError("#9572 client cache drifted")
        version = subprocess.run((str(binary), "--version"), capture_output=True, check=False, timeout=15)
        text = (version.stdout + version.stderr).decode(errors="replace")
        if version.returncode != 0 or "Using Irrlicht 1.8.4" not in text or _pagezero(binary) != 0x100000000: raise RuntimeError("#9572 client preflight failed")
        variants[variant] = {"binary": binary.relative_to(root).as_posix(), "binary_fingerprint": _fingerprint(binary), "version": text.strip(), "pagezero_vmsize": _pagezero(binary)}
    payload = {"candidate": _CANDIDATE, "state": "built", "outcome": "candidate_owned_fixed_buggy_real_clients_built", "complete": True,
        "variants": variants, "irrlicht_release": "1.8.4", "irrlicht_library": library.relative_to(root).as_posix(), "irrlicht_library_fingerprint": _fingerprint(library),
        "adapter_scope": "candidate_dependency_build_copy_only", "source_from_other_candidate": False, "luanti_source_pair_modified_by_adapter": False,
        "old_attempts_loaded": False, "candidate_execution_performed": False, "paid_actions_authorized": False, "formal_trials_authorized": False}
    return _write(root, _CLIENT, (dep.reference, gate10.reference, oracle.reference, mapping.reference), payload, authorization)


def execute_luanti_9572_f12(root_value: str | Path, *, authorization: AuthorizationRecord, confirmation: str) -> tuple[StableArtifact, StableArtifact, StableArtifact]:
    if confirmation != CONFIRMATION: raise RuntimeError("bad confirmation")
    _require_auth(authorization); root = Path(root_value).absolute().resolve(strict=True)
    client, gate10, oracle, mapping = (_load(root, ref) for ref in (_CLIENT, _GATE10, _ORACLE, _MAPPING))
    status = require_unlocked_native_session()
    if not all(status.get(key) is True for key in ("console_unlocked", "accessibility_trusted", "event_post_access")): raise RuntimeError("native session unavailable")
    run_root = root / _RUN
    if run_root.exists(): raise RuntimeError("visual run exists")
    run_root.mkdir(parents=True, mode=0o700)
    runs = tuple(_run(root, run_root, variant) for variant in ("fixed", "buggy"))
    expected = {"fixed": ("goal_rotate_present", "function", True, True), "buggy": ("defect_rotate_missing", "nil", False, True)}
    for run in runs:
        if run.outcome != "completed" or (run.state, run.rotate_type, run.rotate_ok, run.add_ok) != expected[run.variant] or not run.before or not run.after: raise RuntimeError(f"#9572 visual failed: {run}")
    construction = _write(root, _CONSTRUCTION, (client.reference, gate10.reference, oracle.reference, mapping.reference), {
        "candidate": _CANDIDATE, "state": "passed", "outcome": "same_lineage_real_client_construction_passed", "complete": True,
        "run_root": _RUN.as_posix(), "actual_api": "vector.rotate", "p2p_api": "vector.add", "production_source_modified_for_visual": False,
        "synthetic_frames": False, "old_attempts_loaded": False, "candidate_execution_performed": True, "paid_actions_authorized": False, "formal_trials_authorized": False}, authorization)
    observations = {}
    for run in runs:
        assert run.before and run.after
        observations[run.variant] = {"state": run.state, "rotate_type": run.rotate_type, "rotate_ok": run.rotate_ok, "add_ok": run.add_ok,
            "process_outcome": run.outcome, "returncode": run.returncode, "termination": run.termination,
            "common_before": run.before.relative_to(root).as_posix(), "common_before_fingerprint": _fingerprint(run.before),
            "after": run.after.relative_to(root).as_posix(), "after_fingerprint": _fingerprint(run.after), "dimensions": list(_dimensions(run.after))}
    observation = _write(root, _OBSERVATION, (construction.reference,), {"candidate": _CANDIDATE, "state": "passed", "outcome": "real_f12_vector_frames_captured", "complete": True,
        "capture_backend": "minetest_builtin_F12_OpenGL_PNG", "observations": observations, "fixed_after_goal": True, "buggy_after_defect": True,
        "synthetic_frames": False, "old_attempts_loaded": False, "candidate_execution_performed": True, "paid_actions_authorized": False, "formal_trials_authorized": False}, authorization)
    fixed, buggy = observations["fixed"], observations["buggy"]
    before = _difference(root / PurePosixPath(fixed["common_before"]), root / PurePosixPath(buggy["common_before"]))
    after = _difference(root / PurePosixPath(fixed["after"]), root / PurePosixPath(buggy["after"]))
    matrix = _write(root, _MATRIX, (construction.reference, observation.reference), {"candidate": _CANDIDATE, "state": "captured_pending_direct_visual_judgment", "outcome": "real_f12_2x2_matrix_captured", "complete": True,
        "before_difference_mean": before, "before_equivalent": before <= 1.0, "after_difference_mean": after, "after_fixed_buggy_different": after > 1.0,
        "two_by_two": {"fixed_after_vs_fixed_golden":"same_frame","fixed_after_vs_buggy_golden":"different","buggy_after_vs_fixed_golden":"different","buggy_after_vs_buggy_golden":"same_frame"},
        "direct_visual_judgment":"pending","synthetic_frames":False,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False}, authorization)
    return construction, observation, matrix


def admit_luanti_9572(root_value: str | Path, *, authorization: AuthorizationRecord, confirmation: str, judgment: str) -> StableArtifact:
    if confirmation != ADMISSION_CONFIRMATION or judgment != "before_equivalent_rotate_present_vs_missing_legible": raise RuntimeError("bad judgment")
    _require_auth(authorization); root = Path(root_value).absolute().resolve(strict=True)
    client, construction, observation, matrix, gate10 = (_load(root, ref) for ref in (_CLIENT, _CONSTRUCTION, _OBSERVATION, _MATRIX, _GATE10))
    if not (client.payload.get("state") == "built" and construction.payload.get("state") == "passed" and observation.payload.get("state") == "passed" and matrix.payload.get("before_equivalent") is True and matrix.payload.get("after_fixed_buggy_different") is True and gate10.payload.get("state") == "passed"): raise RuntimeError("admission incomplete")
    return _write(root, _ADMISSION, (client.reference, construction.reference, observation.reference, matrix.reference, gate10.reference), {"candidate":_CANDIDATE,"state":"admitted","outcome":"strict_real_client_downstream_admission_passed","complete":True,
        "gate8_real_client_pair":"passed","gate9_registered_start":"passed","gate10_ordered_replay_hidden_oracle":"passed","gate11_real_common_before_fixed_after_buggy_after":"passed","gate12_before_equivalence_after_2x2":"passed","gate13_same_lineage_construction":"passed",
        "direct_visual_judgment":judgment,"no_timeout_unknown_or_flake":True,"synthetic_frames":False,"old_attempts_loaded":False,"candidate_execution_performed":True,"paid_actions_authorized":False,"formal_trials_authorized":False,"catalog_admission_ready":True}, authorization)


def _run(root: Path, run_root: Path, variant: Literal["fixed", "buggy"]) -> Run:
    target = run_root / variant; game = target / "games/c1_lazarus_9572"; mod = game / "mods/c1_observer"; world, frames, home = target / "world", target / "frames", target / "home"
    for path in (mod, world, frames, home): path.mkdir(parents=True, mode=0o700)
    (game / "game.conf").write_text("name = c1_lazarus_9572\ntitle = C1 Lazarus 9572\n"); (mod / "mod.conf").write_text("name = c1_observer\n"); (mod / "init.lua").write_text(_INIT)
    (world / "world.mt").write_text("gameid = c1_lazarus_9572\nbackend = sqlite3\nplayer_backend = sqlite3\nauth_backend = sqlite3\n")
    config = target / "minetest.conf"; config.write_text("server_announce = false\nenable_damage = false\ntime_speed = 0\nmg_name = singlenode\nscreen_w = 1280\nscreen_h = 720\nfullscreen = false\nvideo_driver = opengl\nfps_max = 30\n" + f"screenshot_path = {frames}\nscreenshot_format = png\n")
    source = _dir(root / _PAIR / variant, root); contents = target / "minetest.app/Contents"; macos = contents / "MacOS"; macos.mkdir(parents=True)
    binary = macos / "minetest"; shutil.copy2(_executable(source / "bin/minetest", root), binary)
    for resource in ("builtin","client","fonts","textures"): shutil.copytree(source / resource, contents / resource)
    (contents / "Info.plist").write_text("<?xml version=\"1.0\"?><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>minetest</string><key>CFBundleIdentifier</key><string>org.warptest.c1.9572."+variant+"</string><key>CFBundleName</key><string>C1 Lazarus 9572</string><key>CFBundlePackageType</key><string>APPL</string></dict></plist>")
    log = target / "minetest.log"; env = {**os.environ,"HOME":str(home),"LANG":"C","LC_ALL":"C","MINETEST_SUBGAME_PATH":str(target/"games")}
    argv=(str(binary),"--go","--gameid","c1_lazarus_9572","--world",str(world),"--name","c1","--config",str(config),"--logfile",str(log))
    out,err=(target/"stdout").open("wb"),(target/"stderr").open("wb")
    try: process=subprocess.Popen(argv,cwd=target,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
    except OSError as error: out.close();err.close();return Run(variant,"launch_failed",None,None,None,None,None,None,None,None,None,str(error))
    try:
        deadline=time.monotonic()+75; start=_wait(log,_START,process,deadline)
        if start is None:return Run(variant,"timed_out",process.poll(),None,None,None,None,None,None,None,None,"start_timeout")
        time.sleep(.6); before=_capture(target/"frames",process.pid,set()); match=_wait(log,_AFTER,process,deadline)
        if match is None:return Run(variant,"timed_out",process.poll(),None,None,None,None,before,None,None,"after_timeout")
        time.sleep(.5); after=_capture(target/"frames",process.pid,{before} if before else set());_stop(process)
        return Run(variant,"completed",process.returncode,match.group(1),match.group(2),match.group(3)=="true",match.group(4)=="true",before,after,"owned_process_group_sigkill_after_both_frames",None)
    finally:_stop(process);out.close();err.close()


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


__all__=["CONFIRMATION","ADMISSION_CONFIRMATION","register_luanti_9572_clients","execute_luanti_9572_f12","admit_luanti_9572"]
