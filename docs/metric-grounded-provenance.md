# Metric-Grounded Task Provenance

A reproducible method for deciding **what a benchmark task tests** and **what
its golden oracle is**, replacing hand-picked authoring. It answers two
questions any checkpoint benchmark must address:

1. *Where do the tasks come from?* (cherry-picking / coverage bias)
2. *Where does the ground truth come from?* (LLM-writes-and-LLM-grades circularity)

The method rests on a **defensibility triangle** whose three legs are each a
mechanical, re-derivable computation. Natural-language phrasing is a separate,
non-load-bearing surface layer.

```
              selection (what)                ground truth (answer)
        composite static metrics    ----    execution-anchored recording
                       \                       /
                        \                     /
                         discrimination (value)
                        source-mutation kill rate
```

## Stage 1 - Metric-driven selection (`warptest/metrics/`)

For each game we compute per-function signals with one off-the-shelf tool where
possible, so the numbers are not "our definition of complexity":

- **cyclomatic complexity, nesting depth, NLOC, token count** via `lizard`
  (`static_metrics.py`) - covers JS/TS/C#/C++/Python/Lua with the same tool.
- **fan-in / fan-out** via transparent text-level call coupling over the analysed
  file set (`coupling.py`) - an explicit, documented approximation (name-based,
  no call graph), deterministic and re-derivable.
- **git churn** (commits + changed lines per file / line range) straight from the
  game's own history (`churn.py`).
- **from-start unreachability** of the candidate deep state via Monte-Carlo
  playout of the artifact's own rules (`reachability.py`); this is the signal
  that *justifies why a checkpoint is needed at all*.

Signals are converted to within-corpus **percentile ranks** and combined with
**pre-registered, frozen weights** (`config.py`, versioned + content-hashed).
The ranking is intersected with the discovery semantic surfaces
(`warptest/discovery.py`) so a selected region is actually observable and
controllable - this is what turns "complex code" into "a testable checkpoint".

Selection is therefore a deterministic function of
`(source@commit, git history, frozen weights)`: anyone re-runs it and gets the
same ranking (Gate A). Output: `reports/metrics/<game>-candidates.json`.

### PoC result (Stage 1)

- **2048**: top-ranked region is `GameManager.prototype.move`
  (final 0.812; cc 4, nesting 3, fan-out 15, 122 commits). This is exactly the
  region all three hand-authored 2048 tasks (`merge-left`, `near-win`,
  `near-loss`) target - the metric **recovers the human choice** with no
  hand-picking.
- **luanti**: from 1,587 Lua functions the top regions include
  `item.lua::core.item_place_node` (cc 57), `core.node_dig` (cc 37),
  `falling.lua::set_node` (cc 28) - the true core of world-state mutation that
  luanti checkpoint tasks exercise.
- Reachability curve for 2048 (greedy Monte-Carlo, budget 3000 moves):
  tile 64 -> 0.99, 128 -> 0.88, 256 -> 0.43, 512+ -> 0.00. The near-win task
  (two 1024 tiles) sits at unreachability 1.0, quantitatively justifying the
  checkpoint.

## Stage 2 - Execution-grounded oracle (`warptest/oracle_synth.py`)

The golden oracle is **recorded from the real game**, not authored:

1. The selected region's control-flow branches induce branch-covering seed
   states (for `move`: the merge branch, the win branch, the terminated/no-moves
   branch). This mechanically ties each task to the region.
2. Pin `Math.random` so the post-move tile spawn is deterministic.
3. Restore the seed through the game's own persistence path (localStorage).
4. Drive the game's real `move` via the `__WARPTEST_2048__` hook.
5. Read back `getState()` - the observed after-state is the ground truth. The
   spec's assertions are the **deterministic projection** of that observed state
   (score delta, resulting tile, terminal flags), so the oracle re-passes on a
   normal unpinned run.

The only authored inputs are the seed board + action, both derived from the
region's branches. The answer is whatever the real game computes.

### PoC result (Stage 2)

