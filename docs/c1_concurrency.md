# C1 two-level concurrency and isolation

This document is the operator contract for concurrent C1 execution. It describes
the behavior implemented by `warptest p5-scale`; worker counts and lane mappings
are execution provenance, not performance-only tuning knobs.

## CLI and defaults

```bash
python3 -m warptest p5-scale \
  --c1-outer_worker 8 \
  --c1-inner_worker 4 \
  --report-dir reports/warptest/c1-formal \
  --resume
```

- `--c1-outer_worker` controls concurrent `(arm, model, seed)` invocations and
  defaults to 8.
- `--c1-inner_worker` controls task lanes within each invocation and defaults to
  4.
- Both values must be at least 1. Effective counts are capped by available work,
  so the default maximum requested task concurrency is `8 × 4 = 32`.
- `--c1-workers` is a deprecated alias for `--c1-outer_worker`. Supplying both
  spellings is an error.
- A dry run prints both values and their maximum requested task concurrency.

## Deterministic scheduling

The orchestrator registers the complete Latin-square invocation table before it
starts any worker. Registration ordinal remains the canonical experimental order;
submission, start, and completion ordinals are recorded separately because runtime
completion may be out of order.

Within each invocation, WarpTest resolves every task's game repository, preserves
the manifest order within each game, and interleaves game groups. This produces an
immutable canonical task registration, but tasks are assigned to lanes only when
their shared resources are ready. Active invocations are selected round-robin by
outer ordinal; within one invocation the earliest runnable canonical task wins.
Contenders for one exclusive resource remain FIFO.

The same two-level policy applies to source-index recovery. Outer workers still
own `(arm, model, seed)` invocations, while each recovery invocation starts up to
`min(c1_inner_worker, unfinished tuples)` inner lanes. Every unfinished tuple is
still a separate `one_task_fresh_invocation_v1` producer call and keeps its own
attempt ledger, report directory, task file, and billing record. A run with only
one unfinished outer invocation therefore records effective outer concurrency 1
but may still recover several independent tuples concurrently.

Each GUI/Agent-S lane creates and owns one observer, policy client, and Agent-S
subprocess. It calls `reset(task=...)` before each task and closes all lane-owned
resources when the lane exits. Each State-arm lane similarly owns one synthesis
agent. Stateful workers are never shared between lanes or threads.

The invocation coordinator accepts completed lane results and atomically rewrites
the report in original manifest order. A late-finishing task therefore cannot
overwrite, reorder, or drop another task's result.

## APFS task sandboxes

Before a provider request or either worker pool starts, C1 verifies that:

- `/private/tmp/warptest-c1-sandboxes` and every selected game repository are on
  the same APFS device; and
- macOS reports at least `50 × 2^30` bytes available for important usage. The
  run index records ordinary free space, important-usage capacity, the eligible
  value, and the exact threshold.

Every task receives a repository at:

```text
/private/tmp/warptest-c1-sandboxes/<run>/<invocation>/lane-<id>/<task>-<nonce>/repo
```

The implementation calls macOS `clonefile(2)` recursively and requires an APFS
copy-on-write clone. It validates the filesystem device and Git HEAD/status
fingerprint. It never invokes a copy command that can silently produce a normal
byte-for-byte checkout.

If cloning or validation fails, WarpTest removes the partial marker-owned path.
Backends that already permit source fallback run only that task in the source
repository while holding its machine-shared `runtime_lease` for the full task.
Mutable Unity GUI paths configured with `allow_source_fallback=False` fail closed.
Synthesis, instrumentation, runtime, and restore resolve through a thread-local
repository override while trusted task/oracle configuration continues to come from
the main checkout. The task identity and manifest hash do not change, and there is
no ordinary byte-copy fallback.

After all task processes and handles close, WarpTest validates the run-owned marker,
nonce, and path containment before immediately deleting the clone. Start/end stale
cleanup applies only to valid ownership markers for the exact run; unowned paths are
never deleted.

Resource queueing and task execution use separate clocks. Checkout, explicitly
foreground native-backend, and per-benchmark Unity-serial admission use non-blocking
try-locks. If A is busy, the scheduler
releases every partially acquired resource, requeues A with exponential probing
from 50 ms up to 1 second, and immediately scans for an unrelated B. An idle lane
therefore never waits while holding A's lease. Successful APFS clones keep only
their independent clone lease, so two clones of the same game may still run
concurrently; source fallback holds the source lease for the complete task.

Only after every real lease is owned does the task-file ``budget_ms`` wall-clock
deadline start (formal tasks declare 2400000 ms / 2400 s; the manifest
``c1_task_timeout_s`` is only a fallback). The independent queue clock begins at the first busy observation
and is bounded by `runtime_lock.wait_timeout_s`. Each trial records the non-duplicated
wall-clock `runtime_lock_wait_ms`, defer count, hashed blocking resource kinds,
actual lane, and admission/start/completion ordinals. Exceeding the queue limit is a
pre-policy infrastructure error with complete zero-call accounting, never a model
failure or a task-timeout result.

