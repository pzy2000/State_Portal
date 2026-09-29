# Reproducing the Paper

`p5-scale` runs the registered claims under one auditable configuration defined by
[`benchmarks/p5/manifest.yaml`](../benchmarks/p5/manifest.yaml). This page lists the
commands, then documents the protocol, concurrency, and provenance rules that the
run enforces.

- [Commands](#commands)
- [Visual-oracle calibration](#visual-oracle-calibration)
- [Experiment design](#experiment-design)
- [Execution protocol](#execution-protocol)
- [C1 concurrency and repository isolation](#c1-concurrency-and-repository-isolation)
- [C2 memory-baseline provenance boundary](#c2-memory-baseline-provenance-boundary)

## Commands

```bash
# C1 performance study (5 arms; 61 tasks / 10 apps / 4 models / 3 seeds)
python -m warptest p5-scale --seeds 3 --dry-run  # must print 60 invocations / 3,660 trials

# 1. Online preflight of every provider route in the manifest.
python -m warptest.c1_experiment \
  --root "$PWD" --credentials "$PWD/test_model_batch.py" \
  --manifest "$PWD/benchmarks/p5/manifest.yaml" \
  --output "$PWD/reports/warptest/c1-preflight.json"

# 2. Zero-model proofs for the six native background backends.
python -m warptest.c1_native_canary \
  --root "$PWD" \
  --output-dir "$PWD/reports/warptest/c1-native-backend-proofs"

# 3. Bind those proofs plus the paid headless browser localization check into the
#    required 10-backend c1-calibration-v4 artifact.
python -m warptest.c1_calibration --background-pixel \
  --root "$PWD" --credentials "$PWD/test_model_batch.py" \
  --manifest "$PWD/benchmarks/p5/manifest.yaml" \
  --backend-proof-dir "$PWD/reports/warptest/c1-native-backend-proofs" \
  --output "$PWD/reports/warptest/c1-calibration.json"

# Optional 10-app adaptation check: GPT-5.6 Terra + Kimi K2.6, both no-thinking,
# one seed, two tasks per app, three C1 arms (2 x 20 x 3 = 120 trials).
python -m warptest p5-scale \
  --manifest benchmarks/p5/c1_lightweight_gpt56_terra_kimi_k26.yaml \
  --dry-run --headed --c1-outer_worker 2 --c1-inner_worker 4

# 4. Formal run (credentials are AST-parsed from test_model_batch.py; never imported).
caffeinate -dimsu python -m warptest p5-scale --seeds 3 --aggregate --headed --resume \
  --c1-outer_worker 2 --c1-inner_worker 4 \
  --preflight-artifact reports/warptest/c1-preflight.json \
  --calibration-artifact reports/warptest/c1-calibration.json \
  --report-dir reports/warptest/c1-formal
# This configuration caps outer=2 and inner=4 (8 task lanes). The CLI defaults are
# outer=8 and inner=4. --c1-workers is a deprecated alias for --c1-outer_worker;
# do not pass the alias and replacement together.

# C2 memory transfer only: one audited seed per model, three model jobs in parallel.
# Each model/seed/backend receives isolated report + frozen-memory files; runtime
# access to a shared game checkout is serialized per benchmark repository.
python -m warptest p5-scale --arms c2_memory_baseline --seeds 1 --memory-workers 3 \
  --models gpt-5.6-terra@relay gemini-3.5-flash@relay kimi-k2.6@relay --aggregate
# If interrupted, rerun the exact same --report-dir with --resume. C2 resumes
# only provenance-matched phases and atomic per-task checkpoints; unfinished task
# artifacts are placed in a fresh attempt namespace and are never reused.

# Re-aggregate
python scripts/aggregate_p5_scale.py --run-index reports/warptest/<ts>/p5
```

Outputs land under `reports/warptest/<ts>/p5/`: per-arm reports, `p5-run-index.json`,
and `p5-scale-summary.json/.md`. The aggregation logic is covered by
[`tests/test_p5.py`](../tests/test_p5.py).

## Visual-oracle calibration

The hybrid visual-oracle calibration (`hybrid-v3`) is independent of the C1 run.
`prepare` performs zero-model reference qualification/capture. `judge`, and
VLM-backed `annotate` when explicitly selected, send provider requests.

```bash
HYBRID_VISUAL_PILOT_ROOT="reports/warptest/hybrid-visual-confirmation-v3"
python -m warptest visual-experiment --protocol hybrid-v3 --phase prepare \
  --split pilot --pilot-cohort confirmation \
  --dataset-dir "$HYBRID_VISUAL_PILOT_ROOT" --headed

HYBRID_VISUAL_ROOT="reports/warptest/hybrid-visual-formal-v3"
python -m warptest visual-experiment --protocol hybrid-v3 --phase prepare \
  --split formal --dataset-dir "$HYBRID_VISUAL_ROOT" --headed
python -m warptest visual-experiment --protocol hybrid-v3 --phase judge \
  --dataset-dir "$HYBRID_VISUAL_ROOT"
python -m warptest visual-experiment --protocol hybrid-v3 --phase annotate \
  --dataset-dir "$HYBRID_VISUAL_ROOT" --annotator annotator-a \
  --annotation-mode vlm --annotator-model glm-5.3-flash
WARPTEST_VISUAL_JUDGE_TIMEOUT=600 python -m warptest visual-experiment \
  --protocol hybrid-v3 --phase annotate \
  --dataset-dir "$HYBRID_VISUAL_ROOT" --annotator annotator-b \
  --annotation-mode vlm --annotator-model doubao-seed-2-1-turbo
python -m warptest visual-experiment --protocol hybrid-v3 --phase adjudicate \
  --dataset-dir "$HYBRID_VISUAL_ROOT" --annotator adjudicator \
  --annotation-mode vlm --annotator-model claude-sonnet-5
python -m warptest visual-experiment --protocol hybrid-v3 --phase aggregate \
  --dataset-dir "$HYBRID_VISUAL_ROOT"
```

## Experiment design

- **C1 (checkpoint + GUI)** — the five formal arms (C/D/E/F/G) use the same committed
  [`benchmarks/p5/c1_tasks.yaml`](../benchmarks/p5/c1_tasks.yaml): 61 tasks across
  10 heterogeneous interactive environments—eight each from 2048, SpaceRPG, and
  Luanti, seven from DatingSim, plus five each from OpenRA, JupyterLab,
  LibreOffice, Blender, jynew, and PAL3. Each task is bound to its public prompt
  revision, random-control contract, pinned application revision, and formal
  machine fingerprint; incompatible historical results cannot be pooled.
  `love-resource-action` was removed only from this formal source because its
  transition duplicates `love-office-work-coding` while its goal is a strict
  subset. Its core/C3 task, spec, goldens, and historical reports remain intact.
  C=`c1_agent_s` (Agent S3) from start,
  D=`c1_agent_s_synth` (Agent S3) from a self-synthesized checkpoint,
  F=`c1_agent_scalecua` and G=`c1_agent_scalecua_synth` (CUA Agent, i.e.
  ScaleCUA) likewise, and E=`c1_state` (StatePortal). Every task shares one
  uniform GUI decision budget of 100 steps and one uniform wall-clock budget of
  2400 seconds (`budget_ms`); `horizon_class` is descriptive only and does not
  change either budget.
- **C1 measurement** — every trial reports start success, goal success conditional
  on its own achieved start, end-to-end success, time-to-start, start-to-goal,
  end-to-end time with/without synthesis, and token cost by module and model,
  including reasoning and cached-prompt subsets. Time effects use pre-registered
  horizon-specific administrative restrictions; token and time are primary because
  the manifest price table is intentionally unverified. Seven non-exclusive,
  closed-vocabulary capability labels additionally support descriptive sensitivity
  summaries; they are grader-side metadata and never enter the public issue.
- **C1 models and vision** — decisions use GPT-5.6-Terra, Gemini-3.5-Flash,
  Kimi-K2.6, and GPT-6-Astra through an OpenAI-compatible relay gateway (`@relay`). All four use the registered
  `thinking_disabled` request profile for decision, synthesis, Agent-S
  worker/reflection, and matched-grounding calls. The relay GPT route uses
  `reasoning_effort=none`; Gemini and Kimi use `thinking.type=disabled`.
  ScaleCUA plans and grounds through the per-trial decision model.
  A fixed Claude Sonnet 5 observer supplies structured visual text, while
  Agent-S pixel grounding follows each trial's decision model. Claude remains
  the fixed grounder only for the model-independent background calibration.
  Qwen3-VL Flash is the independent visual judge. All four decision models
  retain the 8192→16384 output-limit retry.
- **C2 (transfer)** — held-out strict success vs A5 (no-memory evolution) from the Phase 3
  suite, and vs A-MEM / Mem0 from the memory-baseline suite.
- **C3 (robustness)** — **unique** defects deduped by a deterministic fingerprint across
  seeds; raw/robust/rejected, browser-vs-engine `defect_path`, and
  player-reachable vs injection-only counts are kept as transparency counters. OpenRA
  contributes only its live gameplay-production C3 tasks, never static map metadata.

Provenance and failure policy: every report carries a `run_context`
(`experiment_id`/`run_id`/`git_commit`/`task_manifest_hash`/`retry_history`); the run
writes `p5-run-index.json`. Infra and rate-limit failures are retried (manifest `retry`
block); a model that returns an unusable output is a genuine failure and is not retried.

## Execution protocol

The protocol is selected by flags, not code edits:

1. `--seeds 3 --dry-run` must show 12 invocations per arm, 60 total, 3,660 C1 trials,
   `c1_outer_worker=8`, `c1_inner_worker=4`, and a maximum requested task
   concurrency of 32 unless the worker flags are overridden. Every registered C1
   task also has the published 2400-second wall-clock budget (`budget_ms` in the
   task file; the manifest `c1_task_timeout_s` is only a fallback). Repository and
   backend leases are acquired first under the independent
   `runtime_lock.wait_timeout_s` infrastructure limit, and every trial records
   `runtime_lock_wait_ms`; only after acquisition does the task clock start.
   Provider calls are capped to the remaining execution time and an overrun
   stops that invocation.
2. Run the manifest-derived online preflight (fourteen routes for the formal
   four-model manifest: four visual decisions, four text synthesis calls,
   Claude observer, four matched grounders, and Qwen judge), the
   six-native-backend zero-model canary,
   and the headless Playwright observer/grounder check that produces the required
   10-backend `c1-calibration-v4` artifact. The artifacts are bound to
   git/manifest/harness/provider/request-profile hashes and rehashed before every
   invocation. Formal Agent-S input remains application-local and does not use a
   headed Quartz/pyautogui calibration path. The six native canary routes reject
   any target process group that becomes active, steals focus, or remains visible
   after startup; Luanti/OpenRA additionally bind source and rebuilt binary hashes
   proving their hidden-at-create paths.
3. For the 10-app adaptation, run the registered 120-trial headed lightweight
   check (GPT-5.6 Terra + Kimi K2.6, both no-thinking, one seed, two tasks per
   app, all three formal arms). It is diagnostic only.
4. Run the full three-seed matrix (`--seeds 3`) with `--resume`. A harness defect invalidates the
   affected run directory; provider/quota failure pauses rather than becoming a
   model failure. Every explicit attempt is hash-chained. Paid infrastructure
   attempts remain in `actual_cost` and are split into
   `transient_infrastructure_cost` with full module/model/reasoning partitions;
   the primary `cost` keeps only the final treatment attempt plus D's registered
   counterfactual synthesis burden. Unknown usage still fails closed.
5. Report the achieved coverage and `complete` value directly; do not derive a
   separate publication-eligibility boolean.

For formal C1, the relay-gateway credential source is parsed as allowlisted string
literals and its key is never written to an artifact. SDK-internal retries and
visual-verdict caching are disabled; explicit attempts, report hashes, runtime seed
readback, shared B/D synthesis attribution, and run-scoped caches are publication
gates. The publication gate recomputes every attempt hash and verifies primary
treatment burden, all-attempt provider spend, retry transient overhead, and the
existing synthesis actual/attributed/transient ledgers independently.

## C1 concurrency and repository isolation

C1 uses a process-wide resource-ready scheduler. `--c1-outer_worker N` overlaps up to
`N` registered `(arm, model, seed)` invocations; `--c1-inner_worker M` creates up
to `M` persistent task lanes inside each invocation. The defaults are 8 and 4,
respectively. Effective counts are capped by the remaining invocation/task count.
The historical `--c1-workers` spelling is only a deprecated alias for the outer
value and conflicts with an explicitly supplied `--c1-outer_worker`.

The Latin-square registration order remains authoritative even when completion is
out of order. Within an invocation, tasks are grouped by resolved game repository,
interleaved so different games start first, and assigned by horizon weight to the
least-loaded lane. Each GUI/Agent-S lane owns its observer, policy client, and
Agent-S subprocess; it calls `reset(task=...)` before every task and closes the
lane-owned clients at shutdown. State-arm lanes likewise own independent synthesis
agents. Lane mappings and worker counts are persisted before execution and are
immutable on `--resume`.

The run index stores each canonical outer registration exactly once. Its
`dispatch_history` records process-level scheduler dispatches and resume
continuations; it is not a paid trial attempt and never replaces the hash-chained
task `attemptN` ledger. A same-directory resume preserves completed registration
timing, restores ordinal counters, and dispatches only provenance-valid pending
work. Missing scheduler provenance or a changed registration fails closed.

Before any C1 provider request or worker-pool startup, every selected repository
must share the sandbox root's APFS device and macOS must report at least 50 GiB
available for important usage. Each task then receives a strict APFS CoW clone
under `/private/tmp/warptest-c1-sandboxes/<run>/<invocation>/lane-<id>/...`.
StatePortal uses `clonefile(2)` and never silently falls back to a byte-for-byte copy.
If cloning or clone verification fails for a task whose backend already permits
source fallback, only that task uses the source checkout under its machine-shared
repository lease for the full task. Mutable Unity GUI paths that set
`allow_source_fallback=False` continue to fail closed. Owned clones are
marker-checked and removed immediately after their processes and file handles
close; no byte-copy fallback is allowed.

Unity concurrency is scoped to the canonical cloned project path. A formal LLM run
with `--c1-inner_worker > 1` performs zero-model, two-clone canaries for jynew and
PAL3 before paid requests. If either canary detects an Editor/license/cache or
request-path conflict, only Unity tasks fall back to a machine-wide serial lease;
other games and Agent-S lanes remain concurrent. Background Unity and OpenRA do not
take the legacy global native-input lock. Explicit foreground-native diagnostics
still do, because they genuinely share desktop focus and input.

An incomplete task stops its current invocation: work that has not started is
cancelled, while already-running lanes settle and persist their results. Unknown
provider usage and shared provenance, ledger, or integrity failures trigger the
global outer stop at every task-dispatch boundary in both normal and recovery
producers. A coordinator atomically writes results in manifest order and records outer
registration/start/completion ordinals, lane IDs, requested/effective workers,
sandbox/lock/canary status, cancellation reason, and observed peak concurrency.
Checkout, native-backend, and Unity-serial leases are probed non-blockingly. A
busy task is requeued while an unrelated ready task may run; the task-file
`budget_ms` wall-clock deadline (2400 seconds for formal tasks) begins only after
full admission. Formal aggregation rejects results that
omit or contradict this scheduler provenance.

Unknown usage remains permanently non-resumable in the same report root: recovery
requires a clean root plus `--c1-source-index`, and replacement trials continue to
use the registered `attempt0`–`attempt2` namespaces. Schema-v3 source recovery
accepts exactly two known-usage error classes: a registered task deadline, or an
explicitly retryable infrastructure result whose hash-chained attempt ledger says
`retryable_infrastructure`. Both require complete primary usage and zero unknown
prompt/completion/total counters; the old paid attempt remains bound as operational
overhead. Non-retryable infrastructure and treatment failures remain
non-replaceable. Manifest, task, harness, model, timeout, provider, and treatment
changes always require a fresh run. The optional
`--c1-source-contract-migration` artifact is limited to metadata transformations
whose scientific contract hashes are already identical, such as a mechanically
validated gate-artifact rebind; it cannot authorize a hash mismatch.

See [`c1_concurrency.md`](c1_concurrency.md) for the complete operator and
resume contract.

## C2 memory-baseline provenance boundary

For the C2 memory-baseline arm, `--seeds` repeats each model and stamps the
per-model run metadata. `--memory-workers N` overlaps up to `N` model jobs while
keeping each model/seed/backend memory file separate and leasing a shared game
checkout for the duration of an actual runtime invocation. Its `--resume` path
is bound to the C2 evaluator/task/game-revision hash and resumes at the last
atomically persisted task, never from artifacts that lack matching provenance.

`--arms c2_memory_baseline` binds its harness hash to the C2 evaluator/memory-backend
code, the `arms.c2_memory_baseline` manifest section, the shared
`models`/`seeds`/`api`/`retry`/`model_request_profiles` fields, the C2
task/split/oracle files, and each game's pinned gitlink commit
([`warptest/c2_provenance.py`](../warptest/c2_provenance.py)). Editing `c1_*` or
`c3_fuzz` manifest sections, `benchmarks/state_loop/*`, or the C3 fuzz-injection
code in [`warptest/spacerpg_fuzz_profile.py`](../warptest/spacerpg_fuzz_profile.py) /
`warptest/openra_fuzz.py` / `warptest/jynew_fuzz.py` while a C2 run is in progress
does **not** invalidate it, so those arms can keep being developed in parallel.
Editing the C2 arm's own config, the shared manifest fields above, or any C2
runtime/task file still changes the hash and correctly blocks `--resume` with "C2
harness changed during the run". A `p5-run-index.json` written before this
boundary narrowed has no `c2_manifest_fingerprint` field; resuming a C2-only run
from it fails clearly and requires a fresh `--report-dir`.
