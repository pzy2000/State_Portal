# Luanti Benchmark Adaptation: Status, Blockers, and Decision Log

> Last updated: 2026-06-16
> Scope: integrate Luanti (formerly Minetest, a C++ voxel engine + Lua mods) into the WarpTest benchmark as a new game,
> fully aligned with the already integrated native games OpenRA / jynew / pal3.

---

## 1. One-Sentence Summary

Luanti is **fully integrated** into WarpTest (discovery / profile / runtime / schema / strategies / synthesis /
effort / paper-tables / specs / benchmarks / tests / packaging / docs), and the engine has been **actually compiled** on this machine.
The Luanti set now contains **25** specs, including 5 production-state checkpoints; semantic runs are split into 17 world-type
`luantiserver` specs and 8 player/save-type macOS `luanti.app` client specs. The 22 visual goldens were captured as real rendered frames by the turnkey script.

---

## 2. Architecture (aligned with OpenRA: server-side for semantics, rendering client for visuals)

```
specs/luanti/*.yaml
  └─ runner.run_spec
       ├─ ensure_luanti_instrumentation   install Lua worldmod + apply C++ screenshot instrumentation (idempotent)
       └─ LuantiRuntimeAdapter.run_checkpoint
            ├─ _ensure_built               cmake build bin/luanti + bin/luantiserver
            ├─ _spec_needs_client(spec)?   choose headless server vs rendering client
            ├─ create temporary world + minetest.conf + request.json
            ├─ run the engine:
            │    · world-type → bin/luantiserver        (no display)
            │    · player/visual → macOS open -W luanti.app / Linux xvfb + bin/luanti (display required)
            └─ parse report.json + screenshot → LuantiProfile maps stages by check prefix
```

- **Checkpoint contract** (isomorphic to jynew/pal3): Python writes `request.json` → the in-engine worldmod processes it → writes `report.json`
  (`checks[]`, mapped by name prefix).
- **Check prefix mapping**: `target.state_synthesized`/`target.world_loaded` → restoration;
  `target.validate.*` → validation; `action[<type>]@i` → smoke; `assertion[<type>]@i` → oracle.

---

## 3. Completed (all landed)

### 3.1 Game-Side Instrumentation (installed on demand, templates shipped with the package)
| File | Purpose |
|------|------|
| `warptest/instrumentation/luanti/warptest/{mod.conf,init.lua}` | test-gated Lua worldmod: read request → synthesize (player + voxels + chest metadata + mod storage) → validate → actions → assertions → write report via `core.write_json` → `request_shutdown` |
| `warptest/instrumentation/luanti/engine/warptest_capture.inc` | C++ screenshot function (reuses `IVideoDriver::createScreenShot`, aligned with OpenRA `WarptestScreenshotPath`) |
| `warptest/instrumentation/luanti/engine/warptest_call.inc` | Call site in the render loop (idempotently inserted into `src/client/game.cpp` via markers) |

### 3.2 Core Python Modules
| File | Change |
|------|------|
| `warptest/luanti_runtime.py` (new) | `ensure_luanti_instrumentation`, `LuantiRuntimeAdapter` (build / temporary world / headless-or-client run / report parsing / screenshot / two-pass save_restore) |
| `warptest/discovery.py` | `_profile_for`/`_hook_name`/`hook_available`/capability guard `is_luanti`/`_launch_command`/`storage_policy_for`/`adapter_layers_for`/`instrumentation_mode`/`_semantic_schema_luanti` |
| `warptest/profiles.py` | `LuantiProfile` + registration in `profile_for` |
| `warptest/runner.py` | add luanti to `resolve_repo`, add LuantiProfile branch + `ensure_luanti_instrumentation` to `run_spec`, install instrumentation in `prepare_repo` |
| `warptest/strategies.py` | `LUANTI_REPLAY_UNSUPPORTED_REASON` + replay exclusion branch |
| `warptest/spec_schema.py` | 10 `luanti_*` action types + 9 assertion types (including `no_luanti_mod_errors`) + `_require_pos` |
| `warptest/synthesis.py` | `SOURCE_VOCABULARY[luanti]`, `BENCHMARK_CONTRACTS[luanti]`, `_payload_for_spec` branch, 11 `_TEMPLATES` entries |
| `warptest/effort.py` | register luanti profile/specs/repo/runtime |
| `pyproject.toml` | add luanti instrumentation to `package-data` |
| `README.md` | luanti commands, two execution modes, display requirements, golden capture script |

