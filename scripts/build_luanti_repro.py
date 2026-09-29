#!/usr/bin/env python3
"""Generate standalone, dependency-free reproductions for Luanti C3 state-fuzz defects.

Reads a ``fuzz.json`` produced by ``warptest fuzz-bench`` (the Luanti engine path)
plus the per-defect ``tasks/<task>/defects/<fingerprint>/state.json`` artifacts
(the exact mutated deep-state), dedups defects by fingerprint, and for every
*unique* defect writes a self-contained reproduction directory:

    <out>/<defect_id>/
      target.json                 the exact mutated deep-state (world/ copy)
      world/                      a minimal Luanti world (devtest, flat mapgen)
        world.mt
        target.json               <- the mod reads this at worldpath/target.json
        worldmods/repro/init.lua  a self-contained Lua replay of the WarpTest
        worldmods/repro/mod.conf  synthesize+validate path (NO warptest code)
      minetest.conf
      run.sh                      drives games/luanti/bin/luantiserver and prints
                                  REPRODUCED / NOT-REPRODUCED (zero warptest/fuzz)
      README.md

The generated reproductions import **nothing** from ``warptest`` or the fuzzer:
they only need the ``luantiserver`` binary + the bundled ``devtest`` game. The
replay mod is a trimmed copy of the WarpTest worldmod's synthesize+validate logic
(``warptest/instrumentation/luanti/warptest/init.lua``) so it hits the same engine
code path -- an unregistered node, a malformed item table, a validation mismatch,
or an engine hard-crash reproduces exactly as the fuzzer observed it.

Usage:
    python scripts/build_luanti_repro.py \
        --report-dir reports/warptest/luanti-fuzz-9h-20260706/luanti \
        --out        reports/warptest/luanti-fuzz-9h-20260706/repro \
        --luanti-bin games/luanti/bin/luantiserver
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Self-contained replay mod (a trimmed copy of the WarpTest worldmod's
# synthesize + validate path). It reads ``worldpath/target.json`` and writes
# ``worldpath/repro-result.json`` with {status, detail, checks, reproduced},
# then requests shutdown. It requires nothing beyond core Luanti + devtest.
# --------------------------------------------------------------------------- #
REPRO_INIT_LUA = r'''-- Standalone reproduction driver for a Luanti C3 state-fuzz defect.
-- Self-contained: it does NOT depend on the WarpTest harness or the fuzzer.
-- It replays the exact mutated deep-state (worldpath/target.json) through the
-- same synthesize+validate steps the fuzzer used, then records whether the
-- engine errored / the state failed to restore (a reproduced defect).

local worldpath = core.get_worldpath()
local DIR_DELIM = "/"
local target_path = worldpath .. DIR_DELIM .. "target.json"
local result_path = worldpath .. DIR_DELIM .. "repro-result.json"
local storage = core.get_mod_storage()
local checks = {}

local function add_check(name, status, detail)
	checks[#checks + 1] = { name = name, status = status, detail = detail or "" }
end

local function all_ok()
	for _, c in ipairs(checks) do
		if c.status ~= "success" then
			return false
		end
	end
	return true
end

local function to_pos(value)
	if type(value) ~= "table" then
		return nil
	end
	local x = tonumber(value.x)
	local y = tonumber(value.y)
	local z = tonumber(value.z)
	if x == nil or y == nil or z == nil then
		return nil
	end
	return { x = math.floor(x + 0.5), y = math.floor(y + 0.5), z = math.floor(z + 0.5) }
end

local function pos_to_string(pos)
	return string.format("%d,%d,%d", pos.x, pos.y, pos.z)
end

local function load_pos(pos)
	pcall(function() core.load_area(pos) end)
	pcall(function() core.forceload_block(pos, true) end)
end

local function load_area(p1, p2)
	local minp = { x = math.min(p1.x, p2.x), y = math.min(p1.y, p2.y), z = math.min(p1.z, p2.z) }
	local maxp = { x = math.max(p1.x, p2.x), y = math.max(p1.y, p2.y), z = math.max(p1.z, p2.z) }
	pcall(function() core.load_area(minp, maxp) end)
	for bx = math.floor(minp.x / 16), math.floor(maxp.x / 16) do
		for by = math.floor(minp.y / 16), math.floor(maxp.y / 16) do
			for bz = math.floor(minp.z / 16), math.floor(maxp.z / 16) do
				pcall(function() core.forceload_block({ x = bx * 16, y = by * 16, z = bz * 16 }, true) end)
			end
		end
	end
	return minp, maxp
end

local function node_def(name, param2)
	local node = { name = name }
	if param2 ~= nil then
		node.param2 = tonumber(param2) or 0
	end
	return node
end

local function set_node(pos, name, param2)
	load_pos(pos)
	core.set_node(pos, node_def(name, param2))
end

local function get_node_name(pos)
	load_pos(pos)
	return core.get_node(pos).name
end

local function set_meta_inventory(pos, list, items, node_name)
	load_pos(pos)
	if node_name and node_name ~= "" then
		core.set_node(pos, { name = node_name })
	end
	local inv = core.get_meta(pos):get_inventory()
	local size = math.max(#items, inv:get_size(list), 8)
	inv:set_size(list, size)
	inv:set_list(list, {})
	for _, entry in ipairs(items) do
		inv:add_item(list, ItemStack(entry.item .. " " .. tostring(entry.count or 1)))
	end
end

-- Synthesize the deep-state. Mirrors the WarpTest worldmod, minus the online
-- player (a dedicated server has none, matching the headless fuzz session, which
-- runs with warptest_allow_headless_player_skip=true: injected player state is
-- simply skipped rather than treated as a synthesis failure).
local function synthesize(target)
	for _, node in ipairs(target.nodes or {}) do
		local pos = to_pos(node.pos)
		if pos and node.node then
			set_node(pos, node.node, node.param2)
		end
	end
	if type(target.fill) == "table" then
		for _, region in ipairs(target.fill) do
			local p1 = to_pos(region["from"])
			local p2 = to_pos(region.to)
			if p1 and p2 and region.node then
				local minp, maxp = load_area(p1, p2)
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
	if type(target.player) == "table" then
		-- Headless: no online player, skip (parity with allow_headless_player_skip).
	end
	for _, meta in ipairs(target.meta_inventory or {}) do
		local pos = to_pos(meta.pos)
		if pos then
			set_meta_inventory(pos, meta.list or "main", meta.items or {}, meta.node)
		end
	end
	for _, kv in ipairs(target.mod_storage or {}) do
		if kv.key ~= nil then
			storage:set_string(tostring(kv.key), tostring(kv.value))
		end
	end
	return true, "Synthesized world state"
end

local function split(str, sep)
	local out = {}
	for piece in string.gmatch(str, "([^" .. sep .. "]+)") do
		out[#out + 1] = piece
	end
	return out
end

local function resolve_field(path)
	if path == "gametime" then
		return tostring(math.floor(core.get_gametime()))
	end
	if path:sub(1, 5) == "node." then
		local coords = split(path:sub(6), ",")
		local pos = { x = tonumber(coords[1]), y = tonumber(coords[2]), z = tonumber(coords[3]) }
		return get_node_name(pos)
	end
	if path:sub(1, 12) == "mod_storage." then
		return storage:get_string(path:sub(13))
	end
	return "<unknown:" .. tostring(path) .. ">"
end

local function validate(validations)
	for _, v in ipairs(validations or {}) do
		local actual = resolve_field(v.path)
		local expected = tostring(v.expected)
		add_check(
			"target.validate." .. tostring(v.path),
			actual == expected and "success" or "failure",
			actual == expected and (tostring(v.path) .. " = " .. actual)
				or (tostring(v.path) .. ": expected " .. expected .. ", got " .. tostring(actual))
		)
	end
end

-- Replay one request's spec, exactly as the fuzz session did (skip_synthesis=false,
-- empty actions/assertions): synthesize then validate.
local function process(target)
	checks = {}
	local ok, detail = synthesize(target)
	add_check("target.state_synthesized", ok and "success" or "failure", detail)
	if not ok then
		return "failure", "Checkpoint restoration failed: " .. tostring(detail)
	end
	validate(target.validations or {})
	return all_ok() and "success" or "failure", all_ok() and "All checks passed." or "One or more checks failed."
end

local function write_result(status, detail, crashed)
	local reproduced = crashed or (status == "failure")
	local report = {
		status = status,
		detail = detail or "",
		checks = checks,
		reproduced = reproduced,
	}
	local encoded = core.write_json(report, true) or '{"status":"failure","reproduced":true,"detail":"json encode failed"}'
	core.safe_file_write(result_path, encoded)
end

local function run()
	local file = io.open(target_path, "r")
	if not file then
		write_result("failure", "could not open target.json: " .. tostring(target_path), true)
		core.request_shutdown("", false, 0)
		return
	end
	local content = file:read("*a")
	file:close()
	local target = core.parse_json(content)
	if type(target) ~= "table" then
		-- The engine's own JSON parser rejected the mutated state. In the fuzz
		-- warm-session this manifested as "timed out waiting for report" (the
		-- worldmod skipped the unparseable request); here it is pinned down as a
		-- concrete parse rejection (typically NaN / Infinity / oversized numbers).
		write_result("failure", "core.parse_json rejected the mutated state (unparseable JSON)", true)
		core.request_shutdown("", false, 0)
		return
	end
	-- pcall the whole replay so a Lua-level error (the common engine defect, e.g. an
	-- unregistered node or a malformed item table) is captured as a reproduced
	-- crash instead of aborting silently. A hard C++ crash escapes pcall; then no
	-- result file is written and run.sh flags it as a reproduced hard-crash.
	local pok, status, detail = pcall(process, target)
	if not pok then
		checks = checks or {}
		add_check("target.state_synthesized", "failure", "synthesis error: " .. tostring(status))
		write_result("failure", "Checkpoint processing crashed: " .. tostring(status), true)
	else
		write_result(status, detail, false)
	end
	core.request_shutdown("", false, 0)
end

core.register_on_mods_loaded(function()
	core.after(1.0, run)
end)
'''

REPRO_MOD_CONF = "name = repro\ndescription = Standalone Luanti state-fuzz defect reproduction (no WarpTest dependency).\n"

WORLD_MT = "\n".join(
    [
        "gameid = devtest",
        # Non-persistent (in-memory) map backend: a mutated state with extreme
        # coordinates / huge fill regions must not balloon map.sqlite and exhaust
        # disk while reproducing. Synthesize+validate run in one session, so an
        # in-memory map is sufficient and the defect reproduces identically.
        "backend = dummy",
        "player_backend = sqlite3",
        "auth_backend = sqlite3",
        "mod_storage_backend = sqlite3",
        "world_name = repro",
        "creative_mode = false",
        "enable_damage = true",
        "load_mod_repro = true",
        "",
    ]
)

MINETEST_CONF = "\n".join(
    [
        "name = repro",
        "mg_name = flat",
        "mgflat_ground_level = 0",
        "enable_damage = true",
        "creative_mode = false",
        "give_initial_stuff = false",
        "enable_sound = false",
        "sound_volume = 0",
        # A mutated state can make the engine spam WARNING[Server]: Block not found
        # (huge/unloaded positions); log only errors and cap+rotate so the debug
        # log cannot balloon and exhaust disk while reproducing.
        "debug_log_level = error",
        "debug_log_size_max = 5",
        # The replay mod reads target.json and writes repro-result.json via
        # io.open / safe_file_write; disable the mod-security sandbox for this
        # ephemeral harness world (identical to the WarpTest harness world).
        "secure.enable_security = false",
        "secure.trusted_mods = repro",
        "",
    ]
)


def _run_sh(luanti_bin_abs: str) -> str:
    return f'''#!/usr/bin/env bash
# Standalone reproduction for one Luanti C3 state-fuzz defect.
# Zero dependency on warptest / the fuzzer: it only drives luantiserver + devtest.
#
# Override the binary with:  LUANTI_SERVER_BIN=/path/to/luantiserver ./run.sh
# Override the timeout with: REPRO_TIMEOUT=180 ./run.sh
set -u

HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
LUANTI_SERVER_BIN="${{LUANTI_SERVER_BIN:-{luanti_bin_abs}}}"
WORLD="$HERE/world"
CONF="$HERE/minetest.conf"
LOG="$HERE/luanti.log"
RESULT="$WORLD/repro-result.json"
TIMEOUT="${{REPRO_TIMEOUT:-120}}"
# A high random port avoids colliding with any other luantiserver (default 30000).
PORT="${{REPRO_PORT:-$(( (RANDOM % 20000) + 40000 ))}}"

if [ ! -x "$LUANTI_SERVER_BIN" ]; then
  echo "ERROR: luantiserver not found/executable at: $LUANTI_SERVER_BIN" >&2
  echo "       set LUANTI_SERVER_BIN=/path/to/luantiserver and re-run." >&2
  exit 3
fi

rm -f "$RESULT" "$LOG"

"$LUANTI_SERVER_BIN" --config "$CONF" --world "$WORLD" --gameid devtest --port "$PORT" --logfile "$LOG" >"$HERE/stdout.log" 2>&1 &
PID=$!

# Watchdog: wait for the result file (the mod writes it and shuts down), for the
# process to die on its own, or for the timeout (a hang -> reproduced defect).
HUNG=0
for _ in $(seq 1 "$TIMEOUT"); do
  [ -f "$RESULT" ] && break
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
sleep 1
if kill -0 "$PID" 2>/dev/null; then
  # Still running: either finished-but-not-exited, or genuinely hung. A runaway
  # synthesis (huge fill region) spins inside a synchronous Lua call, so the
  # engine's main loop never services SIGTERM -- escalate to SIGKILL.
  [ -f "$RESULT" ] || HUNG=1
  kill "$PID" 2>/dev/null
  for _ in 1 2 3; do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
  kill -9 "$PID" 2>/dev/null
  wait "$PID" 2>/dev/null
else
  wait "$PID" 2>/dev/null
fi

echo "--- defect (see README.md) ---"
if [ -f "$RESULT" ]; then
  echo "repro-result.json:"
  cat "$RESULT"
  echo
  if grep -Eq '"reproduced"[[:space:]]*:[[:space:]]*true' "$RESULT" \
     || grep -Eq '"status"[[:space:]]*:[[:space:]]*"failure"' "$RESULT"; then
    echo "RESULT: REPRODUCED (engine reported a restore failure / synthesis error)"
    exit 0
  fi
  echo "RESULT: NOT-REPRODUCED (engine restored the mutated state cleanly)"
  exit 1
fi

if [ "$HUNG" = "1" ]; then
  echo "RESULT: REPRODUCED (engine hung on the mutated state; killed after ${{TIMEOUT}}s)"
  exit 0
fi
echo "RESULT: REPRODUCED (engine hard-crashed before writing a result; see luanti.log / stdout.log)"
exit 0
'''


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _defect_id(task_id: str, fingerprint: str) -> str:
    stem = _SAFE.sub("-", str(task_id)).strip("-") or "luanti"
    fp = _SAFE.sub("", str(fingerprint))[:12] or "nofp"
    return f"{stem}__{fp}"


def _classify(detail: str) -> str:
    d = (detail or "").lower()
    if "concatenate" in d and "item" in d:
        return "harness-mod bug (malformed item table -> Lua concat error in the synthesize path)"
    if "unknown node" in d:
        return "engine/restore: references an unregistered node name"
    if "timed out" in d or "not running" in d or "exited" in d:
        # In the warm-session protocol a request whose JSON the engine cannot parse
        # (NaN / Infinity / oversized numbers / control chars) is silently skipped by
        # the worldmod, so the driver times out waiting for its report. Extreme fills
        # can also make synthesis genuinely hang. The standalone repro pins down which.
        return "un-loadable mutated state (engine timed out; malformed/oversized JSON or runaway synthesis)"
    if "one or more checks failed" in d:
        return "validation mismatch: state restored but declared validations do not hold"
    if "checkpoint processing crashed" in d:
        return "Lua error inside synthesize/validate (caught by pcall)"
    if "checkpoint restoration failed" in d:
        return "synthesize returned failure"
    return "other engine restore failure"


def _load_defects(report_dir: Path) -> list[dict[str, Any]]:
    fuzz_path = report_dir / "fuzz.json"
    data = json.loads(fuzz_path.read_text(encoding="utf-8"))
    tasks_dir = report_dir / "tasks"
    unique: dict[str, dict[str, Any]] = {}
    missing_artifact = 0
    for result in data.get("results", []) or []:
        task_id = str(result.get("task_id"))
        oracle_spec = None
        for mut in result.get("mutations", []) or []:
            if mut.get("verdict") != "defect":
                continue
            fp = str(mut.get("fingerprint") or "")
            if not fp or fp in unique:
                continue
            artifact = tasks_dir / task_id / "defects" / fp / "state.json"
            target: Any = None
            if artifact.exists():
                try:
                    state = json.loads(artifact.read_text(encoding="utf-8"))
                    target = state.get("target")
                    oracle_spec = state.get("oracle_spec")
                except Exception:
                    target = None
            if not isinstance(target, dict):
                # No faithful full-state artifact -> cannot build a standalone repro.
                missing_artifact += 1
                continue
            unique[fp] = {
                "fingerprint": fp,
                "task_id": task_id,
                "oracle_spec": oracle_spec,
                "defect_type": mut.get("defect_type"),
                "detail": mut.get("detail", ""),
                "mutation": mut.get("mutation"),
                "operator_id": mut.get("operator_id"),
                "target": target,
            }
    if missing_artifact:
        print(f"  WARNING: {missing_artifact} unique defect(s) had no full-state artifact and were skipped.")
    return list(unique.values())


def _write_repro(defect: dict[str, Any], out_dir: Path, luanti_bin_abs: str) -> Path:
    defect_id = _defect_id(defect["task_id"], defect["fingerprint"])
    d = out_dir / defect_id
    world = d / "world"
    mod = world / "worldmods" / "repro"
    mod.mkdir(parents=True, exist_ok=True)

    target_json = json.dumps(defect["target"], indent=2, ensure_ascii=False) + "\n"
    (d / "target.json").write_text(target_json, encoding="utf-8")
    (world / "target.json").write_text(target_json, encoding="utf-8")
    (world / "world.mt").write_text(WORLD_MT, encoding="utf-8")
    (mod / "init.lua").write_text(REPRO_INIT_LUA, encoding="utf-8")
    (mod / "mod.conf").write_text(REPRO_MOD_CONF, encoding="utf-8")
    (d / "minetest.conf").write_text(MINETEST_CONF, encoding="utf-8")
    run_sh = d / "run.sh"
    run_sh.write_text(_run_sh(luanti_bin_abs), encoding="utf-8")
    run_sh.chmod(0o755)

    classification = _classify(defect["detail"])
    readme = f"""# Luanti state-fuzz defect reproduction: `{defect_id}`

