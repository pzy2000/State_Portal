# Issue-guided Luanti fuzz (SWE-bench-style)

This pipeline turns the previously random C3 state fuzz into an **issue-guided**
process: it mines historical bug-fix PRs from the Luanti **upstream** repo, distills
corroborated findings into structured priors that steer the existing LLM fuzzer,
and provides a SWE-bench-style **replay/regression** harness for independently
authored fixtures that cross a real fix-commit boundary.

It is `luanti`-only for now. Upstream is resolved from
[`benchmarks/upstream/sources.yaml`](../benchmarks/upstream/sources.yaml)
(`luanti-org/luanti`), never from `.gitmodules` (which only records the fork).

## Four contracts (see `warptest/upstream_contracts.py`)

1. **Dual-axis reachability.** The headless C3 warm session
   (`LuantiWarmSession(render=False, map_backend="dummy")`) only synthesizes
   `nodes / fill / meta_inventory / mod_storage` (the online `player` is skipped) and
   does NOT exercise real world deserialization, server-restart load, or
   settings/mapgen parsing. So each mined card gets two INDEPENDENT labels:
   - `fuzz_reachable` — the current C3 `target` JSON can trigger it (→ priors only).
   - `headless_replayable` — reproducible on a headless server via an independent
     world/config/mod fixture (→ replay only).
   File paths are only an INITIAL screening signal; keyword corroboration lifts
   confidence to `medium`; `high` is reserved for a human `manual_override`. Low-
   confidence matches remain review candidates and are not allowed to create priors
   or replay datasets unless a human explicitly opts them in.

2. **pre/post fix SHA.** Derived from the merge commit, never `baseRefOid`:
   `pre_fix_sha = mergeCommit.parents[0].oid`, `post_fix_sha = mergeCommit.oid`.

3. **Independent replay defect oracle.** Replay does NOT reuse the old repro rule
   (`crashed or status=="failure"`), which mislabels validation mismatches as
   reproduced. Each replay spec carries an EXPLICIT `DefectOracle` (abnormal exit /
   hang timeout / log signature / semantic assertion) and every run yields one of
   five verdicts — never two booleans:
   `PRE_REPRODUCED_POST_FIXED`, `PRE_NOT_REPRODUCED`, `POST_STILL_REPRODUCED`,
   `BUILD_FAILED`, `HARNESS_INCOMPATIBLE`.

4. **Isolated build.** Historical commits are NEVER checked out in `games/luanti`
   (that would corrupt the parent gitlink and let the modern screenshot hook /
   worldmod contaminate old sources). Builds use a bare upstream mirror + a per-SHA
   detached `git worktree` + an external build dir (`BUILD_CLIENT=FALSE
   BUILD_SERVER=TRUE`). CMake installs a complete versioned runtime containing the
   server binary, `builtin/`, and `games/devtest/`; both the old `minetestserver`
   and current `luantiserver` names are supported. The runtime is keyed by
   `build_cache_key(commit, platform, compiler, cmake_args, harness_digest)`.

## Data flow

```
mine_luanti_issues.py         -> bug_cards.jsonl (provenance: pre/post SHA, changed files, refs)
classify_luanti_cards.py      -> dual-axis labels (reason/evidence/confidence/manual_override)
build_luanti_priors.py        -> fuzz_priors.json (structured recipes, from fuzz_reachable only)
  -> fuzz-bench --priors auto  -> deterministic prior seeds + structured prompt hint
build_luanti_replay_tasks.py  -> task manifest for independently authored replay specs
verify_luanti_replay.py       -> reports/warptest/luanti-replay/verify.json (five-state)
```

## Usage

```bash
# 1. Mine a freezable set of upstream fix PRs into bug cards.
python scripts/mine_luanti_issues.py --limit 40 --since 2023-01-01

# 2. Label each card on both reachability axes (idempotent; overrides preserved).
python scripts/classify_luanti_cards.py

# 3. Distill structured priors from fuzz_reachable cards only.
python scripts/build_luanti_priors.py

# 4a. Run the fuzzer WITH priors (issue-guided) ...
warptest fuzz-bench --tasks benchmarks/state_loop/tasks_luanti.yaml \
    --model <model> --priors auto
# 4b. ... or WITHOUT priors (no-prior control):
warptest fuzz-bench --tasks benchmarks/state_loop/tasks_luanti.yaml \
    --model <model> --priors none

# 5. Verify replay specs (control run uses the prebuilt server; --build compiles
#    the pre/post commits via the isolated builder).
python scripts/verify_luanti_replay.py           # control
python scripts/verify_luanti_replay.py --build    # true two-commit regression
```

`fuzz.json` records the priors provenance for every run: the `--priors` arg
(`llm_config.priors`) and, per task, `llm_meta.priors` (source path, SHA256, recipe
ids) plus `num_oracle_seeds / num_prior_seeds / num_llm_seeds_generated`. The seed
order is fixed: **oracle → prior → llm**.

## Publishing issues (guarded)

Draft builders only render Markdown + a `manifest.json` (which now carries
`source_repo` and `default_publish_repo`). Filing is a separate, guarded step:

```bash
python scripts/publish_issues.py --manifest .../github-issues/manifest.json          # dry-run to fork
python scripts/publish_issues.py --manifest ... --yes                                 # file to fork
python scripts/publish_issues.py --manifest ... --target upstream --i-understand-upstream --yes
```

The default target is the **fork**; upstream requires an explicit, acknowledged flag.
Live publishing requires each draft's hidden fingerprint marker, checks GitHub for an
exact existing marker, and atomically records successful/existing URLs in
`publish-results.json`. Re-running after a partial failure therefore only publishes
the remaining drafts; dry-runs never write the journal.

## Replay harness safety

The replay mod writes a start sentinel before reading the fixture. A process failure
without that sentinel is `HARNESS_INCOMPATIBLE`, not an engine defect. Synthesized
fill regions are capped at 100,000 nodes so the replay driver itself cannot create a
watchdog timeout. Oversized-fill cases are harness boundary tests and are excluded
from the engine regression manifest.

## Note on reproducibility / RNG seed

`--seed` only controls the Python RNG that drives operator/mask/seed selection. It
is NOT passed to the LLM provider (`LLMStateFuzzer._chat` sends no `seed`), so LLM
sampling nondeterminism comes from the temperature + the provider. A given
`--seed` therefore does **not** fully replay an LLM run. In contrast, the prior
seeds are fully deterministic given the oracle target + `fuzz_priors.json`, and the
replay dataset is deterministic given the fixed pre/post commits + the explicit
defect oracle.