### 3.3 Evaluation Set
- `specs/luanti/*.yaml`: **25** specs (20 smoke specs + 5 production-state checkpoints).
- `specs/luanti/visual/*.golden.png`: 22 real visual goldens (captured by `capture_goldens.py` in a desktop session).
- `specs/luanti/visual/capture_goldens.py`: turnkey golden capture script (for desktop sessions; prints key errors from the Luanti log on failure).
- `benchmarks/core/public/tasks.yaml` + `hidden/oracles.yaml`: **16** core-bench public tasks + hidden oracle mappings.

### 3.4 Tests
- Added 10 luanti cases to `tests/test_core.py` (inspect/profile/semantics/resolve_repo/instrumentation/strategy/schema/param2/synthesis/profile-run/headless-mode/engine-command).
- Global counts updated accordingly: `specs/*/*.yaml` = 124, core public tasks = 60.
- **Full `tests/test_core.py tests/test_evo.py` suite: 139 passed**.
  - Note: 2 cases in `test_visual.py` fail due to a pre-existing environment issue (**Chromium is not installed on this machine**); reproduced on the original code and unrelated to luanti.

### 3.5 Real Build (this machine: macOS / Apple Silicon / Clang 21)
- `games/luanti/bin/luanti` (8.5 MB, client) + `games/luanti/bin/luantiserver` (4.2 MB, server): compiled via the adapter's real `_ensure_built` (≈158s).
- `games/luanti/build-app/out/luanti.app`: RUN_IN_PLACE=FALSE `.app`, codesigned (used by the golden capture script).
- Verification: `--version` → `Luanti 5.17.0-dev … LuaJIT 2.1 … Built by Clang 21.0`; `strings bin/luanti | grep warptest_screenshot` matches (**the C++ instrumentation is indeed compiled into the binary**).

### 3.6 Real Run Verification
17 world-type specs passing with `warptest run` in **headless** mode on this machine:
```
set-node  dig-node  fill-floor  replace-node  build-column  water-source
stone-platform  tree-grove  chest-inventory  chest-multi-item
mod-storage  mod-storage-counter  gametime-advance  save-restore-chest
complex-house-workshop  irrigated-farmstead  storage-workshop
```

8 player/save-type specs passing with `warptest run` via `luanti.app` in a macOS desktop session on this machine:
```
player-teleport  player-hp  player-inventory  player-pos-and-hp
player-give-stack  save-restore  mine-bridge-outpost  homestead-save-restore
```

`capture_goldens.py` captures the 22 visual goldens; every `.golden.png` must pass the `_screenshot_is_informative` check.

---

## 4. Blockers

### 4.1 Fixed: the macOS client cannot use bare `bin/luanti`
The rendering client (screenshots / online players) needs a GL window and the full app resources. Under the current build, bare `games/luanti/bin/luanti` is a
RUN_IN_PLACE=FALSE binary; outside `.app/Contents/MacOS` it resolves resource paths incorrectly, causing:

- `Game "devtest" not found`
- `Failed to find a valid font`
- the client pops up a window and exits, and the worldmod never gets a chance to write the report / screenshot

In addition, the initial script used `singleplayer` as a regular server player name, which Luanti rejects: `Player name not allowed`.

Fixes:
- The macOS client path now uses `open -n -W build-app/out/luanti.app --args ...`.
- The client player name is now the valid `warptest`.
- The temporary world explicitly copies `worldmods/warptest`, so the global mod in the repo is still found after the app resource root changes.
- The golden capture script adds `load_mod_warptest = true` and writes each spec's `--logfile` to the temporary directory, printing key log lines on failure.

