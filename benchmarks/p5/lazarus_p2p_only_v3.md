# Lazarus P2P-only generated-state protocol v3

The default formal specification, frozen by user instruction on 2026-09-07, is `lazarus_p2p_only_formal_seed0_manifest.yaml`: the same ordered 11 tasks and catalog SHA, decision models gpt-5.6-terra, gemini-3.5-flash, kimi-k2.6 and gpt-6-astra, seed 0, buggy/fixed, and C/D/E. This is 264 cells in 44 six-cell groups. Its independent authorization is `formal_264_paid_run`. Formal specifies the frozen execution protocol, not confirmatory inference or paper readiness: `formal=true`, `confirmatory=false`, `paper_ready=false`, with descriptive results per model.

All four decision models use `thinking_disabled` throughout synthesis, policy and matched grounding (GPT `reasoning_effort=none`; Gemini/Kimi explicit disabled thinking). Observer and judge retain the existing C1 routes. The historical three-model/seeds-012 manifest `lazarus_p2p_only_manifest.yaml` and Astra-only manifest remain explicit legacy schedules with their original identities and non-formal status. Their authorizations cannot authorize the new matrix.

`collect_lazarus_seed0.py --source <three-model-run.json> <astra-run.json> --api-preflight <source-preflight.json> <astra-preflight.json> --out-dir <new-directory>` creates a provider-free historical collection. It selects the exact 264 keys, validates source snapshots and evidence hashes, checkpoint reuse, ledgers and no-thinking evidence, and records source identities separately. It never creates a paid-run readiness or rewrites source identity/status. A collection has `formal=false`, `confirmatory=false`, `paper_ready=false`, and a reference to the frozen formal specification; implementation differences are visible and results are descriptive per model. Task failures with complete evidence remain valid observations.

Collection usage deduplicates response identities in selected cell and referenced shared ledgers; unallocated historical preflight/recovery/shared overhead remains unknown and separate from method-attributed costs. Historical logs do not universally preserve wire request overrides: preflight request evidence, recorded no-thinking expectations and returned reasoning counts are reported separately, including Kimi's registered one-token sentinel anomaly.

## Methods and executable state

- C (`lazarus_agent_s`) constructs Start from the cold application with pixel input, then executes the public reference steps. Phase A and B share 100 decision steps.
- D (`lazarus_agent_s_synth`) restores the shared model-generated state with no actions, passes Start, then uses the same Agent-S factory and Phase B prompt as C. Phase B gets 100 decision steps.
- E (`lazarus_state`) restores the same generated state, passes Start, then executes only that spec's generated semantic actions (at most 100). An empty action list remains empty and cannot count as a completed transition. A pixel policy that only returns done/fail without an accepted input event also cannot claim a transition.

A group has one exploratory synthesis result (16 exploration steps, 8192 output tokens, temperature 0.2). Its immutable `MethodCheckpoint.model_generated_spec` contains an explicit `target.state`, a `p2p_only_typed_restore_v3` strategy, and separate `actions`. Both versions consume the same result. No fixed-version resynthesis or grader-driven repair is permitted. The existing C1 public format-repair allowance is retained; task semantics are not supplied by repair code.

The public state/action schema is in `warptest/lazarus_p2p_only_spec.py`. JupyterLab's public workbench starts unconfigured and exposes candidate components and state controls. Luanti uses explicit menu/settings/world data and candidate menu handlers. OpenRA uses explicit screen/map state and candidate widget handlers. Test checkpoints in `tests/fixtures/lazarus_p2p_only_v3_specs.yaml` are consumed only by tests and the provider-free canary, never by treatment synthesis or as a fallback.

Start success requires coordinator-owned semantic and visual evidence before Phase B. D/E share a per-version verdict after local state, liveness and informative-frame attestation; state mismatch is incomplete infrastructure evidence. Image hashes bind captured evidence; harmless image-byte differences are not a same-Start gate. Shared per-version verdicts are persisted with an integrity hash for restart recovery. The application stays in the same live session. The P2P restorer is bound to the accepted cell's observed state, including C's reached state, and checks the clone-local receipt and restored state hash. The after frame is still compared with both registered visual oracles; accepted actions and generated assertions are not grading evidence.