Standalone, minimal reproduction of one C3 state-fuzz defect. It depends on
**nothing** from `warptest` or the fuzzer -- only the `luantiserver` binary and
the bundled `devtest` game.

## Defect
- Task: `{defect['task_id']}`
- Oracle spec: `{defect['oracle_spec']}`
- Defect type: `{defect['defect_type']}`
- Mutation operator: `{defect.get('operator_id') or defect.get('mutation')}`
- Fingerprint: `{defect['fingerprint']}`
- Root cause (heuristic): {classification}

Original engine detail observed by the fuzzer:

```
{(defect['detail'] or '').strip()}
```

## The mutated deep-state
`target.json` (also copied into `world/target.json`) is the exact perturbed state
the fuzzer injected. The replay mod (`world/worldmods/repro/init.lua`) runs the
same synthesize + validate steps the WarpTest worldmod used, so the engine hits
the identical code path.

## Run it
```bash
./run.sh
# or point at a specific server binary / raise the timeout:
LUANTI_SERVER_BIN=/path/to/luantiserver REPRO_TIMEOUT=180 ./run.sh
```

`run.sh` launches `luantiserver` on `world/`, waits for the mod to write
`world/repro-result.json`, and prints:
- `REPRODUCED` (exit 0) if the engine reported a restore failure / synthesis
  error, or hung / hard-crashed on the mutated state;
