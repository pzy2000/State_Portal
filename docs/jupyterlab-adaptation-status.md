# JupyterLab Benchmark Adaptation -- Status, Blockers, and Decision Log

> Last updated: 2026-07-02
> Scope: integrate JupyterLab (Web/server GUI + Jupyter Server + notebook/kernel/session/workspace APIs) into the WarpTest benchmark as the ninth source-available GUI software.

---

## 1. One-Sentence Summary

JupyterLab is integrated into WarpTest's V1 semantic pipeline (discovery / profile / runtime / schema / strategies / synthesis / effort / paper-tables / specs / benchmarks / tests / docs).

V1 is positioned as a **web/server GUI benchmark**, not JupyterLab Desktop. WarpTest does not modify `games/jupyterlab` and does not add private hooks to the nested repo; checkpoints go only through upstream public interfaces: the Jupyter Server Contents API, the sessions/kernel API, workspace metadata, and the Galata-compatible browser/test config surface.

The current local Python environment can see the JupyterLab source package via `PYTHONPATH=games/jupyterlab`, but server/kernel dependencies such as `jupyter_server` / `jupyter_client` are not yet installed, and `games/jupyterlab` has no `node_modules` or built static assets. Therefore inspect/schema/model-bench can run; a real `warptest run specs/jupyterlab/*.yaml` reports runtime dependency-blocked as expected until the JupyterLab dependencies are installed/built.

---

## 2. Architecture

```
specs/jupyterlab/*.yaml
  └─ runner.run_spec
       └─ JupyterLabRuntimeAdapter.run_checkpoint
            ├─ Start a temporary JupyterLab server
            │    · localhost free port
            │    · token/password disabled
            │    · no browser auto-open
            │    · root/config/data/runtime/workspaces all under report_dir
            ├─ Contents API creates/opens/saves text and notebooks
            ├─ sessions/kernel API starts a notebook session
            ├─ workspace metadata records/validates opened paths
            └─ checks[] → JupyterLabProfile maps phases by prefix
```

- **Check prefix mapping**: `target.state_synthesized` → restoration; `target.validate.*` → validation; `action[<type>]@i` → smoke; `assertion[<type>]@i` → oracle.
- **Write boundary**: all Jupyter runtime writes go under the temporary tree `reports/warptest/<ts>/.../jupyterlab-runtime/` and are never written back to `games/jupyterlab`.

---

## 3. Completed

| File | Change |
|------|------|
| `warptest/jupyterlab_runtime.py` | Adds isolated server runtime, Contents/session/workspace operations, and request/report/checks output |
| `warptest/profiles.py` | Adds `JupyterLabProfile`, default strategy `jupyterlab_workspace_api_validate`, validity `save-restorable` |
| `warptest/discovery.py` | profile detection, hook/API surface, storage policy, adapter layers, semantic schema |
| `warptest/runner.py` | Adds JupyterLab to `resolve_repo` and a JupyterLab runtime branch to `run_spec` |
| `warptest/strategies.py` | replay_from_start V1 exclusion reason |
| `warptest/spec_schema.py` | JupyterLab actions/assertions schema |
| `warptest/synthesis.py` | `SOURCE_VOCABULARY[JupyterLab]`, `BENCHMARK_CONTRACTS[JupyterLab]`, payload kind, 6 deterministic templates |
| `warptest/model_bench.py` | 6 JupyterLab exact-spec model-bench tasks and a conformance checker |
| `warptest/evo.py` / `warptest/effort.py` | JupyterLab included in all-core/evidence/paper-table statistics |
| `.gitmodules` | `games/jupyterlab` registered as a submodule (`https://github.com/anonymous/jupyterlab.git`, branch `main`) |

---

## 4. Evaluation Set

- `specs/jupyterlab/*.yaml` -- 6 smoke specs:
  - `text-file-edit.yaml`
  - `notebook-cell-output.yaml`
  - `workspace-layout.yaml`
  - `session-smoke.yaml`
  - `markdown-file-save.yaml`
  - `notebook-markdown-note.yaml`
- `benchmarks/core/public/tasks.yaml` -- 6 core public tasks:
  - `jupyterlab-text-file-edit`
  - `jupyterlab-notebook-cell-output`
  - `jupyterlab-workspace-layout`
  - `jupyterlab-session-smoke`
  - `jupyterlab-markdown-file-save`
  - `jupyterlab-notebook-markdown-note`
- `benchmarks/core/hidden/oracles.yaml` -- 6 hidden oracle mappings, all pointing to public spec files.
- `benchmarks/model/jupyterlab-tasks.yaml` -- 6 exact executable-spec prompting tasks.
- `benchmarks/state_loop/tasks_jupyterlab.yaml` -- 6 JupyterLab-only state-loop tasks; the main state-loop list samples two representative tasks, text/file and notebook/kernel.
- `scripts/generate_visual_pairs.py --benchmark JupyterLab` -- the JupyterLab before/after visual pair generation entry point is wired in; real PNG generation still requires `jupyterlab`, `jupyter_server`, `jupyter_client`, a Playwright browser, and servable JupyterLab static assets on the local machine.

---

## 5. Blockers and Decisions

### D1. Do not modify JupyterLab source

JupyterLab already provides standard public testing surfaces (Jupyter Server REST APIs, workspace API, Galata helpers). V1 uses these surfaces to cover notebook/text/workspace/session semantics and needs no private hooks in the nested repo.

### D2. Runtime uses a temporary server root

`JupyterLabRuntimeAdapter` points `ServerApp.root_dir`, `JUPYTER_CONFIG_DIR`, `JUPYTER_DATA_DIR`, `JUPYTER_RUNTIME_DIR`, and `LabApp.workspaces_dir` all to the report/temp workspace. This verifies real Jupyter semantics while avoiding pollution of the source checkout or user directories.

### D3. `replay_from_start` excluded in V1

JupyterLab checkpoints are document/session/workspace states synthesized through APIs, not states replayed by manual clicks from a fresh UI. V1 therefore excludes `replay_from_start` and, like other save/API-restorable profiles, reports an unsupported reason in compare.

### D4. Visual oracle: no fake golden committed for now

The local machine currently has no built JupyterLab UI/static assets; V1 does not commit placeholder or fabricated goldens. Once dependencies are installed and the UI can actually be opened, real screenshots/goldens will be captured for the notebook/text editor specs.

---

## 6. Reproduction Commands

```bash
# Read-only inspection
python3 -m warptest inspect games/jupyterlab

# Semantic smoke (currently requires installing JupyterLab runtime dependencies locally first)
python3 -m warptest run specs/jupyterlab/text-file-edit.yaml --repo games/jupyterlab

# JupyterLab source dependencies/build (optional, before running a real server/UI)
cd games/jupyterlab
python -m pip install -e ".[dev]"
jlpm
jlpm build:all
```

---

## 7. Current Status Quick Reference

| Item | Status | Notes |
|----|------|------|
| profile/discovery/schema/synthesis | Integrated | `JupyterLabProfile` + public API semantic schema |
| specs/tasks/model-bench | Integrated | 6 specs / 6 core tasks / 6 model-bench tasks / 6 state-loop tasks |
| nested repo modifications | None | V1 uses upstream APIs/test config surfaces read-only |
| submodule pin | Prepared | parent points `games/jupyterlab` to `4a19f8fff4049a9536e21fa2980fe7e5ee592d01` |
| local real runtime smoke | Dependency-blocked | The current Python environment can find the source package but lacks server deps such as `jupyter_server`/`jupyter_client`, and UI build assets are not installed |
| visual oracle | Not committed | Real goldens will be captured once a real built UI is available |
