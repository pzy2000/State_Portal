#!/usr/bin/env python3
"""Guided operation-sequence fuzzer for the LuaVoxelManip lifetime bug (PR #17047).

Unlike the C3 state fuzzer (which mutates a declarative ``target`` snapshot), this is
a minimal *operation-sequence* fuzzer: each candidate is a randomized sequence of
VoxelManip API + lifecycle operations rendered into a worldmod and executed on an
AddressSanitizer-instrumented ``luantiserver``. A candidate that reaches a
use-after-free aborts under ASan -- that abort is the oracle.

It is ISSUE-GUIDED: the search grammar is biased by the #17047 bug card's signal
(the hot API is the mapgen VoxelManip, the failure mode is using it across the
``emerge`` free boundary). The fuzzer still explores the specifics -- capture mode,
whether to emerge, GC, delay, and which accessor method -- so triggering combos are
DISCOVERED, not hardcoded: plain ``VoxelManip()`` handles and no-emerge sequences do
not crash, while a stashed mapgen vm used after emerge does.

On the first hit it delta-minimizes the parameters and writes a standalone, minimal
reproduction directory (``run.sh`` + world + mod + README) that reproduces on an
ASan pre-fix server and is clean on a post-fix server.

Usage:
    python scripts/fuzz_vmanip_ops.py --iters 24 --seed 1
    python scripts/fuzz_vmanip_ops.py --binary /path/to/asan/luantiserver
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest.luanti_replay import LuantiUpstreamBuilder  # noqa: E402 (read-only)
from warptest.upstream_sources import get_source  # noqa: E402

PRE_SHA = "e6f0377ea2cb60c0c267b9ea570c1b4cc059d5b7"
POST_SHA = "bcf07de10d32f8369e52bf4ef974c069e15602b7"

# Accessor methods that touch the underlying vm buffer (any of them UaF-reads a freed
# MMVManip). The fuzzer picks among these; get_emerged_area is the l_vmanip.cpp:374 site.
_USE_METHODS = ["get_emerged_area", "get_data", "calc_lighting", "update_liquids"]
_CAPTURES = ["mapgen", "plain"]


@dataclass
class OpParams:
    capture: str          # "mapgen" (stash mapgen vm) | "plain" (VoxelManip())
    emerge: bool          # force emerge_area (generates + frees the mapgen vm)
    gc: bool              # collectgarbage before the use
    delay: float          # seconds between emerge and the use
    method: str           # accessor method invoked on the (possibly dangling) vm
    area: int             # emerge/read half-extent

    def render_mod(self) -> str:
        cap = self.capture
        use = self.method
        gc_line = 'collectgarbage("collect")' if self.gc else "-- no gc"
        emerge_line = (
            f"pcall(function() core.emerge_area({{x=0,y=0,z=0}}, {{x={self.area},y={self.area},z={self.area}}}) end)"
            if self.emerge else "-- no emerge"
        )
        if cap == "mapgen":
            capture_block = (
                "core.register_on_generated(function(minp, maxp, seed)\n"
                "  if not stashed then stashed = core.get_mapgen_object('voxelmanip') end\n"
                "end)\n"
            )
            acquire = "local target = stashed"
            guard = "if not target then finish('no_capture','on_generated never fired'); return end"
        else:
            capture_block = ""
            acquire = "local target = VoxelManip()"
            guard = "if not target then finish('no_capture','VoxelManip() returned nil'); return end"
        return f"""
local worldpath = core.get_worldpath()
local result_path = worldpath .. "/fuzz-result.json"
local stashed = nil
local function finish(status, detail)
  local f = io.open(result_path, "w")
  if f then f:write(core.write_json({{status=status, detail=tostring(detail)}}) or '{{}}'); f:close() end
  core.request_shutdown("", false, 0)