- `NOT-REPRODUCED` (exit 1) if the engine restored the state cleanly.
"""
    (d / "README.md").write_text(readme, encoding="utf-8")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report-dir", required=True, help="Fuzz report dir containing fuzz.json (the luanti/ subdir).")
    ap.add_argument("--out", default=None, help="Output dir for repro/. Defaults to <report-dir>/../repro.")
    ap.add_argument("--luanti-bin", default="games/luanti/bin/luantiserver", help="Default luantiserver path baked into run.sh (overridable via LUANTI_SERVER_BIN).")
    args = ap.parse_args()

    report_dir = Path(args.report_dir).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve() if args.out else report_dir.parent / "repro"
    luanti_bin_abs = str(Path(args.luanti_bin).expanduser().resolve())

    out_dir.mkdir(parents=True, exist_ok=True)
    defects = _load_defects(report_dir)
    if not defects:
        print("No reproducible unique defects found (no defect artifacts).")
        return 0

    index: list[dict[str, Any]] = []
    for defect in defects:
        d = _write_repro(defect, out_dir, luanti_bin_abs)
        index.append(
            {
                "defect_id": d.name,
                "task_id": defect["task_id"],
                "defect_type": defect["defect_type"],
                "detail": (defect["detail"] or "").strip()[:200],
                "root_cause": _classify(defect["detail"]),
                "path": str(d.relative_to(out_dir)),
            }
        )
    (out_dir / "INDEX.json").write_text(
        json.dumps({"count": len(index), "defects": index}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    run_all = out_dir / "run_all.sh"
    run_all.write_text(
        "#!/usr/bin/env bash\n"
        "# Run every reproduction and summarize REPRODUCED / NOT-REPRODUCED.\n"
        "set -u\nHERE=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
        "pass=0; fail=0\n"
        "for d in \"$HERE\"/*/; do\n"
        "  [ -x \"$d/run.sh\" ] || continue\n"
        "  if (cd \"$d\" && ./run.sh >run.out 2>&1); then\n"
        "    echo \"REPRODUCED    $(basename \"$d\")\"; pass=$((pass+1))\n"
        "  else\n"
        "    echo \"NOT-REPRODUCED $(basename \"$d\")\"; fail=$((fail+1))\n"
        "  fi\n"
        "done\n"
        "echo \"---\"; echo \"reproduced=$pass not_reproduced=$fail\"\n",
        encoding="utf-8",
    )
    run_all.chmod(0o755)

    print(f"Wrote {len(index)} standalone repro(s) to {out_dir}")
    for entry in index:
        print(f"  {entry['defect_id']}  [{entry['root_cause']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
