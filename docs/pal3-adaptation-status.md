# Pal3.Unity Benchmark Adaptation: Status, Blockers, and Decision Log

> Last updated: 2026-06-17
> Scope: integrate Pal3.Unity (a remake of Chinese Paladin: Sword and Fairy 3 (PAL3), Unity 6000.1.10f1 + C#) into the WarpTest benchmark as a new game,
> strictly mirroring jynew, the other already-integrated native Unity game (as well as OpenRA).

---

## 1. One-Sentence Summary

Pal3.Unity is **fully integrated** into WarpTest (instrumentation / runtime / profile / discovery / schema / strategies /
synthesis / effort / visual / oracle-policy / specs / benchmarks / tests / packaging), and end-to-end runs with real PAL3 data have been completed on this machine.

The currently available resources are mounted at:
`$HOME/Library/Application Support/Jackil/PAL3/PAL3 -> $HOME/Downloads/PAL3`
and
`$HOME/Library/Application Support/unity.Jackil.PAL3/PAL3 -> $HOME/Downloads/PAL3`.
With Unity `6000.1.10f1`
(`/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity`), both headed and batch/headless PAL3 specs run successfully.

Conclusion: **all historical blockers and non-optional wrap-up items have been resolved**. Work is no longer blocked on Unity, PAL3 data mounting, CPK reading, C# hook compilation, semantic report generation, visual goldens, or PAL3 coverage verification.
All 25 specs now declare `visual_oracle`, and real Unity-rendered goldens `specs/pal3/visual/*.golden.png` have been captured.

---

## 2. Architecture (mirroring jynew: Unity batch mode + JSON file IPC, operating Managers inside play mode)

```
specs/pal3/*.yaml
  └─ runner.run_spec
       ├─ ensure_pal3_instrumentation   installs C# WarptestCheckpointCommand.cs into
       │                                Assets/Scripts/Pal3.Game/Command/ (idempotent)
       └─ Pal3RuntimeAdapter.run_checkpoint
            ├─ _find_unity_executable     locates Unity (UNITY_EDITOR_PATH / Hub glob)
            ├─ writes request.json (target/validations/actions/assertions)
            ├─ Unity -batchmode -executeMethod Pal3.Game.Command.WarptestCheckpoint.Run
            │    └─ enters play mode → waits for ServiceLocator managers to be ready
            │       → synthesizes/restores the checkpoint via the game's own console commands
            │       → reads Managers for validate/assertion → writes report.json (+ optional screenshot)
            └─ parses report.json → Pal3Profile maps stages by check prefix
```

- **Checkpoint contract** (isomorphic to jynew): Python writes `request.json` → `WarptestCheckpoint` handles it inside Unity → writes `report.json`
  (`checks[]`, mapped by name prefix).
- **Check prefix mapping**: `target.state_synthesized`/`target.save_loaded` → restoration;
  `target.validate.*` → validation; `action[<type>]...` → smoke; `assertion[<type>]...` → oracle.
- **Key difference from jynew**: jynew's `GameRuntimeData` can be synthesized without entering play mode; all of Pal3's mutable state lives in the Managers registered with
  `ServiceLocator`, which **exist only once the game is running (play mode)**, so Pal3's hook **must always enter play mode**.

---

## 3. Completed (All Landed)

### 3.1 Game-Side Instrumentation (installed on demand; template shipped with the package)
| File | Purpose |
|------|------|
| `warptest/instrumentation/pal3/WarptestCheckpointCommand.cs` | test-gated Unity batch-mode hook: `#if UNITY_EDITOR` guard; `static Run()` parses `--warptest-request/--warptest-report`; enters play mode across domain reloads via EditorPrefs; uses a `MonoBehaviour` coroutine to drive "wait for ready → synthesize/restore → validate → actions → assertions → screenshot → write report → exit"; optional camera/`ScreenCapture` screenshot + informative check |

Installation target: `{repo}/Assets/Scripts/Pal3.Game/Command/WarptestCheckpointCommand.cs` (idempotent; refuses to overwrite a same-named file not created by WarpTest).

### 3.2 Core Python Modules
| File | Changes |
|------|------|
| `warptest/pal3_runtime.py` (new) | `ensure_pal3_instrumentation`, `Pal3RuntimeAdapter` (locate Unity / write request / batch launch / parse report / screenshot metadata, reusing jynew's `_find_unity_executable`/`_capture_metadata`/`_screenshot_is_informative`) |
| `warptest/profiles.py` | `Pal3Profile` (`benchmark=pal3`, `default_strategy=pal3_console_save_validate`, `checkpoint_validity=save-restorable`, `run()` maps stages by check prefix) + registered in `profile_for` |
| `warptest/discovery.py` | `_profile_for`/`_hook_name`/`hook_available`/capability guard `is_pal3`/`_launch_command`/`storage_policy_for`/`adapter_layers_for`/`instrumentation_mode`/`semantic_schema_for` + `_semantic_schema_pal3` |
| `warptest/runner.py` | `resolve_repo` adds `pal3→games/Pal3.Unity`, `run_spec` adds a `Pal3Profile` branch + `ensure_pal3_instrumentation`, `prepare_repo` installs the instrumentation |
| `warptest/strategies.py` | `PAL3_REPLAY_UNSUPPORTED_REASON` + `replay_from_start` exclusion branch |
| `warptest/spec_schema.py` | 10 `pal3_*` action types + 9 assertion types (including `pal3_set_favor`/`pal3_favor_gte`/`no_pal3_utility_errors`) |
| `warptest/synthesis.py` | `SOURCE_VOCABULARY[pal3]`, `BENCHMARK_CONTRACTS[pal3]`, `_payload_for_spec` branch (`pal3_console_checkpoint`), 10 `_TEMPLATES` entries |
| `warptest/oracle_policy.py` | `adaptive_risk_reason` lists pal3/Pal3Profile under `high_risk_runtime` |
| `warptest/visual.py` | `_requires_unity_capture` / `_heavy_visual_runtime` include pal3 |
| `warptest/visual_experiment.py` | capture_failure detection includes pal3 (same unity_capture requirement as jynew) |
| `warptest/effort.py` | Registers the pal3 profile/specs/repo + counts `pal3_runtime.py` as reusable core |
| `pyproject.toml` | `package-data` adds `instrumentation/pal3/*` |

### 3.3 Evaluation Set
- `specs/pal3/*.yaml` - **25** specs: story/script variables 4, scene/map 4, party 4, inventory items 3, money 3, world map regions 2, favor 2, player position 1, combined save/load 2; all carry a real `visual_oracle`.
- `specs/pal3/visual/*.golden.png` - **25** real PAL3 Unity screenshot goldens, all passing the informative check.
- `specs/pal3/visual/capture_goldens.py` - PAL3-specific capture script; a golden is overwritten only after the spec succeeds semantically, the screenshot source is `unity_capture`, and the PNG is informative.
- `benchmarks/core/public/tasks.yaml` + `hidden/oracles.yaml` - **10** core-bench public tasks + hidden oracle mappings (the remaining 15 specs are used for `bench`/smoke).
- The specs of the 10 tasks promoted to core are **aligned verbatim** with `synthesis._TEMPLATES`, ensuring that the deterministic agent can reproduce them exactly (answer-key upper bound).

### 3.4 Vocabulary (`pal3_*`, mirroring `jynew_*`)
- **actions**: `pal3_set_story_var`, `pal3_load_scene`, `pal3_add_team_member`/`pal3_remove_team_member`, `pal3_add_money`, `pal3_add_item`, `pal3_enable_world_region`, `pal3_set_favor`, `pal3_save`, `pal3_load_save`.
- **assertions**: `pal3_story_var_equals`, `pal3_scene_is`, `pal3_team_contains`, `pal3_team_count`, `pal3_item_count`, `pal3_money_gte`, `pal3_world_region_enabled`, `pal3_favor_gte`, `no_pal3_utility_errors`.
- Underneath, these map to the game's real commands: `ScriptVarSetValueCommand` / `SceneLoadCommand` / `TeamAddOrRemoveActorCommand` / `InventoryAddMoneyCommand` / `InventoryAddItemCommand` / `WorldMapEnableRegionCommand` / `FavorAddCommand`, and read `UserVariableManager`/`SceneManager`/`TeamManager`/`InventoryManager`/`WorldMapManager`/`FavorManager`.

### 3.5 Tests and Local Verification
- `tests/test_core.py` adds 8 pal3 cases: inspect/profile + semantics, replay-unsupported, bundled-specs-schema, resolve_repo, instrumentation installation, Unity version mismatch precheck, deterministic synthesis, profile-run stage mapping.
- PAL3 itself adds **25** specs and **10** core-bench public tasks + hidden oracle mappings; global spec/task counts will keep changing with other workstreams such as Luanti/OpenRA and are not used in this document as a criterion for whether PAL3 is unblocked.
- Verification this round: `python3 -m pytest tests/test_core.py -q -k pal3` passes (8 passed, 85 deselected, 25 subtests passed); `python3 -m pytest tests/test_visual.py -q -k pal3` passes (1 passed, 48 deselected, 25 subtests passed).
- Live runs this round: headed visual capture, batch/headless runs of all 25 specs, and the PAL3 core-bench 10-task subset have all produced success evidence; see §6.

---

## 4. Blockers and Unblocking Status

### 4.0 Summary: Are All Blockers Resolved?
| Historical blocker | Current status | Evidence / resolution |
|---|---|---|
| Unity version path mismatch | ✅ Resolved | Uses `/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity`; the runtime also has a `probe.unity_version_mismatch` precheck |
| Missing PAL3 original CPK data | ✅ Resolved | Resources moved to `$HOME/Downloads/PAL3` and mounted via a persistentDataPath symlink |
| Cloud-drive/compressed placeholders in `$HOME/Documents/PAL3` caused CPK reads to hang | ✅ Resolved | CPKs under the Downloads directory no longer carry the `compressed,dataless` flags; `basedata/music/scene` mount normally |
| `Assets/StreamingAssets/PAL3` symlink caused Unity AssetDatabase to time out scanning large resources | ✅ Resolved | CPKs are no longer placed in `Assets`; a persistentDataPath symlink is used instead |
| C# hook compile/runtime issues | ✅ Resolved | The hook now enters play mode, waits for managers, and writes the PAL3 report |
| Semantic end-to-end checkpoint report | ✅ Resolved | headed and headless team/scene specs all succeed |
| `compare`/benchmark PAL3 subset | ✅ Resolved | `compare.json` produced; core-bench PAL3 subset 10/10 runtime validation success, 10/10 fake-visual success |
| Visual goldens | ✅ Completed | 25/25 real PAL3 Unity goldens captured and backfilled into `visual_oracle`; all PNGs informative |

### 4.1 Former Main Blocker (Resolved): Licensed PAL3 Original Data (CPK)
The PAL3 remake **ports only the engine/logic code** and does not include the copyrighted original game data (`.cpk`: textures, models, scenes, scripts).
The resources on this machine are now in `$HOME/Downloads/PAL3` and mounted via an `Application.persistentDataPath/PAL3` symlink, avoiding placing large CPKs in the repository or `Assets/StreamingAssets`.

Previously, several CPKs under `$HOME/Documents/PAL3` carried the macOS `compressed,dataless` flags, and Unity `FileStream` reads hung at the system level, with the log stopping at:
`[GameResourceInitializer] Mounting CPK file: <basedata/basedata.cpk>`.
After moving to `$HOME/Downloads/PAL3`, the CPK file flags reverted to those of normal local files, and `basedata/music/scene` all mount.

### 4.2 Environment Notes: Unity Version and Resource Mount Location
- Requires Unity **6000.1.10f1** (`ProjectSettings/ProjectVersion.txt`); the current live-run path is `/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity`.
- Do not symlink `$HOME/Downloads/PAL3` to `games/Pal3.Unity/Assets/StreamingAssets/PAL3`: Unity AssetDatabase scans the large CPKs, and the first import easily times out.
- Keeping the persistentDataPath symlink mount is recommended; PAL3 original resources are not added to or committed to the repository.
- Machines without Unity: `run` records `probe.unity_not_found`; a Unity version mismatch records `probe.unity_version_mismatch` before the project is launched.
- The Unity Console may still show online-service errors related to `UnityConnectWebRequestException`/access tokens; these have been verified not to block local PAL3 resource loading or WarpTest report generation.

### 4.3 Visual Goldens: Completed
All 25 specs now have `visual_oracle`, with goldens in `specs/pal3/visual/*.golden.png`. Capture is done via
`specs/pal3/visual/capture_goldens.py`: the script calls `Pal3RuntimeAdapter(..., visual_capture=True, headless=False)` directly,
and overwrites a golden only when the spec succeeds semantically, the screenshot source is `unity_capture`, and the PNG passes the informative check. No placeholder images are used.

---

## 5. Key Decision Log (Each with Rationale)

### D1. Adopt the "Native Checkpoint" Mode, Mirroring jynew (Not Web Mode)
Pal3.Unity is a Unity/C# engine and closest to jynew: Python↔engine uses request/report JSON, and the profile maps stages by check prefix.
→ Reuses the existing contract, with no new scoring protocol.

### D2. Variant Locked to **PAL3** (User Choice)
Pal3.Unity supports two variants, PAL3 and PAL3A, via Scripting Define (data directories, managers, and commands all differ: only PAL3A has `TaskManager`, and only PAL3 has the Long Kui/Hua Ying modes). This work targets only the default PAL3.

### D3. Scope = **Full-Stack Parity with jynew** (User Choice full_stack)
Ensures that inspect/schema/unit tests and real Unity semantic runs all close the loop; PAL3 original resources remain locally mounted and out of the repository. No padding just to obtain a "green live run".

### D4. Checkpoints = Synthesized/Restored via the Game's Own Console Commands
Pal3 **saves are themselves "sequences of console command text"** (`SaveManager` writes `slot_{n}_v1.txt`), and `DevCommands` story jumps use the same channel.
→ Synthesis/restoration directly reuses `Pal3.Instance.Execute(ICommand)` / `DebugLogConsole.ExecuteCommand`, which is isomorphic to the game's own mechanism and the most trustworthy.

### D5. C# Hook **Always Enters Play Mode** (Key Difference from jynew)
jynew's `GameRuntimeData` can be synthesized outside play mode; Pal3's state lives entirely in Managers registered with `ServiceLocator` and **does not exist outside play mode**.
→ The Pal3 hook always enters play mode via `EditorApplication.isPlaying=true` + `InitializeOnLoadMethod`/`EditorApplication.update` (resuming across domain reloads), and waits for the managers to be ready before operating.

### D6. Drive the Async Flow with `MonoBehaviour` Coroutines Rather Than UniTask
The jynew hook depends on `Cysharp.Threading.Tasks`; to avoid assuming that the Pal3.Game assembly references UniTask, a temporary GameObject + `IEnumerator` coroutine is used for "wait for scene load/wait for ready".
→ Zero extra dependencies and a more stable compilation surface.

### D7. Namespace Disambiguation (Compile Correctness)
`Pal3` is both the root namespace and the game's main class, so writing `Pal3.Instance` directly is ambiguous; Unity also has `UnityEngine.SceneManagement.SceneManager`.
→ The aliases `using PalApp = global::Pal3.Game.Pal3;` and `using PalSceneManager = global::Pal3.Game.Scene.SceneManager;` fully disambiguate.

### D8. Unity Project Root = Repository Root (Unlike jynew's `jyx2/` Subdirectory)
Pal3.Unity's `Assets/` is at the repository root, so `Pal3RuntimeAdapter._find_project_path` returns the repository root; the C# entry point is `Pal3.Game.Command.WarptestCheckpoint.Run`.

### D9. Benchmark Identifier Unified as `pal3`
The spec `benchmark:`, the `SOURCE_VOCABULARY`/`BENCHMARK_CONTRACTS` keys, the `resolve_repo` candidate, the three `effort` tables, and the profile class attributes all use `pal3` (lowercase, matching jynew's naming style).

### D10. Dedicated Action/Assertion for "Favor"; Position Goes Through `target.validations`
The 25 specs need sufficient breadth of state surface, so `pal3_set_favor` / `pal3_favor_gte` (PAL3 `FavorManager` semantics) are added.
Player position (`ActorSetWorldPosition`/`Facing`/`NavLayer`) is a `target` synthesis field, validated with `target.validations` before smoke, with no separate assertion type (mirroring jynew's use of `target.validations`; precise coordinate assertions are hard in batch mode).

### D11. Handling Delta Commands (Money/Favor)
`InventoryAddMoneyCommand` / `FavorAddCommand` are **incremental**; the hook first resets state with `ResetGameStateCommand` and then adds up to the target value.
Favor `GetFavorByActor` includes `BASE_FAVOR_AMOUNT=20`, so the assertion `int_value` already accounts for the base value (e.g., +50 → assert ≥70).

### D12. The 10 ★ Specs Are Aligned Verbatim with the Synthesis Templates
The target/actions/assertions of the 10 core tasks, such as `pal3-team-join`/`pal3-save-restore`, are identical to `_TEMPLATES`.
→ The deterministic agent (answer-key upper bound) can reproduce the hidden specs exactly, and strict scoring can reach the upper bound.

### D13. `scene-load-tangjiabao` → `scene-load-binhua`
Scene IDs are not fabricated: the real scene `q02/q02` (Binhua), confirmed to exist in the source `DevCommands`, is used instead; Yuzhou uses `q01/b`.

### D14. `replay_from_start` Intentionally Excluded
Same as jynew: deterministic "source → deep-state replay" in Unity batch mode is not implemented; checkpoints are synthesized via the hook and persisted by game saves (console command text).

### D15. Visual Specs Use Real PAL3 Unity Goldens
See §4.3: real PAL3 data and stable semantic runs are in place, and visual goldens are uniformly captured by headed Unity; no placeholder images are committed, and live frames are not forged backward from goldens.

### D16. PAL3 Item Specs Use Real Database IDs
Live runs revealed that PAL3 item database IDs start in the `6001` range, and the earlier `1/2/5` were ignored by `InventoryManager`. The three item specs now use
`6001/6002/6005`, and the `pal3-item-add` deterministic synthesis template was updated accordingly. The C# hook now reads back the inventory after `pal3_add_item`,
and fails with a sample of real item IDs if the count is unchanged, preventing invalid IDs from being falsely reported as action success.

---

## 6. Current Status Quick Reference

| Item | Status | Notes |
|----|------|------|
| Integration completeness (code/schema/synthesis/tests/packaging) | ✅ | Isomorphic to jynew, complete |
| Unit tests | ✅ | `tests/test_core.py -q -k pal3`: 8 passed, 25 subtests passed; `tests/test_visual.py -q -k pal3`: 1 passed, 25 subtests passed |
| `inspect games/Pal3.Unity` | ✅ | Pal3Profile / Pal3RuntimeAdapter / `--warptest-checkpoint` / `Saves/slot_` / `in_place_test_gated` / semantic surface correct |
| `compare` three ablation modes | ✅ | `reports/warptest/20260617T043803Z/compare.json`; `jump_and_validate` succeeds, `replay_from_start` reports unsupported |
| `probe.unity_not_found` graceful failure | ✅ | Verified deterministically |
| C# instrumentation hook | ✅ | Written and shipped with the package; installed into the game repo on demand at runtime |
| headed end-to-end live run | ✅ | `team-join-single`: `reports/warptest/20260617T043600Z`; `scene-load-yuzhou`: `reports/warptest/20260617T043636Z` |
| batch/headless end-to-end live run | ✅ | `team-join-single`: `reports/warptest/20260617T043712Z`; `scene-load-yuzhou`: `reports/warptest/20260617T043730Z` |
| full PAL3 spec coverage | ✅ | `reports/warptest/20260617T052629Z/pal3-full-semantic.json`; 25/25 success |
| core-bench PAL3 subset | ✅ | `reports/warptest/20260617T053055Z/pal3-core-subset/run/core-synthesis.json`; 10/10 runtime validation success, 10/10 fake-visual success |
| Visual goldens | ✅ | `reports/warptest/20260617T053055Z/pal3-final-verification.json`; 25/25 visual_oracle, 25/25 informative golden |

---

## 7. Reproduction Commands

```bash
# Read-only inspection
python3 -m warptest inspect games/Pal3.Unity

# Run a single spec (resources on this machine are currently mounted at $HOME/Downloads/PAL3)
UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
python3 -m warptest run specs/pal3/team-join-single.yaml --repo games/Pal3.Unity

# Compare the three ablation modes
UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
python3 -m warptest compare specs/pal3/team-join-single.yaml --repo games/Pal3.Unity

# Effort / paper tables (any environment)
python3 -m warptest effort
python3 -m warptest paper-tables

# Unit tests
python3 -m pytest tests/test_core.py -q -k pal3

# Scene specs
UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
python3 -m warptest run specs/pal3/scene-load-yuzhou.yaml --repo games/Pal3.Unity

# Recapture real PAL3 visual goldens (requires a logged-in desktop session)
UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
python3 specs/pal3/visual/capture_goldens.py
```

---

## 8. Wrap-Up Status / Next Steps

1. **Capture visual goldens**: ✅ Completed. 25/25 real PAL3 rendered screenshots have been generated and backfilled into `visual_oracle`.
2. **Expand live-run coverage**: ✅ Completed. All 25 PAL3 specs succeed semantically; the 10 PAL3 core tasks succeed on runtime + fake-visual.
3. **Reproducible delivery notes**: ✅ Completed. Both the README and this document record the Unity version, the persistentDataPath symlink, and the constraint that PAL3 original CPKs are not added to the repository.
4. (Optional) Register `games/Pal3.Unity` as a git submodule (requires a remote URL; it is currently a local nested git directory, and since the framework resolves by directory, this has no effect).
5. (Optional) Cleanup: `games/Pal3.Unity` now contains the on-demand-installed instrumentation `Assets/Scripts/Pal3.Game/Command/WarptestCheckpointCommand.cs`; for a clean checkout it can be deleted (the harness will reinstall it on demand on the next run).
