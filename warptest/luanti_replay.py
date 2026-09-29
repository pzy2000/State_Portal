"""Isolated two-commit build + replay harness for the Luanti replay dataset.

Implements Contract 4 (isolated build) and Contract 3 (independent defect oracle):

  * Historical commits are NEVER checked out in ``games/luanti``. Instead a bare
    mirror of the upstream is kept under a cache dir, and each SHA is materialized
    as a detached ``git worktree`` with its own external build directory and a
    complete installed runtime (server binary + builtin + devtest).
  * The server is built ``BUILD_CLIENT=FALSE BUILD_SERVER=TRUE`` (headless; the
    screenshot hook that ``LuantiWarmSession`` injects into ``game.cpp`` is a client
    concern and is never touched here).
  * A replay fixture (a minimal world + a neutral synth mod that reads
    ``target.json``) is copied into a TEMP world; the source under test is not
    modified. The Python side applies a :class:`DefectOracle` to the process
    exit / hang / log / result-json signals -- it does NOT treat a plain
    ``status=="failure"`` as reproduced.

Builds are cached by :func:`build_cache_key`, so re-verifying is cheap and a stale
binary is never reused across incompatible commit/platform/compiler/args/harness.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .upstream_contracts import (
    DefectOracle,
    ReplayObservation,
    build_cache_key,
    sha256_text,
)

# Neutral replay driver: synthesize the deep-state and record a factual result.
# Unlike scripts/build_luanti_repro.py it does NOT decide "reproduced" -- the
# Python DefectOracle owns that decision (Contract 3). A hard crash / hang escapes
# this mod entirely and is caught by the process watchdog + oracle.
_REPLAY_INIT_LUA = r"""-- Neutral Luanti replay driver (issue-guided replay dataset).
local worldpath = core.get_worldpath()
local target_path = worldpath .. "/" .. "target.json"
local result_path = worldpath .. "/" .. "replay-result.json"
local started_path = worldpath .. "/" .. "replay-started"
local storage = core.get_mod_storage()
local MAX_FILL_NODES = 100000

local function to_pos(v)
	if type(v) ~= "table" then return nil end
	local x, y, z = tonumber(v.x), tonumber(v.y), tonumber(v.z)
	if x == nil or y == nil or z == nil then return nil end
	return { x = math.floor(x + 0.5), y = math.floor(y + 0.5), z = math.floor(z + 0.5) }
end

local function load_area(p1, p2)
	local minp = { x = math.min(p1.x, p2.x), y = math.min(p1.y, p2.y), z = math.min(p1.z, p2.z) }
	local maxp = { x = math.max(p1.x, p2.x), y = math.max(p1.y, p2.y), z = math.max(p1.z, p2.z) }
	pcall(function() core.load_area(minp, maxp) end)
	return minp, maxp
end

local function node_def(name, param2)
	local n = { name = name }
	if param2 ~= nil then n.param2 = tonumber(param2) or 0 end
	return n
end