## Costs and evidence identity

Reports separate synthesis, restore/Phase A, execution/Phase B, bootstrap, grading, and P2P. Shared synthesis has an immutable event ID. Experiment actual cost counts that event once; each method's attributed comparison counts one synthesis burden for its group, without charging fixed a new model call. Different action spaces retain their own step counts.

V3 manifest/method fields and generated-runtime implementations participate in scoped identities. Shared synthesis and candidate-only compiled assets live under the new run root. Assets contain no generated Start or transition. Native input is serialized; OpenRA uses its renderer/default-input backend, while Luanti uses its native window input. C and D use identical policy configuration per application.

Historical 132-cell v2 reports remain readable. Their original method, schedule, costs and evidence are not rewritten. V2 groups and canaries cannot be adopted as v3 evidence. An incomplete group must be rerun against the same immutable synthesis checkpoint or in a fresh run; old output files must not be overwritten to hide a mismatch.

## Validation

Focused tests cover the exact matrix, old two-arm defaults, real exploration output finalization, generated parameter/action consumption, all 66 task/arm/version interface combinations with stubs, immutable cache recovery and corruption, pre-transition failure gates, budgets, cost attribution, and actual-state P2P receipt binding.

`python scripts/run_lazarus_p2p_only_runtime_canary.py --run-root <fresh-workspace-directory> --output <new-report.json>` runs fresh candidate applications using test-supplied checkpoints and pixel policies, with real screenshots and P2P controls. It makes no model calls. All 11 tasks and six cells per task must pass for a complete canary. `--task` supports bounded diagnostics; a subset cannot satisfy full readiness. The canary is interface evidence, not autonomous model performance or a replacement for paid visual grading. Old canaries cannot be relabeled with `--adopt-current-complete`.

`python scripts/preflight_lazarus_p2p_only.py` checks readiness against current v3 identities. A complete runtime canary, calibration, source/materialization checks, and API readiness are still required by the paid runner. Changing the manifest alone does not make those checks pass.

## Three-seed operational recovery

The 594-cell schedule uses seeds 0, 1, 2 and preserves `(task_id, model, seed)` pairing in aggregation. A typed, accounted provider outage defers the affected model route while other models continue. Recovery uses new attempt directories, imports complete six-cell groups, and shares only immutable synthesis, attested Start gates and compiled assets. Three cooled recovery rounds use 60, 180 and 600 seconds; the next actual group is the bounded recovery probe. Authentication, per-request capacity, unknown usage and integrity failures stop dispatch. Ordinary transient failures are not global stop conditions.

A separate cell provider ledger observes nested phase meters without changing treatment attribution. Accounted failed synthesis receipts are archived before another synthesis attempt; an incomplete directory without such a receipt remains blocked. Actual recovery usage includes earlier failed attempts and is reported separately from the final treatment rows.
# Authorized conditional Kimi history exception (2026-09-07)

The three-seed exploratory recovery run may explicitly enable
`--kimi-capacity-history-backoff`. Its revision is
`kimi-capacity-old-screenshots-v1`. Only native Agent-S Kimi requests that receive
an explicit single-request TPM ceiling rejection without usage may drop the
oldest prior screenshot, one per retry. The latest frame, all text, image pixels
and resolution, no-thinking profile, and existing step/time budgets are retained.
Each new logical request starts with full history; there is no persistent history
truncation. Ordinary rolling-window 429s retain their ordinary retry behavior.

This opt-in variation has its own provider profile identity, authorization and
protocol-change record. Atomic per-request receipts retain the capacity proof,
original/sent image counts, removed-image hashes and latest-image hash, including
failed retries. Original complete groups remain original-protocol evidence and
are imported only through exact identity compatibility and full snapshot checks.
The HTML report explicitly discloses this variation. Shared synthesis is unchanged.

## Public Start alignment revision (2026-09-07)

