# C1 evaluation contract

Formal C1 separates the public task, hidden reference execution, runtime
observations, and grading policy.  The committed data flow is:

```text
benchmarks/p5/c1_tasks.yaml
  public preconditions + reference_steps
              |
              v
       Agent-S in task clone
              |
              v
  before/after state + screenshots + semantic evidence
              |
              v
trusted parent coordinator loads specs/*/....yaml
  hidden target/checkpoint/actions + evaluation-v1 contract
              |
              v
 semantic + visual result, then global harness gates
```

`benchmarks/p5/c1_tasks.yaml` is the only formal task input exposed to Agent-S.
Its `oracle_spec` path is coordinator metadata and is not included in the agent
payload.  Inside that hidden spec, `target`, `checkpoint`, and `actions` define
the reference start and transition used to produce the registered evidence.
They are not themselves an alternate scoring contract.  All task-selected
grading policy lives under one root:

```yaml
evaluation:
  schema_version: 1
  semantic:
    assertions:
      - type: state_equals
        path: screen
        value: flight
      - type: no_severe_console
    surfaces:
      gui:
        adapter: spacerpg_production_dom_save_v1
        start:
          validate_target: true
          checks: []
        goal:
          use_assertions: true
          checks:
            - name: transition.throttle_delta
              metric: greater_than
              actual: {probe: spacerpg.throttle_percent, phase: after}
              expected: {probe: spacerpg.throttle_percent, phase: before}
            - name: transition.throttle_intent_count
              metric: equals
              actual: {probe: spacerpg.throttle_intent_count}
              expected: {value: 1}
      state:
        adapter: spacerpg_state_profile_v1
        start: {validate_target: true, checks: []}
        goal: {use_assertions: true, checks: []}
  visual_oracle:
    # The existing descriptions, rubrics, and image paths are nested here.
```

The order of each surface's named checks is the report order.  Names must be
unique.  The schema accepts only registered adapters, probes, and comparison
operators; operands are literals or named probes.  Python expressions, imports,
and `eval` are not part of the language.  Unknown adapters, probes, metrics,
arguments, task evidence shapes, or unsupported assertions fail closed.

All 61 formal specs declare both `gui` and `state`.  Five legacy SpaceRPG tasks also
declare `gui_from_start_baseline`, including its public GUI recipe and its exact
checks.  OpenRA expected setting/gameplay check shapes, Unity expected check
groups, save slots, and event order are likewise declared on their surfaces.
LibreOffice's Java and Unity's C# code remain probe implementations: they execute
only the checks selected by the trusted Python evaluator from this contract.

Formal evaluation-v1 specs may not contain root `assertions` or
`visual_oracle`.  Non-C1 legacy specs and model-produced `generated_spec` values
retain their historical root interface.  Trusted runtimes use
`warptest.c1_evaluation` accessors and an in-memory compatibility materializer;
the materialized copy is not another on-disk source of truth.

## Task metrics versus global harness gates

The evaluation contract contains task-selected semantic and visual metrics.
These global validity checks deliberately remain code-owned and are not metrics:

- runtime/process liveness and bounded polling;
- screenshot and semantic-evidence completeness;
- evidence integrity and anti-tamper checks;
- seed, repository, oracle digest, cache, and resume provenance;
- unsupported-assertion failure behavior; and
- hidden-information leakage checks.

A task can satisfy every declared task metric and still be rejected by a global
gate.  Reports keep those two categories separate.

## Trusted loading and agent boundary

Only the trusted coordinator/grader loads `oracle_spec` from the parent
StatePortal checkout.  Its parsed `evaluation` object stays in the control plane.
`task_repo_override` changes only the game-repository path used by a task; it
does not move specs, catalogs, goldens, or evaluator code into the task clone.
If a contract must cross a process boundary, it uses grader-private memory, IPC,
or an agent-inaccessible run directory and remains bound to
`oracle_spec_sha256`.  It is never written into the task checkout.

The evaluation body, check names, thresholds, regular expressions, oracle paths,
and digests must not appear in Agent-S prompts, repo maps, tool observations,
generated specs, transcripts, synthesis metadata, or agent-visible temporary
files.

## Repository isolation

`warptest/c1_sandbox.py` performs the existing macOS/APFS preflight: the sandbox
root and source repository must be on the same APFS device and at least 50 GiB
must be available for important usage.  Each invocation/lane/task gets a unique
nonce and a recursive `clonefile(2)` CoW checkout below
`/private/tmp/warptest-c1-sandboxes/...`.  Device and Git fingerprints are checked
after cloning.  There is no byte-copy fallback.  A marker-owned checkout is
removed immediately after success, exception, timeout, or cancellation, and the
cleanup code refuses missing/tampered markers, wrong run IDs, the run root, and
paths outside the exact owned subtree.

Where source fallback was already allowed, clone failure holds
`runtime_lease(source)` for the entire task and runs serially in the source
checkout.  Mutable Unity GUI paths configured with `allow_source_fallback=False`
continue to fail closed.

Agent exploration goes through `warptest.repo_tools.RepoSandbox`.  Read, list,
grep, and repo-map operations reject absolute paths, `..`, and file or directory
symlink escape.  When a parent benchmark checkout is accidentally selected, they
also hide `specs/`, the parent root's `scripts/`, `benchmarks/**/hidden/`,
`benchmarks/state_loop/`, and `.warptest-cache/`.  Noise such as `.git`,
`node_modules`, build/dist output, caches, and coverage output is filtered.  A
game child repository's own normal source `scripts/` directory remains readable.
Rejected reads are not added to `files_read` or source provenance.

