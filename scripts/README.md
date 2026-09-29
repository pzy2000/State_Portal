# Scripts

Most files here are one-off development, mining, and migration utilities. The
scripts below are the ones used for the paper; everything else is not needed to
reproduce it.

| Script | Purpose |
| --- | --- |
| `fetch_apps.sh` | Fetch applications and baselines at their pinned commits (archive downloads) |
| `aggregate_p5_scale.py` | Aggregate a `p5-scale` run into `p5-scale-summary.json/.md` |
| `derive_execution_channel.py` | Derive the per-task execution-channel column |
| `check_c1_budget_rerun.py` | Verify a performance-study run against the uniform budget contract |
| `preflight_lazarus_p2p_only.py`, `preflight_lazarus_p2p_only_api.py`, `run_lazarus_p2p_only_runtime_canary.py`, `build_lazarus_p2p_only_calibration.py` | Readiness, API probes, runtime canary, and calibration gates for the latent-defect study |
| `run_lazarus_p2p_only.py` | Launch the latent-defect study |
| `audit_lazarus_p2p_only_run.py` | Audit a completed latent-defect run |