### 4.2 Run Modes (verified)
- **Headless mode**: world-type specs now run on `luantiserver` and **require no display at all** → 17/17 pass on this machine.
- **macOS app client mode**: player/visual specs run via `luanti.app` + `open -W` → 8/8 pass on this machine.
- **Turnkey golden capture script**: `python3 specs/luanti/visual/capture_goldens.py` has successfully replaced all 22 goldens with real frames.
- Linux path: `xvfb-run -a python3 -m warptest bench` uses the client path to capture goldens / run player and visual specs.

### 4.3 Environment Prerequisites (required to run real specs)
- macOS: `brew install cmake freetype gettext gmp jpeg-turbo jsoncpp libogg libpng libvorbis luajit zstd sdl2 openal-soft sqlite`; cmake can also be installed via conda.
- Linux: build-essential + cmake + engine libraries + `xvfb` + mesa (software GL).
- `LUANTI_BIN` / `LUANTI_SERVER_BIN` can point to prebuilt binaries to skip the build.

---

## 5. Key Decision Log (each with rationale)

### D1. Use the "native checkpoint" mode instead of the Web mode
Luanti is a C++ engine, closest to OpenRA/jynew/pal3: Python↔engine communicate via request/report JSON, and the profile maps stages by check prefix.
→ Reuses the existing contract with no new scoring protocol.

### D2. Checkpoint state surface = player + voxel world + node metadata + mod storage (user choice)
Aligned with the "deep state" of existing native games, covering the world/inventory/storage semantics specific to voxel engines; the player surface and visuals are included at the user's request.

### D3. Visual oracle uses "in-client real screenshots" (user choice), via C++ engine instrumentation
Aligned with OpenRA's launch-game screenshots. Adds a marker-wrapped, idempotent, additive `warptest_capture.inc` that captures a frame in the `game.cpp`
render loop at `warptest_screenshot_frame` and then exits.
→ Isomorphic to OpenRA; reversible and refreshable; verified to be compiled into the binary.

### D4. Worldmod triggers: `on_joinplayer` (with a player) + `on_mods_loaded` 8s fallback (no player)
When a client session has an online player, on_joinplayer runs the checkpoint; when a headless server has no player, the fallback branch runs world-type checkpoints.
→ The same mod supports both run modes.

### D5. `secure.enable_security = false` (critical bug fix)
The Luanti mod security sandbox **forbids `io.open` on arbitrary paths** by default, so the worldmod could not read the request → every run failed.
Security is disabled in the temporary world's conf, with `secure.trusted_mods = warptest`.
→ Only after this fix are headless reports written correctly; this was a real blocking bug.

### D6. macOS build chain fixes (found during the real build)
- `-DCMAKE_FIND_FRAMEWORK=LAST` + `-DENABLE_GETTEXT=TRUE`: aligned with `doc/compiling/macos.md`.
- `-DCMAKE_CXX_FLAGS=-DLIBICONV_PLUG`: conda's cmake injects `-I/opt/miniconda3/include`, whose GNU `iconv.h` remaps
  `iconv_open` to `libiconv_open`, which the SDK does not export → link failure; `LIBICONV_PLUG` makes it use the plain symbols.
- `-DCMAKE_EXE_LINKER_FLAGS=-lbz2` + disable leveldb/redis/postgres: aligned with CI linking + fewer optional dependencies.
- `_with_homebrew_prefix`: adds `brew --prefix` and keg-only (openal-soft/gettext) paths to `CMAKE_PREFIX_PATH`.
→ All of these have been written back into the adapter, making it actually buildable on macOS.

### D7. Install cmake/ninja via conda
Bottles for macOS Tahoe(26) are missing from the mirror, and ghcr.io is throttled to ~9KB/s; system pip cmake times out, and homebrew py3.14 has no wheel.
→ conda-forge cmake/ninja is the most reliable; engine libraries still come from Homebrew. (These are build tools only, not runtime dependencies.)