`public-start-constraints-v2` adds an explicit, shared public initial-data
constraint for JupyterLab 18464: the existing `total` is a nonzero integer. This
constraint is included in both the C Phase-A prompt and the shared D/E synthesis
input. It does not prescribe the golden's example value 7. Zero is rejected at
Start so the defect-triggering value first appears in the reference action's
new `count` entry. The visual rubrics accept other nonzero initial values and
preserve the fixed/buggy distinction for the new zero readout. The frozen
11-task catalog and its original text/hash remain historical registration data;
the additional public constraint is carried by the versioned runtime contract.

For OpenRA, generated editor state may explicitly select `editor_tab` as
`overlays` or `tiles`; the documented default is the neutral `overlays` palette.
The restorer invokes the candidate's tab handler and reports the actual tab.
Start requires an observed closed or empty tile palette, not merely an editor
screen. The grader ignores unrelated map/camera/minimap/viewport differences,
while retaining the palette-state and target template-order requirements.

For Luanti worldmods, restoration validates installed game IDs before changing
files, selects the first supplied world's game, and refreshes/selects its menu
entry. Start observes the candidate's filtered menu world list and actual
worldmods directories; files on disk alone are insufficient. World names need
not equal the reference fixture name. For the settings task, the before rubric
explicitly distinguishes a harmless Settings button/hover tooltip from an open
settings dialog, which remains invalid at Start.

The authored wording registry regenerates the four updated oracle specs and
their `.goldens.yaml` sidecars. Golden image bytes and historical run reports are
unchanged. Oracle YAML content now participates in `grading_sha256`; runtime
and synthesis identities also change with the repaired adapters/constraints.
Old canary/calibration or checkpoint identities must not be silently adopted.
Provider-free canaries exercise interfaces and P2P controls, not the paid visual
judge's new classification behavior; new paid evidence still requires fresh
readiness/calibration.

## Generated checkpoint schema and repair revision (2026-09-07)

`lzp-checkpoint-schema-v1` is the single public structural schema for generated
states, actions and the checkpoint envelope. The prompt and runtime validators
use the same schema, including component-specific props, required fields,
arrays, enums, integer argument types, safe names and forbidden setting names.
JSON Schema type names are not converted to template placeholders. The local
validator supports exactly the keywords emitted by the schema and rejects
unimplemented keywords rather than silently ignoring them. No new runtime
package dependency is required.

World directory names may contain safe ASCII spaces (e.g. `World With Mods`),
which the candidate already supports; path traversal/separators/control
characters remain rejected. Game IDs and mod names remain slug-constrained,
and installed-game validation stays in the runtime before filesystem mutation.

The one allowed same-model format repair operates on invalid fields only.
Already-valid initial values are restored if the model changes them, including
state, actions, assertions and evidence. Existing array positions are preserved;
missing evidence membership may be appended, but extra actions cannot be
injected by a repair. The raw reply, protected candidate, accepted mapping and
preservation metadata are separate artifacts. An accepted mapping is written
only after structural and method-checkpoint validation. If initial YAML cannot
be parsed, preservation is explicitly marked unavailable; no checkpoint is
invented by the coordinator.

Repository evidence remains bound to actual sandbox reads and at least one
repository citation is required. Public task text has its own evidence type
`{kind: public_task, field: issue, rationale: ...}`. The exact historical sentinel
`Public issue state description` is normalized to this type with an audit note;
arbitrary unread paths are never reclassified as public text. Frozen non-Lazarus
C1 evidence and repair behavior are unchanged.

The final call within the existing exploration budget is reserved for a
checkpoint response after repository observation. It accepts FINAL-wrapped or
plain checkpoint YAML; ACTION is not dispatched in that final slot. Earlier
turns retain strict ReAct parsing. The step budget, model request profiles,
response token limit, one-repair limit and empty-response failure policy are
unchanged. Missing/invalid provider output is still a failure, not a scaffold.

`python scripts/replay_lazarus_schema_repairs.py --run-report <run.json>
--out-dir <new-directory>` reuses archived raw outputs and repair replies without
calling a provider. It checks contract validity and recorded file-read bindings;
it does not rerun tasks, regrade historical results or establish task success.
The schema implementation participates in synthesis and treatment identities,
so old checkpoints cannot silently cross this revision.