end
{capture_block}
core.register_on_mods_loaded(function()
  core.after(0.5, function()
    {emerge_line}
  end)
  core.after(0.5 + {self.delay}, function()
    {gc_line}
    {acquire}
    {guard}
    local ok, err = pcall(function() return target:{use}() end)
    if ok then finish("used_ok", "no crash/error") else finish("caught_error", err) end
  end)
end)
"""


_WORLD_MT = "\n".join([
    "gameid = devtest", "backend = dummy", "player_backend = sqlite3",
    "auth_backend = sqlite3", "mod_storage_backend = sqlite3", "world_name = fuzz",
    "creative_mode = false", "load_mod_fuzzvm = true", "",
])
_CONF = "\n".join([
    "name = fuzz", "mg_name = flat", "mgflat_ground_level = 0", "enable_sound = false",
    "sound_volume = 0", "debug_log_level = error", "debug_log_size_max = 5",
    "secure.enable_security = false", "secure.trusted_mods = fuzzvm", "",
])


def _asan_env() -> dict:
    import os
    env = dict(os.environ)
    env["ASAN_OPTIONS"] = "halt_on_error=1:abort_on_error=1:detect_leaks=0"
    return env


def _write_world(dest: Path, mod_lua: str) -> Path:
    world = dest / "world"
    mod = world / "worldmods" / "fuzzvm"
    mod.mkdir(parents=True, exist_ok=True)
    (world / "world.mt").write_text(_WORLD_MT, encoding="utf-8")
    (mod / "init.lua").write_text(mod_lua, encoding="utf-8")
    (mod / "mod.conf").write_text("name = fuzzvm\ndescription = vmanip op-seq fuzz candidate.\n", encoding="utf-8")
    (dest / "minetest.conf").write_text(_CONF, encoding="utf-8")
    return world


@dataclass
class RunResult:
    crashed: bool
    asan_uaf: bool
    exit_code: int | None
    timed_out: bool
    status: str | None
    detail: str


def _run_candidate(binary: Path, work: Path, mod_lua: str, *, timeout_s: float = 25.0) -> RunResult:
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    world = _write_world(work, mod_lua)
    result_path = world / "fuzz-result.json"
    stdout_path = work / "stdout.log"
    port = 40000 + int(time.time() * 1000) % 20000
    cmd = [str(binary), "--config", str(work / "minetest.conf"), "--world", str(world),
           "--gameid", "devtest", "--port", str(port), "--logfile", str(work / "luanti.log")]
    timed_out = False
    exit_code = None
    with stdout_path.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=str(work), stdout=out, stderr=subprocess.STDOUT, env=_asan_env())
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
    log = stdout_path.read_text(encoding="utf-8", errors="ignore") if stdout_path.exists() else ""
    asan_uaf = "AddressSanitizer: heap-use-after-free" in log
    status = detail = None
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
            status, detail = data.get("status"), data.get("detail")
        except Exception:
            pass
    crashed = asan_uaf or (exit_code not in (None, 0)) or timed_out
    return RunResult(crashed=crashed, asan_uaf=asan_uaf, exit_code=exit_code,
                     timed_out=timed_out, status=status, detail=detail or "")


def _guided_candidate(rng: random.Random) -> OpParams:
    """Issue-guided sampler: biased by the #17047 card toward the mapgen-vm lifetime
    pattern, but still exploring capture/emerge/gc/delay/method."""
    return OpParams(
        capture=rng.choices(_CAPTURES, weights=[0.7, 0.3])[0],   # card: mapgen vm is hot
        emerge=rng.random() < 0.8,                               # card: free happens on emerge
        gc=rng.random() < 0.5,
        delay=rng.choice([2.0, 3.0, 4.0]),
        method=rng.choice(_USE_METHODS),
        area=rng.choice([40, 79, 96]),
    )


def _minimize(binary: Path, work_root: Path, hit: OpParams, timeout_s: float) -> OpParams:
    """Greedily drop non-essential knobs while the crash persists."""
    best = OpParams(**asdict(hit))
    # gc is usually non-essential.
    trial = OpParams(**{**asdict(best), "gc": False})
    if _run_candidate(binary, work_root / "min", trial.render_mod(), timeout_s=timeout_s).asan_uaf:
        best = trial
    # smallest delay that still crashes.
    for d in (1.0, 2.0):
        trial = OpParams(**{**asdict(best), "delay": d})
        if _run_candidate(binary, work_root / "min", trial.render_mod(), timeout_s=timeout_s).asan_uaf:
            best = trial
            break
    # smallest area.
    for a in (16, 40):
        trial = OpParams(**{**asdict(best), "area": a})
        if _run_candidate(binary, work_root / "min", trial.render_mod(), timeout_s=timeout_s).asan_uaf:
            best = trial
            break
    return best


_RUN_SH = r'''#!/usr/bin/env bash
# Standalone minimal reproduction of the LuaVoxelManip use-after-free (luanti PR #17047).
# Requires an AddressSanitizer-instrumented luantiserver (pre-fix commit e6f0377).
# Override the binary: LUANTI_SERVER_BIN=/path/to/asan/luantiserver ./run.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${LUANTI_SERVER_BIN:-__BIN__}"
WORLD="$HERE/world"; CONF="$HERE/minetest.conf"
export ASAN_OPTIONS="halt_on_error=1:abort_on_error=1:detect_leaks=0"
if [ ! -x "$BIN" ]; then echo "ERROR: luantiserver (ASan) not found: $BIN" >&2; exit 3; fi
rm -f "$WORLD/fuzz-result.json" "$HERE/out.log"
PORT="${REPRO_PORT:-$(( (RANDOM % 20000) + 40000 ))}"
"$BIN" --config "$CONF" --world "$WORLD" --gameid devtest --port "$PORT" --logfile "$HERE/luanti.log" >"$HERE/out.log" 2>&1 &
PID=$!
for _ in $(seq 1 30); do [ -f "$WORLD/fuzz-result.json" ] && break; kill -0 "$PID" 2>/dev/null || break; sleep 1; done
sleep 1; kill -9 "$PID" 2>/dev/null; wait "$PID" 2>/dev/null
if grep -q "AddressSanitizer: heap-use-after-free" "$HERE/out.log"; then
  echo "RESULT: REPRODUCED (ASan heap-use-after-free)"; grep -m1 "l_vmanip.cpp" "$HERE/out.log" || true; exit 0
fi
echo "RESULT: NOT-REPRODUCED (no ASan UaF; likely a post-fix / non-ASan binary)"; exit 1
'''


def _emit_repro(out_dir: Path, params: OpParams, binary: Path, asan_excerpt: str) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    _write_world(out_dir, params.render_mod())
    # rename mod dir marker already 'fuzzvm'; keep it.
    run_sh = out_dir / "run.sh"
    run_sh.write_text(_RUN_SH.replace("__BIN__", str(binary)), encoding="utf-8")
    run_sh.chmod(0o755)
    (out_dir / "params.json").write_text(json.dumps(asdict(params), indent=2) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(
        f"""# Minimal reproduction: LuaVoxelManip use-after-free (luanti PR #17047)