### D8. Add a "headless luantiserver mode" (the key architectural decision of this round)
`_spec_needs_client(spec)`: if a spec involves an online player (`target.player` / `luanti_set_player_*` / `luanti_give_item` /
`luanti_player_*` / `luanti_inventory_contains`) or the oracle policy requires screenshots, it uses the client; otherwise it uses `bin/luantiserver` (no display).
→ Lets 17/17 world-only specs actually pass in display-less environments (CI/SSH/this agent), directly mitigating the GL blocker; player/visual specs still use the client.

### D9. Fix `_needs_visual_capture` unnecessarily launching the client
Original logic: any spec declaring a `visual_oracle` forced a screenshot in run_checkpoint → the GL client was launched even under the semantic strategy.
Changed to capture screenshots only when `self._visual_capture` is set (the oracle policy actually requires visuals).
→ Otherwise pure world specs such as `set-node` would try to open a window under semantic and fail.

### D10. Goldens made real via the turnkey script
The 22 goldens were initially Pillow placeholders so the schema/semantic pipeline would not be blocked; they were then all replaced with real rendered frames
via `capture_goldens.py` in a macOS desktop session, and pass the informative check.

### D11. `replay_from_start` intentionally excluded
Same as jynew/pal3: deterministic "source → deep state replay" for the voxel engine is not implemented; checkpoints are synthesized by the worldmod and persisted in the engine's world format.

### D12. macOS client path uses `.app + open`
Bare `bin/luanti` loses the app resource paths and cannot find devtest/font; therefore all macOS client specs use
`open -n -W build-app/out/luanti.app --args ...`. Linux still uses `xvfb-run -a ./bin/luanti ...`.

---

## 6. Current Status Quick Reference

| Item | Status | Notes |
|----|------|------|
| Integration completeness (code/schema/synthesis/tests/docs) | ✅ | Isomorphic to jynew/pal3, complete |
| Real engine build | ✅ | bin/luanti + bin/luantiserver + luanti.app |
| C++ instrumentation compiled into binary | ✅ | strings matches warptest_screenshot_* |
| Headless spec runs (17 world-type) | ✅ | Verified with `warptest run` on this machine |
| macOS app spec runs (8 player/save-type) | ✅ | Verified with `warptest run` on this machine |
| All luanti specs | ✅ | 25/25 success |
| Unit tests | ✅ | tests/test_core.py + tests/test_evo.py 139 passed |
| 22 visual goldens made real | ✅ | 22/22 informative |
| Player/visual specs in display-less environments | ⚠️ Display required | Linux+xvfb or macOS desktop session |

---

## 7. Reproduction Commands

```bash
# Read-only inspection
python3 -m warptest inspect games/luanti

# Run world-type specs without a display (works on this machine)
python3 -m warptest run specs/luanti/set-node.yaml --repo games/luanti

# Run player/visual specs in a macOS desktop session
python3 -m warptest run specs/luanti/player-hp.yaml --repo games/luanti

# Effort / paper tables (any environment)
python3 -m warptest effort
python3 -m warptest paper-tables

# Unit tests
python3 -m pytest tests/test_core.py -q -k luanti

# Recapture visual goldens (unlocked macOS desktop Terminal)
python3 specs/luanti/visual/capture_goldens.py
# Linux:
xvfb-run -a python3 -m warptest bench
```

---

## 8. Remaining Work / Next Steps

1. (Optional) Repeat the 25/25 spec and visual oracle verification on Linux+xvfb to cover the non-macOS client path.
2. (Optional) Automate the `luanti.app` build/install in the adapter to reduce manual build steps before the first macOS client run.
3. (Optional) `games/luanti` currently contains build artifacts (`bin/`, `build/`, `build-app/`, `mods/warptest/`, a modified `src/client/game.cpp`);
   for a clean checkout: `cd games/luanti && git checkout -- src/client/game.cpp && rm -rf build build-app bin mods/warptest`
   (the harness will reinstall the instrumentation and rebuild as needed on the next run).