## Unity and native locks

Unity permits multiple Editors for different project paths but not multiple Editors
for one project path. C1 therefore scopes the normal Unity lease to the canonical
cloned project path. Luanti, OpenRA, jynew, and PAL3 use PID/session-scoped background
input transports and do not register or acquire the legacy machine-wide native-input
lock. A single resource configuration produces both scheduler labels and execution
leases, so those two layers cannot silently disagree. Work that genuinely sends
Quartz or foreground desktop input must opt in explicitly and retains the legacy
global lock.

The two Unity backends are probed independently. A failed concurrency canary adds a
benchmark-specific lease under
``/private/tmp/warptest-c1-unity-serial/<benchmark>``; it does not serialize the other
Unity backend. On the current macOS runner, jynew remains serialized because APFS
project clones still share ``Application.persistentDataPath``, while PAL3 may run in
parallel because its mutable state is clone/session scoped. Run indexes and invocation
sidecars record ``c1_unity_serialized_benchmarks`` / ``unity_serialized_benchmarks``;
the historical boolean remains as a compatibility summary for older reports.

Every formal native process group is monitored and hidden for its full lifetime;
the canary rejects activation, focus theft, or a window that remains visible.
Luanti and OpenRA additionally set `SDL_WINDOW_HIDDEN` in their engine window
constructors and bind source plus fresh binary hashes into provenance. jynew and
PAL3 deliberately use normal headed Unity without `-batchmode`, `-hideWindow`, or
`-nographics`; their unavoidable progress/splash windows may be created briefly
but must be immediately minimized/hidden by the guard. The canary records that
transient visibility, rejects durations above 10 seconds, and requires no active/frontmost observation plus an empty
final visible-window set. This preserves real GameView rendering rather than
substituting a headless capture path. Unity-local AppKit hooks order repeated
visibility requests back out; the independent process-group guard also audits
and, when AX minimize/hide is acknowledged but ineffective, relocates only the
target process group's window offscreen.

APFS cloning and its tracked Git-fingerprint validation hold the same source
lease, with a 300-second cold-check timeout, so two large Unity clones cannot
force each other into a false timeout. jynew and PAL3 never use the generic
serialized source-checkout fallback: Unity may normalize tracked assets during
import, so a clone failure is an infrastructure error instead of permission to
open the pinned source checkout.

For a formal LLM run with Unity tasks and `--c1-inner_worker > 1`, WarpTest performs
separate no-model canaries for jynew and PAL3 before paid requests. Each canary:

1. creates two strict CoW clones of the same game;
2. launches two real Unity sessions and proves distinct PIDs and overlapping live
   intervals;
3. uses isolated request, report, and log paths;
4. performs distinct clean-entry/restore operations and verifies no cross-talk;
5. verifies that the source repository fingerprint is unchanged; and
6. deletes both canary sandboxes after the sessions close.

If a canary reports an Editor, license, cache, or machine-level conflict, all Unity
tasks use a stable machine-wide serial lease for that run. Non-Unity games and
Agent-S lanes remain concurrent. The canary evidence and fallback reason are stored
in the run index and task scheduler metadata.

## Failure, cancellation, and resume

An infrastructure-incomplete task sets its invocation stop event. Lanes do not
start another task after observing it; already-running tasks settle and are
persisted. Unknown provider usage additionally publishes the shared global stop
before the producer returns, so every normal and recovery producer refuses its
next task dispatch.

Recovery uses a soft dispatch close for ordinary timeout and infrastructure
failures: pending tickets are cancelled without provider calls, while claimed or
running tickets finish their atomic persistence. Resource-busy recovery tickets
are deferred back to the shared ready queue, so another runnable tuple may pass
them. The existing hard cancellation operation remains reserved for teardown.

Trial-persistence schema v3 permits a clean `--c1-source-index` root to replace
only registered pending/quarantined tuples plus two exact-usage source errors:
the configured task timeout, and a result explicitly marked
`retryable_incomplete=true`, `infrastructure_error=true`, with a terminal
`retryable_infrastructure` attempt-ledger entry. The importer preserves either
failed attempt as operational overhead. Unknown usage, non-retryable
infrastructure, model/treatment failures, and fabricated or incomplete ledgers
still fail closed.

Shared manifest, harness, provenance, attempt-ledger, preflight, calibration, or
integrity failures also set the global outer stop. Not-yet-started invocations are
marked cancelled; tasks and provider requests that were already issued are allowed
to settle and persist rather than being forcibly killed.

`c1_outer_worker`, `c1_inner_worker`, the ready policy, and `inner_task_plan` are
immutable execution protocol. An incomplete root written before the ready-scheduler
protocol cannot be resumed in place: recovery requires a clean root,
`--c1-source-index`, and an explicit scheduler-only contract migration artifact.
Completed historical reports remain directly aggregatable. Completed trial snapshots
remain hash-bound and are not replaced by completion-order output.

