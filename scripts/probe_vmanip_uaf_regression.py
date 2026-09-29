#!/usr/bin/env python3
"""Standalone two-commit regression probe for luanti-org/luanti PR #17047.

PR #17047 "Fix LuaVoxelManip UaF if it outlives vm": pre-fix, a LuaVoxelManip that
keeps a reference to the mapgen voxelmanip and is used AFTER the underlying MMVManip
is freed dereferences a dangling pointer (use-after-free). Post-fix, the MMVManip
nulls the tracked ref on destruction and ``checkObjectValid`` raises a clean Lua
error ("vm is null") instead of crashing.

This probe reproduces that from a headless dedicated server via a custom worldmod:
it forces map generation (``core.emerge_area``), stashes the mapgen voxelmanip in
``register_on_generated``, then uses it a few seconds later (after the mapgen vm is
gone). It runs the fixture on the PRE-fix and POST-fix ``luantiserver`` binaries and
emits the five-state verdict (Contract 3), NEVER a bare boolean.

It is deliberately standalone: it does NOT modify ``warptest/luanti_replay.py`` (only
reads ``LuantiUpstreamBuilder`` for the per-SHA worktree) and imports the shared
contracts (``DefectOracle`` / ``ReplayObservation`` / ``classify_replay``).

Usage:
    python scripts/probe_vmanip_uaf_regression.py
    python scripts/probe_vmanip_uaf_regression.py --pre-bin ... --post-bin ...
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.luanti_replay import LuantiUpstreamBuilder  # noqa: E402 (read-only use)
from warptest.upstream_contracts import (  # noqa: E402
    DefectOracle,
    ReplayObservation,
    classify_replay,
)
from warptest.upstream_sources import get_source  # noqa: E402

PRE_SHA = "e6f0377ea2cb60c0c267b9ea570c1b4cc059d5b7"
POST_SHA = "bcf07de10d32f8369e52bf4ef974c069e15602b7"
PR_NUMBER = 17047

# Custom worldmod: force generation, stash the mapgen voxelmanip, use it after the
# mapgen vm is freed. pre-fix -> UaF (crash / no result); post-fix -> caught
# "vm is null" Lua error -> clean shutdown with a result file.
_PROBE_INIT_LUA = r'''
local worldpath = core.get_worldpath()
local result_path = worldpath .. "/probe-result.json"
local stashed = nil

local function finish(status, detail)
	local f = io.open(result_path, "w")
	if f then
		f:write(core.write_json({ status = status, detail = tostring(detail) }) or '{"status":"failure"}')
		f:close()
	end
	core.request_shutdown("", false, 0)
end

core.register_on_generated(function(minp, maxp, seed)
	if not stashed then
		-- The mapgen voxelmanip is only valid during generation; stashing it and
		-- using it later is exactly the lifetime bug PR #17047 fixes.
		stashed = core.get_mapgen_object("voxelmanip")
	end
end)

core.register_on_mods_loaded(function()
	-- Force generation so register_on_generated fires even with no player online.
	core.after(0.5, function()
		pcall(function() core.emerge_area({ x = 0, y = 0, z = 0 }, { x = 79, y = 79, z = 79 }) end)
	end)
	-- Use the stashed vm well after the mapgen vm should have been destroyed.
	core.after(6.0, function()
		if not stashed then
			finish("no_capture", "on_generated never fired / no voxelmanip captured")
			return
		end
		local ok, err = pcall(function()
			-- Touch the underlying vm: pre-fix derefs a freed MMVManip (UaF);
			-- post-fix raises a clean "vm is null" error caught here.
			stashed:get_emerged_area()
			stashed:get_data()
		end)
		if ok then
			finish("used_ok", "stashed vm still usable (no crash, no error)")
		else
			finish("caught_error", err)
		end
	end)
end)
'''

_MOD_CONF = "name = probe\ndescription = vmanip UaF two-commit regression probe.\n"
_WORLD_MT = "\n".join([
    "gameid = devtest", "backend = dummy", "player_backend = sqlite3",
    "auth_backend = sqlite3", "mod_storage_backend = sqlite3", "world_name = probe",
    "creative_mode = false", "enable_damage = true", "load_mod_probe = true", "",
])
_CONF = "\n".join([
    "name = probe", "mg_name = flat", "mgflat_ground_level = 0", "enable_sound = false",
    "sound_volume = 0", "debug_log_level = error", "debug_log_size_max = 5",
    "secure.enable_security = false", "secure.trusted_mods = probe", "",
])


def _write_world(dest: Path) -> Path:
    world = dest / "world"
    mod = world / "worldmods" / "probe"
    mod.mkdir(parents=True, exist_ok=True)
    (world / "world.mt").write_text(_WORLD_MT, encoding="utf-8")
    (mod / "init.lua").write_text(_PROBE_INIT_LUA, encoding="utf-8")
    (mod / "mod.conf").write_text(_MOD_CONF, encoding="utf-8")
    (dest / "minetest.conf").write_text(_CONF, encoding="utf-8")
    return world


def _asan_env() -> dict[str, str]:
    import os
    env = dict(os.environ)
    # UaF -> immediate abort so it is a deterministic, observable defect.
    env["ASAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1:detect_leaks=0:exitcode=134"
    return env


def _build_asan(builder: "LuantiUpstreamBuilder", sha: str, *, timeout_s: int = 3600) -> tuple[Path | None, str]:
    """Build an ASan-instrumented RUN_IN_PLACE server for ``sha`` (no install step).

    Overwrites the worktree's ``bin/luantiserver`` with the instrumented build. Does
    NOT touch warptest/luanti_replay.py -- it drives cmake directly here.
    """
    import os
    import platform as _pf
    from warptest.luanti_replay import _server_target, _with_homebrew_prefix

    wt = builder.worktree_for(sha)
    marker = wt / ".asan_built"
    binary = wt / "bin" / (_server_target(wt))
    if marker.exists() and binary.exists():
        return binary, "asan cache hit"
    bd = builder.builds / f"asan-{sha}" / "build"
    bd.mkdir(parents=True, exist_ok=True)
    cxx = "-fsanitize=address -fno-omit-frame-pointer -g -DLIBICONV_PLUG"
    ld = "-fsanitize=address"
    if _pf.system() == "Darwin":
        ld += " -lbz2"
    configure = [
        "cmake", "-S", str(wt), "-B", str(bd),
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DRUN_IN_PLACE=TRUE",
        "-DBUILD_CLIENT=FALSE", "-DBUILD_SERVER=TRUE", "-DBUILD_UNITTESTS=FALSE",
        "-DENABLE_LEVELDB=OFF", "-DENABLE_REDIS=OFF", "-DENABLE_POSTGRESQL=OFF",
        f"-DCMAKE_CXX_FLAGS={cxx}", "-DCMAKE_C_FLAGS=-fsanitize=address -g",
        f"-DCMAKE_EXE_LINKER_FLAGS={ld}",
    ]
    if _pf.system() == "Darwin":
        configure += ["-DCMAKE_FIND_FRAMEWORK=LAST", "-DENABLE_GETTEXT=TRUE"]
    env = _with_homebrew_prefix(dict(os.environ)) if _pf.system() == "Darwin" else dict(os.environ)
    build = ["cmake", "--build", str(bd), "--parallel", str(os.cpu_count() or 2), "--target", _server_target(wt)]
    log: list[str] = []
    for cmd in (configure, build):
        proc = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, timeout=timeout_s, env=env)
        log.append(f"$ {' '.join(cmd)}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
        if proc.returncode != 0 and not binary.exists():
            return None, "\n".join(log)
    if not binary.exists():
        return None, "\n".join(log) + "\nno server binary produced"
    marker.write_text("asan\n", encoding="utf-8")
    return binary, "asan built"


def _run(binary: Path, work: Path, *, timeout_s: float = 40.0, env: dict | None = None) -> ReplayObservation:
    import shutil
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    world = _write_world(work)
    result_path = world / "probe-result.json"
    log_path = work / "luanti.log"
    stdout_path = work / "stdout.log"
    port = 40000 + int(time.time() * 1000) % 20000
    cmd = [str(binary), "--config", str(work / "minetest.conf"), "--world", str(world),
           "--gameid", "devtest", "--port", str(port), "--logfile", str(log_path)]
    timed_out = False
    exit_code = None
    with stdout_path.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=str(work), stdout=out, stderr=subprocess.STDOUT, env=env)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if result_path.exists():
                try:
                    exit_code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); exit_code = proc.wait()
                break
            if proc.poll() is not None:
                exit_code = proc.returncode
                break
            time.sleep(0.2)
        else:
            timed_out = True
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    log_text = ""
    for p in (stdout_path, log_path):
        if p.exists():
            log_text += p.read_text(encoding="utf-8", errors="ignore")
    result_json = None
    if result_path.exists():
        try:
            result_json = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            result_json = None
    return ReplayObservation(
        build_ok=True,
        harness_ok=(result_json is not None) or timed_out or (exit_code not in (None, 0)),
        exit_code=exit_code,
        timed_out=timed_out,
        log_text=log_text,
        result_json=result_json,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pre-bin", default=None)
    ap.add_argument("--post-bin", default=None)
    ap.add_argument("--report", default="reports/warptest/luanti-replay/vmanip-17047.json")
    ap.add_argument("--timeout-s", type=float, default=40.0)
    ap.add_argument("--asan", action="store_true", help="Build both commits with AddressSanitizer so the UaF aborts deterministically.")
    args = ap.parse_args()

    # Resolve the two in-place server binaries (per-SHA worktree), building only if
    # absent. We use the RUN_IN_PLACE binary directly (never the buggy install step).
    src = get_source("luanti", ROOT)
    builder = LuantiUpstreamBuilder(src["upstream_url"])
    builder.ensure_mirror()

    def resolve(sha: str, override: str | None) -> Path | None:
        if override:
            return Path(override)
        wt = builder.worktree_for(sha)
        b = wt / "bin" / "luantiserver"
        return b if b.exists() else None

    run_env = None
    if args.asan:
        run_env = _asan_env()
        print("[probe] building ASan pre-fix binary (this recompiles with -fsanitize=address)...")
        pre_bin, pre_log = _build_asan(builder, PRE_SHA)
        if not pre_bin:
            print("[probe] ASan PRE build failed:\n" + pre_log[-2000:]); return 2
        print(f"[probe]   pre asan -> {pre_bin}")
        print("[probe] building ASan post-fix binary...")
        post_bin, post_log = _build_asan(builder, POST_SHA)
        if not post_bin:
            print("[probe] ASan POST build failed:\n" + post_log[-2000:]); return 2
        print(f"[probe]   post asan -> {post_bin}")
    else:
        pre_bin = resolve(PRE_SHA, args.pre_bin)
        post_bin = resolve(POST_SHA, args.post_bin)
    if not pre_bin or not pre_bin.exists() or not post_bin or not post_bin.exists():
        print(f"Missing binaries: pre={pre_bin} post={post_bin}. Build them first (see luanti_replay).")
        return 2

    # Defect oracle (Contract 3): a crash (abnormal exit) or a hang is a defect. The
    # post-fix clean "vm is null" caught error is NOT a defect. A log SIGSEGV/abort
    # signature also counts (belt-and-braces alongside the exit code).
    oracle = DefectOracle(
        abnormal_exit=True,
        timeout=True,
        log_signatures=["Segmentation fault", "SIGSEGV", "signal 11", "AddressSanitizer", "libc\\+\\+abi"],
        semantic_assertions=[],
    )

    report_dir = (ROOT / args.report).resolve().parent
    report_dir.mkdir(parents=True, exist_ok=True)
    work_root = report_dir / "work" / "vmanip-17047"

    print(f"[probe] PR #{PR_NUMBER} vmanip UaF")
    print(f"[probe] PRE  {PRE_SHA[:10]} -> {pre_bin}")
    print(f"[probe] POST {POST_SHA[:10]} -> {post_bin}")
    print("[probe] running PRE-fix binary...")
    pre = _run(pre_bin, work_root / "pre", timeout_s=args.timeout_s, env=run_env)
    print(f"[probe]   pre: exit={pre.exit_code} timed_out={pre.timed_out} result={pre.result_json}")
    print("[probe] running POST-fix binary...")
    post = _run(post_bin, work_root / "post", timeout_s=args.timeout_s, env=run_env)
    print(f"[probe]   post: exit={post.exit_code} timed_out={post.timed_out} result={post.result_json}")

    verdict = classify_replay(pre, post, oracle)
    verdict["pr_number"] = PR_NUMBER
    verdict["pre_fix_sha"] = PRE_SHA
    verdict["post_fix_sha"] = POST_SHA
    verdict["defect_oracle"] = oracle.to_dict()
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_url": f"https://github.com/luanti-org/luanti/pull/{PR_NUMBER}",
        "verdict": verdict["verdict"],
        "result": verdict,
    }
    out = (ROOT / args.report).resolve()
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[probe] VERDICT = {verdict['verdict']}")
    print(f"[probe] wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