Three specs written to `specs/2048/generated/`; all pass **Gate B** re-run
verification (restoration success + execution-derived assertions hold on a
normal unpinned run), e.g. merge-left records score delta 4 and a resulting 4
tile; near-win records score 6144 and `won=true`; near-loss keeps `over=true`.

## Stage 2b - Issue naturalization (`warptest/issue_naturalize.py`)

The mechanical spec is the primary provenance artifact. An LLM (GPT-5.5) renders
it into a natural bug-report-style issue as a **pure paraphraser**; it never
decides what to test or the answer. Every rendering is gated by `TokenGuard`, a
standalone port of the existing token-equivalence checks:

- **shallow** variant: every grader-critical token recoverable, no template
  boilerplate.
- **explore** variant: strategy name, validity class and framework action-type
  identifiers must be **absent** (recoverable from source/contract instead),
  while all target values remain.

A rendering that drops a token or leaks a mechanism token is rejected and
re-sampled with guard feedback. This keeps the issue readable as prose while
keeping the defensibility triangle fully mechanical. Raw output is content-hashed for audit. (GPT-5.5 only supports the
default temperature; ground-truth determinism is unaffected because it is
mechanical, and phrasing is guard-verified regardless.)

### PoC result (Stage 2b)

Six issues (3 tasks x shallow/explore) all pass the guard (Gate C); explore
issues correctly hide `localStorage_restore_reload` / `save-restorable` / `move`
and instruct source discovery.

## Stage 3 - Discrimination validation (`warptest/mutation.py`)

We mutate **only the selected region's source span** with language-agnostic
operators (relational/arithmetic/boolean swaps, constant tweaks) and measure the
**kill rate**: a mutant is killed if the original game passes all of the task's
execution-grounded oracles but the mutated game fails at least one. High kill
rate is quantitative evidence that the task discriminates behaviour in the
region it claims to test; near-zero flags a task for pruning.

### PoC result (Stage 3)

8 single-point mutants on `move()` (lines 130-191); **kill rate 0.875** (7/8).
The lone survivor (`moved = true` flip) is behaviorally masked in these
scenarios. near-win kills nearly every merge/score/win mutant; near-loss (move
no-ops) kills none; together they cover the region.

## Acceptance gates (convergence criteria)

- **Gate A (reproducible)**: re-running Stage 1 yields identical rankings + scores. PASS.
- **Gate B (oracle valid)**: each generated spec restores and its execution-derived assertions hold on a normal run. PASS (3/3).
- **Gate C (de-leak)**: issues pass shallow + explore token guards. PASS (6/6).
- **Gate D (discrimination)**: region mutation kill rate above threshold. PASS (0.875).
- **Gate E (metric relevance)**: metric score correlates with difficulty/kill rate vs a random baseline. PARTIAL - single strong data point (0.875) plus 100% concordance with the hand-authored 2048 task region; the full metric-vs-kill-rate correlation across a per-region task bank is the scale-up experiment.

## CLI

```
python -m warptest provenance metric-select --game 2048 --top 25
python -m warptest provenance oracle-synth   --game 2048        # writes specs/2048/generated
WARPTEST_NL_API_KEY=... WARPTEST_NL_BASE_URL=... WARPTEST_NL_MODEL=gpt-5.5 \
python -m warptest provenance naturalize     --game 2048
python -m warptest provenance mutation       --game 2048
```

Artifacts: `reports/metrics/*-candidates.json`, `specs/2048/generated/*.yaml`,
`reports/oracle_synth/*.json`, `reports/mutation/*.json`.

## Open items / disclosures

- fan-in/fan-out is a name-based textual approximation, not a sound call graph.
- luanti from-start unreachability uses a conservative documented default (=1.0)
  because there is no cheap faithful simulator; 2048 carries the quantitative
  reachability evidence.
- Pre-registered weights must be frozen before inspecting rankings; `config.py`
  is versioned + content-hashed for this.
- Gate E at scale (metric-vs-difficulty correlation across many regions with
  per-region oracles) is the next experiment; the PoC establishes each leg
  individually with hard numbers on 2048.
- Execution-anchored oracle synthesis for luanti reuses the worldmod path and
  may report `blocked` on hosts that cannot build the engine (consistent with
  the harness's neutral blocked status).
