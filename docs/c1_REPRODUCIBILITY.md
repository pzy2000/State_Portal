# C1 reproducibility contract

The current formal C1 catalog is `c1_v5`. A result is current-formal only when
all of the following identities match the checked-out catalog at aggregation
time:

- the full `benchmarks/p5/c1_tasks.yaml` bytes and the selected task rows;
- every task's `prompt_revision` and `random_control`;
- all ten `benchmark_hosts` entries, including `.gitmodules` URL, parent
  gitlink, child `HEAD`, and a clean child worktree;
- `harness_revision`, OS/build, architecture, physical-screen dimensions, and
  the resulting environment fingerprint;
- GUI report schema v5, State report schema v4, and invocation-context schema
  v6.

Preflight, calibration, normal execution, resume, source import, and formal
aggregation all apply this contract before a model call or before accepting an
existing result. The run index and every invocation context record the ten
application revisions as readable fields in addition to the content-derived
harness hash.

## Prompt revision history

`c1-deep30-v1` was the first Deep30 wording. It mixed checkpoint setup and the
requested transition in ambiguous prose, so a solver could not reliably tell
which state was granted and which work it had to perform. Its raw artifacts are
retained for historical diagnosis only; v1 results are not current-formal and
must not enter a paper-ready table.

`c1-deep30-v2` split that prose into precise public checkpoint preconditions and
reference steps. Results produced with v1 were not silently upgraded or reused:
they remain historical because their public prompt treatment differs.

`c1-deep30-v3` migrated the public contract to the explicit
`preconditions`/`reference_steps` fields and fixed which fields are visible to
the policy versus the hidden verifier. The current catalog retains v3 for the
14 unchanged Deep30 tasks and uses task-level successors where the treatment
changed:

- `c1-deep30-rng-free-v1` for the three 2048 tasks made independent of hidden
  post-move samples;
- `c1-deep30-random-control-v1` for the three DatingSim tasks whose fixed
  per-spec seeds were replaced by the experiment seed;
- `c1-deep30-v5-ui-save-slots` for `jynew-sidequest-trade-submap`, the one
  Unity task using public UI save slots whose wording is otherwise unchanged;
- `c1-deep30-v6-player-reachable` for `jynew-battle-ready-loadout` after
  removing the private raw-key sentinel from both its public wording and hidden
  success contract;
- `c1-deep30-v7-realistic-credits` for `spacerpg-contraband-stealth-run` after
  binding its start and goal to player-reachable credits and pre-acquired
  stealth equipment;
- `c1-deep30-v8-console-syntax` for the six jynew and PAL3 tasks whose console
  entry key, command names, and argument syntax are now written out;
- `c1-deep30-v9-trimmed-wording` for
  `jupyterlab-reproducible-experiment-workspace` after dropping an ordering
  phrase no assertion graded;
- `c1-deep30-v10-locatable-coordinates` for the two deep Luanti builds that now
  state the F5 coordinate readout;
- `c1-legacy-self-contained-v1` for the seven legacy tasks whose undefined
  “registered” objects were replaced by explicit files, scenes, or objects;
- `c1-legacy-v2-playable-story-jump` for `pal3-scene-transition` after its
  public transition was expanded into the normal playable story-scene setup;
- `c1-legacy-v3-locatable-coordinates` for the five legacy Luanti tasks that
  now name a visible landmark, the carried item, and the F5 readout;
- `c1-legacy-v1` for the other 16 unchanged legacy tasks.

Any report with a missing or different task revision, host revision,
environment fingerprint, or random-control contract is historical diagnostic
evidence only. Historical benchmark-name aliases may be decoded for display,
but they do not make such a report formal or paper-ready.

## Randomness contract

`random_control` is the sole formal RNG treatment:

- `2048`, `datingsim`, and `spacerpg` use experiment seeds `[0, 1, 2]`
  with `mulberry32`;
- `luanti` uses experiment seeds `[0, 1, 2]` with
  `luanti_fixed_map_seed`;
- `openra`, `jupyterlab`, `libreoffice`, `blender`, `jynew`, and `pal3`
  declare `not_applicable`; their numeric seed exists only for paired-trial
  assignment (`seed_role: trial_pairing_only`).

Runtime evidence must read back the registered algorithm/seed or the explicit
not-applicable pairing role. The `replay_from_start.random_values` value in the
legacy 2048 merge-left spec is only a scripted reachability-replay fixture. It
is not a formal task RNG treatment and cannot override `random_control`.

## Current formal matrix

The catalog contains 61 tasks over 10 applications. With five formal arms,
four decision models, and three seeds, the registered matrix contains 3,660
trials grouped into 60 arm/model/seed invocations.
