# Commands, Core Bench, and State Loop

Command reference for the `warptest` CLI (the StatePortal harness), the
execution-graded Explore-Hard variant of `core-bench`, and the `state-loop`
Gen+Test loop. The registered paper experiments (`p5-scale`) are documented in
[`reproduce.md`](reproduce.md); runtime prerequisites are in [`runtimes.md`](runtimes.md).

- [Command reference](#command-reference)
- [Explore-Hard variant](#explore-hard-variant-de-leaked-all-76-core-tasks)
- [State Loop](#state-loop)

## Command reference

```bash
# Detect runtime/framework capabilities of a repository
python3 -m warptest inspect games/2048
python3 -m warptest inspect games/SpaceRPG
python3 -m warptest inspect games/DatingSim
python3 -m warptest inspect games/jupyterlab
python3 -m warptest inspect games/LibreOffice
python3 -m warptest inspect games/luanti
python3 -m warptest inspect games/Pal3.Unity

# Restore a checkpoint from a spec, validate it, run the smoke action, write a report
python3 -m warptest run specs/2048/merge-left.yaml --repo games/2048
python3 -m warptest run specs/spacerpg/playable-flight.yaml --repo games/SpaceRPG
python3 -m warptest run specs/love/resource-action.yaml --repo games/DatingSim
python3 -m warptest run specs/jupyterlab/text-file-edit.yaml --repo games/jupyterlab
python3 -m warptest run specs/libreoffice/writer-board-report-toc.yaml --repo games/LibreOffice
python3 -m warptest run specs/luanti/set-node.yaml --repo games/luanti
UNITY_EDITOR_PATH=/Applications/Unity/Unity-6000.1.10f1/Unity.app/Contents/MacOS/Unity \
  python3 -m warptest run specs/pal3/team-join-single.yaml --repo games/Pal3.Unity

python3 -m warptest compare specs/2048/merge-left.yaml --repo games/2048
python3 -m warptest bench --prepare
python3 -m warptest bench --headed
python3 -m warptest bench --compare
python3 -m warptest effort
python3 -m warptest paper-tables

# Core bench (checkpoint synthesis from public issues)
MODELSCOPE_API_KEY=... python3 -m warptest core-bench
MODELSCOPE_API_KEY=... python3 -m warptest core-bench --no-explore
python3 -m warptest core-bench --agent deterministic
MODELSCOPE_API_KEY=... python3 -m warptest core-bench --execute
MODELSCOPE_API_KEY=... python3 -m warptest core-bench --grading explore-execute
# Semantic-only (skip visual) for runtimes whose goldens need a desktop GL session:
MODELSCOPE_API_KEY=... python3 -m warptest core-bench --grading explore-execute \
  --agent deterministic --oracle-policy semantic
# Offline plumbing check (no model / no visual key):
WARPTEST_VISUAL_JUDGE_FAKE=correct python3 -m warptest core-bench \
  --grading explore-execute --agent deterministic --oracle-policy fake-visual
# Model leaderboard through a gateway; mix providers with `@gateway` tags:
GATEWAY_API_KEY=... python3 -m warptest core-bench \
  --base-url http://gateway.example/v1 --api-key-env GATEWAY_API_KEY \
  --models "glm-5.2 deepseek-v4-flash kimi-k2.6"
MODELSCOPE_API_KEY=... GATEWAY_API_KEY=... python3 -m warptest core-bench \
  --models "deepseek-ai/DeepSeek-V4-Flash:DashScope gpt-5.4@gateway claude-opus-4-7@gateway"

# Other benches
python3 -m warptest segment-bench --agents segment_probe,smoke_only --parallel
python3 -m warptest gui-inspect games/SpaceRPG
MODELSCOPE_API_KEY=... python3 -m warptest gui-agent-bench
# Default C3 set includes browser/engine tasks plus OpenRA gameplay production.
MODELSCOPE_API_KEY=... python3 -m warptest fuzz-bench --model "modelA"
MODELSCOPE_API_KEY=... python3 -m warptest fuzz-bench --tasks benchmarks/state_loop/tasks_openra.yaml --model "modelA"
python3 -m warptest evo-bench --memory-baseline-suite --agent deterministic --benchmarks luanti SpaceRPG --iterations 2
MODELSCOPE_API_KEY=... python3 -m warptest evo-bench --task-scope all-core --memory-mode repository \
  --memory-in path/to/evo-memory.json --memory-backend stateportal-anchor --memory-scope both
MODELSCOPE_API_KEY=... python3 -m warptest model-bench
MODELSCOPE_API_KEY=... python3 -m warptest model-bench --headed
python3 -m warptest visual-experiment --sample-count 50 --headed

# State loop
WARPTEST_VISUAL_JUDGE_FAKE=correct python3 -m warptest state-loop --agent deterministic
MODELSCOPE_API_KEY=... python3 -m warptest state-loop --model "modelA"
```

## Explore-Hard variant (de-leaked, all 76 core tasks)

`core-bench --grading explore-execute` is a harder, execution-graded variant over
**all 76 core issues across the 10 benchmarks** (2048 / SpaceRPG / DatingSim / OpenRA /
JupyterLab / LibreOffice / Blender / jynew / PAL3 / Luanti) in
[`benchmarks/core/public/tasks_explore.yaml`](../benchmarks/core/public/tasks_explore.yaml).

**What changes vs strict `core-bench`:**

- **De-leaked issues.** Mechanism-level tokens the model can discover from the
  repository are removed from the prose (checkpoint strategy names, validity classes,
  hook/method names, framework action types like `luanti_set_node` /
  `openra_validate_map` / `libreoffice_replace_text`, and repo-discoverable game ids — OpenRA actor codes
  like `e1`/`2tnk`, luanti node ids like `basenodes:stone`, character ids like
  `char_1`, scenario/mission ids). The designer's intent and all chosen state
  values are kept, as are ids a model cannot recover from the repo (e.g. luanti's
  runtime-registered `stairs:slab_*` and pal3 ambiguous scene codes stay literal). The
  dev-only guard `python3 scripts/_check_explore_issues.py` asserts, per task, that
  intent is still recoverable, mechanism tokens are absent, and every stripped repo
  token is grep-able in the game repo.
- **Combined grading.** Headline success = the model's own `target` intent is recovered
  **and** the checkpoint executes on the real runtime **and** the hidden visual oracle
  passes. The old structural-only grade is still reported as `grading_structural`.
- **Blocked runtimes are neutral.** When the runtime/environment cannot execute a
  benchmark on this host at all (OpenRA unbuildable without mono, an engine needing an
  unlocked desktop GL session, an uninstalled binary, or LibreOffice without matching
  pyuno/soffice configuration), the task is graded `blocked` (counted in
  `summary.explore_execute_blocked`) instead of `failure`, so environment gaps never
  inflate the failure count.
- **Semantic-only fallback.** If the visual audit is skipped by the oracle policy
  (`--oracle-policy semantic`), the grade collapses to target + execution
  (`graded_on: "execution"`). Use this for runtimes whose committed goldens need a
  desktop GL session to regenerate (the luanti voxel frames), so a placeholder/degenerate
  golden never decides the score.
- Selecting `--grading explore-execute` forces `--execute`, coerces a semantic/adaptive
  oracle policy to `visual`, and (unless `--tasks` is overridden) defaults to the
  de-leaked task file. It is per-task execution only, so it is not supported with
  `--models`; run one `--model` at a time.

Per-runtime execution prerequisites are listed in
[`runtimes.md`](runtimes.md#per-application-prerequisites).

**Real vision judge (via a gateway gpt-4o).** The visual oracle needs a vision model.
Point the judge at the gateway and cap the image side to avoid the gateway's
base64-as-tokens TPM limit (a full 1280x800 PNG is ~700k "tokens"; 768 keeps calls small):

```bash
export GATEWAY_API_KEY=...                     # generation model key (@gateway routing)
export GATEWAY_END_USER=<your-user-name>       # sent as X-End-User
export WARPTEST_VISUAL_JUDGE_API_KEY=$GATEWAY_API_KEY
export WARPTEST_VISUAL_JUDGE_BASE_URL=http://gateway.example/v1
export WARPTEST_VISUAL_JUDGE_MODEL=gpt-4o      # gpt-5.4-mini / Qwen3-VL are weaker judges here
export WARPTEST_VISUAL_JUDGE_MAX_IMAGE_SIDE=768

# One generation model at a time; @gateway routes it through the gateway with X-End-User.
python3 -m warptest core-bench --grading explore-execute \
  --agent llm --model "gpt-5.4@gateway" --max-explore-steps 20 --oracle-policy visual
```

To sweep several models, loop the command over `gpt-5.4 claude-sonnet-5 claude-opus-4-8
gemini-3.5-flash deepseek-v4-flash` (each writes its own timestamped report under
`reports/warptest/`). Judge-model choice matters: gpt-4o compares live-vs-golden
holistically; smaller VL judges over-apply rubric must-items and produce false negatives.

**Judge-agnostic goldens (transient modals).** SpaceRPG auto-pops a story-briefing dialog
and DatingSim a random-event modal over the target UI; captured into a golden they confuse
weaker judges. `visual_oracle.capture.hide_selectors` injects a capture-time stylesheet
(`display:none`, render-only, no game-state mutation, survives React re-renders) to hide
them identically in golden regeneration and every live run. The SpaceRPG/DatingSim browser
specs set this (`.dialogue-backdrop` for SpaceRPG; `.modal-overlay:has(.event-title)` for
DatingSim, which spares the intended dialogue). Regenerate an affected golden by copying a
deterministic run's `method-runs/<task_id>/visual-post_smoke.png` over the spec's
`visual_oracle.golden_image`.

**Model-compatibility notes.** The synthesis client auto-drops `temperature` and retries
when a model rejects it (HTTP 400, e.g. some Claude/OpenAI reasoning models); otherwise the
task would silently fall back to the scaffold with zero exploration. The per-benchmark
output contract in `warptest/synthesis.py` (`BENCHMARK_CONTRACTS`) documents the full
`target` schema per benchmark (including DatingSim's `patch`/`expect` and SpaceRPG's nested
`equipment_inventory`/`scenario_id`/`mission_visible` fields) so the model knows the spec
shape it must fill from exploration — this is required for the nested-target tasks to be
solvable at all.

## State Loop

The `state-loop` command runs a **state-management driven Gen+Test loop** for
long-horizon GUI software. Instead of asking "did the model recover a hidden deep
state?" (that is `core-bench`), it asks "can we iterate the code/config + test loop
efficiently?" by reusing GUI state across test runs.

**Task format** (`benchmarks/state_loop/tasks.yaml`):

- Input: a `state_description` (the synthesis agent's sole public issue) + a
  sandboxed task repo. The issue must spell out every task-author-selected
  constant that cannot be recovered through that sandbox, including arbitrary
  fixture values, selected routes, and ordered semantic transitions. It
  intentionally does not copy adapter action names, schema
  structure, or implementation mechanics that source exploration and the
  public benchmark contract can recover. The grader representation, oracle
  implementation, and visual goldens remain private.
- Output: a checkpoint config (the method's `generated_spec`) — **config only, no source edit**.
- Eval: run → GUI `screenshot_before` / `screenshot_after` → vs. oracle goldens → True/False.

Formal C1 uses the `c1_v5` task contract: `state_description` is exactly
`Preconditions: <preconditions>\nReference steps: <reference_steps>`. Agent-S sees
only `preconditions` in Phase A and only `reference_steps` in Phase B;
there is no active public `goal_description`. Success is graded exclusively from
the hidden spec's `evaluation.schema_version: 1` contract, which contains semantic
assertions, the registered GUI/state adapters and named checks, and the visual
rubric. A missing or invalid hidden contract fails closed. Formal specs no longer
accept root `assertions` or `visual_oracle`; non-C1 legacy and model-generated specs
retain that root interface. See
[`c1_EVALUATION_CONTRACT.md`](c1_EVALUATION_CONTRACT.md) for the complete
input, evidence, evaluator, global-gate, and information-boundary split.

Each task carries an `oracle_spec` (a hidden ground-truth spec used **only** by the
grader) from which before/after golden screenshots are auto-generated and cached
(`Repo_Oracle → goldens`). A task passes iff both the before and after live
screenshots match their goldens under the visual judge (reuses `WARPTEST_VISUAL_JUDGE_FAKE`
and the global judge cache).

**State management** (`warptest/state_store.py`): a `StateSession`
keeps the dev server + browser warm across the whole loop, and a `StateStore` caches
the localStorage snapshot of a reached deep state keyed by a `(repo_commit, target,
checkpoint)` fingerprint. The report records a cold-vs-warm restore speedup per task
(observed ~1.3x–15x across 2048/SpaceRPG in the offline run; warm restore avoids
re-paying dev-server/browser startup every iteration).

**Snapshot fast-restore** (`warptest/state_store.py` + `warptest/profiles.py`): on a
fingerprint cache hit the loop re-enters the deep state by injecting a captured
runtime-state snapshot instead of replaying the multi-step restore. The snapshot is
generic per profile — `localStorage` for save-restorable (2048), the test hook's
`getState()` for injection-only (`__SPACERPG_E2E__`, `__WARPTEST_LOVE__`), re-applied via
`setState`. After injection the profile's `_validate` confirms the target was
reconstructed; on a mismatch it falls back to a full restore (correctness guard). The
report's `fast_vs_full_speedup` is an apples-to-apples (no-smoke) comparison measured
in the before-pass; offline it is ~13x on SpaceRPG playable-flight (skips the WebGL
restore), ~1x on 2048 (whose full restore is already a single localStorage reload).

**Cost + seeds** (`warptest/cost.py`): a process-scoped cost meter records every LLM
call (count, prompt/completion tokens, latency, optional `est_usd` via
`WARPTEST_LLM_USD_PER_MTOK`) and is surfaced per task + run in the report — the uniform
currency for the efficiency claim. `--seeds N` repeats each task for pass@1/pass@k and
cost variance.

Report: `reports/warptest/<ts>/state-loop.json` (+ `.md`) with per-task `result`,
`screenshot_before/after`, `oracle_goldens`, `pair_verdict`, `cost`, and
`restore_timing{cold_restore_ms, warm_restore_ms, fast_restore_ms, speedup,
fast_vs_full_speedup, fast_restore_used, fast_restore_fallback}`.

### Key design decisions

- **Config-only output.** The agent produces a checkpoint config (the existing
  `MethodCheckpoint.generated_spec`); it does not edit game source. The loop's job is
  to drive the GUI to a target state efficiently and grade it by before/after pixels.
- **Reuse the existing stack.** Synthesis (`warptest/synthesis.py`), browser runtime
  (`warptest/runtime.py`), profiles (`warptest/profiles.py`), and the visual judge
  (`warptest/visual.py`) are reused; the new surface is `state_store.py` +
  `state_loop.py` + a `state-loop` CLI subcommand + `run_visual_pair_audit`.
- **Task ids reuse core ids** so the offline `--agent deterministic` path (which
  keys templates by id) and the `--agent llm` exploration path both produce runnable
  configs.
- **Cold baseline isolation.** The cold (state-management-OFF) restore is measured in
  a throwaway session created *before* the warm session, because two synchronous
  Playwright instances cannot coexist in one thread.

### Implementation status

- **Browser warm restore + snapshot fast-restore.** `StateSession` supports
  2048/SpaceRPG/DatingSim with full warm-session reuse and snapshot fast-restore
  (inject + validate + fallback), with cost accounting and `--seeds` pass@1/pass@k.
- **Engine/native restore for the C1 task universe.** Luanti uses a persistent
  worldmod poll loop (`warptest_persistent`) that serves seq-tagged requests so one
  engine process handles many restores (skip-spawn, ~9-21x cold/warm), with a
  `skip_synthesis` live-world reuse path and an on-demand C++ screenshot hook for
  before/after frames on the rendered client. C1 also has target-only, live-semantic
  adapters for OpenRA public settings, JupyterLab, Blender, LibreOffice, jynew, and
  PAL3. Formal native sessions use a macOS process-group window guard; Luanti/OpenRA
  create their real SDL/OpenGL windows hidden. Unity remains fully headed (never
  `-batchmode` or `-nographics`) so its real GameView capture remains valid; its
  unavoidable startup progress/splash window must be minimized/hidden by the guard
  within 10 seconds and is never allowed to become active or frontmost. OpenRA and
  jynew retain separate C3 gameplay fuzz sessions (OpenRA resets a native gameplay
  world per fuzz candidate; jynew's `RunWarm` re-synthesizes `GameRuntimeData` per
  candidate without cold-starting Unity).
- **Vision GUI-agent baseline** (`warptest/gui_agent.py`, `gui-agent-bench`): a
  computer-use agent that drives from the entry screen with raw pixel `click(x,y)` /
  key actions (no hook/injection/oracle), graded on the before->after pair vs the same
  goldens, recording steps / wall-clock / LLM cost for the C1 comparison.
- **State fuzzing** (`warptest/fuzzing.py`, `fuzz-bench`): mutate a seed deep state
  toward boundary/invalid values, inject each on a warm session, and flag a defect when
  the game crashes / renders blank / emits severe console errors / soft-locks (robust
  otherwise; unbuildable mutations are rejected). The default C3 manifest is distinct
  from C1 and includes four OpenRA gameplay-production tasks; OpenRA keeps mod/map
  immutable and resets a native gameplay world between candidates.
- **Memory-backend comparison** (`warptest/memory_backends.py`,
  `evo-bench --memory-baseline-suite`): StatePortal anchor vs A-MEM vs Mem0 under the
  same frozen-train → held-out transfer protocol.
- **Full-scale orchestration** (`p5-scale`): see [`reproduce.md`](reproduce.md).
