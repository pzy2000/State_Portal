# Visual Audit Policy Design Notes

This note records what the visual audit path actually does. It is a design
record, not a paper section. Claims about speed, cost, or accuracy must be
backed by fresh benchmark reports before being promoted into the manuscript.

## Current Benchmark Need

WarpTest currently has three kinds of evaluation evidence:

- Semantic oracles and runtime validation are the primary acceptance path.
- Visual oracles add evidence for cases where the rendered game state is an
  important part of correctness or where semantic probes can miss a visibly bad
  state.
- LLM-as-a-judge visual checks are useful but expensive, slower than local
  deterministic checks, and potentially noisy. They should therefore be treated
  as an audit layer rather than as the main source of truth.

The visual judge is intentionally monotone with respect to the semantic result:
it can downgrade a semantic success to an audited failure or add supporting
evidence, but it must not convert a semantic failure into success.

## Main Design Decisions

1. `oracle_policy` decides whether visual audit is selected for a case:
   `semantic`, `visual`, `adaptive`, or `fake-visual`.
2. `CascadePolicyConfig` carries the execution knobs for a selected audit:
   frame count, frame interval, and cache mode. The retained
   `cascade_policy` field (`full-only` or `conservative`) now only selects the
   default frame count: `full-only` keeps single-frame capture, `conservative`
   captures three browser frames. The field name is legacy and does not imply a
   judge cascade.

All commands route through the shared `apply_oracle_policy(...)` layer. Commands
do not wire visual judging directly. This keeps `run`, `compare`, `bench`,
`core-bench`, and `model-bench` on one semantics path.

Fake visual judging is a judge backend, not a policy. The `fake-visual` oracle
policy selects the same visual audit path as real visual judging, but the
backend is `WARPTEST_VISUAL_JUDGE_FAKE`. Fake verdicts are written into
per-report artifacts so tests can inspect the audit record, but they do not
write the global visual-judge cache by default.

## There Is Exactly One Judge Tier

There is **no draft judge and no escalation rule**. Every selected visual audit
that misses the cache issues exactly one full-judge request. An earlier
draft-then-escalate implementation existed but was never wired to any production
run path, never configured with a draft model, and has been removed so that
neither code readers nor the manuscript can mistake it for a measured mechanism.

The deterministic checks that run before the judge are **reject-only**: they can
fail an audit without a model call, but nothing can accept an audit without the
judge. There is no perceptual-hash or pixel-distance fast accept.

## Sampling Axes

The implementation keeps two sampling levels separate:

- `visual_sample_rate` is case-level sampling. It decides whether an eligible
  case enters the visual audit path under adaptive policy.
- `sample_count` and `sample_interval_ms` are frame-level sampling. They decide
  how many visual frames are captured once a case is selected.

Keeping these axes separate matters for both engineering and paper reporting.
Case-level sampling changes which tasks are audited; frame-level sampling
changes the visual evidence available for one audited task.

## Audit Algorithm

1. Validate the visual oracle schema and oracle presence.
2. Respect semantic status. A semantic failure remains a final failure; the
   visual audit may add evidence but cannot repair it.
3. Capture visual evidence. Browser profiles default to three frames at a 300 ms
   interval under `conservative`; `full-only`, OpenRA, and Jynew default to one
   frame unless explicitly overridden.
4. Run screenshot-quality checks and collapse byte-identical duplicate frames.
5. Build a contact sheet when multiple unique frames remain.
6. Check the global cache when enabled and when the backend is not fake.
7. On cache miss, run the full judge once.
8. Write per-report prompt/response/audit artifacts, and write the global cache
   only when cache policy allows it and the backend is not fake.

## Status Semantics

Reports preserve these top-level fields:

- `semantic_status`
- `visual_audit_status`
- `oracle_policy`
- `visual_audit_reason`

Execution details live under `visual_oracle_result.data["visual_audit"]`.

The `visual_audit_status` enum is:

- `pending`: audit has not yet been decided.
- `skipped`: policy or missing oracle prevented audit.
- `success`: visual audit was selected and the final visual verdict matched the
  expected label.
- `failure`: visual audit was selected and the final visual verdict disagreed,
  quality checks failed, or the backend failed.

Missing visual-oracle behavior is policy-specific:

- `semantic`: skipped as `semantic_policy`.
- `adaptive`: skipped with `visual_audit_reason=missing_visual_oracle`.
- `visual` and `fake-visual`: failure because the caller explicitly requested a
  visual audit but no visual oracle exists.

Overall status follows three rules:

- semantic failure always remains failure;
- semantic success plus visual audit failure becomes failure;
- skipped visual audit leaves semantic status as the deciding result.

## Report Shape

The `visual_audit` child structure records:

- `cascade_policy`
- `judge_backend`
- `sample_count`
- `sample_interval_ms`
- `visual_sample_rate`
- `cache_hit`
- `global_cache_written`
- `frame_paths`
- `contact_sheet_path`
- `full_verdict`
- `final_verdict_source`
- `audit_decision_reason`

`final_verdict_source` is `full`, `fake`, or `cache`.

## Cache Policy

Per-report prompt and response artifacts are always the first audit record. The
optional global cache lives under `.warptest-cache/visual-judge/`.

Cache keys include:

- visual audit cache version and prompt version;
- execution policy;
- oracle content hash;
- golden-image hash;
- frame or contact-sheet hash;
- judge model id;
- judge backend kind.

Fake backends do not write the global cache by default. This prevents local test
fixtures from polluting future paid-model audit results.

## Paper Update Hooks

Potential manuscript placement:

- Method: describe the oracle-policy selection layer and the monotone status
  invariant as a layered audit architecture.
- Experiments: report visual-audit coverage, cache hit rate, and the number of
  visual audits that downgraded semantic successes.
- Limitations: state that visual LLM judging is auxiliary evidence, depends on
  model image capability, and cannot replace semantic oracles.

Useful metrics for the next paper update:

- number of cases eligible for visual audit;
- number selected by policy;
- number skipped because of missing visual oracle;
- frame count distribution and duplicate-frame collapse rate;
- cache hit rate and global-cache write count;
- final visual audit failures that downgraded semantic successes;
- cost and latency split between deterministic checks and the full judge.

## Current Guardrails

Do not describe the visual audit as a speculative-verification or draft/verify
cascade. That mechanism is not implemented. Do not claim cost or latency savings
from judge tiering; the only implemented savings are the reject-only
deterministic guards, the semantic short circuit, the duplicate-frame collapse,
and the verdict cache, and none of these has been measured on a full suite yet.