local function synthesize(target)
	for _, node in ipairs(target.nodes or {}) do
		local pos = to_pos(node.pos)
		if pos and node.node then
			pcall(function() core.load_area(pos) end)
			core.set_node(pos, node_def(node.node, node.param2))
		end
	end
	if type(target.fill) == "table" then
		for _, region in ipairs(target.fill) do
			local p1, p2 = to_pos(region["from"]), to_pos(region.to)
			if p1 and p2 and region.node then
				local minp, maxp = load_area(p1, p2)
				local sx = maxp.x - minp.x + 1
				local sy = maxp.y - minp.y + 1
				local sz = maxp.z - minp.z + 1
				local volume = sx * sy * sz
				if volume > MAX_FILL_NODES then
					error("replay fixture rejected: fill volume " .. tostring(volume) ..
						" exceeds " .. tostring(MAX_FILL_NODES))
				end
				for x = minp.x, maxp.x do
					for y = minp.y, maxp.y do
						for z = minp.z, maxp.z do
							core.set_node({ x = x, y = y, z = z }, node_def(region.node, region.param2))
						end
					end
				end
			end
		end
	end
	for _, meta in ipairs(target.meta_inventory or {}) do
		local pos = to_pos(meta.pos)
		if pos then
			if meta.node and meta.node ~= "" then core.set_node(pos, { name = meta.node }) end
			local inv = core.get_meta(pos):get_inventory()
			inv:set_size(meta.list or "main", math.max(#(meta.items or {}), 8))
			for _, entry in ipairs(meta.items or {}) do
				inv:add_item(meta.list or "main", ItemStack(entry.item .. " " .. tostring(entry.count or 1)))
			end
		end
	end
	for _, kv in ipairs(target.mod_storage or {}) do
		if kv.key ~= nil then storage:set_string(tostring(kv.key), tostring(kv.value)) end
	end
	return true
end

local function run()
	local status, detail = "success", "synthesized"
	core.safe_file_write(started_path, "started\n")
	local file = io.open(target_path, "r")
	local target = nil
	if file then
		local content = file:read("*a"); file:close()
		target = core.parse_json(content)
	end
	if type(target) ~= "table" then
		status, detail = "failure", "core.parse_json rejected the target state"
	else
		local ok, err = pcall(synthesize, target)
		if not ok then status, detail = "failure", "synthesis error: " .. tostring(err) end
	end
	local report = { status = status, detail = detail }
	core.safe_file_write(result_path, core.write_json(report, true) or '{"status":"failure"}')
	core.request_shutdown("", false, 0)
end

core.register_on_mods_loaded(function() core.after(1.0, run) end)
"""

_REPLAY_MOD_CONF = "name = replay\ndescription = Neutral Luanti replay synth driver (issue-guided dataset).\n"

_WORLD_MT = "\n".join(
    [
        "gameid = devtest",
        "backend = dummy",
        "player_backend = sqlite3",
        "auth_backend = sqlite3",
        "mod_storage_backend = sqlite3",
        "world_name = replay",
        "creative_mode = false",
        "enable_damage = true",
        "load_mod_replay = true",
        "",
    ]
)

_MINETEST_CONF = "\n".join(
    [
        "name = replay",
        "mg_name = flat",
        "mgflat_ground_level = 0",
        "enable_damage = true",
        "creative_mode = false",
        "give_initial_stuff = false",
        "enable_sound = false",
        "sound_volume = 0",
        "debug_log_level = error",
        "debug_log_size_max = 5",
        "secure.enable_security = false",
        "secure.trusted_mods = replay",
        "",
    ]
)

_RUNTIME_LAYOUT_VERSION = 2
_HARNESS_DIGEST = sha256_text(
    _REPLAY_INIT_LUA
    + _WORLD_MT
    + _MINETEST_CONF
    + f"runtime-layout:{_RUNTIME_LAYOUT_VERSION}"
)

_SERVER_CMAKE_ARGS = [
    "-DCMAKE_BUILD_TYPE=Release",
    "-DRUN_IN_PLACE=TRUE",
    "-DBUILD_CLIENT=FALSE",
    "-DBUILD_SERVER=TRUE",
    "-DBUILD_UNITTESTS=FALSE",
    "-DINSTALL_DEVTEST=TRUE",
    "-DENABLE_LEVELDB=OFF",
    "-DENABLE_REDIS=OFF",
    "-DENABLE_POSTGRESQL=OFF",
]

_DARWIN_CMAKE_ARGS = [
    "-DCMAKE_FIND_FRAMEWORK=LAST",
    "-DENABLE_GETTEXT=TRUE",
    "-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG",
    "-DCMAKE_EXE_LINKER_FLAGS=-lbz2",
]


def _effective_cmake_args() -> list[str]:
    args = list(_SERVER_CMAKE_ARGS)
    if platform.system() == "Darwin":
        args.extend(_DARWIN_CMAKE_ARGS)
    return args


def _server_target(worktree: Path) -> str:
    """Return the server target across the Minetest -> Luanti rename."""

    cmake = (worktree / "CMakeLists.txt").read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"(?im)^\s*project\s*\(\s*([A-Za-z0-9_-]+)", cmake)
    project_name = match.group(1).lower() if match else "luanti"
    return "minetestserver" if project_name == "minetest" else "luantiserver"


def _runtime_binary(runtime: Path) -> Path | None:
    if (
        not (runtime / "builtin").is_dir()
        or not (runtime / "games" / "devtest").is_dir()
    ):
        return None
    for name in ("luantiserver", "minetestserver"):
        candidate = runtime / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def _find_staged_dir(stage: Path, name: str, required_child: str) -> Path | None:
    for candidate in sorted(stage.rglob(name)):
        if candidate.is_dir() and (candidate / required_child).exists():
            return candidate
    return None


def _normalize_staged_runtime(stage: Path, runtime: Path, server_name: str) -> Path:
    """Normalize platform-specific CMake install layouts into RUN_IN_PLACE form."""

    binaries = [path for path in stage.rglob(server_name) if path.is_file()]
    builtin = _find_staged_dir(stage, "builtin", "init.lua")
    devtest = _find_staged_dir(stage, "devtest", "game.conf")
    if not binaries or builtin is None or devtest is None:
        raise RuntimeError(
            "installed runtime is incomplete "
            f"(binary={bool(binaries)} builtin={builtin is not None} devtest={devtest is not None})"
        )
    shutil.rmtree(runtime, ignore_errors=True)
    (runtime / "bin").mkdir(parents=True, exist_ok=True)
    (runtime / "games").mkdir(parents=True, exist_ok=True)
    out_bin = runtime / "bin" / server_name
    shutil.copy2(binaries[0], out_bin)
    out_bin.chmod(0o755)
    shutil.copytree(builtin, runtime / "builtin")
    shutil.copytree(devtest, runtime / "games" / "devtest")
    return out_bin


def _compiler_id() -> str:
    cc = shutil.which("clang++") or shutil.which("g++") or "cc"
    try:
        out = subprocess.run(
            [cc, "--version"], capture_output=True, text=True, timeout=15
        ).stdout
        return out.splitlines()[0] if out else cc
    except Exception:
        return cc


class LuantiUpstreamBuilder:
    """Bare-cache + per-SHA worktree + external build (Contract 4)."""

    def __init__(self, upstream_url: str, cache_root: Path | None = None) -> None:
        self.upstream_url = upstream_url
        self.cache_root = (
            Path(
                cache_root
                or os.environ.get(
                    "WARPTEST_REPLAY_CACHE",
                    Path.home() / ".cache" / "warptest" / "luanti-replay",
                )
            )
            .expanduser()
            .resolve()
        )
        self.bare = self.cache_root / "upstream.git"
        self.worktrees = self.cache_root / "worktrees"
        self.builds = self.cache_root / "builds"
        for d in (self.worktrees, self.builds):
            d.mkdir(parents=True, exist_ok=True)

    def ensure_mirror(self) -> None:
        if not self.bare.exists():
            self.bare.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--bare", self.upstream_url, str(self.bare)],
                check=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(self.bare), "fetch", "--all", "--tags", "--prune"],
                check=False,
            )

    def fetch_sha(self, sha: str) -> None:
        # Ensure the specific commit is present even if it is not on a fetched ref.
        subprocess.run(
            ["git", "-C", str(self.bare), "fetch", "origin", sha], check=False
        )

    def worktree_for(self, sha: str) -> Path:
        wt = self.worktrees / sha
        if (wt / ".git").exists():
            return wt
        self.fetch_sha(sha)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
        subprocess.run(
            ["git", "-C", str(self.bare), "worktree", "add", "--detach", str(wt), sha],
            check=True,
        )
        return wt

    def cache_key(self, sha: str) -> str:
        return build_cache_key(
            commit=sha,
            platform=platform.platform(),
            compiler=_compiler_id(),
            cmake_args=_effective_cmake_args(),
            harness_digest=_HARNESS_DIGEST,
        )

    def build_server(
        self, sha: str, *, timeout_s: int | None = None
    ) -> tuple[Path | None, str]:
        """Return ``(luantiserver_path | None, log)`` for a given commit.

        A cached binary (keyed by :func:`build_cache_key`) is reused when present.
        """

        key = self.cache_key(sha)
        cache_dir = self.builds / key
        runtime = cache_dir / "runtime"
        cached_bin = _runtime_binary(runtime)
        if cached_bin is not None:
            return cached_bin, "cache hit"
        timeout_s = timeout_s or int(
            os.environ.get("WARPTEST_LUANTI_BUILD_TIMEOUT", "2700")
        )
        wt = self.worktree_for(sha)
        build_dir = cache_dir / "build"
        stage = cache_dir / "install"
        build_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        configure = [
            "cmake",
            "-S",
            str(wt),
            "-B",
            str(build_dir),
            *_effective_cmake_args(),
        ]
        if platform.system() == "Darwin":
            env = _with_homebrew_prefix(env)
        server_name = _server_target(wt)
        build = [
            "cmake",
            "--build",
            str(build_dir),
            "--parallel",
            str(os.cpu_count() or 2),
            "--target",
            server_name,
        ]
        shutil.rmtree(stage, ignore_errors=True)
        install = ["cmake", "--install", str(build_dir), "--prefix", str(stage)]
        log_lines: list[str] = []
        for cmd in (configure, build, install):
            proc = subprocess.run(
                cmd,
                cwd=str(wt),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
            )
            log_lines.append(
                f"$ {' '.join(cmd)}\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"
            )
            if proc.returncode != 0:
                return None, "\n".join(log_lines)
        try:
            out_bin = _normalize_staged_runtime(stage, runtime, server_name)
        except RuntimeError as error:
            return None, "\n".join(log_lines) + f"\n{error}"
        return out_bin, "built"


def _with_homebrew_prefix(env: dict[str, str]) -> dict[str, str]:
    brew = shutil.which("brew")
    if not brew:
        return env
    try:
        prefix = subprocess.run(
            [brew, "--prefix"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
    except Exception:
        prefix = "/opt/homebrew"
    prefix = prefix or "/opt/homebrew"
    paths = [prefix]
    for keg in ("openal-soft", "gettext", "sqlite", "jpeg-turbo"):
        opt = Path(prefix) / "opt" / keg
        if opt.exists():
            paths.append(str(opt))
    env = dict(env)
    existing = env.get("CMAKE_PREFIX_PATH", "")
    env["CMAKE_PREFIX_PATH"] = ";".join(paths + ([existing] if existing else []))
    return env


def write_fixture(dest: Path, target: dict[str, Any]) -> Path:
    """Materialize a self-contained replay world under ``dest`` and return it."""

    import json

    world = dest / "world"
    mod = world / "worldmods" / "replay"
    mod.mkdir(parents=True, exist_ok=True)
    target_json = json.dumps(target, indent=2, ensure_ascii=False) + "\n"
    (world / "target.json").write_text(target_json, encoding="utf-8")
    (world / "world.mt").write_text(_WORLD_MT, encoding="utf-8")
    (mod / "init.lua").write_text(_REPLAY_INIT_LUA, encoding="utf-8")
    (mod / "mod.conf").write_text(_REPLAY_MOD_CONF, encoding="utf-8")
    (dest / "minetest.conf").write_text(_MINETEST_CONF, encoding="utf-8")
    return world


def run_replay(
    binary: Path,
    fixture_dir: Path,
    *,
    oracle: DefectOracle,
    timeout_s: float = 60.0,
    games_dir: Path | None = None,
) -> ReplayObservation:
    """Run one fixture on one server binary and return a scored observation."""

    import json

    world = fixture_dir / "world"
    conf = fixture_dir / "minetest.conf"
    result_path = world / "replay-result.json"
    started_path = world / "replay-started"
    log_path = fixture_dir / "luanti.log"
    stdout_path = fixture_dir / "stdout.log"
    for p in (result_path, started_path, log_path, stdout_path):
        p.unlink(missing_ok=True)
    port = 40000 + (int(time.time() * 1000) % 20000)
    cmd = [
        str(binary),
        "--config",
        str(conf),
        "--world",
        str(world),
        "--gameid",
        "devtest",
        "--port",
        str(port),
        "--logfile",
        str(log_path),
    ]
    env = dict(os.environ)
    if games_dir is not None:
        # Point the server at a games dir that contains devtest (RUN_IN_PLACE uses
        # <bin>/../games; historical worktrees bundle it under games/devtest).
        env["MINETEST_GAME_PATH"] = str(games_dir)
    timed_out = False
    exit_code: int | None = None
    launch_error: str | None = None
    with stdout_path.open("w", encoding="utf-8") as out:
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(fixture_dir), stdout=out, stderr=subprocess.STDOUT, env=env
            )
        except OSError as error:
            launch_error = f"launch failed: {type(error).__name__}: {error}"
            out.write(launch_error + "\n")
        else:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if result_path.exists():
                    # Give the engine a moment to shut down cleanly after writing.
                    try:
                        exit_code = proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        exit_code = proc.wait()
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
        harness_ok=started_path.exists() or result_json is not None,
        exit_code=exit_code,
        timed_out=timed_out,
        log_text=log_text,
        result_json=result_json,
    )