The scheduler-only artifact uses migration ID
`c1_resource_ready_scheduler_v2`, binds the exact source/current commits,
harness hashes, changed-path list and its SHA-256, and asserts completed-only
import plus requeue of every unfinished tuple. Any path outside the registered
scheduler/locking/report/test/documentation surface fails closed.

The recovery parallelism change uses the separate, generic migration ID
`c1_recovery_parallel_scheduler_v1`. It is valid only for a new clean recovery
root passed through `--c1-source-contract-migration`; it never permits an old
root to resume across a harness hash. Both import and aggregation independently
re-audit direct and flattened lineage, exact source/current index and commit
bindings, the changed-path list and hash, completed-only import, and requeue of
all unfinished tuples. A minimal artifact has this shape (values are placeholders,
not a binding to an existing experiment):

```json
{
  "schema_version": 2,
  "kind": "c1_source_contract_migration",
  "migration_id": "c1_recovery_parallel_scheduler_v1",
  "source_index": "/absolute/old-root/p5-run-index.json",
  "source_index_sha256": "<sha256>",
  "source_run_id": "<run-id>",
  "source_manifest_hash": "<hash>",
  "source_harness_hash": "<hash>",
  "source_git_commit": "<commit>",
  "current_manifest_hash": "<same-manifest-hash>",
  "current_harness_hash": "<new-harness-hash>",
  "current_git_commit": "<new-commit>",
  "allowed_migrations": ["git_commit_rebind", "harness_rebind"],
  "replaceable_source_statuses": [
    "pending",
    "quarantined_unknown_usage",
    "exact_usage_task_timeout",
    "exact_usage_retryable_infrastructure",
    "registered_exact_usage_harness_failure"
  ],
  "scheduler_policy": "global_resource_ready_round_robin_v2",
  "recovery_execution": "one_task_fresh_invocation_v1",
  "recovery_dispatch": "two_level_resource_ready_v1",
  "affected_trials": [{
    "arm": "<arm>",
    "model": "<model>",
    "seed": 0,
    "task_id": "<task-id>",
    "source_status": "<pending-or-registered-incomplete-status>"
  }],
  "preserved_model_failure_trials": [],
  "registered_harness_failure_trials": [],
  "completed_trial_import_only": true,
  "unfinished_tuples_requeued": true,
  "normal_execution_unchanged": true,
  "treatment_semantics_unchanged": true,
  "changed_paths": ["<sorted registered paths>"],
  "changed_paths_sha256": "<sha256 of compact JSON path list>",
  "reason": "<non-empty treatment-neutral explanation>"
}
```

If preflight or calibration gate metadata only changes through the same registered
commit/harness rebinding, its corresponding migration name must also appear in the
sorted `allowed_migrations` list. Task/manifest, grader, prompt, billing, treatment,
or unregistered path drift is rejected.

The canonical `execution_order` contains one row per registered outer invocation.
Outer scheduler schema v2 appends process-level entries to that row's
`dispatch_history` when provenance-valid pending work is continued after an
interruption. These dispatch entries are not task attempts and do not participate
in cost accounting. Paid/replacement work keeps the separate hash-chained
`attemptN` contract. Unknown usage can never create another dispatch in the same
root; it requires a clean root and `--c1-source-index`.

## Scheduler provenance

The run index and invocation reports record, as applicable:

- requested/effective outer and inner workers;
- invocation registration, start, and completion ordinals;
- outer `dispatch_history`, with preserved ordinals across same-root resume;
- canonical task registration, actual lane, admission/start/completion ordinals;
- per-invocation worker limit, dispatch-close reason, and active/peak task counts;
- queue wait, defer count, hashed blocking resources, and observed peak admitted
  concurrency;
- sandbox mode and clone-fallback error;
- Unity canary result and serial-fallback state; and
- cancellation status and reason.

The run index stores `c1_resource_scheduler` schema 1 and reports store inner
scheduler schema 2, both using
`global_resource_ready_round_robin_v2`. Formal aggregation accepts historical
inner schema 1 and current schema 2. It requires the current scheduler block for new runs and
continues to reject missing tasks, duplicate coordinates, unknown usage, invalid
resume provenance, worker-limit violations, overlapping exclusive-resource
intervals, and inconsistent scheduler evidence.

## Validation

The focused regression suite is:

```bash
python -m pytest -q \
  tests/test_c1_concurrency.py \
  tests/test_c1_trial_persistence.py \
  tests/test_c1_unity_runtime.py \
  tests/test_c1_unity_gui_integration.py
```

It covers defaults and alias conflicts, deterministic lane assignment, out-of-order
completion, per-lane Agent-S and synthesis-agent ownership, cancellation propagation,
strict CoW isolation and cleanup, serial source fallback, capacity failure, Unity
canary overlap, immutable resume protocol, and aggregation provenance.