Discovered by the guided op-sequence fuzzer (`scripts/fuzz_vmanip_ops.py`), then
delta-minimized. The worldmod stashes the mapgen VoxelManip in `on_generated`,
forces an `emerge_area` (which frees the mapgen `MMVManip`), then calls
`{params.method}()` on the now-dangling handle.

- Pre-fix (`{PRE_SHA[:10]}`) under ASan: **heap-use-after-free** at `l_vmanip.cpp` /
  freed in `emerge.cpp` (server aborts).
- Post-fix (`{POST_SHA[:10]}`): the fix nulls the tracked ref and raises a clean
  `checkObjectValid(): vm is null` Lua error (no crash).

## Run
```bash
LUANTI_SERVER_BIN=/path/to/asan/luantiserver ./run.sh   # prints REPRODUCED / NOT-REPRODUCED
```

## Minimized triggering parameters
```json
{json.dumps(asdict(params), indent=2)}
```

## Captured ASan report (excerpt)
```
{asan_excerpt.strip()[:1500]}
```
""",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--binary", default=None, help="ASan pre-fix luantiserver (default: the built pre-fix worktree binary).")
    ap.add_argument("--iters", type=int, default=24)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout-s", type=float, default=25.0)
    ap.add_argument("--out", default="reports/warptest/luanti-replay/vmanip-uaf-repro")
    args = ap.parse_args()

    if args.binary:
        binary = Path(args.binary)
    else:
        builder = LuantiUpstreamBuilder(get_source("luanti", ROOT)["upstream_url"])
        binary = builder.worktree_for(PRE_SHA) / "bin" / "luantiserver"
    if not binary.exists():
        print(f"ASan pre-fix binary not found: {binary}. Build it first (probe --asan).")
        return 2

    rng = random.Random(args.seed)
    work_root = (ROOT / args.out).resolve().parent / "fuzz-work"
    log: list[dict] = []
    hit = None
    hit_log = ""
    print(f"[opfuzz] guided VoxelManip op-sequence fuzz on {binary.name} (ASan), iters={args.iters} seed={args.seed}")
    for i in range(args.iters):
        params = _guided_candidate(rng)
        res = _run_candidate(binary, work_root / f"iter{i}", params.render_mod(), timeout_s=args.timeout_s)
        tag = "UaF!" if res.asan_uaf else ("crash" if res.crashed else (res.status or "clean"))
        print(f"[opfuzz] iter {i:>2} capture={params.capture:<6} emerge={int(params.emerge)} gc={int(params.gc)} "
              f"delay={params.delay} method={params.method:<16} -> {tag}")
        log.append({"iter": i, "params": asdict(params), "asan_uaf": res.asan_uaf,
                    "crashed": res.crashed, "exit_code": res.exit_code, "status": res.status})
        if res.asan_uaf and hit is None:
            hit = params
            hit_log = (work_root / f"iter{i}" / "stdout.log").read_text(encoding="utf-8", errors="ignore")
            print(f"[opfuzz] >>> UaF discovered at iter {i}; minimizing...")
            break

    report_dir = (ROOT / args.out).resolve().parent
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "opfuzz-log.json").write_text(json.dumps({"seed": args.seed, "iters": args.iters, "trials": log}, indent=2) + "\n", encoding="utf-8")

    if hit is None:
        print("[opfuzz] no UaF found in this budget; try more --iters or a different --seed.")
        return 1

    minimized = _minimize(binary, work_root, hit, args.timeout_s)
    # Re-run the minimized candidate to capture its own ASan excerpt.
    final = _run_candidate(binary, work_root / "final", minimized.render_mod(), timeout_s=args.timeout_s)
    excerpt_src = work_root / "final" / "stdout.log"
    excerpt = ""
    if excerpt_src.exists():
        text = excerpt_src.read_text(encoding="utf-8", errors="ignore")
        lo = text.find("AddressSanitizer")
        excerpt = text[lo:lo + 1500] if lo >= 0 else hit_log[:1500]
    out_dir = (ROOT / args.out).resolve()
    _emit_repro(out_dir, minimized, binary, excerpt or hit_log)
    print(f"[opfuzz] minimized params: {asdict(minimized)}")
    print(f"[opfuzz] minimized still UaF: {final.asan_uaf}")
    print(f"[opfuzz] wrote standalone repro -> {out_dir.relative_to(ROOT)}/  (run ./run.sh)")

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pr_number": 17047,
        "binary": str(binary),
        "discovered_iter": next(t["iter"] for t in log if t["asan_uaf"]),
        "minimized_params": asdict(minimized),
        "repro_dir": str(out_dir.relative_to(ROOT)),
    }
    (report_dir / "opfuzz-hit.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
